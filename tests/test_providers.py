"""Behavioral tests for date boundaries, financial calculations and failures."""

import copy
import io
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import providers


NOW = datetime(2026, 9, 18, 18, 0, tzinfo=providers.EASTERN)


def chart(symbol="SPY", count=25, closes=None, volumes=None, last_day=None):
    last_day = last_day or NOW.date()
    days = []
    day = last_day
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    stamps = [int(datetime(day.year, day.month, day.day, 9, 30, tzinfo=providers.EASTERN).timestamp()) for day in days]
    closes = closes if closes is not None else [100.0] * (count - 1) + [103.0]
    volumes = volumes if volumes is not None else [1000] * (count - 1) + [2000]
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "USD", "chartPreviousClose": 50,
                 "regularMarketTime": int(datetime(last_day.year, last_day.month, last_day.day, 16, 0, tzinfo=providers.EASTERN).timestamp())},
        "timestamp": stamps, "indicators": {"quote": [{"close": closes, "volume": volumes}]},
    }]}}


def rss(items):
    return ("<rss version='2.0'><channel>" + "".join(items) + "</channel></rss>").encode()


def rss_item(title="Nvidia semiconductor earnings", date="Fri, 18 Sep 2026 16:00:00 -0400", link="https://example.com/news", summary="Revenue rose."):
    return "<item><title><![CDATA[%s]]></title><link><![CDATA[%s]]></link><pubDate>%s</pubDate><description><![CDATA[%s]]></description></item>" % (title, link, date, summary)


def complete_sources():
    return [{"name": "test", "status": "ok", "detail": "fixture", "url": "https://example.com"}]


class ChartTests(unittest.TestCase):
    def test_daily_change_uses_previous_bar_not_range_previous_close(self):
        quote = providers.parse_chart(chart(), "SPY", NOW)
        self.assertEqual(quote["price"], 103)
        self.assertAlmostEqual(quote["change_pct"], 3)
        self.assertEqual(quote["previous_close"], 100)
        self.assertEqual(quote["previous_date"], "2026-09-17")
        self.assertEqual(quote["trading_date"], "2026-09-18")

    def test_volume_excludes_current_day_and_requires_twenty_prior_days(self):
        quote = providers.parse_chart(chart(), "SPY", NOW)
        self.assertEqual(quote["volume_ratio"], 2)
        short = providers.parse_chart(chart(count=20), "SPY", NOW)
        self.assertIsNone(short["volume_ratio"])

    def test_intraday_volume_ratio_is_unavailable(self):
        early = NOW.replace(hour=12)
        quote = providers.parse_chart(chart(), "SPY", early)
        self.assertFalse(quote["session_complete"])
        self.assertIsNone(quote["volume_ratio"])
        self.assertEqual(quote["as_of_kind"], "daily_bar_timestamp")

    def test_waits_past_normal_close_without_assuming_early_close_calendar(self):
        quote = providers.parse_chart(chart(), "SPY", NOW.replace(hour=16, minute=14))
        self.assertIsNone(quote["volume_ratio"])
        quote = providers.parse_chart(chart(), "SPY", NOW.replace(hour=16, minute=15))
        self.assertEqual(quote["volume_ratio"], 2)

    def test_weekend_preserves_friday_date_and_complete_volume(self):
        sunday = NOW + timedelta(days=2)
        quote = providers.parse_chart(chart(), "SPY", sunday)
        self.assertEqual(quote["trading_date"], "2026-09-18")
        self.assertEqual(quote["as_of"], "2026-09-18T16:00:00-04:00")
        self.assertEqual(quote["volume_ratio"], 2)

    def test_no_multi_day_return_when_previous_close_missing(self):
        payload = chart(closes=[100.0] * 23 + [None, 103.0])
        quote = providers.parse_chart(payload, "SPY", NOW)
        self.assertIsNone(quote["change_pct"])
        self.assertIsNone(quote["previous_close"])

    def test_null_latest_row_keeps_actual_previous_quote_date(self):
        payload = chart(closes=[100.0] * 24 + [None])
        quote = providers.parse_chart(payload, "SPY", NOW)
        self.assertEqual(quote["trading_date"], "2026-09-17")
        self.assertEqual(quote["as_of_kind"], "daily_bar_timestamp")
        self.assertTrue(quote["session_complete"])

    def test_missing_volume_is_not_imputed(self):
        volumes = [1000] * 25
        volumes[-2] = None
        self.assertIsNone(providers.parse_chart(chart(volumes=volumes), "SPY", NOW)["volume_ratio"])

    def test_future_rows_are_excluded(self):
        payload = chart(count=2)
        quote = providers.parse_chart(payload, "SPY", NOW.replace(hour=8))
        self.assertEqual(quote["trading_date"], "2026-09-17")
        self.assertIsNone(quote["change_pct"])

    def test_rejects_invalid_prices_and_wrong_instruments(self):
        for value in (None, 0, -1, float("nan"), float("inf"), True, "123"):
            with self.subTest(value=value), self.assertRaises(providers.ProviderError):
                providers.parse_chart(chart(count=1, closes=[value]), "SPY", NOW)
        with self.assertRaises(providers.ProviderError):
            providers.parse_chart(chart(symbol="QQQ"), "SPY", NOW)
        payload = chart()
        payload["chart"]["result"][0]["meta"]["currency"] = "EUR"
        with self.assertRaises(providers.ProviderError):
            providers.parse_chart(payload, "SPY", NOW)

    def test_malformed_payloads_are_readable_errors(self):
        for payload in (b"not-json", {"chart": {"result": None}}, {"chart": {"error": {"description": "bad"}}}):
            with self.subTest(payload=payload), self.assertRaises(providers.ProviderError):
                providers.parse_chart(payload, "SPY", NOW)


