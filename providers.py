"""Small, inspectable market-data adapters. Python 3.9+, standard library only.

Yahoo's chart endpoint is an unofficial, best-effort source and may be delayed,
rate-limited or changed. CNBC / Federal Reserve RSS coverage is not exhaustive.
No source failure ever activates the fictional demo.

Returns are consecutive Yahoo daily CLOSE price changes, not dividend-adjusted
total returns. chartPreviousClose is deliberately NOT used: with range=2mo it
can refer to the beginning of the requested range. Current daily bars can be
incomplete. A volume ratio needs 20 preceding daily volumes; for today's bar we
wait until 16:15 America/New_York. This conservative rule does not implement an
exchange holiday / early-close calendar.

Heat = min(abs(change_pct)*12, 48) + min(abs(relative_pct)*10, 20)
     + min(max(volume_ratio-1, 0)*12, 12) + min(news_count*4, 20).
Missing terms contribute no points; heat_complete is false. A missing return
or a quote from a different market_date yields no heat score. This is attention
based on price, unusual volume and captured headlines, not a buy/sell score.
Keyword tags and importance are transparent rules, not verified causation.
"""

import hashlib
import html
import json
import math
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as day_time, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
INDEX_NAMES = {
    "SPY": "标普 500 ETF", "QQQ": "纳斯达克 100 ETF",
    "DIA": "道琼斯 ETF", "IWM": "罗素 2000 ETF",
}
SECTOR_NAMES = {
    "XLK": "信息技术", "XLC": "通信服务", "XLY": "可选消费",
    "XLP": "必需消费", "XLE": "能源", "XLF": "金融", "XLV": "医疗保健",
    "XLI": "工业", "XLB": "原材料", "XLRE": "房地产", "XLU": "公用事业",
}
ALL_NAMES = dict(INDEX_NAMES, **SECTOR_NAMES)
FEEDS = (
    ("CNBC · 头条", "CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC · 市场", "CNBC", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("CNBC · 科技", "CNBC", "https://www.cnbc.com/id/19854910/device/rss/rss.html"),
    ("CNBC · 经济", "CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC · 美国", "CNBC", "https://www.cnbc.com/id/15837362/device/rss/rss.html"),
    ("Federal Reserve", "Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),
)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 10
REQUEST_DEADLINE = 15
MAX_NEWS_ITEMS = 200
NEWS_RETENTION_HOURS = 72

SECTOR_KEYWORDS = {
    "XLK": ("technology", "tech stocks", "semiconductor", "chipmaker", "chipmakers", "nvidia", "microsoft", "apple", "broadcom", "software", "artificial intelligence", "ai", "cloud computing"),
    "XLC": ("alphabet", "google", "meta", "facebook", "netflix", "disney", "telecom", "communications", "streaming", "advertising"),
    "XLY": ("amazon", "tesla", "consumer discretionary", "retail sales", "electric vehicle", "automaker", "automakers", "home depot", "nike", "mcdonald"),
    "XLP": ("consumer staples", "walmart", "costco", "coca-cola", "pepsico", "procter", "grocery", "supermarket"),
    "XLE": ("crude", "oil", "opec", "exxon", "chevron", "natural gas", "energy stocks", "petroleum"),
    "XLF": ("bank", "banks", "banking", "financials", "jpmorgan", "goldman", "morgan stanley", "insurance", "visa", "mastercard"),
    "XLV": ("healthcare", "health care", "biotech", "pharmaceutical", "pharma", "drug", "fda", "eli lilly", "pfizer", "unitedhealth"),
    "XLI": ("industrial", "industrials", "aerospace", "airline", "airlines", "boeing", "caterpillar", "defense", "transportation", "manufacturing"),
    "XLB": ("materials", "copper", "mining", "chemicals", "steel", "lithium", "aluminum", "gold miner"),
    "XLRE": ("real estate", "reit", "reits", "housing", "commercial property", "mortgage"),
    "XLU": ("utilities", "utility", "electricity", "power grid", "duke energy", "nextera"),
}
MACRO_WORDS = ("federal reserve", "fomc", "interest rate", "rate cut", "rate hike", "inflation", "cpi", "pce", "nonfarm", "payrolls", "jobs report", "gdp", "treasury yield")
GEOPOLITICAL_WORDS = ("tariff", "tariffs", "sanctions", "war", "trade deal", "trade war")
EARNINGS_WORDS = ("earnings", "quarterly results", "revenue", "profit", "guidance")
COMPANY_WORDS = ("merger", "acquisition", "acquire", "buyback", "bankruptcy", "ipo", "antitrust", "fda")
MARKET_WORDS = ("stock market", "stocks", "s&p 500", "nasdaq", "dow", "selloff", "sell-off", "rally", "record high")


class ProviderError(ValueError):
    """A readable failure safe to show in the dashboard."""


def eastern_now(now=None):
    """Normalize injected datetimes; a naive test clock is interpreted as ET."""
    if now is None:
        return datetime.now(EASTERN)
    if now.tzinfo is None:
        return now.replace(tzinfo=EASTERN)
    return now.astimezone(EASTERN)


def _number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or (positive and value <= 0):
        return None
    return float(value)


def _timestamp(value):
    value = _number(value)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, EASTERN)
    except (ValueError, OverflowError, OSError):
        return None


def _tls_context():
    """Keep verified TLS, including on macOS Pythons missing their CA bundle.

    An explicit SSL_CERT_FILE / SSL_CERT_DIR always takes precedence. Only when
    neither a configured nor a normal OpenSSL CA location is available do we
    use macOS's system PEM bundle. Certificate and hostname checks stay enabled.
    """
    paths = ssl.get_default_verify_paths()
    explicit_ca = any(name in os.environ for name in ("SSL_CERT_FILE", "SSL_CERT_DIR"))
    if (sys.platform == "darwin" and not explicit_ca
            and not paths.cafile and not paths.capath
            and os.path.isfile("/etc/ssl/cert.pem")):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


def fetch_bytes(url):
    """Bound response bytes, socket waits and elapsed read time; no retries."""
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; MarketDaily/1.0; personal dashboard)",
        "Accept": "application/json, application/rss+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "identity",
    })
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT, context=_tls_context()) as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_RESPONSE_BYTES:
                raise ProviderError("响应超过 2 MB 限制")
            chunks = []
            size = 0
            # read1 avoids waiting for a whole chunk while a server trickles data.
            read = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() - started > REQUEST_DEADLINE:
                    raise ProviderError("读取超时")
                chunk = read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ProviderError("响应超过 2 MB 限制")
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise ProviderError("HTTP %s" % exc.code) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderError("网络超时") from exc
    except urllib.error.URLError as exc:
        raise ProviderError("网络连接失败（%s）" % type(exc.reason).__name__) from exc
    except OSError as exc:
        raise ProviderError("网络读取失败（%s）" % type(exc).__name__) from exc


