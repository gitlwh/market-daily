"""Offline earnings-calendar boundary, provenance and integration checks."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earnings
import server

ET = ZoneInfo("America/New_York")


def nasdaq_payload(rows):
    return {"data": {"rows": rows}, "status": {"rCode": 200}}


def calendar_row(symbol="EXAMPLE", cap="$12,000,000,000", eps="$1.25", time="time-after-hours"):
    return {"symbol": symbol, "name": "Example Company", "marketCap": cap,
            "epsForecast": eps, "noOfEsts": "12", "time": time}


class CalendarParsingTests(unittest.TestCase):
    def test_numeric_parser_keeps_zero_losses_and_large_cap_units(self):
        cases = [(0, 0), ("$0.00", 0), ("($0.42)", -.42), ("-$0.15", -.15),
                 ("$12,345,678,900", 12345678900), ("10B", 10000000000),
                 ("1.5T", 1500000000000), ("250M", 250000000), ("5K", 5000)]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(earnings.parse_number(raw), expected)

    def test_missing_nonfinite_and_foreign_currency_values_are_not_usd_numbers(self):
        for raw in (None, "", "N/A", "--", "—", float("nan"), float("inf"),
                    "NaN", "Infinity", "€15B", "C$15B", "GBP20B", True):
            with self.subTest(raw=raw):
                self.assertIsNone(earnings.parse_number(raw))

    def test_zero_and_negative_eps_survive_and_analyst_counts_remain_eps_counts(self):
        rows = [calendar_row("ZERO", eps="$0.00"), calendar_row("LOSS", eps="($0.42)")]
        companies = earnings.parse_calendar(nasdaq_payload(rows), "2026-09-23")
        self.assertEqual([item["eps_estimate"] for item in companies], [0, -.42])
        for company in companies:
            self.assertEqual(company["eps_currency"], "USD")
            self.assertEqual(company["analyst_count"], 12)
            self.assertEqual(company["report_date"], "2026-09-23")
            self.assertEqual(company["report_time"], "after_close")
            self.assertIn("2026-09-23", company["source_url"])

    def test_missing_eps_and_counts_are_unknown_instead_of_zero(self):
        row = calendar_row(eps="N/A")
        row["noOfEsts"] = "N/A"
        company = earnings.parse_calendar(nasdaq_payload([row]), "2026-09-23")[0]
        self.assertIsNone(company["eps_estimate"])
        self.assertIsNone(company["analyst_count"])

    def test_report_timing_keeps_unspecified_time_unknown(self):
        cases = [("time-pre-market", "before_open"), ("time-after-hours", "after_close"),
                 ("time-not-supplied", "unknown"), (None, "unknown")]
        for raw, expected in cases:
            with self.subTest(time=raw):
                company = earnings.parse_calendar(nasdaq_payload([calendar_row(time=raw)]), "2026-09-23")[0]
                self.assertEqual(company["report_time"], expected)

    def test_explicit_empty_successful_payload_is_not_a_source_failure(self):
        for rows in ([], None):
            with self.subTest(rows=rows):
                self.assertEqual(earnings.parse_calendar(nasdaq_payload(rows), "2026-09-23"), [])

    def test_missing_rows_and_error_envelopes_cannot_become_successful_empty_days(self):
        payloads = [{}, {"data": None}, {"data": {}}, nasdaq_payload("not a row array"),
                    {"data": {"rows": []}, "status": {"rCode": 403}}]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(earnings.ProviderError):
                earnings.parse_calendar(payload, "2026-09-23")

    def test_ranking_uses_usd_ten_billion_inclusive_and_limits_to_fifteen(self):
        companies = [{"symbol": "LARGE%02d" % index, "market_cap": 10000000000 + index * 1000000000}
                     for index in range(20)]
        companies.extend([{"symbol": "SMALL", "market_cap": 9999999999},
                          {"symbol": "MISSING", "market_cap": None}])
        ranked = earnings.rank_companies(companies)
        self.assertEqual(len(ranked), 15)
        self.assertEqual([item["symbol"] for item in ranked], ["LARGE%02d" % index for index in range(19, 4, -1)])
        boundary = earnings.rank_companies(companies, limit=100)
        self.assertEqual(len(boundary), 20)
        self.assertEqual(boundary[-1]["market_cap"], 10000000000)


class EarningsWeekTests(unittest.TestCase):
    def test_week_is_monday_through_sunday_in_eastern_time(self):
        cases = [
            (datetime(2026, 9, 28, 1, tzinfo=timezone.utc), "2026-09-21", "2026-09-27"),
            (datetime(2026, 9, 28, 5, tzinfo=timezone.utc), "2026-09-28", "2026-10-04"),
            (datetime(2027, 1, 1, 12, tzinfo=ET), "2026-12-28", "2027-01-03"),
            (datetime(2026, 3, 8, 18, tzinfo=ET), "2026-03-02", "2026-03-08"),
            (datetime(2026, 11, 1, 18, tzinfo=ET), "2026-10-26", "2026-11-01"),
        ]
        for now, monday, sunday in cases:
            with self.subTest(now=now):
                start, end = earnings.week_bounds(now)
                self.assertEqual(start, date.fromisoformat(monday))
                self.assertEqual(end, date.fromisoformat(sunday))
                self.assertEqual(start.weekday(), 0)
                self.assertEqual(end.weekday(), 6)
                self.assertEqual(end - start, timedelta(days=6))


class EarningsCollectionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 27, 18, tzinfo=ET)

    def collect(self, fetcher):
        with mock.patch.object(earnings, "fetch_bytes", side_effect=fetcher):
            return earnings.collect_earnings(now=self.now)

    def test_successful_empty_calendar_has_explicit_coverage_and_zero_counts(self):
        dates = []

        def fetch(url):
            day = parse_qs(urlparse(url).query)["date"][0]
            dates.append(day)
            return json.dumps(nasdaq_payload([])).encode()

        result = self.collect(fetch)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["week_start"], "2026-09-21")
        self.assertEqual(result["week_end"], "2026-09-27")
        self.assertEqual(result["total_scheduled"], 0)
        self.assertEqual(result["total_large"], 0)
        self.assertEqual(result["companies"], [])
        self.assertTrue(result["coverage_dates"])
        self.assertEqual(sorted(result["coverage_dates"]), sorted(set(dates)))
        self.assertTrue(all("2026-09-21" <= day <= "2026-09-27" for day in dates))
        self.assertEqual(result["errors"], [])

    def test_failed_day_is_excluded_from_coverage_and_keeps_partial_valid_results(self):
        failed_day = "2026-09-24"

        def fetch(url):
            day = parse_qs(urlparse(url).query)["date"][0]
            if day == failed_day:
                raise earnings.ProviderError("Calendar source unavailable")
            rows = [calendar_row("LARGE"), calendar_row("SMALL", cap="$900,000,000")] if day == "2026-09-23" else []
            return json.dumps(nasdaq_payload(rows)).encode()

        result = self.collect(fetch)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn(failed_day, result["coverage_dates"])
        self.assertIn("2026-09-23", result["coverage_dates"])
        self.assertEqual(result["total_scheduled"], 2)
        self.assertEqual(result["total_large"], 1)
        self.assertEqual([item["symbol"] for item in result["companies"]], ["LARGE"])
        self.assertTrue(result["errors"])
        self.assertTrue(any(failed_day in error for error in result["errors"]))

    def test_every_calendar_day_failing_is_error_with_no_fabricated_coverage(self):
        result = self.collect(lambda url: (_ for _ in ()).throw(earnings.ProviderError("Unavailable")))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["coverage_dates"], [])
        self.assertEqual(result["companies"], [])
        self.assertTrue(result["errors"])
        self.assertTrue(any(source["status"] == "error" for source in result["sources"]))

    def test_missing_payloads_are_not_reported_as_no_companies_this_week(self):
        result = self.collect(lambda url: b'{"data": null, "status": {"rCode": 200}}')
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["coverage_dates"], [])
        self.assertTrue(result["errors"])

    def test_missing_analyst_targets_do_not_reject_calendar_or_infer_price_move_from_eps(self):
        def fetch(url):
            day = parse_qs(urlparse(url).query)["date"][0]
            rows = [calendar_row(eps="$5.75")] if day == "2026-09-23" else []
            return json.dumps(nasdaq_payload(rows)).encode()

        result = self.collect(fetch)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["companies"]), 1)
        company = result["companies"][0]
        self.assertEqual(company["eps_estimate"], 5.75)
        self.assertEqual(company["analyst_count"], 12)
        analyst = company["analyst"]
        self.assertEqual(analyst["status"], "unavailable")
        for field in ("target_mean", "current_price", "upside_pct", "count", "horizon", "as_of"):
            self.assertIsNone(analyst[field], "EPS coverage must not substitute for price-target evidence")

    def test_demo_is_offline_fictional_and_returns_independent_copies(self):
        with mock.patch.object(earnings, "fetch_bytes", side_effect=AssertionError("Demo contacted network")):
            one = earnings.demo_earnings()
            two = earnings.demo_earnings()
        self.assertTrue(one["companies"])
        for company in one["companies"]:
            label = str(company.get("name", "")) + str(company.get("name_zh", ""))
            self.assertTrue(any(word in label for word in ("虚构", "演示", "示例")))
        one["companies"][0]["name"] = "Changed demo"
        self.assertNotEqual(one["companies"][0]["name"], two["companies"][0]["name"])


def market_snapshot():
    return {
        "date": "2026-09-27", "generated_at": "2026-09-27T18:00:00-04:00",
        "market_date": "2026-09-25", "mode": "live", "status": "ok",
        "indices": [{"symbol": "SPY", "price": 600, "change_pct": .5}],
        "sectors": [], "alerts": [], "summary": ["市场测试摘要"],
        "news": [{"title": "A saved market headline", "summary": "A feed snippet",
                  "url": "https://example.com/news", "source": "Test feed",
                  "published_at": "2026-09-27T13:00:00-04:00"}],
        "sources": [{"name": "Test market source", "status": "ok", "detail": "Quotes available"}],
        "errors": [],
    }


def calendar_snapshot(status="ok"):
    return {
        "status": status, "week_start": "2026-09-21", "week_end": "2026-09-27",
        "as_of": "2026-09-27T18:00:00-04:00", "timezone": "America/New_York",
        "threshold_usd": 10000000000, "limit": 15, "total_scheduled": 1, "total_large": 1,
        "companies": [{"symbol": "EXAMPLE", "name": "Example Company", "name_zh": None,
                       "report_date": "2026-09-23", "report_time": "after_close",
                       "market_cap": 12000000000, "eps_estimate": 0, "eps_currency": "USD",
                       "analyst_count": 12, "source_url": "https://www.nasdaq.com/market-activity/earnings",
                       "analyst": {"status": "unavailable", "target_mean": None,
                                   "current_price": None, "upside_pct": None, "count": None}}],
        "coverage_dates": ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25"],
        "sources": [{"name": "Test earnings calendar", "url": "https://example.com/calendar",
                     "status": status, "detail": "Fixture calendar coverage"}],
        "errors": [] if status == "ok" else ["One calendar day unavailable"],
    }


class EarningsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "reports.db"
        self.now = datetime(2026, 9, 27, 18, tzinfo=ET)

    def app(self, collector=None):
        return server.Application(self.path, schedule=False, collector=market_snapshot,
                                  earnings_collector=collector)

    def collect(self, app):
        with mock.patch.object(server, "market_now", return_value=self.now):
            app._collect()
        return app.archive.get()

    def test_calendar_is_attached_and_persisted_without_changing_market_and_news(self):
        calendar = calendar_snapshot()
        collector = mock.Mock(return_value=calendar)
        app = self.app(collector)
        saved = self.collect(app)
        collector.assert_called_once_with(now=self.now)
        self.assertEqual(saved["earnings"], calendar)
        self.assertEqual(saved["news"], market_snapshot()["news"])
        self.assertEqual(saved["indices"], market_snapshot()["indices"])
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(server.Archive(self.path).get()["earnings"], calendar)
        self.assertIn(calendar["sources"][0], saved["sources"])

    def test_partial_calendar_preserves_valid_quotes_and_news(self):
        app = self.app(mock.Mock(return_value=calendar_snapshot("partial")))
        saved = self.collect(app)
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(saved["news"], market_snapshot()["news"])
        self.assertEqual(saved["indices"], market_snapshot()["indices"])
        self.assertEqual(saved["earnings"]["status"], "partial")
        self.assertTrue(saved["errors"])
        self.assertEqual(app.state()["last_status"], "partial")

    def test_partial_calendar_can_be_saved_when_other_market_sources_fail(self):
        failed_market = market_snapshot()
        failed_market.update(status="error", indices=[], news=[], market_date=None,
                             errors=["Quotes and news unavailable"])
        calendar = calendar_snapshot("partial")
        app = server.Application(self.path, schedule=False,
                                 collector=lambda: copy.deepcopy(failed_market),
                                 earnings_collector=mock.Mock(return_value=calendar))
        saved = self.collect(app)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["status"], "partial")
        self.assertEqual(saved["earnings"], calendar)
        self.assertEqual(saved["indices"], [])
        self.assertIn("Quotes and news unavailable", saved["errors"])

    def test_malformed_calendar_payload_preserves_market_success_as_partial(self):
        for payload in (None, [], {"status": "bogus"}):
            with self.subTest(payload=payload):
                app = self.app(mock.Mock(return_value=payload))
                with self.assertLogs(server.LOG, level="ERROR"):
                    saved = self.collect(app)
                self.assertEqual(saved["status"], "partial")
                self.assertEqual(saved["earnings"]["status"], "error")
                self.assertEqual(saved["news"], market_snapshot()["news"])

    def test_calendar_exception_cannot_discard_successful_market_collection(self):
        collector = mock.Mock(side_effect=RuntimeError("Calendar transport failed"))
        app = self.app(collector)
        with self.assertLogs(server.LOG, level="ERROR"):
            saved = self.collect(app)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["news"], market_snapshot()["news"])
        self.assertEqual(saved["indices"], market_snapshot()["indices"])
        self.assertEqual(saved["status"], "partial")
        calendar = saved["earnings"]
        self.assertEqual(calendar["status"], "error")
        self.assertEqual(calendar["week_start"], "2026-09-21")
        self.assertEqual(calendar["week_end"], "2026-09-27")
        self.assertEqual(calendar["coverage_dates"], [])
        self.assertTrue(calendar["errors"])

    def test_optional_calendar_dependency_does_not_change_legacy_collection(self):
        saved = self.collect(self.app())
        self.assertEqual(saved, market_snapshot())
        self.assertNotIn("earnings", saved)

    def test_old_archive_has_no_invented_current_calendar(self):
        collector = mock.Mock(side_effect=AssertionError("Viewing an archive cannot recollect earnings"))
        app = self.app(collector)
        old = market_snapshot()
        app.archive.save(old)
        with mock.patch.object(server, "market_now", return_value=datetime(2026, 10, 15, 18, tzinfo=ET)):
            saved = app.dashboard(old["date"])["snapshot"]
        self.assertNotIn("earnings", saved)
        collector.assert_not_called()

    def test_archived_week_is_preserved_when_viewed_in_a_later_week(self):
        collector = mock.Mock(return_value=calendar_snapshot())
        app = self.app(collector)
        self.collect(app)
        with mock.patch.object(server, "market_now", return_value=datetime(2026, 10, 15, 18, tzinfo=ET)):
            viewed = app.dashboard("2026-09-27")["snapshot"]
        self.assertEqual(viewed["earnings"]["week_start"], "2026-09-21")
        self.assertEqual(viewed["earnings"]["week_end"], "2026-09-27")
        self.assertEqual(collector.call_count, 1)

    def test_demo_calendar_never_calls_live_collector_or_changes_archive(self):
        collector = mock.Mock(side_effect=AssertionError("Demo must remain offline"))
        app = self.app(collector)
        old = market_snapshot()
        app.archive.save(old)
        with mock.patch.object(earnings, "collect_earnings", side_effect=AssertionError("Demo must remain offline")):
            result = app.dashboard(demo=True)
        self.assertEqual(result["snapshot"]["mode"], "demo")
        self.assertIn("earnings", result["snapshot"])
        self.assertTrue(result["snapshot"]["earnings"]["companies"])
        self.assertEqual(app.archive.get(), old)
        collector.assert_not_called()

    def test_markdown_export_preserves_week_and_zero_eps_without_inventing_prediction(self):
        snapshot = market_snapshot()
        snapshot["earnings"] = calendar_snapshot()
        exported = server.markdown_report(snapshot)
        self.assertIn("2026-09-21", exported)
        self.assertIn("2026-09-27", exported)
        self.assertIn("EXAMPLE", exported)
        self.assertIn("| 0 |", exported)
        self.assertIn("目标价空间不是财报当天涨跌预测", exported)
        self.assertNotIn("None", exported)


if __name__ == "__main__":
    unittest.main()
