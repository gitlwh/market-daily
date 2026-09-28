#!/usr/bin/env python3
"""Run one complete collection and publish atomic static JSON/Markdown files."""
import argparse
import copy
import json
import logging
import os
from pathlib import Path

import earnings
import leader_screen
import providers
import sentiment
from server import Application, markdown_report
from translations import Translator


SCHEMA_VERSION = 1


def write_atomic(path, content, binary=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if binary:
        temporary.write_bytes(content)
    else:
        temporary.write_text(content, encoding="utf-8")
    os.replace(str(temporary), str(path))


def write_json(path, value):
    write_atomic(path, json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def existing_sentiment_history(report_dir, before=None):
    result = []
    for path in sorted(report_dir.glob("????-??-??.json")):
        if before and path.stem >= before:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            snapshot = payload.get("snapshot", payload)
            mood = snapshot.get("sentiment") or {}
            if isinstance(mood.get("score"), (int, float)):
                result.append({"date": snapshot["date"], "score": mood["score"], "label": mood.get("label")})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return result[-29:]


def publish(output, snapshot, demo=None):
    output = Path(output)
    reports = output / "reports"
    snapshot = copy.deepcopy(snapshot)
    mood = snapshot.get("sentiment") or {}
    history = existing_sentiment_history(reports, snapshot["date"])
    if isinstance(mood.get("score"), (int, float)):
        history.append({"date": snapshot["date"], "score": mood["score"], "label": mood.get("label")})
    snapshot["sentiment_history"] = history[-30:]
    payload = {"schema_version": SCHEMA_VERSION, "snapshot": snapshot,
               "dates": [], "collection": {"running": False, "last_error": None,
                                               "last_finished": snapshot.get("generated_at"),
                                               "last_status": snapshot.get("status")},
               "schedule": {"enabled": True, "hour": 18, "minute": 30,
                            "timezone": "America/New_York", "next_run": None, "kind": "cloud_cron"}}
    write_json(reports / (snapshot["date"] + ".json"), payload)
    write_atomic(reports / (snapshot["date"] + ".md"), markdown_report(snapshot))
    dates = sorted((path.stem for path in reports.glob("????-??-??.json")), reverse=True)
    payload["dates"] = dates
    write_json(output / "latest.json", payload)
    write_json(output / "index.json", {"schema_version": SCHEMA_VERSION, "generated_at": snapshot.get("generated_at"), "dates": dates})
    if demo:
        write_json(output / "demo.json", demo)
    return payload


def demo_payload(app):
    return dict({"schema_version": SCHEMA_VERSION}, **app.dashboard(demo=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description="采集一次并生成静态网页可读取的数据文件")
    parser.add_argument("--db", default="data/market.db")
    parser.add_argument("--output", default="web-data")
    parser.add_argument("--no-translate", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    translator = None if args.no_translate else Translator(args.db)
    app = Application(args.db, schedule=False, translator=translator,
                      earnings_collector=earnings.collect_earnings,
                      leader_collector=leader_screen.collect_leader_screen,
                      sentiment_collector=sentiment.collect_sentiment)
    app._collect()
    snapshot = app.archive.get()
    if not snapshot or snapshot.get("status") == "error":
        logging.error("没有可发布的有效日报；保留原有 latest.json")
        return 1
    result = publish(args.output, snapshot, demo_payload(app))
    logging.info("Published %s with %d archived dates to %s", snapshot["date"], len(result["dates"]), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