def parse_chart(payload, symbol, now=None):
    """Parse Yahoo daily bars. Source bar times and actual market times differ."""
    now = eastern_now(now)
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        chart = payload["chart"]
        if chart.get("error"):
            raise ProviderError("行情源返回错误")
        result = chart["result"][0]
        meta = result.get("meta") or {}
        if meta.get("symbol", symbol).upper() != symbol:
            raise ProviderError("行情代码与请求不一致")
        if meta.get("currency", "USD") != "USD":
            raise ProviderError("行情币种不是 USD")
        stamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ProviderError("行情格式不可识别") from exc

    by_date = {}
    for index, raw_time in enumerate(stamps):
        stamp = _timestamp(raw_time)
        if stamp is None or stamp > now:
            continue
        close = _number(closes[index] if index < len(closes) else None, positive=True)
        volume = _number(volumes[index] if index < len(volumes) else None)
        if volume is not None and volume < 0:
            volume = None
        by_date[stamp.date()] = {"stamp": stamp, "close": close, "volume": volume}
    bars = [by_date[day] for day in sorted(by_date)]
    available = [index for index, bar in enumerate(bars) if bar["close"] is not None]
    if not available:
        raise ProviderError("没有有效日线价格")
    latest_index = available[-1]
    bar = bars[latest_index]
    bar_date = bar["stamp"].date()
    # Never jump across a missing close and label a multi-day move as one day.
    previous = bars[latest_index - 1] if latest_index else None
    previous_close = previous["close"] if previous else None
    change = ((bar["close"] / previous_close - 1) * 100) if previous_close else None
    complete = bar_date < now.date() or now.time() >= day_time(16, 15)
    # Older timestamps in meta cannot override the selected bar's timestamp.
    market_time = _timestamp(meta.get("regularMarketTime"))
    if market_time and market_time.date() == bar_date and bar["stamp"] <= market_time <= now:
        as_of, as_of_kind = market_time, "regular_market_time"
    else:
        as_of, as_of_kind = bar["stamp"], "daily_bar_timestamp"
    window = bars[max(0, latest_index - 20):latest_index]
    ratio = None
    if complete and bar["volume"] is not None and len(window) == 20:
        history_volumes = [item["volume"] for item in window]
        if all(value is not None and value > 0 for value in history_volumes):
            ratio = bar["volume"] / (sum(history_volumes) / 20)
    return {
        "symbol": symbol, "name": ALL_NAMES.get(symbol, symbol),
        "price": round(bar["close"], 4),
        "change_pct": round(change, 4) if change is not None else None,
        "previous_close": round(previous_close, 4) if previous_close is not None else None,
        "previous_date": previous["stamp"].date().isoformat() if previous else None,
        "trading_date": bar_date.isoformat(),
        "as_of": as_of.isoformat(timespec="seconds"), "as_of_kind": as_of_kind,
        "session_complete": complete,
        "volume": int(bar["volume"]) if bar["volume"] is not None else None,
        "volume_ratio": round(ratio, 4) if ratio is not None else None,
        "history": [round(item["close"], 4) for item in bars[:latest_index + 1] if item["close"] is not None][-30:],
        "history_dates": [item["stamp"].date().isoformat() for item in bars[:latest_index + 1] if item["close"] is not None][-30:],
    }


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value, limit=320):
    parser = _PlainText()
    parser.feed(value or "")
    clean = re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()
    return clean if len(clean) <= limit else clean[:limit - 1].rstrip() + "…"