class NewsTests(unittest.TestCase):
    def test_et_calendar_date_filters_utc_boundary_and_future(self):
        payload = rss([
            rss_item(date="Fri, 18 Sep 2026 02:00:00 +0000", link="https://example.com/yesterday"),
            rss_item(date="Fri, 18 Sep 2026 05:00:00 +0000", link="https://example.com/today"),
            rss_item(date="Fri, 18 Sep 2026 23:00:00 +0000", link="https://example.com/future"),
            rss_item(date="unknown", link="https://example.com/undated"),
            rss_item(date="Fri, 18 Sep 2026 12:00:00", link="https://example.com/no-timezone"),
        ])
        news = providers.parse_feed(payload, "CNBC", NOW)
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["url"], "https://example.com/today")
        self.assertEqual(news[0]["published_at"], "2026-09-18T01:00:00-04:00")

    def test_atom_publication_date_is_used_instead_of_update(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Stocks rally</title><link href="https://example.com/atom"/>
        <published>2026-09-17T12:00:00-04:00</published><updated>2026-09-18T12:00:00-04:00</updated>
        </entry></feed>'''
        self.assertEqual(providers.parse_feed(xml, "CNBC", NOW), [])
        xml = xml.replace(b"2026-09-17", b"2026-09-18")
        self.assertEqual(providers.parse_feed(xml, "CNBC", NOW)[0]["url"], "https://example.com/atom")

    def test_strip_markup_reject_unsafe_links(self):
        payload = rss([
            rss_item(title="<b>Stocks &amp; banks</b>", summary="<p>News.</p><script>bad()</script>", link="https://example.com/a"),
            rss_item(link="javascript:alert(1)"),
            rss_item(link="https://user:password@example.com/private"),
        ])
        news = providers.parse_feed(payload, "CNBC", NOW)
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["title"], "Stocks & banks")
        self.assertEqual(news[0]["summary"], "News.")

    def test_keyword_boundaries_do_not_match_ai_inside_chair(self):
        tags, category, importance, score = providers.classify_news("Chair discusses policy", "", "CNBC")
        self.assertNotIn("XLK", tags)
        tags, category, importance, score = providers.classify_news("Nvidia AI earnings rise", "", "CNBC")
        self.assertIn("XLK", tags)
        self.assertEqual(category, "财报业绩")
        self.assertEqual(importance, "medium")

    def test_deduplicates_tracking_urls_and_normalized_titles(self):
        payload = rss([
            rss_item(link="https://www.example.com/story?utm_source=rss"),
            rss_item(title="Different update", link="https://example.com/story#fragment"),
            rss_item(title="NVIDIA: semiconductor earnings!", link="https://example.com/other"),
        ])
        self.assertEqual(len(providers.deduplicate_news(providers.parse_feed(payload, "CNBC", NOW))), 1)

    def test_malformed_or_non_feed_responses_fail(self):
        for xml in (b"<html>Access denied</html>", b"garbage", b'<!DOCTYPE rss [<!ENTITY x "huge">]><rss/>'):
            with self.subTest(xml=xml), self.assertRaises(providers.ProviderError):
                providers.parse_feed(xml, "CNBC", NOW)

    def test_current_day_filter_respects_winter_timezone(self):
        winter = datetime(2026, 1, 7, 12, tzinfo=providers.EASTERN)
        payload = rss([rss_item(date="Wed, 07 Jan 2026 04:30:00 +0000")])
        self.assertEqual(providers.parse_feed(payload, "CNBC", winter), [])

    def test_rolling_72_hours_includes_boundary_but_not_older_or_future_items(self):
        payload = rss([
            rss_item(title="Today", date=NOW.isoformat(), link="https://example.com/today"),
            rss_item(title="Boundary", date=(NOW - timedelta(hours=72)).isoformat(), link="https://example.com/boundary"),
            rss_item(title="Expired", date=(NOW - timedelta(hours=72, seconds=1)).isoformat(), link="https://example.com/expired"),
            rss_item(title="Future", date=(NOW + timedelta(seconds=1)).isoformat(), link="https://example.com/future"),
        ])
        recent = providers.parse_feed(payload, "CNBC", NOW, lookback_hours=72)
        self.assertEqual({item["title"] for item in recent}, {"Today", "Boundary"})
        self.assertEqual([item["title"] for item in providers.parse_feed(payload, "CNBC", NOW)], ["Today"])

    def test_rolling_hours_measure_elapsed_time_across_both_dst_transitions(self):
        for report_time, boundary, expired in (
                ("2026-11-02T12:00:00-05:00", "2026-10-30T13:00:00-04:00", "2026-10-30T12:30:00-04:00"),
                ("2026-03-09T12:00:00-04:00", "2026-03-06T11:00:00-05:00", "2026-03-06T10:59:59-05:00")):
            with self.subTest(report_time=report_time):
                clock = datetime.fromisoformat(report_time)
                payload = rss([rss_item(title="Boundary", date=boundary, link="https://example.com/boundary"),
                               rss_item(title="Expired", date=expired, link="https://example.com/expired")])
                news = providers.parse_feed(payload, "CNBC", clock, lookback_hours=72)
                self.assertEqual([item["title"] for item in news], ["Boundary"])

    def test_rolling_window_rejects_invalid_durations(self):
        for duration in (0, -1, True, "72", float("nan"), float("inf")):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                providers.parse_feed(rss([]), "CNBC", NOW, lookback_hours=duration)


class ReportTests(unittest.TestCase):
    def test_no_relative_heat_or_alerts_for_wrong_trading_date(self):
        quotes = {"SPY": providers.parse_chart(chart(), "SPY", NOW),
                  "XLK": providers.parse_chart(chart("XLK", last_day=NOW.date() - timedelta(days=1)), "XLK", NOW)}
        report = providers.derive_snapshot(quotes, [], complete_sources(), now=NOW)
        xlk = next(item for item in report["sectors"] if item["symbol"] == "XLK")
        self.assertIsNone(xlk["relative_pct"])
        self.assertIsNone(xlk["heat_score"])
        self.assertFalse(xlk["comparable"])
        self.assertFalse(any(item["symbol"] == "XLK" for item in report["alerts"]))
        self.assertTrue(report["errors"])

    def test_relative_requires_matching_previous_date_too(self):
        spy = providers.parse_chart(chart(), "SPY", NOW)
        xlk = providers.parse_chart(chart("XLK"), "XLK", NOW)
        xlk["previous_date"] = "2026-09-16"
        report = providers.derive_snapshot({"SPY": spy, "XLK": xlk}, [], complete_sources(), now=NOW)
        xlk = next(item for item in report["sectors"] if item["symbol"] == "XLK")
        self.assertIsNone(xlk["relative_pct"])
        self.assertFalse(xlk["heat_complete"])

    def test_missing_quotes_remain_null_and_no_market_date_is_invented(self):
        report = providers.derive_snapshot({}, [], [{"name": "failed", "status": "error"}], ["failure"], NOW)
        self.assertEqual(report["status"], "error")
        self.assertIsNone(report["market_date"])
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["alerts"], [])
        self.assertTrue(all(item["price"] is None and item["change_pct"] is None for item in report["indices"]))
        self.assertTrue(all(item["heat_score"] is None for item in report["sectors"]))

    def test_heat_formula_is_bounded_and_measures_attention_in_both_directions(self):
        self.assertEqual(providers.heat_score(10, 10, 5, 50), 100)
        self.assertEqual(providers.heat_score(-10, -10, 5, 50), 100)
        self.assertEqual(providers.heat_score(1, 0.5, 1.5, 2), 31)
        self.assertEqual(providers.heat_score(1, None, None, 0), 12)
        self.assertIsNone(providers.heat_score(None, 1, 1, 10))

    def test_successful_empty_feeds_are_distinct_from_total_failure(self):
        with patch.object(providers, "fetch_bytes", return_value=b"<rss><channel/></rss>"), patch.object(providers, "_collect_quote", side_effect=providers.ProviderError("HTTP 429")):
            report = providers.collect_snapshot(NOW)
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["news"], [])
        self.assertEqual(report["sources"][1]["status"], "ok")
        self.assertIsNone(report["indices"][0]["price"])

    def test_one_failed_source_does_not_discard_other_data_or_trigger_demo(self):
        def fetch_quote(symbol, now):
            if symbol == "QQQ":
                raise providers.ProviderError("HTTP 429")
            return providers.parse_chart(chart(symbol), symbol, now)

        with patch.object(providers, "_collect_quote", side_effect=fetch_quote), patch.object(providers, "fetch_bytes", return_value=rss([rss_item()])), patch.object(providers, "demo_snapshot", side_effect=AssertionError("must not be called")):
            report = providers.collect_snapshot(NOW)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["mode"], "live")
        self.assertEqual(len(report["news"]), 1)
        self.assertIsNone(next(item for item in report["indices"] if item["symbol"] == "QQQ")["price"])
        self.assertEqual(report["indices"][0]["price"], 103)
        self.assertTrue(any("QQQ" in error and "429" in error for error in report["errors"]))

    def test_demo_is_explicit_fixed_and_json_serializable_without_nan(self):
        one, two = providers.demo_snapshot(), providers.demo_snapshot()
        self.assertEqual(one, two)
        self.assertEqual(one["mode"], "demo")
        self.assertEqual(one["date"], "2026-09-18")
        self.assertTrue(all("虚构" in item["title"] for item in one["news"]))
        self.assertEqual(len(one["indices"]), 4)
        self.assertEqual(len(one["sectors"]), 11)
        self.assertTrue(one["alerts"])
        self.assertEqual(one["news_coverage"], {"today_count": 8, "hours24_count": 9, "hours72_count": 10, "retention_hours": 72})
        self.assertTrue(all("虚构" in item["title"] for item in one["recent_news"]))
        json.dumps(one, allow_nan=False)

    def test_recent_coverage_is_deduplicated_and_does_not_change_daily_heat(self):
        payload = rss([
            rss_item(title="Nvidia item %d" % index, date=(NOW - timedelta(hours=age)).isoformat(),
                     link="https://example.com/item-%d" % index)
            for index, age in enumerate((1, 20, 24, 48, 72, 72 + 1 / 3600, -1))
        ])
        recent = providers.parse_feed(payload, "CNBC", NOW, lookback_hours=100)
        recent.append(copy.deepcopy(recent[0]))
        quotes = {symbol: providers.parse_chart(chart(symbol), symbol, NOW) for symbol in ("SPY", "XLK")}
        report = providers.derive_snapshot(quotes, recent, complete_sources(), now=NOW)
        daily = providers.derive_snapshot(quotes, recent[:1], complete_sources(), now=NOW)
        self.assertEqual(report["news_coverage"], {"today_count": 1, "hours24_count": 3, "hours72_count": 5, "retention_hours": 72})
        self.assertEqual(report["news"], daily["news"])
        self.assertEqual(report["sectors"], daily["sectors"])
        self.assertEqual(report["summary"], daily["summary"])

    def test_collection_reports_each_source_today_and_recent_counts(self):
        payload = rss([
            rss_item(title="Nvidia today", date=(NOW - timedelta(hours=1)).isoformat(), link="https://example.com/today"),
            rss_item(title="Nvidia yesterday", date=(NOW - timedelta(hours=23)).isoformat(), link="https://example.com/yesterday"),
        ])
        with patch.object(providers, "_collect_quote", side_effect=providers.ProviderError("HTTP 429")), \
                patch.object(providers, "fetch_bytes", return_value=payload) as fetch:
            report = providers.collect_snapshot(NOW)
        self.assertEqual(fetch.call_count, 6)
        self.assertEqual(report["news_coverage"], {"today_count": 1, "hours24_count": 2, "hours72_count": 2, "retention_hours": 72})
        self.assertTrue(all("今日 1 条 / 最近72小时 2 条" in source["detail"] for source in report["sources"][1:]))
        self.assertEqual({source["name"] for source in report["sources"][1:]},
                         {"CNBC · 头条", "CNBC · 市场", "CNBC · 科技", "CNBC · 经济", "CNBC · 美国", "Federal Reserve"})


class DownloadTests(unittest.TestCase):
    def test_rejects_excess_content_length_before_read(self):
        response = io.BytesIO(b"never read")
        response.headers = {"Content-Length": str(providers.MAX_RESPONSE_BYTES + 1)}
        with patch.object(providers.urllib.request, "urlopen", return_value=response), self.assertRaises(providers.ProviderError):
            providers.fetch_bytes("https://example.com")

    def test_caps_unknown_content_length(self):
        response = io.BytesIO(b"x" * (providers.MAX_RESPONSE_BYTES + 1))
        response.headers = {}
        with patch.object(providers.urllib.request, "urlopen", return_value=response), self.assertRaises(providers.ProviderError):
            providers.fetch_bytes("https://example.com")

    def test_timeout_is_passed_to_transport_and_bytes_are_preserved(self):
        response = io.BytesIO(b"hello")
        response.headers = {}
        with patch.object(providers.urllib.request, "urlopen", return_value=response) as opening:
            self.assertEqual(providers.fetch_bytes("https://example.com"), b"hello")
        self.assertEqual(opening.call_args.kwargs["timeout"], providers.REQUEST_TIMEOUT)
        context = opening.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, providers.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class TLSConfigurationTests(unittest.TestCase):
    def make_context(self, platform="darwin", cafile=None, capath=None, environment=None, system_bundle=True):
        paths = SimpleNamespace(cafile=cafile, capath=capath)
        with patch.object(providers.sys, "platform", platform), \
                patch.object(providers.ssl, "get_default_verify_paths", return_value=paths), \
                patch.object(providers.ssl, "create_default_context") as create, \
                patch.object(providers.os.path, "isfile", return_value=system_bundle), \
                patch.dict(providers.os.environ, environment or {}, clear=True):
            providers._tls_context()
        return create

    def test_macos_missing_ca_uses_verified_system_bundle(self):
        self.make_context().assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_custom_ca_file_and_directory_are_never_overridden(self):
        for variable in ("SSL_CERT_FILE", "SSL_CERT_DIR"):
            with self.subTest(variable=variable):
                self.make_context(environment={variable: "/user/custom/ca"}).assert_called_once_with()

    def test_normal_default_ca_locations_take_precedence(self):
        self.make_context(cafile="/normal/cert.pem").assert_called_once_with()
        self.make_context(capath="/normal/certificates").assert_called_once_with()

    def test_other_platforms_and_missing_system_bundle_keep_defaults(self):
        self.make_context(platform="linux").assert_called_once_with()
        self.make_context(system_bundle=False).assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
