"""Explainable public-equity screens with explicit unknown data states."""
import json
import math
import statistics
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from providers import ProviderError, fetch_bytes


STRATEGIES = (
    {"id": "drawdown_repair", "name": "跌后修复"},
    {"id": "value_turnaround", "name": "低估值转强"},
    {"id": "growth_strength", "name": "成长强势"},
)


def _number(value, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    value = float(value)
    return value if not positive or value > 0 else None


def _condition(key, label, status, actual, threshold, date=None, source=None):
    return {"key": key, "label": label, "status": status, "actual": actual,
            "threshold": threshold, "date": date, "source": source}


def _result(conditions, matched_reason):
    if any(item["status"] == "UNKNOWN" for item in conditions):
        status, reason = "insufficient", "必要数据不足，不能判断是否命中"
    elif all(item["status"] == "PASS" for item in conditions):
        status, reason = "candidate", matched_reason
    else:
        status, reason = "not_matched", "至少一项必要条件未通过"
    return {"status": status, "reason": reason, "conditions": conditions,
            "checks": {item["key"]: item["status"] for item in conditions}}


def parse_analyst_forecasts(payload):
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        rows = payload["data"]["yearlyForecast"]["rows"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("年度 EPS 预测格式不可识别") from exc
    result = []
    for row in rows or []:
        fiscal_end = str(row.get("fiscalEnd") or "").strip()
        eps = _number(row.get("consensusEPSForecast"))
        if fiscal_end and eps is not None:
            result.append({"fiscal_end": fiscal_end, "eps": eps,
                           "analyst_count": row.get("noOfEstimates")})
    return result


def parse_quarterly_fundamentals(payload):
    try:
        if isinstance(payload, (bytes, str)):
            payload = json.loads(payload)
        series = payload["timeseries"]["result"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("季度财务格式不可识别") from exc
    values = {}
    for group in series:
        key = (group.get("meta") or {}).get("type", [None])[0]
        if key not in ("quarterlyTotalRevenue", "quarterlyDilutedEPS"):
            continue
        short = "revenue" if key == "quarterlyTotalRevenue" else "eps"
        for row in group.get(key) or []:
            raw = _number((row.get("reportedValue") or {}).get("raw"))
            if raw is not None and row.get("asOfDate"):
                values.setdefault(row["asOfDate"], {})[short] = raw
    return [{"date": date, **item} for date, item in sorted(values.items())
            if "revenue" in item and "eps" in item]


def fetch_stock_fundamentals(stock, now):
    symbol = stock["symbol"]
    encoded = urllib.parse.quote(symbol, safe="")
    errors = []
    forecasts, quarters = [], []
    try:
        url = "https://api.nasdaq.com/api/analyst/%s/earnings-forecast" % encoded
        forecasts = parse_analyst_forecasts(fetch_bytes(url))
    except Exception as exc:
        errors.append("EPS 预测：%s" % (str(exc) if isinstance(exc, ProviderError) else "读取失败"))
    try:
        start = int((now - timedelta(days=1100)).timestamp())
        end = int((now + timedelta(days=2)).timestamp())
        url = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
               "?symbol=%s&type=quarterlyTotalRevenue,quarterlyDilutedEPS&period1=%d&period2=%d"
               % (encoded, encoded, start, end))
        quarters = parse_quarterly_fundamentals(fetch_bytes(url))
    except Exception as exc:
        errors.append("季度财务：%s" % (str(exc) if isinstance(exc, ProviderError) else "读取失败"))
    return symbol, forecasts, quarters, errors


def enrich_fundamentals(stocks, now):
    by_symbol = {stock["symbol"]: stock for stock in stocks}
    with ThreadPoolExecutor(max_workers=8) as executor:
        pending = {executor.submit(fetch_stock_fundamentals, stock, now): stock["symbol"] for stock in stocks}
        for future in as_completed(pending):
            symbol = pending[future]
            try:
                _, forecasts, quarters, errors = future.result()
            except Exception:
                forecasts, quarters, errors = [], [], ["基本面数据处理失败"]
            by_symbol[symbol]["eps_forecasts"] = forecasts
            by_symbol[symbol]["quarterly_fundamentals"] = quarters
            if errors:
                by_symbol[symbol]["fundamental_errors"] = errors


def load_estimate_archives(report_dir="web-data/estimate-history"):
    archives = []
    for path in sorted(Path(report_dir).glob("????-??-??.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            archives.append((path.stem, payload.get("stocks") or {}))
        except (OSError, ValueError, TypeError):
            continue
    return archives


def _old_forecast(stock, target_date, archives):
    forecasts = stock.get("eps_forecasts") or []
    if not forecasts:
        return None, None
    fiscal_end = forecasts[0]["fiscal_end"]
    for date, symbols in archives:
        if date <= target_date:
            old = (symbols.get(stock["symbol"]) or {}).get(fiscal_end)
            return old, date
    return None, None


def _returns(closes, sessions):
    return (closes[-1] / closes[-1 - sessions] - 1) * 100 if len(closes) > sessions else None


def _quarter_growth(quarters):
    if len(quarters) < 6:
        return None
    growth = []
    for index in (-2, -1):
        current, prior = quarters[index], quarters[index - 4]
        if prior["revenue"] <= 0 or current["eps"] <= 0 or prior["eps"] <= 0:
            return None
        growth.append({"date": current["date"],
                       "revenue": (current["revenue"] / prior["revenue"] - 1) * 100,
                       "eps": (current["eps"] / prior["eps"] - 1) * 100})
    return growth


def evaluate(stocks, sector_returns, industry_forward_pes, archives=None):
    archives = archives or []
    counts = {item["id"]: 0 for item in STRATEGIES}
    pending = {item["id"]: 0 for item in STRATEGIES}
    for stock in stocks:
        history = stock.get("history") or []
        closes = [point["close"] for point in history]
        date = stock.get("trading_date")
        forecasts = stock.get("eps_forecasts") or []
        current_eps = forecasts[0] if forecasts else None
        next_eps = forecasts[1] if len(forecasts) > 1 else None
        target_date = history[-64]["date"] if len(history) >= 64 else ""
        old_eps, old_date = _old_forecast(stock, target_date, archives) if target_date else (None, None)
        revision = ((current_eps["eps"] / old_eps - 1) * 100
                    if current_eps and old_eps and old_eps > 0 else None)

        # 1. Drawdown repair.
        high252 = max(closes[-252:]) if len(closes) >= 252 else None
        drawdown = (closes[-1] / high252 - 1) * 100 if high252 else None
        above5 = None
        ma50_now = ma50_old = None
        if len(closes) >= 70:
            above5 = all(closes[index] > sum(closes[index - 49:index + 1]) / 50
                         for index in range(len(closes) - 5, len(closes)))
            ma50_now = sum(closes[-50:]) / 50
            ma50_old = sum(closes[-70:-20]) / 50
        repair = [
            _condition("drawdown", "较 252 日高点回撤", "UNKNOWN" if drawdown is None else "PASS" if drawdown <= -20 else "FAIL",
                       None if drawdown is None else round(drawdown, 2), "≤ -20%", date, "Yahoo Finance 复权收盘"),
            _condition("above_ma50_5d", "连续 5 日站上 MA50", "UNKNOWN" if above5 is None else "PASS" if above5 else "FAIL",
                       above5, "连续 5 个交易日", date, "Yahoo Finance 复权收盘"),
            _condition("ma50_rising", "MA50 开始上升", "UNKNOWN" if ma50_now is None else "PASS" if ma50_now > ma50_old else "FAIL",
                       None if ma50_now is None else round(ma50_now - ma50_old, 2), "MA50[t] > MA50[t-20]", date, "Yahoo Finance 复权收盘"),
            _condition("eps_revision", "同财年 EPS 预期未下降", "UNKNOWN" if revision is None else "PASS" if revision >= 0 and current_eps["eps"] > 0 else "FAIL",
                       None if revision is None else round(revision, 2), "当前 ≥ 63 个交易日前，且均为正", old_date, "Nasdaq 一致预期快照"),
        ]

        # 2. Value turnaround.
        forward_pe = _number(stock.get("forward_pe"), positive=True)
        peers = industry_forward_pes.get(stock.get("industry"), [])
        peer_median = statistics.median(peers) if len(peers) >= 6 else None
        eps_growth = (next_eps["eps"] / current_eps["eps"] - 1) * 100 if current_eps and next_eps and current_eps["eps"] > 0 and next_eps["eps"] > 0 else None
        ret63 = _returns(closes, 63)
        sector63 = (sector_returns.get(stock.get("sector")) or {}).get("return_63d")
        value = [
            _condition("peer_discount", "未来 P/E 低于同行", "UNKNOWN" if forward_pe is None or peer_median is None else "PASS" if forward_pe <= peer_median * .8 else "FAIL",
                       {"forward_pe": forward_pe, "peer_median": None if peer_median is None else round(peer_median, 2), "peer_count": len(peers)}, "≤ 同细分行业中位数 × 0.8；至少 5 家其他同行", date, "Yahoo Finance / Nasdaq 行业"),
            _condition("next_fy_growth", "下一财年 EPS 增长", "UNKNOWN" if eps_growth is None else "PASS" if eps_growth > 0 else "FAIL",
                       None if eps_growth is None else round(eps_growth, 2), "下一财年 > 本财年，且均为正", date, "Nasdaq 一致预期"),
            _condition("eps_revision", "同财年 EPS 预期未下修", "UNKNOWN" if revision is None else "PASS" if revision >= 0 else "FAIL",
                       None if revision is None else round(revision, 2), "当前 ≥ 63 个交易日前", old_date, "Nasdaq 一致预期快照"),
            _condition("above_ma200", "站上 MA200", "UNKNOWN" if len(closes) < 200 else "PASS" if closes[-1] > sum(closes[-200:]) / 200 else "FAIL",
                       None if len(closes) < 200 else round((closes[-1] / (sum(closes[-200:]) / 200) - 1) * 100, 2), "> 0%", date, "Yahoo Finance 复权收盘"),
            _condition("sector_relative_63d", "63 日跑赢板块", "UNKNOWN" if ret63 is None or sector63 is None else "PASS" if ret63 > sector63 else "FAIL",
                       None if ret63 is None or sector63 is None else round(ret63 - sector63, 2), "> 0 个百分点", date, "个股与固定板块 ETF 复权收盘"),
        ]

        # 3. Growth strength.
        growth = _quarter_growth(stock.get("quarterly_fundamentals") or [])
        revenue_pass = growth is not None and all(item["revenue"] >= 10 for item in growth)
        eps_pass = growth is not None and all(item["eps"] >= 10 for item in growth)
        ret126 = _returns(closes, 126)
        sector126 = (sector_returns.get(stock.get("sector")) or {}).get("return_126d")
        ma200_now = sum(closes[-200:]) / 200 if len(closes) >= 220 else None
        ma200_old = sum(closes[-220:-20]) / 200 if len(closes) >= 220 else None
        growth_conditions = [
            _condition("revenue_growth", "连续两季营收同比增长", "UNKNOWN" if growth is None else "PASS" if revenue_pass else "FAIL",
                       None if growth is None else [round(item["revenue"], 2) for item in growth], "两季各 ≥ 10%", growth[-1]["date"] if growth else None, "Yahoo Finance 季度财务"),
            _condition("eps_growth", "连续两季 EPS 同比增长", "UNKNOWN" if growth is None else "PASS" if eps_pass else "FAIL",
                       None if growth is None else [round(item["eps"], 2) for item in growth], "两季各 ≥ 10%，当期与去年同期均为正", growth[-1]["date"] if growth else None, "Yahoo Finance 季度财务"),
            _condition("eps_revision_up", "同财年 EPS 预期上调", "UNKNOWN" if revision is None else "PASS" if revision >= 3 else "FAIL",
                       None if revision is None else round(revision, 2), "较 63 个交易日前 ≥ 3%", old_date, "Nasdaq 一致预期快照"),
            _condition("ma200_rising", "长期趋势向上", "UNKNOWN" if ma200_now is None else "PASS" if closes[-1] > ma200_now > ma200_old else "FAIL",
                       None if ma200_now is None else {"price_vs_ma200_pct": round((closes[-1] / ma200_now - 1) * 100, 2), "ma200_change": round(ma200_now - ma200_old, 2)}, "价格 > MA200，且 MA200[t] > MA200[t-20]", date, "Yahoo Finance 复权收盘"),
            _condition("sector_relative_126d", "126 日跑赢板块", "UNKNOWN" if ret126 is None or sector126 is None else "PASS" if ret126 > sector126 else "FAIL",
                       None if ret126 is None or sector126 is None else round(ret126 - sector126, 2), "> 0 个百分点", date, "个股与固定板块 ETF 复权收盘"),
        ]
        stock["strategy_results"] = {
            "drawdown_repair": _result(repair, "回撤后的价格趋势正在修复，盈利预期未继续恶化"),
            "value_turnaround": _result(value, "相对同行估值有折扣，盈利和相对价格趋势开始转强"),
            "growth_strength": _result(growth_conditions, "营收、盈利、预期与相对价格趋势共同走强"),
        }
        for strategy_id, result in stock["strategy_results"].items():
            counts[strategy_id] += result["status"] == "candidate"
            pending[strategy_id] += result["status"] == "insufficient"
    return [{**strategy, "candidate_count": counts[strategy["id"]],
             "insufficient_count": pending[strategy["id"]]} for strategy in STRATEGIES]


def estimate_snapshot(stocks):
    result = {}
    for stock in stocks:
        rows = stock.get("eps_forecasts") or []
        if rows:
            result[stock["symbol"]] = {row["fiscal_end"]: row["eps"] for row in rows}
    return {"stocks": result}