def safe_url(value):
    value = (value or "").strip()
    if re.search(r"[\x00-\x20\x7f]", value):
        return None
    try:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return None
        # Accessing port validates malformed / out-of-range ports.
        _ = parts.port
        return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    except ValueError:
        return None


def canonical_url(value):
    url = safe_url(value)
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in ("fbclid", "gclid", "__source")]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.removeprefix("www."), parts.path.rstrip("/"), urllib.parse.urlencode(sorted(query)), ""))


def _matches(text, words):
    return any(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, flags=re.I) for word in words)


def classify_news(title, summary, source):
    text = title + " " + summary
    sectors = [symbol for symbol, keywords in SECTOR_KEYWORDS.items() if _matches(text, keywords)]
    category, score = "市场动态", 1
    if _matches(text, MARKET_WORDS):
        score = 3
    if _matches(text, COMPANY_WORDS):
        category, score = "公司动态", 4
    if _matches(text, EARNINGS_WORDS):
        category, score = "财报业绩", 5
    if _matches(text, GEOPOLITICAL_WORDS):
        category, score = "宏观政策", 6
    if _matches(text, MACRO_WORDS):
        category, score = "宏观政策", 7
    if source == "Federal Reserve":
        score += 2
        if category == "市场动态":
            category = "宏观政策"
    if len(sectors) > 1:
        score += 1
    importance = "high" if score >= 7 else "medium" if score >= 4 else "normal"
    return sectors, category, importance, min(score, 10)


def _local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _element_text(item, names):
    for name in names:
        for child in item:
            if _local_name(child.tag) == name:
                return "".join(child.itertext()).strip()
    return ""


def _published(value):
    if not value:
        return None
    try:
        stamp = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    # An unknown timezone cannot establish which ET calendar day an item belongs to.
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(EASTERN)


def _in_news_window(published, now, lookback_hours=None):
    if published is None or published.timestamp() > now.timestamp():
        return False
    if lookback_hours is None:
        return published.date() == now.date()
    # Timestamp arithmetic measures elapsed hours across DST transitions.
    return published.timestamp() >= now.timestamp() - lookback_hours * 3600


