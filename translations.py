"""Best-effort Chinese news translation with a persistent, content-keyed cache.

Only public RSS titles and snippets go to MyMemory's documented GET API.
Anonymous usage has a 5,000 character/day limit; an explicitly configured
MYMEMORY_EMAIL enables the provider's higher allowance. No private data or
credentials are inferred. Originals are always preserved.
"""
import copy
import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from providers import ProviderError, fetch_bytes

PROVIDER = "MyMemory"
API_URL = "https://api.mymemory.translated.net/get"


def needs_translation(text):
    letters = re.findall(r"[A-Za-z]", text or "")
    chinese = re.findall(r"[\u3400-\u9fff]", text or "")
    return bool(letters) and len(chinese) < max(2, len(letters) // 5)


def split_text(text, max_bytes=480):
    """Respect the API's 500-byte input limit without dropping text."""
    parts = []
    remaining = text.strip()
    while remaining:
        end, size = 0, 0
        for char in remaining:
            length = len(char.encode("utf-8"))
            if size + length > max_bytes:
                break
            end += 1
            size += length
        if not end:
            raise ValueError("Byte limit cannot fit a character")
        if end < len(remaining):
            boundary = remaining.rfind(" ", 0, end)
            if boundary > end // 2:
                end = boundary
        parts.append(remaining[:end].strip())
        remaining = remaining[end:].lstrip()
    return parts


def parse_translation(payload, original):
    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
        if data.get("quotaFinished") or str(data.get("responseStatus")) == "429":
            raise ProviderError("免费翻译额度已用尽，原文仍保留；稍后可重试")
        if str(data.get("responseStatus")) != "200":
            raise ProviderError("翻译服务暂不可用")
        value = data["responseData"]["translatedText"]
        if not isinstance(value, str) or not value.strip():
            raise ProviderError("翻译服务返回空译文")
        value = html.unescape(value).strip()
        if needs_translation(original) and not re.search(r"[\u3400-\u9fff]", value):
            raise ProviderError("翻译服务未返回中文")
        return value
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ProviderError("翻译响应格式不可识别") from exc


class Translator:
    def __init__(self, db_path, fetcher=None, email=None, daily_budget=None, time_budget=75):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher or fetch_bytes
        self.email = os.environ.get("MYMEMORY_EMAIL", "").strip() if email is None else email
        self.daily_budget = daily_budget if daily_budget is not None else (49000 if self.email else 4900)
        self.time_budget = time_budget
        self.lock = threading.Lock()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS translations "
                         "(cache_key TEXT PRIMARY KEY, translated TEXT NOT NULL, created_at TEXT NOT NULL, "
                         "provider TEXT NOT NULL DEFAULT 'MyMemory')")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(translations)")}
            if "provider" not in columns:
                conn.execute("ALTER TABLE translations ADD COLUMN provider TEXT NOT NULL DEFAULT 'MyMemory'")
            conn.execute("CREATE TABLE IF NOT EXISTS translation_usage "
                         "(date TEXT PRIMARY KEY, characters INTEGER NOT NULL)")

    @staticmethod
    def cache_key(text):
        return hashlib.sha256(("mymemory-v1:en:zh-CN:" + text).encode("utf-8")).hexdigest()

    def cached(self, text):
        entry = self.cached_entry(text)
        return entry[0] if entry else None

    def cached_entry(self, text):
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            row = conn.execute("SELECT translated, provider FROM translations WHERE cache_key=?",
                               (self.cache_key(text),)).fetchone()
        return row

    def save(self, text, translated, provider=PROVIDER):
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("INSERT OR REPLACE INTO translations(cache_key, translated, created_at, provider) "
                         "VALUES (?, ?, ?, ?)",
                         (self.cache_key(text), translated, datetime.now(timezone.utc).isoformat(), provider))

    def reserve(self, characters):
        # Reserve before transport, including failures, so retries cannot spend unbounded quota.
        today = datetime.now(timezone.utc).date().isoformat()
        with sqlite3.connect(str(self.db_path), timeout=10) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT characters FROM translation_usage WHERE date=?", (today,)).fetchone()
            used = row[0] if row else 0
            if used + characters > self.daily_budget:
                raise ProviderError("已达到本地每日免费翻译预算，未译内容保留原文")
            conn.execute("INSERT INTO translation_usage VALUES (?, ?) ON CONFLICT(date) "
                         "DO UPDATE SET characters=excluded.characters", (today, used + characters))

    def translate(self, text, deadline=None):
        if not needs_translation(text):
            return text
        cached = self.cached(text)
        if cached:
            return cached
        self.reserve(len(text))
        translated = []
        for part in split_text(text):
            if deadline is not None and time.monotonic() >= deadline:
                raise ProviderError("翻译处理超时，未译内容保留原文")
            params = {"q": part, "langpair": "en|zh-CN", "mt": "1"}
            if self.email:
                params["de"] = self.email
            translated.append(parse_translation(self.fetcher(API_URL + "?" + urlencode(params)), part))
        result = " ".join(translated)
        self.save(text, result)
        return result

    def enrich_snapshot(self, snapshot):
        result = copy.deepcopy(snapshot)
        items = result.get("news", []) + result.get("recent_news", [])
        if result.get("mode") == "demo":
            for item in items:
                for field in ("title", "summary"):
                    text = item.get(field) or ""
                    if not needs_translation(text):
                        item[field + "_zh"] = text
                item["translation_status"] = "original" if not needs_translation(item.get("title", "")) else "unavailable"
                item["translation_provider"] = None
            return result
        unique = {}
        for item in items:
            unique[(item.get("id"), item.get("title"), item.get("summary"))] = item
        # Translate today's title/snippet first, then wider-window titles, then other snippets.
        texts = []
        for item in result.get("news", []):
            texts.extend((item.get("title", ""), item.get("summary", "")))
        for field in ("title", "summary"):
            texts.extend(item.get(field, "") for item in unique.values())
        texts = list(dict.fromkeys(text for text in texts if text))
        values, origins, errors = {}, {}, []
        missing = []
        for text in texts:
            if not needs_translation(text):
                values[text] = text
            else:
                saved = self.cached_entry(text)
                if saved:
                    values[text], origins[text] = saved
                else:
                    missing.append(text)
        deadline = time.monotonic() + self.time_budget
        failures = 0

        def work(text):
            nonlocal failures
            with self.lock:
                if failures >= 3 or time.monotonic() >= deadline:
                    return text, None, "翻译请求已暂停，未译内容保留原文"
            try:
                return text, self.translate(text, deadline=deadline), None
            except Exception as exc:
                with self.lock:
                    failures += 1
                detail = str(exc) if isinstance(exc, ProviderError) else "翻译服务暂不可用"
                return text, None, detail

        if missing:
            with ThreadPoolExecutor(max_workers=3) as executor:
                for text, translated, error in executor.map(work, missing):
                    if translated:
                        values[text] = translated
                        origins[text] = PROVIDER
                    elif error and error not in errors:
                        errors.append(error)
        counts = {"translated": 0, "partial": 0, "unavailable": 0, "original": 0}
        counted = set()
        for item in items:
            required, obtained = 0, 0
            for field in ("title", "summary"):
                text = item.get(field) or ""
                translated = values.get(text)
                item[field + "_zh"] = translated if translated else None
                if needs_translation(text):
                    required += 1
                    obtained += bool(translated)
            status = "original" if not required else "translated" if required == obtained else "partial" if obtained else "unavailable"
            item["translation_status"] = status
            item_providers = {origins[text] for text in (item.get("title"), item.get("summary")) if text in origins}
            item["translation_provider"] = " / ".join(sorted(item_providers)) if item_providers else None
            key = (item.get("id"), item.get("title"), item.get("summary"))
            if key not in counted:
                counts[status] += 1
                counted.add(key)
        result["translation"] = {"provider": " / ".join(sorted(set(origins.values()))) or PROVIDER,
                                 "status": "partial" if errors else "ok",
                                 "counts": counts, "detail": "；".join(errors) if errors else "中文机器翻译已缓存，可展开核对原文"}
        return result
