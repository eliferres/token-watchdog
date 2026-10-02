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
        self.assertTrue(out.startswith("token-watchdog: 7 days to 2026-09-30, 1 projects, 1 sessions, 4 calls\n"))
        self.assertIn("cache hit 95%", out)
        self.assertTrue(out.endswith("CLEAN: no flags\n"))

    def test_a_flag_exits_1_and_names_the_session(self) -> None:
        self.add_spike()
        code, out, _ = run(*self.window)
        self.assertEqual(code, 1)
        self.assertIn("outsized-turn  web spike-se: one call weighed 568k, over 500k", out)
        self.assertTrue(out.endswith("FLAGGED: 1 flag in 1 session\n"))

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
        self.assertIn("2 days to 2026-09-30, 1 projects, 1 sessions, 1 calls", out)
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
            '{"thresholds": {"turn_max": -1}}': "thresholds.turn_max must be a number of 0 or more",
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

    def test_version_and_module_entry_point(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "token_watchdog", "--version"], cwd=str(ROOT),
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, f"token-watchdog {tw.__version__}\n"))


if __name__ == "__main__":
    unittest.main()