def parse_feed(payload, source, now=None, lookback_hours=None):
    """Return today's ET entries by default, or an explicit rolling hour window.

    Every window ends at ``now`` and excludes future / undated publications.
    A rolling window includes an item exactly on its lower time boundary.
    """
    now = eastern_now(now)
    if lookback_hours is not None and _number(lookback_hours, positive=True) is None:
        raise ValueError("lookback_hours must be a positive finite number")
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProviderError("RSS 超过 2 MB 限制")
    security_text = payload.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in security_text or b"<!ENTITY" in security_text:
        raise ProviderError("RSS 含不支持的 XML 声明")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ProviderError("RSS 格式不可识别") from exc
    if _local_name(root.tag) not in ("rss", "feed", "rdf"):
        raise ProviderError("来源未返回 RSS / Atom")
    entries = [item for item in root.iter() if _local_name(item.tag) in ("item", "entry")][:300]
    results = []
    for item in entries:
        published = _published(_element_text(item, ("pubdate", "published", "date")))
        if not _in_news_window(published, now, lookback_hours):
            continue
        title = plain_text(_element_text(item, ("title",)), 300)
        description = plain_text(_element_text(item, ("description", "summary")), 240)
        link = _element_text(item, ("link",))
        if not link:
            for child in item:
                if _local_name(child.tag) == "link" and child.get("rel", "alternate") == "alternate":
                    link = child.get("href", "")
                    break
        link = safe_url(link)
        if not title or not link:
            continue
        sectors, category, importance, score = classify_news(title, description, source)
        results.append({
            "id": hashlib.sha256(canonical_url(link).encode("utf-8")).hexdigest()[:16],
            "title": title, "summary": description, "url": link,
            "source": source, "published_at": published.isoformat(timespec="seconds"),
            "sectors": sectors, "category": category, "importance": importance, "score": score,
        })
    return results


def deduplicate_news(news):
    seen_urls, seen_titles = set(), set()
    result = []
    def sort_key(item):
        published = _published(item.get("published_at"))
        return item["score"], published.timestamp() if published else 0

    for item in sorted(news, key=sort_key, reverse=True):
        url = canonical_url(item["url"])
        title = re.sub(r"[\W_]+", "", item["title"].casefold())
        if url in seen_urls or title in seen_titles:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        result.append(item)
    return result[:MAX_NEWS_ITEMS]


def _missing_quote(symbol, name):
    return {"symbol": symbol, "name": name, "price": None, "change_pct": None,
            "as_of": None, "trading_date": None, "previous_date": None,
            "history": [], "history_dates": [], "volume_ratio": None,
            "session_complete": False, "as_of_kind": None}


def heat_score(change, relative, volume, news_count):
    if change is None:
        return None
    result = min(abs(change) * 12, 48) + min(news_count * 4, 20)
    if relative is not None:
        result += min(abs(relative) * 10, 20)
    if volume is not None:
        result += min(max(volume - 1, 0) * 12, 12)
    return int(round(min(result, 100)))


