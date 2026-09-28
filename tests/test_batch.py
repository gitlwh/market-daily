import json
import tempfile
import unittest
from pathlib import Path

import batch
import leader_screen
import providers
import sentiment


class BatchPublishTests(unittest.TestCase):
    def snapshot(self, day="2026-09-18", score=64):
        result = providers.demo_snapshot()
        result.update(date=day, generated_at=day + "T18:00:00-04:00", mode="live")
        result["leader_screen"] = leader_screen.demo_leader_screen()
        result["sentiment"] = sentiment.demo_sentiment()
        result["sentiment"].update(score=score, label=sentiment.mood_label(score))
        return result

    def test_publish_writes_latest_index_daily_json_markdown_and_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = batch.publish(output, self.snapshot(), {"snapshot": {"mode": "demo"}})
            for relative in ("latest.json", "index.json", "demo.json",
                             "reports/2026-09-18.json", "reports/2026-09-18.md"):
                self.assertTrue((output / relative).is_file(), relative)
            latest = json.loads((output / "latest.json").read_text())
            self.assertEqual(latest["schema_version"], 1)
            self.assertEqual(latest["dates"], ["2026-09-18"])
            self.assertEqual(latest["snapshot"]["sentiment_history"][-1]["score"], 64)
            self.assertIn("多策略选股观察", (output / "reports/2026-09-18.md").read_text())
            self.assertEqual(result["snapshot"]["date"], "2026-09-18")

    def test_history_accumulates_without_duplicate_same_day(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            batch.publish(output, self.snapshot("2026-09-17", 45))
            batch.publish(output, self.snapshot("2026-09-18", 65))
            latest = json.loads((output / "latest.json").read_text())
            self.assertEqual(latest["dates"], ["2026-09-18", "2026-09-17"])
            self.assertEqual([item["score"] for item in latest["snapshot"]["sentiment_history"]], [45, 65])
            batch.publish(output, self.snapshot("2026-09-18", 70))
            latest = json.loads((output / "latest.json").read_text())
            self.assertEqual([item["score"] for item in latest["snapshot"]["sentiment_history"]], [45, 70])


if __name__ == "__main__":
    unittest.main()
