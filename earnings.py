"""Public Nasdaq earnings calendar; dates are estimates, not company confirmations.

EPS consensus is never converted into a share-price forecast. Price-target
fields remain unavailable until a verifiable source is connected.
"""
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from providers import EASTERN, ProviderError, eastern_now, fetch_bytes

CALENDAR_URL = "https://www.nasdaq.com/market-activity/earnings"
API_URL = "https://api.nasdaq.com/api/calendar/earnings"
THRESHOLD = 10_000_000_000
LIMIT = 15
NOTE = "财报日历为预计日期，请以公司公告为准。EPS 是盈利预期；目标价空间不等于财报当天涨跌预测。"


def week_bounds(now=None):
    today = eastern_now(now).date()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def parse_number(value):
    """Parse dollar-denominated calendar values; missing/foreign currency is null."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip().upper().replace(",", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = re.sub(r"^USD\s*", "", text).replace("$", "")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([KMBT]?)", text)
    if not match:
        return None
    result = float(match[1]) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[match[2]]
    if not math.isfinite(result):
        return None
    return -abs(result) if negative else result


def unavailable_analyst():
    return {"status": "unavailable", "target_mean": None, "current_price": None,
            "upside_pct": None, "rating": None, "count": None, "currency": "USD",
            "horizon": None, "as_of": None, "source_url": None}


def parse_calendar(payload, report_date):
    day = date.fromisoformat(report_date)
    try:
        payload = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
        if str(payload.get("status", {}).get("rCode")) != "200":
            raise ProviderError("财报源返回失败状态")
        data = payload.get("data")
        if not isinstance(data, dict) or "rows" not in data:
            raise ProviderError("财报源未提供该日日历")
        if data.get("asOf"):
            try:
                returned_day = datetime.strptime(data["asOf"], "%a, %b %d, %Y").date()
            except (TypeError, ValueError) as exc:
                raise ProviderError("无法核对日历返回日期") from exc
            if returned_day != day:
                raise ProviderError("日历返回日期与请求不一致")
        rows = data["rows"]
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise ProviderError("财报列表格式不可识别")
    except (AttributeError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("财报响应格式不可识别") from exc
    timing = {"time-pre-market": "before_open", "time-after-hours": "after_close",
              "time-during-market": "during_market", "time-not-supplied": "unknown"}
    companies = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError("财报列表含无效公司记录")
        symbol = str(row.get("symbol") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9.^-]{1,20}", symbol):
            raise ProviderError("财报列表含无效证券代码")
        cap = parse_number(row.get("marketCap"))
        count = parse_number(row.get("noOfEsts"))
        companies.append({
            "symbol": symbol, "name": str(row.get("name") or symbol), "name_zh": None,
            "report_date": day.isoformat(), "report_time": timing.get(row.get("time"), "unknown"),
            "market_cap": cap if cap is not None and cap > 0 else None,
            "eps_estimate": parse_number(row.get("epsForecast")), "eps_currency": "USD",
            "analyst_count": int(count) if count is not None and count >= 0 and count.is_integer() else None,
            "fiscal_quarter": row.get("fiscalQuarterEnding"),
            "source_url": CALENDAR_URL + "?date=" + day.isoformat(),
            "analyst": unavailable_analyst(),
        })
    return companies


def rank_companies(companies, limit=LIMIT):
    candidates = [item for item in companies if isinstance(item.get("market_cap"), (int, float))
                  and not isinstance(item["market_cap"], bool) and math.isfinite(item["market_cap"])
                  and item["market_cap"] >= THRESHOLD]
    candidates.sort(key=lambda item: (-item["market_cap"], item.get("report_date", ""), item["symbol"]))
    ranked, seen = [], set()
    for item in candidates:
        if item["symbol"] not in seen:
            ranked.append(item)
            seen.add(item["symbol"])
    return ranked[:limit]


def collect_earnings(now=None):
    now = eastern_now(now)
    start, end = week_bounds(now)
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range(7)]
    companies, covered, errors = [], [], []

    def read_day(day):
        return parse_calendar(fetch_bytes(API_URL + "?date=" + day), day)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(read_day, day): day for day in dates}
        for future in as_completed(futures):
            day = futures[future]
            try:
                companies.extend(future.result())
                covered.append(day)
            except Exception as exc:
                message = str(exc) if isinstance(exc, ProviderError) else "日历数据处理失败"
                errors.append(day + "：" + message)
    # Duplicate source rows must not inflate the counts or the ranking.
    unique = {(item["symbol"], item["report_date"]): item for item in companies}
    companies = list(unique.values())
    status = "ok" if len(covered) == len(dates) else "partial" if covered else "error"
    large = rank_companies(companies, limit=len(companies))
    return {"status": status, "week_start": start.isoformat(), "week_end": end.isoformat(),
            "as_of": now.isoformat(timespec="seconds"), "timezone": "America/New_York",
            "threshold_usd": THRESHOLD, "limit": LIMIT, "total_scheduled": len(companies),
            "total_large": len(large), "companies": large[:LIMIT], "coverage_dates": sorted(covered),
            "sources": [{"name": "Nasdaq 财报日历", "url": CALENDAR_URL, "status": status,
                         "detail": "成功取得 %d / 7 个美东日期；预计时间，以公司公告为准" % len(covered)}],
            "errors": sorted(errors), "note": NOTE}


def demo_earnings():
    rows = [
        {"symbol": "DEMOA", "name": "演示·虚构云计算公司", "marketCap": "$250,000,000,000",
         "epsForecast": "$1.25", "noOfEsts": "12", "time": "time-after-hours"},
        {"symbol": "DEMOB", "name": "演示·虚构零售公司", "marketCap": "$85,000,000,000",
         "epsForecast": "$0.00", "noOfEsts": "8", "time": "time-pre-market"},
        {"symbol": "DEMOC", "name": "演示·虚构生物科技公司", "marketCap": "$12,000,000,000",
         "epsForecast": "($0.42)", "noOfEsts": "4", "time": "time-not-supplied"},
    ]
    dates = ["2026-09-17", "2026-09-15", "2026-09-18"]
    companies = [parse_calendar({"data": {"rows": [row]}, "status": {"rCode": 200}}, day)[0]
                 for row, day in zip(rows, dates)]
    for company in companies:
        company["source_url"] = None
    companies[0]["analyst"] = {"status": "available", "target_mean": 120, "current_price": 100,
                               "upside_pct": 20, "rating": "buy", "count": 10, "currency": "USD",
                               "horizon": "12个月（虚构示例）", "as_of": "2026-09-18T16:00:00-04:00",
                               "source_url": None}
    return {"status": "ok", "week_start": "2026-09-14", "week_end": "2026-09-20",
            "as_of": "2026-09-18T18:00:00-04:00", "timezone": "America/New_York",
            "threshold_usd": THRESHOLD, "limit": LIMIT, "total_scheduled": 3, "total_large": 3,
            "companies": companies, "coverage_dates": dates, "sources": [], "errors": [],
            "note": "演示模式：公司、日期、EPS 和目标价均为虚构。" + NOTE}