def derive_snapshot(quotes, news, sources, errors=None, now=None, mode="live"):
    """Split recent feed entries into rolling coverage and today's report.

    Only today's ET entries affect sector heat and the daily summary. Comparable
    returns require matching both trading dates and previous-close dates.
    """
    now = eastern_now(now)
    errors = list(errors or [])
    recent_news = deduplicate_news([
        item for item in news
        if _in_news_window(_published(item.get("published_at")), now, NEWS_RETENTION_HOURS)
    ])
    news = [item for item in recent_news if _in_news_window(_published(item["published_at"]), now)]
    coverage = {
        "today_count": len(news),
        "hours24_count": sum(_in_news_window(_published(item["published_at"]), now, 24) for item in recent_news),
        "hours72_count": len(recent_news),
        "retention_hours": NEWS_RETENTION_HOURS,
    }
    dates = [quote["trading_date"] for quote in quotes.values() if quote.get("trading_date")]
    market_date = max(dates) if dates else None
    benchmark = quotes.get("SPY")
    indices = [dict(quotes.get(symbol) or _missing_quote(symbol, name)) for symbol, name in INDEX_NAMES.items()]
    sectors, alerts = [], []
    for symbol, name in SECTOR_NAMES.items():
        sector = dict(quotes.get(symbol) or _missing_quote(symbol, name))
        matching = bool(market_date and sector.get("trading_date") == market_date)
        relative = None
        if (matching and benchmark and benchmark.get("trading_date") == market_date
                and sector.get("previous_date") == benchmark.get("previous_date")
                and sector.get("change_pct") is not None and benchmark.get("change_pct") is not None):
            relative = round(sector["change_pct"] - benchmark["change_pct"], 4)
        news_count = sum(symbol in item["sectors"] for item in news)
        sector.update({"relative_pct": relative, "news_count": news_count,
                       "heat_score": heat_score(sector["change_pct"], relative, sector.get("volume_ratio"), news_count) if matching else None,
                       "heat_complete": bool(matching and relative is not None and sector.get("volume_ratio") is not None
                                             and all(source["status"] == "ok" for source in sources)),
                       "comparable": matching})
        sectors.append(sector)
        if not matching:
            continue
        move = sector["change_pct"]
        if move is not None and abs(move) >= 2:
            direction = "上涨" if move > 0 else "下跌"
            alerts.append({"symbol": symbol, "name": name, "kind": "price", "severity": "high" if abs(move) >= 3 else "medium",
                           "title": name + "显著" + direction,
                           "detail": "%s 日线涨跌 %+.2f%%，触及 ±2%% 阈值。" % (market_date, move)})
        ratio = sector.get("volume_ratio")
        if ratio is not None and ratio >= 1.5:
            alerts.append({"symbol": symbol, "name": name, "kind": "volume", "severity": "high" if ratio >= 2 else "medium",
                           "title": name + "成交量放大",
                           "detail": "%s 日成交量为此前 20 个日线成交量均值的 %.2f 倍。" % (market_date, ratio)})
        if relative is not None and abs(relative) >= 1:
            alerts.append({"symbol": symbol, "name": name, "kind": "relative", "severity": "medium",
                           "title": name + ("相对强势" if relative > 0 else "相对弱势"),
                           "detail": "%s 与 SPY 的日涨跌差为 %+.2f 个百分点。" % (market_date, relative)})
    sectors.sort(key=lambda item: (item["heat_score"] is not None, item["heat_score"] or 0), reverse=True)
    alerts.sort(key=lambda item: item["severity"] == "high", reverse=True)
    summary = []
    if market_date:
        suffix = "（今天的新闻单独按美东日历日归集）" if market_date != now.date().isoformat() else ""
        summary.append("最近可用行情日期：%s；已获得 %d / 15 只 ETF 行情%s。" % (market_date, len(quotes), suffix))
        comparable = [item for item in sectors if item["comparable"] and item["change_pct"] is not None]
        if comparable:
            leading = max(comparable, key=lambda item: item["change_pct"])
            lagging = min(comparable, key=lambda item: item["change_pct"])
            summary.append("同日已收录板块中，%s %+.2f%%，%s %+.2f%%；%d 涨 / %d 跌 / %d 平。" % (
                leading["name"], leading["change_pct"], lagging["name"], lagging["change_pct"],
                sum(item["change_pct"] > 0 for item in comparable), sum(item["change_pct"] < 0 for item in comparable),
                sum(item["change_pct"] == 0 for item in comparable)))
    else:
        summary.append("本次未获得有效行情，板块热度与行情异动暂不可计算。")
    if len(set(dates)) > 1:
        errors.append("行情交易日期不一致；旧日期板块不参与热度、异动及相对比较。")
    summary.append("%s（美东）已收录 %d 条去重新闻，其中 %d 条命中高关注规则；分类和排序由关键词规则生成。" % (
        now.date().isoformat(), len(news), sum(item["importance"] == "high" for item in news)))
    if now.time() < day_time(16, 15) and market_date == now.date().isoformat():
        summary.append("当前日线可能尚未完成；量比等待美东 16:15 后计算。")
    if any(item.get("as_of_kind") == "daily_bar_timestamp" for item in list(indices) + sectors):
        summary.append("部分来源仅提供日线柱时间；该时间不是经核实的收盘成交时间。")
    successful = sum(source["status"] == "ok" for source in sources)
    if len(quotes) == len(ALL_NAMES) and successful == len(sources) and not errors:
        status = "ok"
    elif quotes or recent_news or successful:
        status = "partial"
    else:
        status = "error"
    return {"date": now.date().isoformat(), "generated_at": now.isoformat(timespec="seconds"),
            "market_date": market_date, "mode": mode, "status": status,
            "indices": indices, "sectors": sectors, "news": news, "recent_news": recent_news,
            "news_coverage": coverage, "alerts": alerts,
            "summary": summary, "sources": sources, "errors": errors,
            "methodology": {
                "price": "Yahoo 日线 close 与上一根日线 close 比较；不含股息再投资，盘中日线可能未完成。指数使用 ETF 代理。",
                "heat": "min(|日涨跌%|×12,48) + min(|相对SPY百分点|×10,20) + min(max(20日量比−1,0)×12,12) + min(当日已收录相关新闻数×4,20)。缺失项不计分；未完整计分由 heat_complete 标识。",
                "volume": "本日成交量 / 此前20个日线成交量均值；当日量比仅 ET16:15 后计算，不含提前收盘日历。",
                "news": "RSS 新闻保留截至报告生成时间的最近72小时；日报和热度仅计入报告当日美东日历日发布的新闻。标题与摘要关键词分类、跨源去重、规则排序。非完整新闻覆盖，非因果判断。",
            }}


