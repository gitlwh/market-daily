"""Deterministic watchlist screen for large, liquid US sector leaders.

This is an idea-generation screen, not a buy recommendation.  The curated
universe is deliberately stable and auditable: ten familiar leaders for each
of the eleven dashboard sectors.  Signals use completed, split/dividend
adjusted daily closes from Yahoo's unofficial chart endpoint.
"""
import json
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as day_time, timedelta

from providers import EASTERN, ProviderError, eastern_now, fetch_bytes


SECTOR_LEADERS = {
    "XLK": (("AAPL", "苹果"), ("MSFT", "微软"), ("NVDA", "英伟达"), ("AVGO", "博通"),
            ("ORCL", "甲骨文"), ("CRM", "赛富时"), ("AMD", "超威半导体"), ("ADBE", "Adobe"),
            ("CSCO", "思科"), ("ACN", "埃森哲")),
    "XLC": (("GOOGL", "谷歌"), ("META", "Meta"), ("NFLX", "奈飞"), ("DIS", "迪士尼"),
            ("TMUS", "T-Mobile"), ("VZ", "威瑞森"), ("T", "美国电话电报"), ("CHTR", "Charter"),
            ("EA", "艺电"), ("SPOT", "Spotify")),
    "XLY": (("AMZN", "亚马逊"), ("TSLA", "特斯拉"), ("HD", "家得宝"), ("MCD", "麦当劳"),
            ("BKNG", "Booking"), ("LOW", "劳氏"), ("TJX", "TJX"), ("NKE", "耐克"),
            ("SBUX", "星巴克"), ("MAR", "万豪")),
    "XLP": (("WMT", "沃尔玛"), ("COST", "开市客"), ("PG", "宝洁"), ("KO", "可口可乐"),
            ("PEP", "百事"), ("PM", "菲利普莫里斯国际"), ("MO", "奥驰亚"), ("MDLZ", "亿滋"),
            ("CL", "高露洁"), ("KMB", "金佰利")),
    "XLE": (("XOM", "埃克森美孚"), ("CVX", "雪佛龙"), ("COP", "康菲石油"), ("SLB", "斯伦贝谢"),
            ("EOG", "EOG 能源"), ("MPC", "马拉松原油"), ("PSX", "Phillips 66"), ("VLO", "瓦莱罗"),
            ("OXY", "西方石油"), ("KMI", "金德摩根")),
    "XLF": (("BRK-B", "伯克希尔哈撒韦"), ("JPM", "摩根大通"), ("V", "Visa"), ("MA", "万事达"),
            ("BAC", "美国银行"), ("WFC", "富国银行"), ("GS", "高盛"), ("MS", "摩根士丹利"),
            ("AXP", "美国运通"), ("BLK", "贝莱德")),
    "XLV": (("LLY", "礼来"), ("UNH", "联合健康"), ("JNJ", "强生"), ("ABBV", "艾伯维"),
            ("MRK", "默沙东"), ("TMO", "赛默飞世尔"), ("ABT", "雅培"), ("AMGN", "安进"),
            ("ISRG", "直觉外科"), ("PFE", "辉瑞")),
    "XLI": (("GE", "GE 航空"), ("CAT", "卡特彼勒"), ("RTX", "RTX"), ("BA", "波音"),
            ("HON", "霍尼韦尔"), ("UNP", "联合太平洋"), ("UPS", "联合包裹"), ("DE", "迪尔"),
            ("ETN", "伊顿"), ("LMT", "洛克希德马丁")),
    "XLB": (("LIN", "林德"), ("SHW", "宣伟"), ("FCX", "自由港麦克莫兰"), ("APD", "空气化工"),
            ("ECL", "艺康"), ("NEM", "纽蒙特"), ("NUE", "纽柯"), ("DOW", "陶氏"),
            ("MLM", "马丁玛丽埃塔"), ("VMC", "火神材料")),
    "XLRE": (("PLD", "安博"), ("AMT", "美国电塔"), ("EQIX", "Equinix"), ("WELL", "Welltower"),
             ("SPG", "西蒙地产"), ("PSA", "大众仓储"), ("O", "Realty Income"), ("CCI", "冠城国际"),
             ("DLR", "Digital Realty"), ("CBRE", "世邦魏理仕")),
    "XLU": (("NEE", "新纪元能源"), ("SO", "南方公司"), ("DUK", "杜克能源"), ("CEG", "Constellation Energy"),
            ("AEP", "美国电力"), ("SRE", "桑普拉"), ("VST", "Vistra"), ("EXC", "Exelon"),
            ("XEL", "Xcel Energy"), ("PEG", "PSEG")),
}

