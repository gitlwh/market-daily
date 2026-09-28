import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import leader_screen
import server
from providers import EASTERN, ProviderError


NOW = datetime(2026, 9, 18, 18, 0, tzinfo=EASTERN)


def bars(values, start=datetime(2025, 1, 2, tzinfo=EASTERN)):
    return [{"date": (start + timedelta(days=index)).date().isoformat(), "close": value}
            for index, value in enumerate(values)]


def yahoo_payload(values, symbol="AAPL", end=NOW - timedelta(days=1)):
    days = [end - timedelta(days=len(values) - 1 - index) for index in range(len(values))]
    return json.dumps({"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "USD"},
        "timestamp": [int(day.timestamp()) for day in days],
        "indicators": {"adjclose": [{"adjclose": values}]},
    }]}}).encode()


class LeaderScreenUnitTests(unittest.TestCase):
    def test_universe_has_eleven_sectors_and_ten_unique_stocks_each(self):
        self.assertEqual(len(leader_screen.SECTOR_LEADERS), 11)
        symbols = []
        for entries in leader_screen.SECTOR_LEADERS.values():
            self.assertEqual(len(entries), 10)
            symbols.extend(symbol for symbol, _ in entries)
        self.assertEqual(len(symbols), 110)
        self.assertEqual(len(set(symbols)), 110)

    def test_candidate_requires_gain_pullback_recent_high_and_uptrend(self):
        # Rising long-term series, sharp 60-session advance, then a 7% pullback.
        values = [50 + index * .12 for index in range(140)]
        values += [67 + index * .75 for index in range(51)]
        values += [105 + index * 1.5 for index in range(5)]
        values += [112, 110, 108, 106, 104]
        result = leader_screen.screen_stock("TEST", "测试", "XLK", bars(values))
        self.assertEqual(result["status"], "candidate")
        self.assertTrue(all(result["checks"].values()))
        self.assertGreaterEqual(result["gain_pct"], 20)
        self.assertGreaterEqual(-result["pullback_pct"], 5)
        self.assertLessEqual(-result["pullback_pct"], 12)

    def test_one_failed_rule_is_not_candidate(self):
        values = [100 + index * .01 for index in range(205)]
        result = leader_screen.screen_stock("TEST", "测试", "XLK", bars(values))
        self.assertEqual(result["status"], "not_matched")
        self.assertFalse(result["checks"]["prior_gain"])

    def test_insufficient_history_is_explicit(self):
        result = leader_screen.screen_stock("TEST", "测试", "XLK", bars([100] * 199))
        self.assertEqual(result["status"], "insufficient")
        self.assertIn("200", result["reason"])

    def test_parser_uses_adjusted_close_and_omits_incomplete_today(self):
        payload = yahoo_payload([90, 91], end=NOW.replace(hour=14))
        parsed = leader_screen.parse_history(payload, "AAPL", NOW.replace(hour=14))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["close"], 90)

    def test_parser_rejects_wrong_symbol_currency_and_malformed_values(self):
        wrong = json.loads(yahoo_payload([100]).decode())
        wrong["chart"]["result"][0]["meta"]["symbol"] = "MSFT"
        with self.assertRaises(ProviderError):
            leader_screen.parse_history(wrong, "AAPL", NOW)
        wrong["chart"]["result"][0]["meta"].update(symbol="AAPL", currency="EUR")
        with self.assertRaises(ProviderError):
            leader_screen.parse_history(wrong, "AAPL", NOW)

    def test_demo_is_independent_and_has_three_candidates(self):
        one, two = leader_screen.demo_leader_screen(), leader_screen.demo_leader_screen()
        one["stocks"][0]["name"] = "changed"
        self.assertNotEqual(one["stocks"][0]["name"], two["stocks"][0]["name"])
        self.assertEqual(two["universe_size"], 110)
        self.assertEqual(two["candidate_count"], 3)


class LeaderCollectorTests(unittest.TestCase):
    def test_collector_preserves_partial_results(self):
        values = [80 + index * .2 for index in range(220)]
        def fake_fetch(url):
            symbol = url.split("/chart/")[1].split("?")[0]
            symbol = symbol.replace("%2D", "-")
            if symbol == "AAPL":
                raise ProviderError("offline")
            return yahoo_payload(values, symbol)
        with patch.object(leader_screen, "fetch_bytes", side_effect=fake_fetch):
            result = leader_screen.collect_leader_screen(NOW)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["available_count"], 109)
        failed = next(item for item in result["stocks"] if item["symbol"] == "AAPL")
        self.assertEqual(failed["status"], "insufficient")
        self.assertTrue(result["errors"])


class LeaderServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def base_snapshot(self):
        return {"date": "2026-09-18", "generated_at": NOW.isoformat(), "market_date": "2026-09-18",
                "mode": "live", "status": "ok", "indices": [], "sectors": [], "news": [],
                "recent_news": [], "news_coverage": {}, "alerts": [], "summary": [],
                "sources": [{"name": "test", "status": "ok"}], "errors": []}

    def test_screen_is_archived_with_report(self):
        expected = leader_screen.demo_leader_screen()
        app = server.Application(self.temp.name + "/db.sqlite", schedule=False,
                                 collector=self.base_snapshot, leader_collector=lambda now: expected)
        app._collect()
        saved = app.archive.get()
        self.assertEqual(saved["leader_screen"]["universe_size"], 110)
        self.assertIn(expected["source"], saved["sources"])

    def test_screen_failure_does_not_discard_market_report(self):
        app = server.Application(self.temp.name + "/db.sqlite", schedule=False,
                                 collector=self.base_snapshot,
                                 leader_collector=lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
        app._collect()
        saved = app.archive.get()
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["leader_screen"]["status"], "error")

    def test_demo_dashboard_contains_isolated_screen(self):
        app = server.Application(self.temp.name + "/db.sqlite", schedule=False)
        one, two = app.dashboard(demo=True), app.dashboard(demo=True)
        one["snapshot"]["leader_screen"]["stocks"][0]["name"] = "changed"
        self.assertNotEqual(one["snapshot"]["leader_screen"]["stocks"][0]["name"],
                            two["snapshot"]["leader_screen"]["stocks"][0]["name"])


if __name__ == "__main__":
    unittest.main()
