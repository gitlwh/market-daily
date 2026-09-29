"""Deterministic watchlist screen for large, liquid US stocks.

This is an idea-generation screen, not a buy recommendation.  The curated
universe combines ten familiar leaders for each dashboard sector with the
current top 100 US-listed stocks by market capitalization. Signals use
completed, split/dividend adjusted daily closes from Yahoo's unofficial chart
endpoint.
"""
import json
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as day_time, timedelta

from providers import EASTERN, ProviderError, eastern_now, fetch_bytes
import advanced_strategies


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
MARKET_CAP_SOURCE_URL = ("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false"
                         "&scrIds=largest_market_cap&count=250&start=0")
SECTOR_SOURCE_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
MAJOR_US_EXCHANGES = {"NMS", "NYQ", "NGM", "NCM"}
NASDAQ_SECTORS = {
    "Technology": "XLK", "Telecommunications": "XLC",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Energy": "XLE", "Finance": "XLF", "Health Care": "XLV",
    "Industrials": "XLI", "Basic Materials": "XLB",
    "Real Estate": "XLRE", "Utilities": "XLU",
}
SECTOR_OVERRIDES = {"BRK-A": "XLF"}


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


def parse_market_cap_top_100(payload):
    """Parse and rank major-US-exchange stocks by reported market cap."""
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        if payload.get("finance"):
            rows = payload["finance"]["result"][0]["quotes"]
        else:  # Kept for deterministic fixtures and provider-shape tolerance.
            data = payload["data"]
            rows = data.get("rows") or data["table"]["rows"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("市值榜格式不可识别") from exc
    ranked = []
    for row in rows:
        try:
            symbol = str(row.get("symbol") or "").strip().upper().replace("/", "-")
            market_cap = _finite(float(str(row.get("marketCap")).replace(",", "")), positive=True)
        except (TypeError, ValueError):
            continue
        if row.get("exchange") and row.get("exchange") not in MAJOR_US_EXCHANGES:
            continue
        sector = NASDAQ_SECTORS.get(str(row.get("sector") or "").strip(), "OTHER")
        if not symbol or market_cap is None:
            continue
        pe_ratio = _finite(row.get("trailingPE"), positive=True)
        forward_pe = _finite(row.get("forwardPE"), positive=True)
        ranked.append({"symbol": symbol,
                       "name": str(row.get("shortName") or row.get("name") or symbol).strip(),
                       "sector": sector, "market_cap": round(market_cap), "pe_ratio": pe_ratio,
                       "forward_pe": forward_pe})
    ranked.sort(key=lambda item: item["market_cap"], reverse=True)
    result, seen = [], set()
    for item in ranked:
        if item["symbol"] in seen:
            continue
        seen.add(item["symbol"])
        item["market_cap_rank"] = len(result) + 1
        result.append(item)
        if len(result) == 100:
            break
    if len(result) < 100:
        raise ProviderError("市值榜有效股票不足 100 家")
    return result


def parse_pe_lookup(payload):
    """Keep positive trailing P/E values from the broader screener response."""
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        rows = payload["finance"]["result"][0]["quotes"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return {str(row.get("symbol") or "").strip().upper().replace("/", "-"): pe
            for row in rows if (pe := _finite(row.get("trailingPE"), positive=True)) is not None}


def parse_quote_metadata(payload):
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        rows = payload["finance"]["result"][0]["quotes"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return {str(row.get("symbol") or "").strip().upper().replace("/", "-"): {
                "pe_ratio": _finite(row.get("trailingPE"), positive=True),
                "forward_pe": _finite(row.get("forwardPE"), positive=True)}
            for row in rows if row.get("symbol")}


def parse_sector_map(payload):
    """Return Yahoo-style symbols mapped to the dashboard's eleven sectors."""
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        rows = payload["data"]["rows"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("板块分类格式不可识别") from exc
    result = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper().replace("/", "-")
        sector = NASDAQ_SECTORS.get(str(row.get("sector") or "").strip())
        if symbol and sector:
            result[symbol] = sector
    return result


def parse_industry_map(payload):
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        rows = payload["data"]["rows"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}
    return {str(row.get("symbol") or "").strip().upper().replace("/", "-"): str(row.get("industry") or "").strip()
            for row in rows if row.get("symbol") and row.get("industry")}


def completed_weekly_closes(bars):
    """Return completed calendar-week closes; omit an in-progress Mon-Thu week."""
    weeks = {}
    for bar in bars:
        stamp = datetime.fromisoformat(bar["date"]).date()
        weeks[stamp.isocalendar()[:2]] = (stamp, bar["close"])
    ordered = [value for _, value in sorted(weeks.items())]
    if ordered and ordered[-1][0].weekday() < 4:
        ordered.pop()
    return ordered


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
    return_20d = (current / closes[-21] - 1) * 100 if len(closes) >= 21 else None
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
            "return_20d_pct": round(return_20d, 2) if return_20d is not None else None,
            "checks": checks, "reason": "符合四项观察条件" if matched else "尚未同时满足四项观察条件",
            "history": [{"date": bar["date"], "close": round(bar["close"], 4)} for bar in bars[-300:]]}


def collect_leader_screen(now=None):
    now = eastern_now(now)
    universe = {}
    for sector, entries in SECTOR_LEADERS.items():
        for symbol, name in entries:
            universe[symbol] = {"symbol": symbol, "name": name, "sector": sector,
                                "universe_tags": ["sector_leader"]}
    errors, universe_error, sector_error = [], None, None
    quote_metadata, industry_map = {}, {}
    try:
        market_cap_payload = fetch_bytes(MARKET_CAP_SOURCE_URL)
        market_cap_top = parse_market_cap_top_100(market_cap_payload)
        quote_metadata = parse_quote_metadata(market_cap_payload)
        for symbol, stock in universe.items():
            if symbol in quote_metadata:
                stock.update({key: value for key, value in quote_metadata[symbol].items() if value is not None})
        try:
            classification_payload = fetch_bytes(SECTOR_SOURCE_URL, max_bytes=8 * 1024 * 1024)
            sector_map = parse_sector_map(classification_payload)
            industry_map = parse_industry_map(classification_payload)
        except Exception as exc:
            sector_map, industry_map = {}, {}
            sector_error = str(exc) if isinstance(exc, ProviderError) else "板块分类读取失败"
            errors.append("市值前 100 板块分类：%s" % sector_error)
        for stock in market_cap_top:
            stock["sector"] = SECTOR_OVERRIDES.get(stock["symbol"], sector_map.get(stock["symbol"], stock["sector"]))
            stock["industry"] = industry_map.get(stock["symbol"])
        for symbol, stock in universe.items():
            stock["industry"] = industry_map.get(symbol)
        for stock in market_cap_top:
            if stock["symbol"] in universe:
                universe[stock["symbol"]].update(market_cap=stock["market_cap"],
                                                  market_cap_rank=stock["market_cap_rank"],
                                                  pe_ratio=stock.get("pe_ratio"), forward_pe=stock.get("forward_pe"),
                                                  industry=stock.get("industry"))
                universe[stock["symbol"]]["universe_tags"].append("market_cap_top_100")
            else:
                universe[stock["symbol"]] = dict(stock, universe_tags=["market_cap_top_100"])
    except Exception as exc:
        universe_error = str(exc) if isinstance(exc, ProviderError) else "市值榜读取失败"
        errors.append("市值前 100：%s；本次仍筛选固定板块龙头" % universe_error)
    universe = list(universe.values())
    results, retry = [], []

    def collect_one(stock):
        symbol = stock["symbol"]
        encoded = urllib.parse.quote(symbol, safe="")
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=2y&events=div%%2Csplits" % encoded
        result = screen_stock(symbol, stock["name"], stock["sector"], parse_history(fetch_bytes(url), symbol, now))
        result.update({key: stock[key] for key in ("universe_tags", "market_cap", "market_cap_rank", "pe_ratio", "forward_pe", "industry") if key in stock})
        return result

    with ThreadPoolExecutor(max_workers=8) as executor:
        pending = {executor.submit(collect_one, item): item for item in universe}
        for future in as_completed(pending):
            stock = pending[future]
            try:
                results.append(future.result())
            except Exception as exc:
                message = str(exc) if isinstance(exc, ProviderError) else "数据处理失败"
                retry.append((stock, message))
    # Retry only failures, at lower concurrency. Successful symbols are never
    # fetched twice and a persistent failure stays explicit in the payload.
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {executor.submit(collect_one, stock): (stock, first_error)
                   for stock, first_error in retry}
        for future in as_completed(pending):
            stock, first_error = pending[future]
            symbol, name, sector = stock["symbol"], stock["name"], stock["sector"]
            try:
                results.append(future.result())
            except Exception as exc:
                message = str(exc) if isinstance(exc, ProviderError) else first_error
                results.append({"symbol": symbol, "name": name, "sector": sector,
                                "status": "insufficient", "reason": message, "trading_date": None,
                                **{key: stock[key] for key in ("universe_tags", "market_cap", "market_cap_rank", "pe_ratio", "forward_pe", "industry") if key in stock}})
                errors.append("%s：%s（重试后仍失败）" % (symbol, message))
    order = {stock["symbol"]: index for index, stock in enumerate(universe)}
    results.sort(key=lambda item: (item["status"] != "candidate", order[item["symbol"]]))
    advanced_strategies.enrich_fundamentals(results, now)

    def benchmark_returns(symbol):
        encoded = urllib.parse.quote(symbol, safe="")
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=2y&events=div%%2Csplits" % encoded
        bars = parse_history(fetch_bytes(url), symbol, now)
        closes = [bar["close"] for bar in bars]
        return {"return_63d": round((closes[-1] / closes[-64] - 1) * 100, 4) if len(closes) >= 64 else None,
                "return_126d": round((closes[-1] / closes[-127] - 1) * 100, 4) if len(closes) >= 127 else None}

    sector_returns = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        pending = {executor.submit(benchmark_returns, sector): sector for sector in SECTOR_LEADERS}
        for future in as_completed(pending):
            sector = pending[future]
            try:
                sector_returns[sector] = future.result()
            except Exception:
                sector_returns[sector] = {"return_63d": None, "return_126d": None}

    industry_forward_pes = {}
    sector_forward_pes = {}
    universe_by_symbol = {item["symbol"]: item for item in universe}
    for symbol, meta in quote_metadata.items():
        industry, forward_pe = industry_map.get(symbol), meta.get("forward_pe")
        if industry and _finite(forward_pe, positive=True):
            industry_forward_pes.setdefault(industry, []).append(forward_pe)
        stock = universe_by_symbol.get(symbol)
        if stock and _finite(forward_pe, positive=True):
            sector_forward_pes.setdefault(stock.get("sector"), []).append(forward_pe)
    for stock in results:
        if _finite(stock.get("forward_pe"), positive=True):
            continue
        forecasts, history = stock.get("eps_forecasts") or [], stock.get("history") or []
        next_eps = forecasts[1].get("eps") if len(forecasts) > 1 else None
        if history and _finite(next_eps, positive=True):
            proxy = history[-1]["close"] / next_eps
            stock["forward_pe_proxy"] = proxy
            if stock.get("industry"):
                industry_forward_pes.setdefault(stock["industry"], []).append(proxy)
            sector_forward_pes.setdefault(stock.get("sector"), []).append(proxy)
    strategies = advanced_strategies.evaluate(
        results, sector_returns, industry_forward_pes, advanced_strategies.load_estimate_archives(),
        sector_forward_pes)
    available = sum(item["status"] != "insufficient" for item in results)
    status = "ok" if available == len(universe) else "partial" if available else "error"
    if (universe_error or sector_error) and status == "ok":
        status = "partial"
    top_count = sum("market_cap_top_100" in item.get("universe_tags", []) for item in universe)
    return {"status": status, "as_of": now.isoformat(timespec="seconds"), "universe_size": len(universe),
            "available_count": available, "candidate_count": sum(item["status"] == "candidate" for item in results),
            "sector_leader_count": 110, "market_cap_top_100_count": top_count,
            "strategies": strategies,
            "rules": dict(RULES), "stocks": results, "errors": errors,
            "source": {"name": "Yahoo Finance · 多策略选股观察", "url": SOURCE_URL, "status": status,
                       "detail": "%d / %d 家合并股票池具备完整筛选样本；市值榜 %d / 100 家；非官方接口，可能延迟" % (available, len(universe), top_count)},
            "note": "规则筛选的研究候选，不是买入建议。历史走势不能保证未来表现。"}


def demo_leader_screen():
    now = datetime(2026, 9, 18, 18, tzinfo=EASTERN)
    stocks = []
    rank = 0
    for sector, entries in SECTOR_LEADERS.items():
        for index, (symbol, name) in enumerate(entries):
            rank += 1
            candidate = index == 0 and sector in ("XLK", "XLY", "XLF")
            stocks.append({"symbol": symbol, "name": name, "sector": sector,
                           "universe_tags": ["sector_leader"] + (["market_cap_top_100"] if rank <= 100 else []),
                           "market_cap_rank": rank if rank <= 100 else None,
                           "market_cap": (5_000_000_000_000 - rank * 20_000_000_000) if rank <= 100 else None,
                           "pe_ratio": 12 + rank % 24 if rank <= 100 else None,
                           "status": "candidate" if candidate else "not_matched", "trading_date": "2026-09-18",
                           "price": 120 + index, "recent_high": 130 + index, "high_date": "2026-09-14",
                           "sessions_since_high": 4, "gain_pct": 28.0 if candidate else 12.0,
                           "pullback_pct": -7.69, "sma50": 112, "sma200": 98,
                           "checks": {"prior_gain": candidate, "recent_high": True, "pullback": True, "uptrend": True},
                           "reason": "符合四项观察条件" if candidate else "尚未同时满足四项观察条件",
                           "return_20d_pct": 6.0 if rank % 4 == 0 else 1.0,
                           "history": [{"date": (now.date() - timedelta(days=day)).isoformat(), "close": 120 - day * .08} for day in range(29, -1, -1)]})
    for demo_index, stock in enumerate(stocks, 1):
        stock["strategy_results"] = {}
        for strategy_index, strategy in enumerate(advanced_strategies.STRATEGIES, 2):
            matched = demo_index % (strategy_index + 3) == 0
            conditions = [{"key": "demo_%d" % index, "label": "演示条件 %d" % index,
                           "status": "PASS" if matched or index < 3 else "FAIL",
                           "actual": "虚构示例", "threshold": "演示阈值",
                           "date": "2026-09-18", "source": "本地演示数据"}
                          for index in range(1, 5)]
            stock["strategy_results"][strategy["id"]] = {
                "status": "candidate" if matched else "not_matched",
                "reason": "演示模式命中" if matched else "演示模式未命中",
                "conditions": conditions,
                "checks": {item["key"]: item["status"] for item in conditions}}
    return {"status": "ok", "as_of": now.isoformat(), "universe_size": 110, "available_count": 110,
            "candidate_count": 3, "sector_leader_count": 110, "market_cap_top_100_count": 100,
            "strategies": [{**strategy,
                            "candidate_count": sum(s["strategy_results"][strategy["id"]]["status"] == "candidate" for s in stocks),
                            "insufficient_count": 0}
                           for strategy in advanced_strategies.STRATEGIES],
            "rules": dict(RULES), "stocks": stocks, "errors": [],
            "source": {"name": "本地演示选股数据", "url": None, "status": "ok", "detail": "110 家虚构走势示例"},
            "note": "演示模式：价格与筛选结果均为虚构。规则筛选的研究候选，不是买入建议。"}
