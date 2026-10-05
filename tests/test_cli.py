"""The command: text and JSON reports, exit codes, config files, errors."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import token_watchdog as tw
from fake_logs import assistant, write_log

ROOT = Path(__file__).parent.parent


def run(*argv: str) -> tuple:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = tw.main(list(argv))
        except SystemExit as exc:  # argparse exits for --version and bad flags
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.logs = self.tmp / "projects"
        # A healthy session: small fresh input, mostly cache reads.
        write_log(self.logs, "-home-dev-api", "calm.jsonl", [
            assistant(f"calm{n}", f"2026-09-2{n}T12:00:00Z", "calm-session", inp=100, write=20_000,
                      read=400_000, out=2_000, cwd="/home/dev/api") for n in range(4, 8)])
        self.window = ("--projects-dir", str(self.logs), "--now", "2026-09-30")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def add_spike(self) -> None:
        write_log(self.logs, "-home-dev-web", "spike.jsonl", [
            assistant("spike1", "2026-09-29T12:00:00Z", "spike-session", inp=10, write=450_000, out=1_000,
                      cwd="/home/dev/web")])

    def test_clean_window_exits_0(self) -> None:
        code, out, err = run(*self.window)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("token-watchdog: 7 days to 2026-09-30, 1 project, 1 session, 4 calls\n"))
        self.assertIn("cache hit 95%", out)
        self.assertTrue(out.endswith("CLEAN: no flags\n"))

    def test_a_flag_exits_1_and_names_the_session(self) -> None:
        self.add_spike()
        code, out, _ = run(*self.window)
        self.assertEqual(code, 1)
        self.assertIn("  outsized-turn  web spike-se\n                 one call weighed 568k, over 500k, 100% of its session\n", out)
        self.assertTrue(out.endswith("FLAGGED: 1 flag in 1 session\n"))

    def test_a_one_call_session_says_call(self) -> None:
        self.add_spike()
        _, out, _ = run(*self.window)
        self.assertIn("  spike-se     1 call   hit", out)
        self.assertNotIn(" 1 calls", out)

    def test_json_carries_the_same_verdict(self) -> None:
        self.add_spike()
        code, out, _ = run(*self.window, "--json")
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual([f["rule"] for f in data["flags"]], ["outsized-turn"])
        self.assertEqual(data["window"], {"first_day": "2026-09-24", "last_day": "2026-09-30", "days": 7})
        self.assertEqual(len(data["days"]), 7)
        self.assertEqual(data["version"], tw.__version__)

    def test_days_and_top_shape_the_report(self) -> None:
        self.add_spike()
        _, out, _ = run(*self.window, "--days", "2", "--top", "1")
        self.assertIn("2 days to 2026-09-30, 1 project, 1 session, 1 call", out)
        self.assertIn("top sessions (1 of 1)", out)

    def test_empty_window_is_clean(self) -> None:
        code, out, _ = run("--projects-dir", str(self.logs), "--now", "2026-01-31")
        self.assertEqual(code, 0)
        self.assertIn("no API calls in this window", out)

    def test_config_file_overrides_weights_and_thresholds(self) -> None:
        self.add_spike()
        config = self.tmp / "watch.json"
        config.write_text(json.dumps({"weights": {"output": 4}, "thresholds": {"turn_max": 1_000_000}}))
        code, out, _ = run(*self.window, "--config", str(config), "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["weights"]["output"], 4)
        self.assertEqual(data["sessions"][0]["largest_turn"], 10 + 450_000 * 1.25 + 1_000 * 4)

    def test_config_errors_exit_2_in_one_line(self) -> None:
        cases = {
            '{"weights": {"thinking": 3}}': "unknown weights key 'thinking'",
            '{"thresholds": {"turn_max": -1}}': "thresholds.turn_max must be a finite number of 0 or more",
            '{"limits": {}}': "unknown key 'limits'",
            "{not json": "is not valid JSON",
            "[]": "must be a JSON object",
        }
        config = self.tmp / "bad.json"
        for text, expected in cases.items():
            config.write_text(text)
            code, out, err = run(*self.window, "--config", str(config))
            with self.subTest(text=text):
                self.assertEqual((code, out), (2, ""))
                self.assertIn(expected, err)
                self.assertEqual(err.count("\n"), 1)

    def test_bad_arguments_exit_2_in_one_line(self) -> None:
        for argv, expected in ((("--now", "30/09/2026"), "expected a date as YYYY-MM-DD"),
                               (("--days", "zero"), "expected a whole number of 1 or more"),
                               (("--projects-dir", str(self.tmp / "missing")), "no transcript folder at"),
                               (("--config", str(self.tmp / "missing.json")), "cannot read config")):
            code, out, err = run(*argv)
            with self.subTest(argv=argv):
                self.assertEqual((code, out), (2, ""))
                self.assertTrue(err.startswith("token-watchdog: "), err)
                self.assertIn(expected, err)
                self.assertEqual(err.count("\n"), 1)

    def test_malformed_lines_are_reported(self) -> None:
        write_log(self.logs, "-home-dev-api", "torn.jsonl", [], raw_lines=('{"type": "assist',))
        _, out, _ = run(*self.window)
        self.assertIn("skipped 1 malformed line in 1 file\n", out)

    def test_unreadable_files_are_counted_and_reported(self) -> None:
        locked = write_log(self.logs, "-home-dev-api", "locked.jsonl",
                           [assistant("x1", "2026-09-29T12:00:00Z", "locked", inp=1)])
        real_open = Path.open

        def refuse(path, *args, **kwargs):
            if path == locked:
                raise PermissionError(13, "Permission denied")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", autospec=True, side_effect=refuse):
            code, out, _ = run(*self.window)
            _, data, _ = run(*self.window, "--json")
        self.assertEqual(code, 0)
        self.assertIn("4 calls", out)
        self.assertIn("skipped 1 unreadable file\n", out)
        self.assertEqual(json.loads(data)["unreadable_files"], 1)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads any folder")
    def test_unreadable_folders_are_counted_and_reported(self) -> None:
        locked = self.logs / "-home-dev-locked"
        write_log(self.logs, "-home-dev-locked", "s.jsonl", [assistant("l1", "2026-09-29T12:00:00Z", "l", inp=1)])
        locked.chmod(0)
        try:
            code, out, _ = run(*self.window)
            _, data, _ = run(*self.window, "--json")
        finally:
            locked.chmod(0o755)
        self.assertEqual(code, 0)
        self.assertIn("skipped 1 unreadable folder\n", out)
        self.assertEqual(json.loads(data)["unreadable_folders"], 1)

    def test_one_project_folder_is_read_as_one_project(self) -> None:
        sub = assistant("calm-sub", "2026-09-26T12:00:00Z", "calm-session", inp=50, sidechain=True,
                        cwd="/home/dev/api")
        write_log(self.logs, "-home-dev-api", "agent-a1.jsonl", [sub], subagent_of="calm-session")
        code, out, _ = run("--projects-dir", str(self.logs / "-home-dev-api"), "--now", "2026-09-30", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([(p["project"], p["label"], p["sessions"]) for p in data["projects"]],
                         [("-home-dev-api", "api", 1)])
        self.assertEqual(data["sessions"][0]["subagent_turns"], 1)

    def test_config_dir_variable_finds_the_logs(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp)}):
            code, out, _ = run("--now", "2026-09-30")
        self.assertEqual(code, 0)
        self.assertIn("4 calls", out)

    def test_projects_sharing_a_folder_basename_keep_distinct_labels(self) -> None:
        for project in ("-srv-a-app", "-srv-b-app"):
            write_log(self.logs, project, "s.jsonl", [
                assistant(project, "2026-09-29T12:00:00Z", project, inp=10, cwd="/srv/x/app")])
        _, out, _ = run(*self.window, "--json")
        labels = sorted(p["label"] for p in json.loads(out)["projects"])
        self.assertEqual(labels, ["-srv-a-app", "-srv-b-app", "api"])

    def test_window_of_zero_usage_calls_reports_cleanly(self) -> None:
        empty = self.tmp / "empty"
        write_log(empty, "-home-dev-api", "s.jsonl", [
            assistant(f"z{n}", "2026-09-29T12:00:00Z", "s", cwd="/home/dev/api") for n in range(3)])
        for extra in ((), ("--json",)):
            code, out, err = run("--projects-dir", str(empty), "--now", "2026-09-30", *extra)
            with self.subTest(extra=extra):
                self.assertEqual((code, err), (0, ""))
        self.assertIn("weighted 0 input-equivalent", run("--projects-dir", str(empty), "--now", "2026-09-30")[1])

    def test_window_outside_the_calendar_exits_2(self) -> None:
        for argv in (("--days", "800000"), ("--now", "9999-12-31"), ("--now", "0001-01-01", "--days", "2")):
            code, out, err = run(*self.window[:2], *argv)
            with self.subTest(argv=argv):
                self.assertEqual((code, out), (2, ""))
                self.assertIn("outside the calendar", err)
                self.assertEqual(err.count("\n"), 1)

    def test_non_finite_config_numbers_exit_2(self) -> None:
        config = self.tmp / "nan.json"
        for text in ('{"thresholds": {"cache_hit_min": NaN}}', '{"weights": {"output": Infinity}}'):
            config.write_text(text)
            code, _, err = run(*self.window, "--config", str(config))
            with self.subTest(text=text):
                self.assertEqual(code, 2)
                self.assertIn("must be a finite number of 0 or more", err)

    def test_an_unexpected_error_exits_2_in_one_line_never_1(self) -> None:
        with mock.patch.object(tw, "scan", side_effect=RuntimeError("boom")):
            code, out, err = run(*self.window)
        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, "token-watchdog: unexpected error: RuntimeError: boom\n")
        with mock.patch.object(tw, "render_text", side_effect=KeyError("days")):
            code, out, err = run(*self.window)
        self.assertEqual((code, out, err), (2, "", "token-watchdog: unexpected error: KeyError: 'days'\n"))

    def test_a_reader_that_closes_early_gets_no_traceback(self) -> None:
        for n in range(400):  # well past a pipe buffer of JSON
            write_log(self.logs, "-home-dev-api", f"bulk{n}.jsonl", [
                assistant(f"bulk{n}", "2026-09-29T12:00:00Z", f"bulk-session-{n}", inp=10, cwd="/home/dev/api")])
        proc = subprocess.Popen([sys.executable, "-m", "token_watchdog", *self.window, "--json"],
                                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdout.close()  # the reader is gone before the report is written
        err = proc.stderr.read().decode()
        proc.stderr.close()
        self.assertEqual((proc.wait(), err), (0, ""))

    def test_version_and_module_entry_point(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "token_watchdog", "--version"], cwd=str(ROOT),
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, f"token-watchdog {tw.__version__}\n"))


if __name__ == "__main__":
    unittest.main()
