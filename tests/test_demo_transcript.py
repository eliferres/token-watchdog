"""demo/transcript.json is a record of real runs, and demo/terminal.svg draws it.

Each recorded command is run again with bash inside a fresh copy of the
repository, and its combined output and exit status must equal the record.
UPDATE_DEMO_TRANSCRIPT=1 rewrites the record from the run instead; that is the
only way the file is meant to change. The picture is then checked row by row
against the record, so the README cannot show output the tool never printed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Tuple

REPO = Path(__file__).resolve().parent.parent
RECORD = REPO / "demo" / "transcript.json"
PICTURE = REPO / "demo" / "terminal.svg"
PLACEHOLDER = "/path/to/checkout"
SVG = "{http://www.w3.org/2000/svg}"
CUT = "…"


def replay(entries: List[dict]) -> List[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "token-watchdog"
        shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.egg-info", "build", "dist", ".venv"))
        results = []
        for entry in entries:
            proc = subprocess.run(["bash", "-c", entry["cmd"]], cwd=str(copy), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = proc.stdout
            # macOS reports the temp dir under /private/var as well as /var.
            for form in (str(copy.resolve()), str(copy)):
                out = out.replace(form, PLACEHOLDER)
            results.append({"cmd": entry["cmd"], "out": out.rstrip("\n"), "status": proc.returncode})
        return results


def picture_rows() -> List[Tuple[str, str]]:
    """("cmd" | "more" | "out", text) per drawn session row, top to bottom.

    The renderer draws a command as a prompt tspan followed by the command text,
    continues a wrapped command on its own row indented four spaces with class
    "cmd", and draws output rows as plain text. The title bar is the one text
    element with its own font size.
    """
    rows = []
    for el in ET.parse(PICTURE).getroot().iter(SVG + "text"):
        if el.get("font-size"):
            continue
        spans = el.findall(SVG + "tspan")
        if spans:
            rows.append(("cmd", spans[-1].text or ""))
        elif el.get("class") == "cmd":
            rows.append(("more", (el.text or "")[4:]))
        else:
            rows.append(("out", el.text or ""))
    return rows


def drawn_as(row: str, line: str) -> bool:
    """A row shows its line whole, or cut once at the end with an ellipsis."""
    if row == line:
        return True
    return row.endswith(CUT) and row.count(CUT) == 1 and line.startswith(row[:-1]) and len(row) - 1 < len(line)


class DemoTranscriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.record = json.loads(RECORD.read_text(encoding="utf-8"))
        cls.actual = replay(cls.record)
        if os.environ.get("UPDATE_DEMO_TRANSCRIPT"):
            RECORD.write_text(json.dumps(cls.actual, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            cls.record = cls.actual

    def test_each_command_prints_what_was_recorded(self) -> None:
        self.assertEqual(len(self.actual), len(self.record))
        for want, got in zip(self.record, self.actual):
            with self.subTest(cmd=want["cmd"]):
                self.assertEqual(got["out"], want["out"])
                self.assertEqual(got["status"], want["status"])

    def test_record_holds_no_machine_paths(self) -> None:
        text = RECORD.read_text(encoding="utf-8")
        for marker in ("/Users/", "/home/runner", "/private/var", "/var/folders", "/tmp/"):
            self.assertNotIn(marker, text)

    def test_readme_console_blocks_match_the_record(self) -> None:
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```console\n(.*?)```", readme, re.S)
        self.assertTrue(blocks, "the README shows no console session")
        recorded = {entry["cmd"]: entry["out"] for entry in self.record}
        for block in blocks:
            cmd, _, out = block.partition("\n")
            self.assertTrue(cmd.startswith("$ "), block[:60])
            with self.subTest(cmd=cmd):
                self.assertIn(cmd[2:], recorded, "README command missing from the record")
                self.assertEqual(out.rstrip("\n"), recorded[cmd[2:]])

    def test_picture_draws_the_record_in_order(self) -> None:
        rows = picture_rows()
        self.assertTrue(rows, "the picture has no session rows")
        at = 0
        for entry in self.record:
            if at == len(rows):
                break  # the picture may end between commands
            kind, text = rows[at]
            self.assertEqual(kind, "cmd", f"row {at + 1} should start {entry['cmd']!r}")
            pieces = [text]
            at += 1
            while at < len(rows) and rows[at][0] == "more":
                pieces.append(rows[at][1])
                at += 1
            rebuilt = " ".join(p[:-2] if p.endswith(" \\") else p for p in pieces)
            self.assertEqual(rebuilt, entry["cmd"])
            for line in (l for l in entry["out"].splitlines() if l.strip()):
                self.assertLess(at, len(rows), f"the picture stops inside {entry['cmd']!r}")
                kind, text = rows[at]
                self.assertEqual(kind, "out", f"row {at + 1} should be {line!r}")
                self.assertTrue(drawn_as(text, line), f"row {at + 1} is {text!r}, recorded {line!r}")
                at += 1
        self.assertEqual(at, len(rows), "the picture has rows the record does not account for")


if __name__ == "__main__":
    unittest.main()
