import tempfile
import unittest
from datetime import datetime, timedelta

import sentiment
import server
from providers import EASTERN


NOW = datetime(2026, 9, 18, 18, tzinfo=EASTERN)


def history(start, step, count=220):
    return [{"date": (NOW.date() - timedelta(days=count - index)).isoformat(),
             "close": start + step * index} for index in range(count)]


def stock(symbol, rising=True):
    values = history(100, .2 if rising else -.1, 90)
    closes = [item["close"] for item in values]
    return {"symbol": symbol, "status": "not_matched", "history": values,
            "price": closes[-1], "sma50": sum(closes[-50:]) / 50}


def snapshot():
    return {"date": "2026-09-18", "generated_at": NOW.isoformat(), "market_date": "2026-09-18",
            "mode": "live", "status": "ok", "indices": [],
            "sectors": [{"symbol": "X%d" % index, "comparable": True,
                         "change_pct": 1 if index < 7 else -1} for index in range(11)],
            "news": [{"title": "Stocks rally on growth", "summary": "record high"},
                     {"title": "Company issues downgrade", "summary": "layoff"}],
            "recent_news": [], "news_coverage": {}, "alerts": [], "summary": [],
            "sources": [{"name": "test", "status": "ok"}], "errors": []}


class SentimentTests(unittest.TestCase):
    def test_all_six_components_and_weighted_score(self):
        leader = {"stocks": [stock("S%d" % index, index < 7) for index in range(10)]}
        result = sentiment.calculate_sentiment(snapshot(), leader,
                                               {"SPY": history(100, .25), "^VIX": history(24, -.03)}, NOW)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage_weight"], 100)
        self.assertEqual(len(result["components"]), 6)
        self.assertTrue(0 <= result["score"] <= 100)
        self.assertEqual(result["label"], sentiment.mood_label(result["score"]))

    def test_missing_inputs_are_not_scored_as_zero(self):
        report = snapshot()
        report["sectors"] = []
        report["news"] = []
        result = sentiment.calculate_sentiment(report, {"stocks": []}, {}, NOW)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["score"])
        self.assertEqual(result["coverage_weight"], 0)

    def test_partial_score_renormalizes_available_weight(self):
        report = snapshot()
        report["sectors"] = []
        report["news"] = []
        result = sentiment.calculate_sentiment(report, {"stocks": []},
                                               {"SPY": history(100, .1)}, NOW)
        self.assertEqual(result["coverage_weight"], 40)
        self.assertEqual(result["status"], "partial")
        scores = {item["key"]: item["score"] for item in result["components"]}
        expected = (scores["trend"] * 25 + scores["momentum"] * 15) / 40
        self.assertAlmostEqual(result["score"], expected, places=1)

    def test_label_boundaries(self):
        self.assertEqual([sentiment.mood_label(value) for value in (0, 20, 21, 40, 41, 59, 60, 79, 80, 100)],
                         ["极度恐慌", "极度恐慌", "偏谨慎", "偏谨慎", "中性", "中性", "偏乐观", "偏乐观", "极度乐观", "极度乐观"])

    def test_archive_history_only_contains_scored_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = server.Archive(directory + "/db.sqlite")
            for day, score in (("2026-09-16", 42), ("2026-09-17", None), ("2026-09-18", 64)):
                report = snapshot()
                report["date"] = day
                report["generated_at"] = day + "T18:00:00-04:00"
                report["sentiment"] = {"score": score, "label": sentiment.mood_label(score)}
                archive.save(report)
            self.assertEqual(archive.sentiment_history(), [
                {"date": "2026-09-16", "score": 42, "label": "中性"},
                {"date": "2026-09-18", "score": 64, "label": "偏乐观"}])


if __name__ == "__main__":
    unittest.main()
