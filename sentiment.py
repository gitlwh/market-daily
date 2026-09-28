"""Transparent multi-factor US market sentiment score (0-100)."""
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from leader_screen import parse_history
from providers import EASTERN, ProviderError, eastern_now, fetch_bytes


WEIGHTS = {"trend": 25, "sector_breadth": 20, "leader_breadth": 15,
           "momentum": 15, "volatility": 15, "news": 10}
LABELS = {"trend": "大盘趋势", "sector_breadth": "板块广度", "leader_breadth": "龙头广度",
          "momentum": "市场动量", "volatility": "波动率", "news": "新闻情绪"}
POSITIVE_WORDS = ("beat", "beats", "record high", "rally", "surge", "upgrade", "growth", "rebound",
                  "降息", "上涨", "反弹", "超预期", "创新高", "增长", "上调")
NEGATIVE_WORDS = ("miss", "misses", "selloff", "plunge", "downgrade", "layoff", "recession", "tariff",
                  "下跌", "暴跌", "裁员", "衰退", "低于预期", "下调", "关税")


def clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def mood_label(score):
    if score is None:
        return "数据不足"
    if score <= 20:
        return "极度恐慌"
    if score <= 40:
        return "偏谨慎"
    if score < 60:
        return "中性"
    if score < 80:
        return "偏乐观"
    return "极度乐观"


def _component(key, score=None, detail="", raw=None):
    valid = isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
    return {"key": key, "label": LABELS[key], "weight": WEIGHTS[key],
            "available": valid, "score": round(clamp(score), 1) if valid else None,
            "detail": detail, "raw": raw or {}}


def _fetch_history(symbol, now):
    encoded = urllib.parse.quote(symbol, safe="")
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=6mo&events=div%%2Csplits" % encoded
    return parse_history(fetch_bytes(url), symbol, now)


def calculate_sentiment(snapshot, leader_result, histories, now=None):
    now = eastern_now(now)
    spy = histories.get("SPY", [])
    vix = histories.get("^VIX", [])
    components = []

    if len(spy) >= 50:
        closes = [item["close"] for item in spy]
        price, sma20, sma50 = closes[-1], sum(closes[-20:]) / 20, sum(closes[-50:]) / 50
        score = 50 + clamp((price / sma20 - 1) * 500, -20, 20) + clamp((sma20 / sma50 - 1) * 500, -30, 30)
        components.append(_component("trend", score,
            "SPY %.2f；20 日均线 %.2f，50 日均线 %.2f" % (price, sma20, sma50),
            {"price": round(price, 4), "sma20": round(sma20, 4), "sma50": round(sma50, 4)}))
    else:
        components.append(_component("trend", detail="SPY 完整日线不足 50 根"))

    sectors = [item for item in snapshot.get("sectors", []) if item.get("comparable") and isinstance(item.get("change_pct"), (int, float))]
    if sectors:
        rising = sum(item["change_pct"] > 0 for item in sectors)
        score = rising / len(sectors) * 100
        components.append(_component("sector_breadth", score, "%d / %d 个可比板块上涨" % (rising, len(sectors)),
                                     {"rising": rising, "total": len(sectors)}))
    else:
        components.append(_component("sector_breadth", detail="没有同一交易日的可比板块行情"))

    leaders = [item for item in (leader_result or {}).get("stocks", [])
               if item.get("status") != "insufficient" and len(item.get("history") or []) >= 50]
    if leaders:
        strong = 0
        for item in leaders:
            closes = [point["close"] for point in item["history"] if isinstance(point.get("close"), (int, float))]
            if len(closes) >= 50 and closes[-1] > sum(closes[-50:]) / 50 and len(closes) >= 21 and closes[-1] > closes[-21]:
                strong += 1
        components.append(_component("leader_breadth", strong / len(leaders) * 100,
            "%d / %d 家龙头站上 50 日均线且 20 日动量为正" % (strong, len(leaders)),
            {"strong": strong, "total": len(leaders)}))
    else:
        components.append(_component("leader_breadth", detail="龙头股票历史样本不足"))

    if len(spy) >= 21:
        momentum = (spy[-1]["close"] / spy[-21]["close"] - 1) * 100
        components.append(_component("momentum", 50 + momentum * 5, "SPY 20 个交易日涨跌 %+.2f%%" % momentum,
                                     {"return_20d_pct": round(momentum, 2)}))
    else:
        components.append(_component("momentum", detail="SPY 完整日线不足 21 根"))

    if len(vix) >= 21:
        level = vix[-1]["close"]
        change = (level / vix[-21]["close"] - 1) * 100
        level_score = clamp((35 - level) / 23 * 100)
        change_score = clamp(50 - change * 2.5)
        components.append(_component("volatility", level_score * .65 + change_score * .35,
            "VIX %.2f，20 个交易日变化 %+.2f%%（VIX 越低，情绪分越高）" % (level, change),
            {"vix": round(level, 2), "change_20d_pct": round(change, 2)}))
    else:
        components.append(_component("volatility", detail="VIX 完整日线不足 21 根"))

    news = snapshot.get("news") or []
    if news:
        positive = negative = 0
        for item in news:
            text = " ".join(str(item.get(field) or "") for field in ("title", "summary", "title_zh", "summary_zh")).lower()
            positive += sum(word in text for word in POSITIVE_WORDS)
            negative += sum(word in text for word in NEGATIVE_WORDS)
        score = 50 if positive + negative == 0 else positive / (positive + negative) * 100
        components.append(_component("news", score, "当日已收录新闻命中积极词 %d 次、消极词 %d 次" % (positive, negative),
                                     {"positive_hits": positive, "negative_hits": negative, "articles": len(news)}))
    else:
        components.append(_component("news", detail="当日未收录可分析的新闻"))

    available = [item for item in components if item["available"]]
    covered_weight = sum(item["weight"] for item in available)
    score = (sum(item["score"] * item["weight"] for item in available) / covered_weight) if covered_weight else None
    score = round(score, 1) if score is not None else None
    strongest = sorted(available, key=lambda item: item["score"], reverse=True)
    explanation = "数据不足，暂时无法形成综合情绪判断。"
    if strongest:
        explanation = "%s提供较强支撑；%s构成主要压力。" % (strongest[0]["label"], strongest[-1]["label"])
    confidence = "高" if covered_weight >= 90 else "中" if covered_weight >= 70 else "低"
    return {"status": "ok" if covered_weight == 100 else "partial" if covered_weight else "error",
            "as_of": now.isoformat(timespec="seconds"), "market_date": snapshot.get("market_date"),
            "score": score, "label": mood_label(score), "coverage_weight": covered_weight,
            "confidence": confidence, "components": components, "explanation": explanation,
            "methodology": "六项分数按可用权重重新归一化；缺失项不按零分处理。",
            "note": "市场情绪是历史行情和已收录新闻的规则指标，不预测未来涨跌。"}


