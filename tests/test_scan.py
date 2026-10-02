"""Reading transcripts: deduplication, subagents, malformed input, time windows."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import token_watchdog as tw
from fake_logs import assistant, user, write_log

UTC = timezone.utc
EST = timezone(timedelta(hours=-5))


def scan_window(root: Path, end_day: date = date(2026, 9, 30), days: int = 7, tz=UTC):
    start, end = tw.window_bounds(end_day, days, tz)
    return tw.scan(root, start, end)


class ScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_streamed_copies_of_one_message_count_once_with_final_counts(self) -> None:
        write_log(self.root, "-home-dev-demo-app", "s1.jsonl", [
            assistant("msg_a", "2026-09-29T10:00:00.000Z", "s1", inp=5, write=100, read=1000, out=8),
            assistant("msg_a", "2026-09-29T10:00:01.000Z", "s1", inp=5, write=100, read=1000, out=8),
            assistant("msg_a", "2026-09-29T10:00:02.000Z", "s1", inp=9, write=200, read=2000, out=640),
        ])
        result = scan_window(self.root)
        self.assertEqual(len(result.turns), 1)
        self.assertEqual(result.turns[0].tokens,
                         {"input": 9, "cache_write_5m": 200, "cache_write_1h": 0, "cache_read": 2000, "output": 640})

    def test_message_copied_into_a_resumed_session_counts_once_in_the_first(self) -> None:
        original = assistant("msg_b", "2026-09-28T09:00:00Z", "first", inp=10, out=10)
        write_log(self.root, "p", "first.jsonl", [original])
        copy = dict(original, sessionId="resumed")
        write_log(self.root, "p", "resumed.jsonl", [copy, assistant("msg_c", "2026-09-29T09:00:00Z", "resumed", inp=1)])
        result = scan_window(self.root)
        owners = sorted((t.session, t.tokens["input"]) for t in result.turns)
        self.assertEqual(owners, [("first", 10), ("resumed", 1)])

    def test_subagent_transcript_rolls_into_its_parent_session(self) -> None:
        write_log(self.root, "p", "parent.jsonl", [assistant("m1", "2026-09-29T09:00:00Z", "parent", inp=10)])
        sub = assistant("m2", "2026-09-29T09:01:00Z", "parent", inp=20, sidechain=True)
        write_log(self.root, "p", "agent-x1.jsonl", [sub], subagent_of="parent")
        result = scan_window(self.root)
        self.assertEqual({t.session for t in result.turns}, {"parent"})
        self.assertEqual(sorted(t.sidechain for t in result.turns), [False, True])

    def test_subagent_without_session_id_takes_its_folder_name(self) -> None:
        sub = assistant("m2", "2026-09-29T09:01:00Z", "x", inp=20)
        del sub["sessionId"]
        write_log(self.root, "p", "agent-x1.jsonl", [sub], subagent_of="parent-session")
        turn = scan_window(self.root).turns[0]
        self.assertEqual((turn.session, turn.sidechain), ("parent-session", True))

    def test_malformed_lines_are_counted_and_skipped(self) -> None:
        good = assistant("m1", "2026-09-29T09:00:00Z", "s", inp=10)
        bad_usage = assistant("m2", "2026-09-29T09:00:00Z", "s")
        bad_usage["message"]["usage"]["input_tokens"] = "ten"
        bad_time = assistant("m3", "yesterday", "s", inp=1)
        write_log(self.root, "p", "s.jsonl", [good, user("2026-09-29T08:59:00Z", "s"), bad_usage, bad_time],
                  raw_lines=('{"type": "assistant", "message": {"id": "m4", "us', "[1, 2]", ""))
        write_log(self.root, "p", "clean.jsonl", [assistant("m5", "2026-09-29T09:00:00Z", "c", inp=1)])
        result = scan_window(self.root)
        self.assertEqual(len(result.turns), 2)
        self.assertEqual((result.malformed_lines, result.malformed_files, result.files_read), (4, 1, 2))

    def test_one_hour_cache_writes_are_weighted_apart_from_five_minute_writes(self) -> None:
        entry = assistant("m1", "2026-09-29T09:00:00Z", "s", write=1000)
        entry["message"]["usage"]["cache_creation"] = {
            "ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 800}
        write_log(self.root, "p", "s.jsonl", [entry, assistant("m2", "2026-09-29T09:01:00Z", "s", write=1000)])
        split, flat = sorted(scan_window(self.root).turns, key=lambda t: t.owner)
        self.assertEqual(split.weighted(tw.DEFAULT_WEIGHTS), 200 * 1.25 + 800 * 2.0)
        self.assertEqual(flat.weighted(tw.DEFAULT_WEIGHTS), 1000 * 1.25)  # no breakdown: five-minute

    def test_inconsistent_cache_breakdown_is_malformed(self) -> None:
        entry = assistant("m1", "2026-09-29T09:00:00Z", "s", write=100)
        entry["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": 900}
        write_log(self.root, "p", "s.jsonl", [entry])
        self.assertEqual(scan_window(self.root).malformed_lines, 1)

    def test_message_without_id_is_counted_every_time(self) -> None:
        loose = assistant("x", "2026-09-29T09:00:00Z", "s", inp=3)
        del loose["message"]["id"]
        del loose["requestId"]
        write_log(self.root, "p", "s.jsonl", [loose, loose])
        self.assertEqual(len(scan_window(self.root).turns), 2)

    def test_day_buckets_follow_the_report_time_zone(self) -> None:
        # 02:00 UTC on the 30th is 21:00 on the 29th in UTC-5.
        write_log(self.root, "p", "s.jsonl", [assistant("m1", "2026-09-30T02:00:00Z", "s", inp=10)])
        for tz, expected in ((UTC, "2026-09-30"), (EST, "2026-09-29")):
            result = scan_window(self.root, tz=tz)
            report = tw.summarize(result, tw.DEFAULT_WEIGHTS, date(2026, 9, 30), 7, tz)
            busy = [d["day"] for d in report["days"] if d["turns"]]
            self.assertEqual(busy, [expected], tz)

    def test_window_edges_are_whole_days_in_the_time_zone(self) -> None:
        write_log(self.root, "p", "s.jsonl", [
            assistant("early", "2026-09-24T04:59:59Z", "s", inp=1),  # 23:59:59 on the 23rd in UTC-5
            assistant("first", "2026-09-24T05:00:00Z", "s", inp=2),
            assistant("last", "2026-10-01T04:59:59Z", "s", inp=4),
            assistant("late", "2026-10-01T05:00:00Z", "s", inp=8),
        ])
        result = scan_window(self.root, tz=EST)
        self.assertEqual(sorted(t.tokens["input"] for t in result.turns), [2, 4])

    def test_files_last_written_before_the_window_are_not_read(self) -> None:
        path = write_log(self.root, "p", "old.jsonl", [assistant("m1", "2026-09-29T09:00:00Z", "s", inp=1)])
        stale = datetime(2026, 9, 1, tzinfo=UTC).timestamp()
        os.utime(path, (stale, stale))
        self.assertEqual(scan_window(self.root).files_read, 0)

    def test_each_file_path_is_resolved_once_not_per_line(self) -> None:
        write_log(self.root, "p", "s.jsonl", [
            assistant(f"m{n}", "2026-09-29T09:00:00Z", "s", inp=1) for n in range(50)])
        original = Path.relative_to
        with mock.patch.object(Path, "relative_to", autospec=True, side_effect=original) as spy:
            scan_window(self.root)
        self.assertEqual(spy.call_count, 1)

    def test_project_label_comes_from_the_working_directory(self) -> None:
        write_log(self.root, "-home-dev-api", "s.jsonl",
                  [assistant("m1", "2026-09-29T09:00:00Z", "s", inp=1, cwd="/home/dev/api")])
        self.assertEqual(scan_window(self.root).labels, {"-home-dev-api": "api"})

    def test_project_label_prefers_the_directory_the_session_started_in(self) -> None:
        write_log(self.root, "-home-dev-api", "s.jsonl", [
            assistant("m1", "2026-09-29T09:00:00Z", "s", inp=1, cwd="/home/dev/api/docs"),
            assistant("m2", "2026-09-29T09:01:00Z", "s", inp=1, cwd="/home/dev/api")])
        self.assertEqual(scan_window(self.root).labels, {"-home-dev-api": "api"})


class WeightsAndRatiosTest(unittest.TestCase):
    def test_default_weights_follow_price_ratios(self) -> None:
        turn = tw.Turn("p", "s", False, datetime(2026, 9, 29, tzinfo=UTC),
                       {"input": 100, "cache_write_5m": 100, "cache_write_1h": 100, "cache_read": 100, "output": 100}, (None, "", 0))
        self.assertAlmostEqual(turn.weighted(tw.DEFAULT_WEIGHTS), 100 + 125 + 200 + 10 + 500)

    def test_cache_hit_and_reread_ratio(self) -> None:
        tokens = {"input": 10, "cache_write_5m": 40, "cache_write_1h": 50, "cache_read": 900, "output": 5}
        self.assertAlmostEqual(tw.cache_hit(tokens), 0.9)
        self.assertAlmostEqual(tw.reread_ratio(tokens), 10.0)
        empty = {"input": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 5}
        self.assertIsNone(tw.cache_hit(empty))
        self.assertIsNone(tw.reread_ratio(empty))


class TimestampTest(unittest.TestCase):
    def test_parses_the_forms_found_in_transcripts(self) -> None:
        expected = datetime(2026, 9, 29, 10, 0, 0, 123000, tzinfo=UTC)
        self.assertEqual(tw.parse_timestamp("2026-09-29T10:00:00.123Z"), expected)
        self.assertEqual(tw.parse_timestamp("2026-09-29T10:00:00.1230000Z"), expected)
        self.assertEqual(tw.parse_timestamp("2026-09-29T06:00:00.123-04:00"), expected)
        self.assertEqual(tw.parse_timestamp("2026-09-29T10:00:00"), expected.replace(microsecond=0))

    def test_rejects_what_is_not_a_timestamp(self) -> None:
        for text in ("", "yesterday", None, 1727600000):
            self.assertIsNone(tw.parse_timestamp(text))


class ProjectsDirTest(unittest.TestCase):
    def test_config_dir_variable_moves_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/opt/claude-config"}):
            self.assertEqual(tw.default_projects_dir(), Path("/opt/claude-config/projects"))
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=Path("/h")):
            self.assertEqual(tw.default_projects_dir(), Path("/h/.claude/projects"))


if __name__ == "__main__":
    unittest.main()
