"""Behavioral checks for the local API, durable archive and collection scheduler.

These tests use a temporary database and synthetic collector. They never contact
market/news services or change the application's normal data directory.
"""
import copy
import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def snapshot(day="2026-09-18", status="ok", mode="live"):
    return {
        "date": day,
        "generated_at": day + "T18:00:00-04:00",
        "market_date": day,
        "mode": mode,
        "status": status,
        "indices": [],
        "sectors": [{"name": "信息技术", "symbol": "XLK", "price": 250,
                     "change_pct": 2.5, "relative_pct": 1.2, "volume_ratio": None}],
        "summary": ["信息技术上涨。"],
        "news": [{"title": "Chip company earnings", "url": "https://example.com/news",
                  "source": "Test", "published_at": day + "T14:00:00-04:00"}],
        "alerts": [],
        "sources": [{"name": "Synthetic test source", "status": "ok", "detail": "test only"}],
        "errors": [],
    }


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "reports.db"
        self.archive = server.Archive(self.path)

    def test_empty_archive_does_not_invent_a_report(self):
        self.assertIsNone(self.archive.get())
        self.assertEqual(self.archive.dates(), [])

    def test_reports_survive_restart_and_are_ordered_by_report_day(self):
        newest = snapshot("2026-09-18")
        older = snapshot("2026-09-17")
        self.archive.save(newest)
        self.archive.save(older)
        restarted = server.Archive(self.path)
        self.assertEqual(restarted.get(), newest)
        self.assertEqual(restarted.get("2026-09-17"), older)
        self.assertEqual(restarted.dates(), ["2026-09-18", "2026-09-17"])
        self.assertIsNone(restarted.get("2026-09-16"))

    def test_same_day_refresh_replaces_one_report(self):
        original = snapshot()
        revised = copy.deepcopy(original)
        revised["summary"] = ["Updated report"]
        self.archive.save(original)
        self.archive.save(revised)
        self.assertEqual(self.archive.get(), revised)
        self.assertEqual(self.archive.dates(), [original["date"]])

    def test_partial_live_report_is_preserved_with_its_errors(self):
        report = snapshot(status="partial")
        report["errors"] = ["One source unavailable"]
        self.archive.save(report)
        self.assertEqual(self.archive.get(), report)

    def test_demo_and_failed_collection_cannot_overwrite_live_report(self):
        original = snapshot()
        self.archive.save(original)
        for replacement in (snapshot(mode="demo"), snapshot(status="error")):
            with self.subTest(mode=replacement["mode"], status=replacement["status"]):
                with self.assertRaises(ValueError):
                    self.archive.save(replacement)
                self.assertEqual(self.archive.get(), original)

    def test_invalid_date_and_non_json_numbers_are_not_saved(self):
        for invalid in ("2026-02-30", "2026-9-18", "2026-09-18T18:00:00", "../etc/passwd"):
            with self.subTest(date=invalid), self.assertRaises(ValueError):
                self.archive.save(snapshot(invalid))
        report = snapshot()
        report["sectors"][0]["price"] = float("nan")
        with self.assertRaises(ValueError):
            self.archive.save(report)
        self.assertEqual(self.archive.dates(), [])


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "reports.db"
        self.archive = server.Archive(self.path)

    def test_due_uses_eastern_day_and_local_clock(self):
        # UTC has crossed midnight while New York is still on the prior date.
        before = datetime(2026, 9, 18, 21, 59, tzinfo=timezone.utc)
        after = datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(self.archive.schedule_due(before, 18, 0))
        self.assertTrue(self.archive.schedule_due(after, 18, 0))
        self.archive.record_schedule(after.astimezone(server.ET), True)
        self.assertFalse(self.archive.schedule_due(after, 18, 0))

    def test_failed_run_retries_after_thirty_minutes(self):
        attempted = datetime(2026, 9, 18, 18, 5, tzinfo=server.ET)
        self.archive.record_schedule(attempted, False)
        self.assertFalse(self.archive.schedule_due(attempted + timedelta(minutes=29, seconds=59), 18, 0))
        self.assertTrue(self.archive.schedule_due(attempted + timedelta(minutes=30), 18, 0))

    def test_success_is_durable_but_does_not_skip_next_day(self):
        attempted = datetime(2026, 9, 18, 18, 0, tzinfo=server.ET)
        self.archive.record_schedule(attempted, True)
        restarted = server.Archive(self.path)
        self.assertFalse(restarted.schedule_due(attempted + timedelta(hours=4), 18, 0))
        self.assertTrue(restarted.schedule_due(attempted + timedelta(days=1), 18, 0))

    def test_next_run_changes_utc_offset_across_both_dst_boundaries(self):
        cases = [
            (datetime(2026, 3, 7, 19, 0, tzinfo=server.ET), "2026-03-08T18:00:00-04:00"),
            (datetime(2026, 10, 31, 19, 0, tzinfo=server.ET), "2026-11-01T18:00:00-05:00"),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(server.next_run(now, 18, 0).isoformat(), expected)

    def test_next_run_at_cutoff_points_to_following_day(self):
        now = datetime(2026, 9, 18, 18, 0, tzinfo=server.ET)
        self.assertEqual(server.next_run(now - timedelta(seconds=1), 18, 0), now)
        self.assertEqual(server.next_run(now, 18, 0), now + timedelta(days=1))


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = server.Application(Path(self.temp.name) / "reports.db", schedule=False,
                                      collector=lambda: snapshot())

    def finish(self):
        self.app.worker.join(timeout=3)
        self.assertFalse(self.app.worker.is_alive(), "Collector did not finish")
        self.assertFalse(self.app.state()["running"])
        self.assertIsNotNone(self.app.state()["last_finished"])

    def test_overlapping_refreshes_share_one_collection(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def collect():
            calls.append(True)
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("Test did not release collector")
            return snapshot()

        self.app.collector = collect
        try:
            self.assertTrue(self.app.refresh())
            self.assertTrue(entered.wait(timeout=1))
            self.assertTrue(self.app.state()["running"])
            self.assertFalse(self.app.refresh())
            self.assertFalse(self.app.refresh(scheduled=True))
        finally:
            release.set()
            self.finish()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.app.archive.get(), snapshot())

    def test_complete_source_failure_keeps_previous_report(self):
        previous = snapshot()
        self.app.archive.save(previous)
        self.app.collector = lambda: {"status": "error", "errors": ["Sources unavailable"]}
        self.app.refresh()
        self.finish()
        self.assertEqual(self.app.archive.get(), previous)
        self.assertIn("Sources unavailable", self.app.state()["last_error"])

    def test_unexpected_exception_keeps_report_and_allows_retry(self):
        previous = snapshot()
        self.app.archive.save(previous)
        self.app.collector = mock.Mock(side_effect=RuntimeError("Network failed"))
        with self.assertLogs(server.LOG, level="ERROR"):
            self.app.refresh()
            self.finish()
        self.assertEqual(self.app.archive.get(), previous)
        self.assertIn("Network failed", self.app.state()["last_error"])
        updated = snapshot("2026-09-19")
        self.app.collector = lambda: updated
        self.assertTrue(self.app.refresh())
        self.finish()
        self.assertIsNone(self.app.state()["last_error"])
        self.assertEqual(self.app.archive.get(), updated)

    def test_partial_scheduled_report_is_saved_and_remains_retryable(self):
        now = datetime(2026, 9, 18, 18, 0, tzinfo=server.ET)
        report = snapshot(status="partial")
        report["errors"] = ["News temporarily unavailable"]
        self.app.collector = lambda: report
        with mock.patch.object(server, "market_now", return_value=now):
            self.app.refresh(scheduled=True)
            self.finish()
        self.assertEqual(self.app.archive.get(), report)
        self.assertIn("News temporarily unavailable", self.app.state()["last_error"])
        self.assertFalse(self.app.archive.schedule_due(now + timedelta(minutes=29), 18, 0))
        self.assertTrue(self.app.archive.schedule_due(now + timedelta(minutes=30), 18, 0))

    def test_successful_scheduled_collection_is_not_repeated_that_day(self):
        now = datetime(2026, 9, 18, 18, 0, tzinfo=server.ET)
        with mock.patch.object(server, "market_now", return_value=now):
            self.app.refresh(scheduled=True)
            self.finish()
        self.assertFalse(self.app.archive.schedule_due(now + timedelta(hours=2), 18, 0))

    def test_demo_endpoint_shape_does_not_persist_demo(self):
        self.assertIsNone(self.app.dashboard()["snapshot"])
        response = self.app.dashboard(demo=True)
        self.assertEqual(response["snapshot"]["mode"], "demo")
        self.assertEqual(self.app.archive.dates(), [])
        self.assertIsNone(self.app.dashboard()["snapshot"])
        self.assertIsNone(response["schedule"]["next_run"])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = server.Application(Path(self.temp.name) / "reports.db", schedule=False,
                                      collector=lambda: snapshot())
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.app))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        if self.app.worker:
            self.app.worker.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_empty_dashboard_and_health_are_honest(self):
        code, headers, body = self.request("/api/dashboard")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIsNone(data["snapshot"])
        self.assertEqual(data["dates"], [])
        self.assertFalse(data["collection"]["running"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        code, _, body = self.request("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_selected_day_never_falls_back_to_latest(self):
        self.app.archive.save(snapshot())
        self.app.archive.save(snapshot("2026-09-19"))
        code, _, body = self.request("/api/dashboard?date=2026-09-18")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["snapshot"]["date"], "2026-09-18")
        code, _, body = self.request("/api/dashboard?date=2026-09-17")
        self.assertEqual(code, 200)
        self.assertIsNone(json.loads(body)["snapshot"])

    def test_malformed_dates_return_actionable_client_error(self):
        for path in ("/api/dashboard?date=2026-02-30", "/api/export?date=bad"):
            with self.subTest(path=path):
                code, _, body = self.request(path)
                self.assertEqual(code, 400)
                self.assertIn("YYYY-MM-DD", json.loads(body)["error"])

    def test_exports_match_saved_report_and_use_download_headers(self):
        report = snapshot()
        self.app.archive.save(report)
        for fmt in ("json", "md"):
            with self.subTest(format=fmt):
                code, headers, body = self.request("/api/export?date=2026-09-18&format=" + fmt)
                self.assertEqual(code, 200)
                self.assertIn("market-daily-2026-09-18." + fmt, headers["Content-Disposition"])
                if fmt == "json":
                    self.assertEqual(json.loads(body), report)
                else:
                    text = body.decode("utf-8")
                    self.assertIn("信息技术", text)
                    self.assertIn("+2.50%", text)
                    self.assertIn("https://example.com/news", text)
        self.assertEqual(self.request("/api/export?date=2026-09-17")[0], 404)
        self.assertEqual(self.request("/api/export?format=exe")[0], 400)

    def test_demo_route_cannot_pollute_archive(self):
        self.app.archive.save(snapshot())
        code, _, body = self.request("/api/demo")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["snapshot"]["mode"], "demo")
        self.assertEqual(self.app.archive.get(), snapshot())
        self.assertEqual(self.app.archive.dates(), ["2026-09-18"])

    def test_refresh_rejects_foreign_origins_hosts_and_cross_site_requests(self):
        self.app.refresh = mock.Mock(return_value=True)
        headers_to_reject = [
            {"Origin": "https://example.com"},
            {"Host": "example.com"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "null"},
        ]
        for headers in headers_to_reject:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/refresh", "POST", "{}", headers)[0], 403)
        self.app.refresh.assert_not_called()

    def test_refresh_accepts_same_origin_and_reports_already_running(self):
        self.app.refresh = mock.Mock(side_effect=[True, False])
        origin = "http://127.0.0.1:" + str(self.httpd.server_port)
        for expected in (True, False):
            code, _, body = self.request("/api/refresh", "POST", "{}", {"Origin": origin})
            self.assertEqual(code, 202)
            data = json.loads(body)
            self.assertTrue(data["running"])
            self.assertEqual(data["started"], expected)

    def test_refresh_rejects_invalid_and_oversized_json(self):
        self.app.refresh = mock.Mock(return_value=True)
        for body in ("{", "[]", "null", "x" * 1025):
            with self.subTest(body=body[:10]):
                self.assertEqual(self.request("/api/refresh", "POST", body)[0], 400)
        self.app.refresh.assert_not_called()

    def test_api_and_static_routes_are_not_a_filesystem_browser(self):
        for path in ("/server.py", "/../server.py", "/%2e%2e/server.py", "/data/market.db", "/api/unknown"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/health", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/unknown", "POST", "{}")[0], 404)