def collect_sentiment(snapshot, leader_result, now=None):
    now = eastern_now(now)
    histories, errors = {}, []
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = {executor.submit(_fetch_history, symbol, now): symbol for symbol in ("SPY", "^VIX")}
        for future in as_completed(pending):
            symbol = pending[future]
            try:
                histories[symbol] = future.result()
            except Exception as exc:
                errors.append("%s：%s" % (symbol, str(exc) if isinstance(exc, ProviderError) else "数据处理失败"))
    result = calculate_sentiment(snapshot, leader_result, histories, now)
    result["errors"] = errors
    result["source"] = {"name": "Yahoo Finance · 市场情绪", "url": "https://finance.yahoo.com/",
                        "status": "ok" if not errors else "partial" if histories else "error",
                        "detail": "SPY 与 VIX 复权日线；另结合板块、固定龙头池和当日已收录新闻"}
    return result


def demo_sentiment():
    components = [_component(key, score, detail) for key, score, detail in (
        ("trend", 72, "SPY 高于 20 日及 50 日均线（虚构）"),
        ("sector_breadth", 64, "7 / 11 个板块上涨（虚构）"),
        ("leader_breadth", 58, "64 / 110 家龙头趋势为正（虚构）"),
        ("momentum", 68, "SPY 20 日上涨 3.60%（虚构）"),
        ("volatility", 61, "VIX 18.40（虚构）"),
        ("news", 55, "积极词 6 次、消极词 5 次（虚构）"))]
    score = round(sum(item["score"] * item["weight"] for item in components) / 100, 1)
    return {"status": "ok", "as_of": "2026-09-18T18:00:00-04:00", "market_date": "2026-09-18",
            "score": score, "label": mood_label(score), "coverage_weight": 100, "confidence": "高",
            "components": components, "explanation": "大盘趋势提供较强支撑；新闻情绪相对中性。",
            "methodology": "六项分数按可用权重重新归一化；缺失项不按零分处理。", "errors": [],
            "source": {"name": "本地演示情绪数据", "url": None, "status": "ok", "detail": "全部数值均为虚构"},
            "note": "演示模式：全部情绪数据均为虚构。市场情绪指标不预测未来涨跌。"}