def _collect_quote(symbol, now):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=2mo" % symbol
    return parse_chart(fetch_bytes(url), symbol, now)


def collect_snapshot(now=None):
    """Fetch sources concurrently, preserving partial success and honest errors."""
    now = eastern_now(now)
    quotes, news, errors = {}, [], []
    feed_results = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        pending = {executor.submit(_collect_quote, symbol, now): ("quote", symbol) for symbol in ALL_NAMES}
        for label, source, url in FEEDS:
            pending[executor.submit(lambda u=url, s=source: parse_feed(fetch_bytes(u), s, now, lookback_hours=NEWS_RETENTION_HOURS))] = ("feed", label)
        for future in as_completed(pending):
            kind, name = pending[future]
            try:
                value = future.result()
                if kind == "quote":
                    quotes[name] = value
                else:
                    news.extend(value)
                    today_count = sum(_in_news_window(_published(item["published_at"]), now) for item in value)
                    feed_results[name] = {"status": "ok", "detail": "%s（美东）今日 %d 条 / 最近72小时 %d 条，跨源去重前" % (now.date().isoformat(), today_count, len(value))}
            except Exception as exc:
                # One malformed provider response must not discard other sources.
                message = str(exc) if isinstance(exc, ProviderError) else "数据处理失败（%s）" % type(exc).__name__
                errors.append("%s：%s" % (name, message))
                if kind == "feed":
                    feed_results[name] = {"status": "error", "detail": message}
    sources = [{"name": "Yahoo Finance", "status": "ok" if len(quotes) == len(ALL_NAMES) else "partial" if quotes else "error",
                "detail": "%d / 15 只 ETF 行情；非官方接口，可能延迟" % len(quotes), "url": "https://finance.yahoo.com/"}]
    for label, source, url in FEEDS:
        sources.append(dict({"name": label, "url": url}, **feed_results[label]))
    return derive_snapshot(quotes, news, sources, sorted(errors), now)


