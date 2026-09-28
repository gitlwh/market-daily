"""Offline checks for honest, cached Chinese translations of public feed text."""
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import translations


def official_response(text="中文译文", **extra):
    response = {"responseStatus": 200, "responseData": {"translatedText": text},
                "responseDetails": "", "quotaFinished": False}
    response.update(extra)
    return json.dumps(response).encode("utf-8")


def article(identifier="one", title="Stocks rally after earnings", summary="Technology shares lead the market"):
    return {"id": identifier, "title": title, "summary": summary,
            "url": "https://example.com/news", "source": "Test feed",
            "published_at": "2026-09-18T15:00:00-04:00", "sectors": ["XLK"]}


def report(news=None, recent=None, mode="live"):
    return {"mode": mode, "status": "ok", "date": "2026-09-18",
            "news": [article()] if news is None else news,
            "recent_news": [] if recent is None else recent, "errors": []}


def query_text(url):
    return parse_qs(urlparse(url).query)["q"][0]


class TranslationParsingTests(unittest.TestCase):
    def test_success_decodes_official_response_and_html_entities(self):
        self.assertEqual(translations.parse_translation(
            official_response("人工智能 &amp; 半导体"), "AI and semiconductors"), "人工智能 & 半导体")

    def test_provider_error_and_quota_messages_are_never_accepted_as_translations(self):
        failures = [
            official_response("MYMEMORY WARNING: quota exhausted", quotaFinished=True),
            official_response("今日额度已经用尽", quotaFinished=True),
            official_response("今日额度已经用尽", responseStatus=429),
            official_response("服务出错", responseStatus="403"),
            official_response("QUERY LENGTH LIMIT EXCEEDED. MAX ALLOWED QUERY: 500 BYTES"),
            official_response("Stocks rally after earnings"),
            official_response(""),
            official_response(None),
            b"not JSON",
            b"{}",
            b"[]",
        ]
        for payload in failures:
            with self.subTest(payload=payload[:80]), self.assertRaises(translations.ProviderError):
                translations.parse_translation(payload, "Stocks rally after earnings")

    def test_byte_splitting_handles_unicode_without_dropping_characters(self):
        # A character count limit would allow these multibyte strings to exceed 500 bytes.
        original = "Aé中🚀" * 250
        parts = translations.split_text(original, max_bytes=500)
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), original)
        self.assertTrue(all(len(part.encode("utf-8")) <= 500 for part in parts))
        self.assertTrue(all(part for part in parts))

    def test_byte_splitting_prefers_word_boundaries(self):
        original = "Stocks advance after earnings. " * 80
        parts = translations.split_text(original)
        self.assertEqual(" ".join(parts), original.strip())
        self.assertTrue(all(len(part.encode("utf-8")) <= 500 for part in parts))
        self.assertEqual(translations.split_text("  \n "), [])


class TranslatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "translations.db"

    def translator(self, fetcher=None, **kwargs):
        return translations.Translator(self.path, fetcher=fetcher or mock.Mock(return_value=official_response()),
                                       email="", **kwargs)

    def test_title_and_summary_are_translated_while_all_original_fields_survive(self):
        source = report()
        original = copy.deepcopy(source)
        mapped = {source["news"][0]["title"]: "财报发布后股市上涨",
                  source["news"][0]["summary"]: "科技股领涨市场"}
        fetcher = mock.Mock(side_effect=lambda url: official_response(mapped[query_text(url)]))
        translated = self.translator(fetcher).enrich_snapshot(source)
        item = translated["news"][0]
        self.assertEqual(source, original, "Translation must not mutate the source snapshot")
        for key, value in original["news"][0].items():
            self.assertEqual(item[key], value)
        self.assertEqual(item["title_zh"], "财报发布后股市上涨")
        self.assertEqual(item["summary_zh"], "科技股领涨市场")
        self.assertEqual(item["translation_status"], "translated")
        self.assertEqual(item["translation_provider"], "MyMemory")
        self.assertEqual(translated["translation"]["counts"]["translated"], 1)
        self.assertEqual(fetcher.call_count, 2)
        self.assertEqual(translated["status"], source["status"])

    def test_same_article_in_today_and_recent_window_translates_once(self):
        item = article()
        fetcher = mock.Mock(return_value=official_response())
        translator = self.translator(fetcher)
        translated = translator.enrich_snapshot(report([item], [copy.deepcopy(item)]))
        self.assertEqual(fetcher.call_count, 2, "Only two distinct content fields should be requested")
        self.assertEqual(translated["news"][0], translated["recent_news"][0])
        self.assertEqual(translated["translation"]["counts"]["translated"], 1)
        translator.enrich_snapshot(report([item], [copy.deepcopy(item)]))
        self.assertEqual(fetcher.call_count, 2, "Refreshing unchanged feed data must use cache")

    def test_identical_content_with_different_feed_ids_reuses_the_same_cache(self):
        first, second = article("first"), article("second")
        fetcher = mock.Mock(return_value=official_response())
        translator = self.translator(fetcher)
        translator.enrich_snapshot(report([first, second]))
        self.assertEqual(fetcher.call_count, 2)

    def test_changed_content_invalidates_only_the_edited_field(self):
        fetcher = mock.Mock(return_value=official_response())
        translator = self.translator(fetcher)
        translator.enrich_snapshot(report())
        changed = article(title="Stocks fall after revised earnings")
        translator.enrich_snapshot(report([changed]))
        self.assertEqual(fetcher.call_count, 3)
        requested = [query_text(call.args[0]) for call in fetcher.call_args_list]
        self.assertEqual(requested.count(changed["summary"]), 1)
        self.assertIn(changed["title"], requested)

    def test_cached_translation_survives_restart_and_provider_outage(self):
        first_fetcher = mock.Mock(return_value=official_response("已有中文缓存"))
        expected = self.translator(first_fetcher).enrich_snapshot(report())
        offline = mock.Mock(side_effect=translations.ProviderError("Offline"))
        restarted = self.translator(offline)
        actual = restarted.enrich_snapshot(report())
        self.assertEqual(actual["news"], expected["news"])
        offline.assert_not_called()

    def test_cached_fields_are_kept_when_new_fields_cannot_be_translated(self):
        cached = article()
        self.translator().enrich_snapshot(report([cached]))
        offline = mock.Mock(side_effect=translations.ProviderError("Offline"))
        source = report([cached, article("new", "A new headline", "A new summary")])
        translated = self.translator(offline).enrich_snapshot(source)
        self.assertEqual(translated["news"][0]["translation_status"], "translated")
        self.assertEqual(translated["news"][1]["translation_status"], "unavailable")
        self.assertIsNotNone(translated["news"][0]["title_zh"])
        self.assertIsNone(translated["news"][1]["title_zh"])
        self.assertEqual(translated["translation"]["status"], "partial")

    def test_error_responses_leave_originals_visible_without_fake_chinese(self):
        source = report()
        error = official_response("今日额度已用尽", quotaFinished=True)
        fetcher = mock.Mock(return_value=error)
        translator = self.translator(fetcher)
        translated = translator.enrich_snapshot(source)
        item = translated["news"][0]
        self.assertEqual(item["title"], source["news"][0]["title"])
        self.assertEqual(item["summary"], source["news"][0]["summary"])
        self.assertIsNone(item["title_zh"])
        self.assertIsNone(item["summary_zh"])
        self.assertEqual(item["translation_status"], "unavailable")
        self.assertIsNone(item["translation_provider"])
        self.assertIsNone(translator.cached(item["title"]))
        self.assertIsNone(translator.cached(item["summary"]))

    def test_one_translated_field_reports_partial_instead_of_complete(self):
        source = report()
        title = source["news"][0]["title"]

        def fetch(url):
            if query_text(url) == title:
                return official_response("财报发布后股市上涨")
            raise translations.ProviderError("Summary failed")

        item = self.translator(fetch).enrich_snapshot(source)["news"][0]
        self.assertEqual(item["title_zh"], "财报发布后股市上涨")
        self.assertIsNone(item["summary_zh"])
        self.assertEqual(item["translation_status"], "partial")

    def test_chinese_text_and_empty_fields_do_not_call_translation_service(self):
        fetcher = mock.Mock(side_effect=AssertionError("Unexpected translation request"))
        source = report([article(title="美股 AI 板块上涨", summary="科技公司发布最新业绩。"),
                         article("empty", "", "")])
        translated = self.translator(fetcher).enrich_snapshot(source)
        fetcher.assert_not_called()
        self.assertEqual(translated["news"][0]["title_zh"], "美股 AI 板块上涨")
        self.assertEqual(translated["news"][0]["translation_status"], "original")

    def test_demo_never_contacts_service_and_preserves_fixture_translations(self):
        item = article()
        item.update(title_zh="演示：科技股上涨", summary_zh="演示摘要")
        source = report([item, article("other", "Another fictional headline", "")], mode="demo")
        fetcher = mock.Mock(side_effect=AssertionError("Demo must remain offline"))
        translated = self.translator(fetcher).enrich_snapshot(source)
        fetcher.assert_not_called()
        self.assertEqual(translated["mode"], "demo")
        self.assertEqual(translated["news"][0]["title_zh"], item["title_zh"])
        self.assertEqual(translated["news"][0]["summary_zh"], item["summary_zh"])
        self.assertEqual(translated["news"][1]["title"], source["news"][1]["title"])

    def test_transport_chunks_stay_under_five_hundred_utf8_bytes(self):
        original = "Markets react to café prices and semiconductor earnings. " * 50
        fetcher = mock.Mock(return_value=official_response("市场新闻"))
        translator = self.translator(fetcher)
        translated = translator.translate(original)
        requested = [query_text(call.args[0]) for call in fetcher.call_args_list]
        self.assertGreater(len(requested), 1)
        self.assertEqual(" ".join(requested), original.strip())
        self.assertTrue(all(len(text.encode("utf-8")) <= 500 for text in requested))
        self.assertEqual(translated.count("市场新闻"), len(requested))
        for call in fetcher.call_args_list:
            query = parse_qs(urlparse(call.args[0]).query)
            self.assertEqual(query["langpair"], ["en|zh-CN"])
            self.assertNotIn("de", query)
        translator.translate(original)
        self.assertEqual(fetcher.call_count, len(requested), "Cache should store the whole translated text")

    def test_repeated_failures_open_a_circuit_for_the_rest_of_the_batch(self):
        calls = []
        lock = threading.Lock()

        def offline(url):
            with lock:
                calls.append(url)
            raise translations.ProviderError("Network unavailable")

        source = report([article(str(index), "Headline number " + str(index),
                                 "Summary number " + str(index)) for index in range(100)])
        translated = self.translator(offline).enrich_snapshot(source)
        # Three failures trigger the circuit; at most two other workers can already be in flight.
        self.assertGreaterEqual(len(calls), 3)
        self.assertLessEqual(len(calls), 5)
        self.assertTrue(all(item["translation_status"] == "unavailable" for item in translated["news"]))
        self.assertEqual(translated["translation"]["counts"]["unavailable"], 100)

    def test_expired_batch_deadline_starts_no_new_network_requests(self):
        fetcher = mock.Mock(return_value=official_response())
        translated = self.translator(fetcher, time_budget=0).enrich_snapshot(report())
        fetcher.assert_not_called()
        self.assertEqual(translated["news"][0]["translation_status"], "unavailable")

    def test_daily_budget_persists_across_instances_and_cache_uses_no_quota(self):
        first = "First public headline"
        second = "Second public headline"
        budget = len(first) + len(second) - 1
        fetcher = mock.Mock(return_value=official_response("第一条新闻"))
        self.assertEqual(self.translator(fetcher, daily_budget=budget).translate(first), "第一条新闻")
        restarted_fetcher = mock.Mock(return_value=official_response())
        restarted = self.translator(restarted_fetcher, daily_budget=budget)
        self.assertEqual(restarted.translate(first), "第一条新闻")
        with self.assertRaises(translations.ProviderError):
            restarted.translate(second)
        restarted_fetcher.assert_not_called()

    def test_failed_requests_consume_reserved_budget_across_restarts(self):
        title = "Public headline"
        offline = mock.Mock(side_effect=translations.ProviderError("Service unavailable"))
        with self.assertRaises(translations.ProviderError):
            self.translator(offline, daily_budget=len(title)).translate(title)
        healthy = mock.Mock(return_value=official_response())
        with self.assertRaises(translations.ProviderError):
            self.translator(healthy, daily_budget=len(title)).translate(title)
        self.assertEqual(offline.call_count, 1)
        healthy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