RULES = {"gain_min_pct": 20.0, "pullback_min_pct": 5.0, "pullback_max_pct": 12.0,
         "high_window": 20, "high_recency": 10, "gain_lookback": 60,
         "sma_short": 50, "sma_long": 200}
SOURCE_URL = "https://finance.yahoo.com/"


def _finite(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    value = float(value)
    return value if not positive or value > 0 else None


def parse_history(payload, symbol, now=None):
    """Return completed adjusted-close bars and reject mismatched/invalid data."""
    now = eastern_now(now)
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        chart = payload["chart"]
        if chart.get("error"):
            raise ProviderError("行情源返回错误")
        result = chart["result"][0]
        meta = result.get("meta") or {}
        if str(meta.get("symbol", symbol)).upper() != symbol:
            raise ProviderError("行情代码与请求不一致")
        if meta.get("currency", "USD") != "USD":
            raise ProviderError("行情币种不是 USD")
        stamps = result.get("timestamp") or []
        adjusted = result["indicators"]["adjclose"][0].get("adjclose") or []
    except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ProviderError("个股历史行情格式不可识别") from exc
    by_date = {}
    for index, timestamp in enumerate(stamps):
        try:
            stamp = datetime.fromtimestamp(timestamp, EASTERN)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        close = _finite(adjusted[index] if index < len(adjusted) else None, positive=True)
        if close is None or stamp > now:
            continue
        # Never turn an unfinished current daily bar into a screen signal.
        if stamp.date() == now.date() and now.time() < day_time(16, 15):
            continue
        by_date[stamp.date().isoformat()] = close
    return [{"date": day, "close": close} for day, close in sorted(by_date.items())]


def screen_stock(symbol, name, sector, bars, rules=None):
    rules = dict(RULES, **(rules or {}))
    needed = rules["sma_long"]
    if len(bars) < needed:
        return {"symbol": symbol, "name": name, "sector": sector, "status": "insufficient",
                "reason": "完整复权日线不足 %d 根" % needed, "trading_date": bars[-1]["date"] if bars else None}
    closes = [bar["close"] for bar in bars]
    current = closes[-1]
    start = max(0, len(closes) - rules["high_window"])
    high_index = max(range(start, len(closes)), key=closes.__getitem__)
    high = closes[high_index]
    sessions_since_high = len(closes) - 1 - high_index
    base_index = high_index - rules["gain_lookback"]
    if base_index < 0:
        return {"symbol": symbol, "name": name, "sector": sector, "status": "insufficient",
                "reason": "高点前历史样本不足", "trading_date": bars[-1]["date"]}
    gain = (high / closes[base_index] - 1) * 100
    pullback = (current / high - 1) * 100
    sma50 = sum(closes[-rules["sma_short"]:]) / rules["sma_short"]
    sma200 = sum(closes[-rules["sma_long"]:]) / rules["sma_long"]
    checks = {
        "prior_gain": gain >= rules["gain_min_pct"],
        "recent_high": sessions_since_high < rules["high_recency"],
        "pullback": rules["pullback_min_pct"] <= -pullback <= rules["pullback_max_pct"],
        "uptrend": current > sma50 > sma200,
    }
    matched = all(checks.values())
    return {"symbol": symbol, "name": name, "sector": sector,
            "status": "candidate" if matched else "not_matched",
            "trading_date": bars[-1]["date"], "price": round(current, 4),
            "recent_high": round(high, 4), "high_date": bars[high_index]["date"],
            "sessions_since_high": sessions_since_high, "gain_pct": round(gain, 2),
            "pullback_pct": round(pullback, 2), "sma50": round(sma50, 4), "sma200": round(sma200, 4),
            "checks": checks, "reason": "符合四项观察条件" if matched else "尚未同时满足四项观察条件",
            "history": [{"date": bar["date"], "close": round(bar["close"], 4)} for bar in bars[-90:]]}


def collect_leader_screen(now=None):
    now = eastern_now(now)
    universe = [(symbol, name, sector) for sector, entries in SECTOR_LEADERS.items() for symbol, name in entries]
    results, errors, retry = [], [], []

    def collect_one(symbol, name, sector):
        encoded = urllib.parse.quote(symbol, safe="")
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=1y&events=div%%2Csplits" % encoded
        return screen_stock(symbol, name, sector, parse_history(fetch_bytes(url), symbol, now))

    with ThreadPoolExecutor(max_workers=8) as executor:
        pending = {executor.submit(collect_one, *item): item for item in universe}
        for future in as_completed(pending):
            symbol, name, sector = pending[future]
            try:
                results.append(future.result())
            except Exception as exc:
                message = str(exc) if isinstance(exc, ProviderError) else "数据处理失败"
                retry.append((symbol, name, sector, message))
    # Retry only failures, at lower concurrency. Successful symbols are never
    # fetched twice and a persistent failure stays explicit in the payload.
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {executor.submit(collect_one, symbol, name, sector): (symbol, name, sector, first_error)
                   for symbol, name, sector, first_error in retry}
        for future in as_completed(pending):
            symbol, name, sector, first_error = pending[future]
            try:
                results.append(future.result())
            except Exception as exc:
                message = str(exc) if isinstance(exc, ProviderError) else first_error
                results.append({"symbol": symbol, "name": name, "sector": sector,
                                "status": "insufficient", "reason": message, "trading_date": None})
                errors.append("%s：%s（重试后仍失败）" % (symbol, message))
    order = {symbol: index for index, (symbol, _, _) in enumerate(universe)}
    results.sort(key=lambda item: (item["status"] != "candidate", order[item["symbol"]]))
    available = sum(item["status"] != "insufficient" for item in results)
    status = "ok" if available == len(universe) else "partial" if available else "error"
    return {"status": status, "as_of": now.isoformat(timespec="seconds"), "universe_size": len(universe),
            "available_count": available, "candidate_count": sum(item["status"] == "candidate" for item in results),
            "rules": dict(RULES), "stocks": results, "errors": errors,
            "source": {"name": "Yahoo Finance · 龙头回调观察", "url": SOURCE_URL, "status": status,
                       "detail": "%d / %d 只固定股票池具备完整筛选样本；非官方接口，可能延迟" % (available, len(universe))},
            "note": "规则筛选的研究候选，不是买入建议。历史走势不能保证未来表现。"}


def demo_leader_screen():
    now = datetime(2026, 9, 18, 18, tzinfo=EASTERN)
    stocks = []
    for sector, entries in SECTOR_LEADERS.items():
        for index, (symbol, name) in enumerate(entries):
            candidate = index == 0 and sector in ("XLK", "XLY", "XLF")
            stocks.append({"symbol": symbol, "name": name, "sector": sector,
                           "status": "candidate" if candidate else "not_matched", "trading_date": "2026-09-18",
                           "price": 120 + index, "recent_high": 130 + index, "high_date": "2026-09-14",
                           "sessions_since_high": 4, "gain_pct": 28.0 if candidate else 12.0,
                           "pullback_pct": -7.69, "sma50": 112, "sma200": 98,
                           "checks": {"prior_gain": candidate, "recent_high": True, "pullback": True, "uptrend": True},
                           "reason": "符合四项观察条件" if candidate else "尚未同时满足四项观察条件",
                           "history": [{"date": (now.date() - timedelta(days=day)).isoformat(), "close": 120 - day * .08} for day in range(29, -1, -1)]})
    return {"status": "ok", "as_of": now.isoformat(), "universe_size": 110, "available_count": 110,
            "candidate_count": 3, "rules": dict(RULES), "stocks": stocks, "errors": [],
            "source": {"name": "本地演示选股数据", "url": None, "status": "ok", "detail": "110 家虚构走势示例"},
            "note": "演示模式：价格与筛选结果均为虚构。规则筛选的研究候选，不是买入建议。"}