def demo_snapshot():
    """Explicit, deterministic fictional fixture. Never invoked by live collection."""
    now = datetime(2026, 9, 18, 18, 0, tzinfo=EASTERN)
    rows = {
        "SPY": (602.18, 0.82, 1.03), "QQQ": (523.41, 1.36, 1.17),
        "DIA": (447.63, 0.34, 0.91), "IWM": (224.57, -0.28, 1.08),
        "XLK": (242.36, 2.84, 1.78), "XLC": (101.62, 1.65, 1.21),
        "XLY": (213.44, 0.93, 1.08), "XLP": (81.26, -0.42, 0.87),
        "XLE": (88.73, -2.37, 1.63), "XLF": (48.92, 0.68, 1.12),
        "XLV": (147.85, 0.21, 0.94), "XLI": (139.07, 1.14, 1.32),
        "XLB": (92.18, -0.76, 1.04), "XLRE": (43.62, -1.28, 1.41),
        "XLU": (78.95, 1.42, 1.54),
    }
    quotes = {}
    days = []
    day = now.date()
    while len(days) < 30:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    days.reverse()
    for index, (symbol, (price, change, volume)) in enumerate(rows.items()):
        previous = price / (1 + change / 100)
        history = [round(previous * (0.96 + item * 0.04 / 28 + math.sin(item * 0.9 + index) * 0.004), 2) for item in range(29)]
        history[-1] = round(previous, 4)
        history.append(price)
        quotes[symbol] = {"symbol": symbol, "name": ALL_NAMES[symbol], "price": price,
                          "change_pct": change, "previous_close": round(previous, 4), "previous_date": "2026-09-17",
                          "trading_date": "2026-09-18", "as_of": "2026-09-18T16:00:00-04:00",
                          "as_of_kind": "fictional_demo", "session_complete": True,
                          "volume_ratio": volume, "history": history, "history_dates": days}
    headlines = [
        ("[演示·虚构] AI 基础设施主题受关注，科技板块量价走强", "虚构场景：信息技术 ETF 上涨 2.84%，成交量为此前 20 日均值的 1.78 倍。用于演示行情和新闻如何并列展示。", ["XLK", "XLU"], "市场动态", 8, "15:42"),
        ("[演示·虚构] 美联储政策讨论成为今日宏观焦点", "虚构场景：模拟一条政策新闻的归类与高关注标记。未引用真实会议、官员言论或政策决定。", [], "宏观政策", 9, "14:30"),
        ("[演示·虚构] 原油主题降温，能源板块出现价格异动", "虚构场景：能源 ETF 下跌 2.37%，成交量放大。关联主题只表示关键词匹配，不代表因果验证。", ["XLE"], "市场动态", 7, "15:18"),
        ("[演示·虚构] 云服务公司发布季度业绩，科技新闻增加", "虚构场景：模拟财报标题和简短摘要，展示相关新闻数量如何参与热度评分。", ["XLK"], "财报业绩", 6, "13:24"),
        ("[演示·虚构] 电网投资议题进入市场视野", "虚构场景：公用事业与工业主题同时匹配一条新闻，用于演示板块筛选。", ["XLU", "XLI"], "公司动态", 5, "12:10"),
        ("[演示·虚构] 数字广告主题升温，通信服务板块走高", "虚构场景：通信服务 ETF 上涨 1.65%。新闻卡片保留日期、来源和板块标签。", ["XLC"], "公司动态", 4, "11:35"),
        ("[演示·虚构] 银行业绩预期成为金融板块讨论话题", "虚构场景：金融 ETF 上涨 0.68%，低于大盘代理 ETF 的 0.82%。", ["XLF"], "财报业绩", 5, "10:50"),
        ("[演示·虚构] 地产主题承压，板块表现弱于大盘", "虚构场景：房地产 ETF 下跌 1.28%，相对 SPY 落后 2.10 个百分点。", ["XLRE"], "市场动态", 4, "10:12"),
    ]
    news = []
    for index, (title, summary, sectors, category, score, stamp) in enumerate(headlines):
        news.append({"id": "demo-%d" % index, "title": title, "summary": summary,
                     "url": "https://example.com/fictional-market-demo/%d" % index,
                     "source": "演示数据 · 非真实新闻", "published_at": "2026-09-18T%s:00-04:00" % stamp,
                     "sectors": sectors, "category": category, "importance": "high" if score >= 7 else "medium", "score": score})
    news.extend([
        {"id": "demo-recent-1", "title": "[演示·虚构] 昨日晚间科技主题回顾", "summary": "虚构的前一日新闻：可在最近24小时中看到，不计入今日热度和日报。",
         "url": "https://example.com/fictional-market-demo/recent-1", "source": "演示数据 · 非真实新闻",
         "published_at": "2026-09-17T20:00:00-04:00", "sectors": ["XLK"], "category": "市场动态", "importance": "medium", "score": 4},
        {"id": "demo-recent-2", "title": "[演示·虚构] 前日能源主题回顾", "summary": "虚构的前日新闻：仅在最近72小时中显示，不计入今日热度和日报。",
         "url": "https://example.com/fictional-market-demo/recent-2", "source": "演示数据 · 非真实新闻",
         "published_at": "2026-09-16T11:00:00-04:00", "sectors": ["XLE"], "category": "市场动态", "importance": "medium", "score": 4},
    ])
    sources = [{"name": "本地演示数据", "status": "ok", "detail": "所有价格、新闻、走势均为虚构，仅用于界面预览", "url": "https://example.com/"}]
    result = derive_snapshot(quotes, news, sources, now=now, mode="demo")
    result["summary"].insert(0, "演示模式：以下行情、新闻与走势均为虚构，固定日期 2026-09-18。")
    return result