class MarkdownTests(unittest.TestCase):
    def test_missing_numbers_and_markup_do_not_break_table_or_add_unsafe_url(self):
        report = snapshot()
        sector = report["sectors"][0]
        sector.update(name="A | B\nC", change_pct=None, relative_pct=None)
        report["news"][0].update(title="Headline [test]", url="javascript:alert(1)")
        exported = server.markdown_report(report)
        self.assertIn("A \\| B C", exported)
        self.assertIn("Headline \\[test\\]", exported)
        self.assertNotIn("javascript:", exported)
        self.assertNotIn("None", exported)


class CommandLineTests(unittest.TestCase):
    def test_collect_once_failure_returns_nonzero_even_with_saved_report_today(self):
        now = datetime(2026, 9, 18, 18, 0, tzinfo=server.ET)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "reports.db"
            archived = snapshot()
            server.Archive(db_path).save(archived)
            argv = ["server.py", "--collect-once", "--db", str(db_path)]
            failed = {"status": "error", "errors": ["All sources offline"]}
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(server, "market_now", return_value=now), \
                    mock.patch.object(server.providers, "collect_snapshot", return_value=failed), \
                    mock.patch.object(server.logging, "basicConfig"):
                self.assertNotEqual(server.main(), 0)
            self.assertEqual(server.Archive(db_path).get(), archived)


if __name__ == "__main__":
    unittest.main()
