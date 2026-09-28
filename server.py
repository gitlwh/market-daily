#!/usr/bin/env python3
"""Local US-market dashboard: HTTP API, daily scheduler and SQLite archive."""
import argparse
import json
import logging
import mimetypes
import sqlite3
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import providers
import earnings
import leader_screen
import sentiment
from translations import Translator

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
LOG = logging.getLogger("market-daily")


def market_now():
    return datetime.now(ET)


def validate_date(value):
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("日期格式必须为 YYYY-MM-DD")
    return value


def next_run(now, hour, minute):
    local = now.astimezone(ET)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS reports "
                         "(date TEXT PRIMARY KEY, generated_at TEXT NOT NULL, payload TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS schedule_runs "
                         "(date TEXT PRIMARY KEY, attempted_at TEXT NOT NULL, success INTEGER NOT NULL)")

    def connect(self):
        return sqlite3.connect(str(self.path), timeout=10)

    def save(self, snapshot):
        if snapshot.get("mode") != "live" or snapshot.get("status") == "error":
            raise ValueError("只归档至少有一个来源成功的真实采集结果")
        day = validate_date(snapshot["date"])
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        with self.connect() as conn:
            conn.execute("INSERT INTO reports(date, generated_at, payload) VALUES (?, ?, ?) "
                         "ON CONFLICT(date) DO UPDATE SET generated_at=excluded.generated_at, "
                         "payload=excluded.payload", (day, snapshot["generated_at"], payload))

    def get(self, day=None):
        with self.connect() as conn:
            if day:
                row = conn.execute("SELECT payload FROM reports WHERE date=?", (day,)).fetchone()
            else:
                row = conn.execute("SELECT payload FROM reports ORDER BY date DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def dates(self):
        with self.connect() as conn:
            return [row[0] for row in conn.execute("SELECT date FROM reports ORDER BY date DESC")]

    def sentiment_history(self, end=None, limit=30):
        query = "SELECT payload FROM reports"
        params = []
        if end:
            query += " WHERE date<=?"
            params.append(validate_date(end))
        query += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in reversed(rows):
            snapshot = json.loads(row[0])
            mood = snapshot.get("sentiment") or {}
            if isinstance(mood.get("score"), (int, float)):
                result.append({"date": snapshot["date"], "score": mood["score"], "label": mood.get("label")})
        return result

    def record_schedule(self, now, success):
        with self.connect() as conn:
            conn.execute("INSERT INTO schedule_runs(date, attempted_at, success) VALUES (?, ?, ?) "
                         "ON CONFLICT(date) DO UPDATE SET attempted_at=excluded.attempted_at, "
                         "success=excluded.success", (now.date().isoformat(), now.isoformat(), int(success)))

    def schedule_due(self, now, hour, minute):
        local = now.astimezone(ET)
        if (local.hour, local.minute) < (hour, minute):
            return False
        with self.connect() as conn:
            row = conn.execute("SELECT attempted_at, success FROM schedule_runs WHERE date=?",
                               (local.date().isoformat(),)).fetchone()
        # Failed scheduled collection retries at most every 30 minutes; successful days persist across restarts.
        return not row or (not row[1] and local - datetime.fromisoformat(row[0]) >= timedelta(minutes=30))


class Application:
    def __init__(self, db_path, schedule=True, hour=18, minute=0, collector=None, translator=None,
                 earnings_collector=None, leader_collector=None, sentiment_collector=None):
        self.archive = Archive(db_path)
        self.enabled = schedule
        self.hour = hour
        self.minute = minute
        self.collector = collector or providers.collect_snapshot
        self.translator = translator
        self.earnings_collector = earnings_collector
        self.leader_collector = leader_collector
        self.sentiment_collector = sentiment_collector
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.running = False
        self.last_error = None
        self.last_finished = None
        self.last_status = None
        self.worker = None

    def state(self):
        with self.lock:
            return {"running": self.running, "last_error": self.last_error,
                    "last_finished": self.last_finished, "last_status": self.last_status}

    def dashboard(self, day=None, demo=False):
        snapshot = providers.demo_snapshot() if demo else self.archive.get(day)
        if demo:
            snapshot["earnings"] = earnings.demo_earnings()
            snapshot["leader_screen"] = leader_screen.demo_leader_screen()
            snapshot["sentiment"] = sentiment.demo_sentiment()
            snapshot["sentiment_history"] = [
                {"date": (date(2026, 9, 8) + timedelta(days=index)).isoformat(), "score": score,
                 "label": sentiment.mood_label(score)}
                for index, score in enumerate((48, 52, 50, 56, 59, 57, 61, 63, 60, 64, 65))]
        elif snapshot:
            snapshot["sentiment_history"] = self.archive.sentiment_history(snapshot.get("date"))
        return {"snapshot": snapshot,
                "dates": self.archive.dates(), "collection": self.state(),
                "schedule": {"enabled": self.enabled, "hour": self.hour, "minute": self.minute,
                             "timezone": "America/New_York",
                             "next_run": next_run(market_now(), self.hour, self.minute).isoformat()
                             if self.enabled else None}}

    def refresh(self, scheduled=False):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.last_error = None
            self.worker = threading.Thread(target=self._collect, args=(scheduled,), daemon=True,
                                           name="market-collector")
            self.worker.start()
        return True

    def _collect(self, scheduled=False):
        started = market_now()
        success = False
        error = None
        status = "error"
        try:
            snapshot = self.collector()
            if self.earnings_collector:
                try:
                    calendar = self.earnings_collector(now=started)
                    if not isinstance(calendar, dict) or calendar.get("status") not in ("ok", "partial", "error"):
                        raise ValueError("Invalid earnings calendar payload")
                except Exception:
                    LOG.exception("Earnings calendar failed; preserving market/news data")
                    week_start = started.date() - timedelta(days=started.weekday())
                    calendar = {"status": "error", "week_start": week_start.isoformat(),
                                "week_end": (week_start + timedelta(days=6)).isoformat(),
                                "as_of": started.isoformat(), "companies": [], "coverage_dates": [],
                                "errors": ["财报日历暂时无法读取，请稍后重试"], "sources": []}
                snapshot["earnings"] = calendar
                snapshot.setdefault("sources", []).extend(calendar.get("sources", []))
                if calendar.get("status") != "ok":
                    snapshot.setdefault("errors", []).append("本周财报：部分或全部日期未取得，请查看财报区的来源状态")
                    if snapshot.get("status") == "ok":
                        snapshot["status"] = "partial"
                    elif snapshot.get("status") == "error" and calendar.get("coverage_dates"):
                        snapshot["status"] = "partial"
                elif snapshot.get("status") == "error":
                    snapshot["status"] = "partial"
            if self.leader_collector:
                try:
                    screen = self.leader_collector(now=started)
                    if not isinstance(screen, dict) or screen.get("status") not in ("ok", "partial", "error"):
                        raise ValueError("Invalid leader screen payload")
                except Exception:
                    LOG.exception("Leader screen failed; preserving market/news data")
                    screen = {"status": "error", "as_of": started.isoformat(), "universe_size": 0,
                              "available_count": 0, "candidate_count": 0, "stocks": [],
                              "sector_leader_count": 110, "market_cap_top_100_count": 0,
                              "rules": dict(leader_screen.RULES),
                              "errors": ["龙头回调观察暂时无法读取，请稍后重试"],
                              "source": {"name": "Yahoo Finance · 龙头回调观察", "url": "https://finance.yahoo.com/",
                                         "status": "error", "detail": "本次未取得个股历史行情"},
                              "note": "规则筛选的研究候选，不是买入建议。"}
                snapshot["leader_screen"] = screen
                if screen.get("source"):
                    snapshot.setdefault("sources", []).append(screen["source"])
            if self.sentiment_collector:
                try:
                    mood = self.sentiment_collector(snapshot, snapshot.get("leader_screen"), now=started)
                    if not isinstance(mood, dict) or mood.get("status") not in ("ok", "partial", "error"):
                        raise ValueError("Invalid sentiment payload")
                except Exception:
                    LOG.exception("Sentiment calculation failed; preserving other report data")
                    mood = {"status": "error", "as_of": started.isoformat(), "score": None,
                            "label": "数据不足", "coverage_weight": 0, "confidence": "低",
                            "components": [], "errors": ["市场情绪暂时无法计算"],
                            "note": "市场情绪指标不预测未来涨跌。"}
                snapshot["sentiment"] = mood
                if mood.get("source"):
                    snapshot.setdefault("sources", []).append(mood["source"])
            if snapshot.get("status") == "error":
                error = "；".join(snapshot.get("errors", [])) or "所有数据源采集失败，请稍后重试。"
            else:
                if self.translator:
                    try:
                        snapshot = self.translator.enrich_snapshot(snapshot)
                        LOG.info("Chinese news: %s", snapshot.get("translation", {}))
                    except Exception:
                        LOG.exception("Translation failed; preserving collected originals")
                        snapshot["translation"] = {"provider": "MyMemory", "status": "partial",
                                                   "detail": "中文翻译暂不可用，已保留原文"}
                self.archive.save(snapshot)
                status = snapshot["status"]
                success = snapshot.get("status") == "ok"
                if snapshot.get("errors"):
                    error = "部分数据未取得：" + "；".join(snapshot["errors"])
                LOG.info("Report archived for %s (%s)", snapshot["date"], snapshot["status"])
        except Exception as exc:
            LOG.exception("Collection failed")
            error = "采集失败：%s" % str(exc)[:500]
        finally:
            if scheduled:
                try:
                    self.archive.record_schedule(started, success)
                except Exception:
                    LOG.exception("Could not save scheduler state")
            with self.lock:
                self.running = False
                self.last_error = error
                self.last_finished = market_now().isoformat()
                self.last_status = status

    def start_scheduler(self):
        if not self.enabled:
            return
        threading.Thread(target=self._schedule_loop, daemon=True, name="daily-scheduler").start()

    def _schedule_loop(self):
        while not self.stop.is_set():
            try:
                if self.archive.schedule_due(market_now(), self.hour, self.minute):
                    self.refresh(scheduled=True)
            except Exception:
                LOG.exception("Scheduler failed; will retry")
            self.stop.wait(15)


def markdown_report(snapshot):
    def clean(value):
        return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")

    def pct(value):
        return "—" if value is None else "%+.2f%%" % value

    lines = ["# 美股市场日报 · " + snapshot["date"], "",
             "采集时间：" + snapshot["generated_at"],
             "行情日期：" + str(snapshot.get("market_date") or "暂无"),
             "数据状态：" + snapshot["status"], "", "## 今日摘要", ""]
    lines.extend("- " + clean(item) for item in snapshot.get("summary", []))
    lines += ["", "## 板块表现（行业 ETF 代理）", "",
              "| 板块 | ETF | 涨跌幅 | 相对 SPY（百分点） | 量比 |",
              "| --- | --- | ---: | ---: | ---: |"]
    for sector in snapshot.get("sectors", []):
        relative = "—" if sector.get("relative_pct") is None else "%+.2f" % sector["relative_pct"]
        volume = "—" if sector.get("volume_ratio") is None else "%.2fx" % sector["volume_ratio"]
        lines.append("| %s | %s | %s | %s | %s |" %
                     (clean(sector["name"]), clean(sector["symbol"]), pct(sector.get("change_pct")), relative, volume))
    lines += ["", "## 板块异动", ""]
    lines.extend("- **%s**：%s" % (clean(item["title"]), clean(item["detail"]))
                 for item in snapshot.get("alerts", []))
    lines += ["", "## 当日新闻", ""]
    for item in snapshot.get("news", []):
        title = clean(item.get("title_zh") or item["title"]).replace("[", "\\[").replace("]", "\\]")
        url = item.get("url", "")
        if urlparse(url).scheme not in ("https", "http"):
            url = ""
        lines += ["- [%s](%s) · %s · %s" % (title, url.replace(")", "%29"), clean(item["source"]),
                                               clean(item["published_at"]))]
        if item.get("summary_zh"):
            lines.append("  " + clean(item["summary_zh"]))
        if item.get("title_zh") and item["title_zh"] != item["title"]:
            lines.append("  原标题：" + clean(item["title"]))
            lines.append("  中文为机器翻译，请以来源原文为准。")
    if snapshot.get("translation"):
        lines += ["", "翻译状态：" + clean(snapshot["translation"].get("detail", ""))]
    mood = snapshot.get("sentiment")
    if mood:
        lines += ["", "## 市场情绪", "",
                  "综合分数：%s / 100 · %s · 置信度 %s · 可用权重 %s%%" % (
                      clean(mood.get("score")), clean(mood.get("label")), clean(mood.get("confidence")),
                      clean(mood.get("coverage_weight"))), "",
                  "| 分项 | 分数 | 权重 | 依据 |", "| --- | ---: | ---: | --- |"]
        for item in mood.get("components", []):
            lines.append("| %s | %s | %s%% | %s |" % (
                clean(item.get("label")), clean(item.get("score")), clean(item.get("weight")), clean(item.get("detail"))))
        lines += ["", clean(mood.get("explanation")), clean(mood.get("note"))]
    screen = snapshot.get("leader_screen")
    if screen:
        candidates = [item for item in screen.get("stocks", []) if item.get("status") == "candidate"]
        lines += ["", "## 龙头回调观察", "",
                  "合并股票池：11 个板块各 10 家龙头，并加入市值前 100 后去重，共 %d 家；符合观察条件 %d 家。规则筛选的研究候选，不是买入建议。" % (screen.get("universe_size", 0), len(candidates)), "",
                  "| 公司 | 板块 | 行情日期 | 前期上涨 | 距高点回调 | 50 / 200 日均线 |",
                  "| --- | --- | --- | ---: | ---: | ---: |"]
        for item in candidates:
            lines.append("| %s · %s | %s | %s | %s | %s | %.2f / %.2f |" % (
                clean(item.get("name")), clean(item.get("symbol")), clean(item.get("sector")),
                clean(item.get("trading_date")), pct(item.get("gain_pct")), pct(item.get("pullback_pct")),
                item.get("sma50", 0), item.get("sma200", 0)))
        if not candidates:
            lines.append("\n本份日报暂无同时满足四项规则的研究候选。")
    calendar = snapshot.get("earnings")
    if calendar:
        lines += ["", "## 本周大公司财报", "",
                  "美东周区间：%s 至 %s" % (clean(calendar.get("week_start")), clean(calendar.get("week_end"))),
                  "按市值排序；预计日期以公司公告为准。EPS 是每股盈利预期，目标价空间不是财报当天涨跌预测。", "",
                  "| 公司 | 预计日期 | 时段 | 市值（美元） | EPS 预期 | 分析师目标价空间 |",
                  "| --- | --- | --- | ---: | ---: | ---: |"]
        times = {"before_open": "盘前", "after_close": "盘后", "during_market": "盘中", "unknown": "待定"}
        for item in calendar.get("companies", []):
            analyst = item.get("analyst") or {}
            cap = "—" if item.get("market_cap") is None else "%.2f 亿" % (item["market_cap"] / 100000000)
            lines.append("| %s · %s | %s | %s | %s | %s | %s |" % (
                clean(item.get("name_zh") or item.get("name")), clean(item.get("symbol")),
                clean(item.get("report_date")), times.get(item.get("report_time"), "待定"), cap,
                clean(item.get("eps_estimate")), pct(analyst.get("upside_pct"))))
        if not calendar.get("companies"):
            lines.append("\n" + ("财报来源暂不可用。" if calendar.get("status") == "error" else "已取得的日历中暂无符合市值条件的公司。"))
        lines.extend("- 财报来源提示：" + clean(error) for error in calendar.get("errors", []))
    lines += ["", "## 来源状态", ""]
    lines.extend("- %s：%s，%s" % (clean(item["name"]), clean(item["status"]), clean(item["detail"]))
                 for item in snapshot.get("sources", []))
    lines += ["", "行业 ETF 为板块代理；热度、新闻优先级和龙头回调观察由公开规则计算。行情可能延迟，历史表现不保证未来结果。", ""]
    return "\n".join(lines)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MarketDaily/1.0"

        def log_message(self, fmt, *args):
            LOG.info("%s %s", self.client_address[0], fmt % args)

        def respond(self, status, body, content_type="application/json; charset=utf-8", download=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % download)
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            try:
                host = urlparse("http://" + self.headers.get("Host", "")).hostname
            except ValueError:
                host = None
            return host in ("127.0.0.1", "localhost", "::1")

        def selected_date(self, query):
            values = query.get("date", [])
            return validate_date(values[0]) if values else None

        def do_GET(self):
            if not self.valid_host():
                self.respond(403, {"error": "请通过 localhost 或 127.0.0.1 访问"})
                return
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/api/dashboard":
                    self.respond(200, app.dashboard(self.selected_date(query)))
                elif parsed.path == "/api/demo":
                    self.respond(200, app.dashboard(demo=True))
                elif parsed.path == "/api/health":
                    self.respond(200, {"ok": True, "collection": app.state(), "timezone": "America/New_York"})
                elif parsed.path == "/api/export":
                    snapshot = app.archive.get(self.selected_date(query))
                    if not snapshot:
                        self.respond(404, {"error": "该日期暂无已保存日报"})
                        return
                    fmt = query.get("format", ["md"])[0]
                    if fmt not in ("md", "json"):
                        raise ValueError("导出格式必须为 md 或 json")
                    filename = "market-daily-%s.%s" % (snapshot["date"], fmt)
                    if fmt == "json":
                        self.respond(200, snapshot, download=filename)
                    else:
                        self.respond(200, markdown_report(snapshot), "text/markdown; charset=utf-8", filename)
                else:
                    files = {"/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css",
                             "/app.js": "app.js", "/favicon.svg": "favicon.svg"}
                    filename = files.get(parsed.path)
                    path = ROOT / "static" / (filename or "__missing__")
                    if filename and path.is_file():
                        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                        self.respond(200, path.read_bytes(), mime + "; charset=utf-8")
                    else:
                        self.respond(404, {"error": "页面不存在"})
            except ValueError as exc:
                self.respond(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                LOG.exception("Request failed")
                self.respond(500, {"error": "服务暂时无法处理请求，请检查终端日志"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if (not self.valid_host() or self.headers.get("Sec-Fetch-Site") == "cross-site"
                    or (origin and origin != "http://" + self.headers.get("Host", ""))):
                self.respond(403, {"error": "仅接受本机网页发起的采集请求"})
                return
            if urlparse(self.path).path != "/api/refresh":
                self.respond(404, {"error": "接口不存在"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1024:
                    raise ValueError("请求体过大")
                if length:
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError("请求体必须为 JSON 对象")
            except (ValueError, UnicodeDecodeError) as exc:
                self.respond(400, {"error": str(exc)})
                return
            started = app.refresh()
            self.respond(202, {"running": True, "started": started})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="美股市场日报 · 本地网页与自动采集")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=str(ROOT / "data" / "market.db"))
    parser.add_argument("--hour", type=int, choices=range(24), default=18, metavar="0-23")
    parser.add_argument("--minute", type=int, choices=range(60), default=0, metavar="0-59")
    parser.add_argument("--no-schedule", action="store_true")
    parser.add_argument("--no-startup-refresh", action="store_true")
    parser.add_argument("--collect-once", action="store_true", help="采集一次并退出，可配合系统定时任务")
    parser.add_argument("--translate-saved", action="store_true", help="只翻译最新已存日报并退出，不重新采集行情")
    parser.add_argument("--no-translate", action="store_true", help="保留新闻原文，跳过在线翻译")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.translate_saved and (args.collect_once or args.no_translate):
        parser.error("--translate-saved 不能与 --collect-once 或 --no-translate 同时使用")
    translator = None if args.no_translate else Translator(args.db)
    app = Application(args.db, not args.no_schedule, args.hour, args.minute,
                      translator=translator, earnings_collector=earnings.collect_earnings,
                      leader_collector=leader_screen.collect_leader_screen,
                      sentiment_collector=sentiment.collect_sentiment)
    if args.translate_saved:
        snapshot = app.archive.get()
        if not snapshot:
            LOG.error("尚无日报，请先运行 --collect-once")
            return 1
        translated = translator.enrich_snapshot(snapshot)
        app.archive.save(translated)
        LOG.info("Translation finished: %s", translated.get("translation", {}))
        return 0
    if args.collect_once:
        app.refresh()
        app.worker.join()
        LOG.info("Collection finished: %s", app.state())
        return 1 if app.state()["last_status"] == "error" else 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    server.daemon_threads = True
    server.timeout = 1
    LOG.info("Open http://127.0.0.1:%s · timezone America/New_York · daily %02d:%02d%s",
             args.port, args.hour, args.minute, " (disabled)" if args.no_schedule else "")
    latest = app.archive.get()
    if not args.no_startup_refresh and (not latest or latest["date"] != market_now().date().isoformat()
                                       or "recent_news" not in latest or "earnings" not in latest
                                       or "leader_screen" not in latest
                                       or "sentiment" not in latest
                                       or (translator and "translation" not in latest)):
        app.refresh(scheduled=app.enabled and app.archive.schedule_due(market_now(), args.hour, args.minute))
    app.start_scheduler()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        LOG.info("Shutting down")
    finally:
        app.stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
