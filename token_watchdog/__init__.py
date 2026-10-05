"""Audit Claude Code session logs for wasteful token use, without calling a model.

Claude Code writes one JSONL transcript per session under ~/.claude/projects/<project>/,
with subagent transcripts beside it in <session-id>/subagents/. Every assistant entry
carries the API usage record for that call. This module reads those records, weights
them into one input-equivalent number, and breaks the result down by project, session
and day.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__version__ = "0.1.0"

# Input-equivalent weights, in units of one fresh input token. They follow the ratios
# in Anthropic's API price list, which hold across current models: a cache write that
# lives five minutes costs 1.25x base input, one that lives an hour 2x, a cache read
# 0.1x, an output token 5x. Weighting by price ratio is what makes a 100k-token cache
# read and a 10k-token fresh input comparable in one column.
DEFAULT_WEIGHTS = {"input": 1.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0,
                   "cache_read": 0.1, "output": 5.0}

# Where each flag fires. Defaults are set so that steady, healthy agent use stays
# quiet and each flag marks something worth opening the session for.
DEFAULT_THRESHOLDS = {
    # Sessions lighter than this are not judged on cache hit or re-reads: a short
    # session writes its whole context once and has few turns to read it back.
    "min_session_weighted": 1_000_000,
    "cache_hit_min": 0.80,
    "reread_max": 100.0,
    # A call is flagged when it is both this large and this big a part of its own
    # session. On a 1M-context model a full-context cache write passes 500k as a
    # matter of course; it is worth a look only when it dominates its session.
    "turn_max": 500_000,
    "turn_share_min": 0.10,
    # A session is flagged past this many times its fair share (1/N of the window,
    # N = sessions over min_session_weighted), and only when N is at least the
    # minimum below: among three or four sessions a big share is arithmetic.
    "session_share_factor": 2.0,
    "session_share_min_sessions": 5,
    # In a busy week twice a fair share is tiny (113 sessions put it under 2%), so a
    # session must also hold at least this share of the window.
    "session_share_min": 0.10,
}

USAGE_FIELDS = (
    ("input", "input_tokens"),
    ("cache_write_5m", "cache_creation_input_tokens"),  # split below when the record allows
    ("cache_read", "cache_read_input_tokens"),
    ("output", "output_tokens"),
)

_FRACTION = re.compile(r"\.(\d+)")
_NON_NAME = re.compile(r"[^A-Za-z0-9-]")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass
class Turn:
    """One API call, counted once however many transcript lines repeat it."""

    project: str
    session: str
    sidechain: bool
    when: datetime
    tokens: Dict[str, int]
    owner: Tuple[datetime, str, int]  # (time, file, line): the earliest copy wins

    def weighted(self, weights: Dict[str, float]) -> float:
        return sum(self.tokens[k] * weights[k] for k in self.tokens)


@dataclass
class Scan:
    turns: List[Turn] = field(default_factory=list)
    files_read: int = 0
    malformed_lines: int = 0
    malformed_files: int = 0
    unreadable_files: int = 0
    unreadable_folders: int = 0
    labels: Dict[str, str] = field(default_factory=dict)  # project dir -> readable name


def default_projects_dir() -> Path:
    """Where Claude Code keeps transcripts: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base).expanduser() / "projects" if base else Path.home() / ".claude" / "projects"


def parse_timestamp(text: str) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp into an aware datetime; a missing offset means UTC.

    Python 3.9's fromisoformat rejects a trailing Z and fractions that are not three
    or six digits long, so both are normalized first.
    """
    if not isinstance(text, str) or not text:
        return None
    text = text.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def window_bounds(end_day: date, days: int, tz: Optional[tzinfo]) -> Tuple[datetime, datetime]:
    """The window is whole calendar days in the report's time zone, ending with end_day."""
    start_day = end_day - timedelta(days=days - 1)
    start = datetime.combine(start_day, time.min)
    end = datetime.combine(end_day + timedelta(days=1), time.min)
    if tz is None:
        return start.astimezone(), end.astimezone()  # naive -> local zone, DST-aware
    return start.replace(tzinfo=tz), end.replace(tzinfo=tz)


def transcript_files(root: Path) -> Tuple[List[Path], int]:
    """Every .jsonl file under root, and how many folders could not be listed.

    os.walk rather than rglob, because rglob passes over an unreadable folder in
    silence and the report should say what it could not see.
    """
    unreadable: List[OSError] = []
    files = []
    for folder, _, names in os.walk(root, onerror=unreadable.append):
        files += [Path(folder) / n for n in names if n.endswith(".jsonl")]
    return sorted(p for p in files if p.is_file()), len(unreadable)


def _usage_tokens(usage: object) -> Optional[Dict[str, int]]:
    if not isinstance(usage, dict):
        return None
    tokens = {}
    for name, key in USAGE_FIELDS:
        value = usage.get(key, 0)
        if value is None:
            value = 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        tokens[name] = value
    # The record's total covers both cache lifetimes; the cache_creation breakdown,
    # when present, says how many were one-hour writes. Records that carry an
    # iterations list can report a total of 0 beside a real breakdown, so when the
    # breakdown adds up to more than the total, the breakdown is used.
    breakdown = usage.get("cache_creation")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    parts = []
    for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
        value = breakdown.get(key) or 0
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        parts.append(value)
    five_minute, one_hour = parts
    if five_minute + one_hour > tokens["cache_write_5m"]:
        tokens["cache_write_5m"] = five_minute
    else:
        tokens["cache_write_5m"] -= one_hour
    tokens["cache_write_1h"] = one_hour
    return tokens


def cache_writes(tokens: Dict[str, int]) -> int:
    return tokens["cache_write_5m"] + tokens["cache_write_1h"]


def _project_and_session(parts: Tuple[str, ...], path: Path, entry: dict) -> Tuple[str, str, bool]:
    """parts is the file's path relative to the transcript root, computed once per file."""
    project = parts[0]
    in_subagents = "subagents" in parts[1:-1] or path.name.startswith("agent-")
    session = entry.get("sessionId")
    if not isinstance(session, str) or not session:
        # A subagent file lives in <session-id>/subagents/, so its folder names the parent.
        session = parts[1] if in_subagents and len(parts) > 3 else path.stem
    sidechain = in_subagents or entry.get("isSidechain") is True
    return project, session, sidechain


def scan(root: Path, start: datetime, end: datetime) -> Scan:
    """Collect every API call made in [start, end) under root.

    Three properties of real transcripts shape this loop:
    - A streamed response is written as several lines that share one message id,
      and the output count grows from line to line. The copy with the largest counts
      is the final one; summing them all would overcount, keeping the first would
      undercount output.
    - A message id can also appear in more than one transcript file (most often
      subagent files). It is counted once, credited to its earliest copy.
    - A line can be cut short by a crash or be something other than JSON. It is
      counted and skipped.
    """
    result = Scan()
    by_id: Dict[str, Turn] = {}
    loose: List[Turn] = []
    floor = start.timestamp()
    files, result.unreadable_folders = transcript_files(root)
    # Transcripts directly inside root mean root is one project's folder, not the
    # folder of projects: name every file's project after root itself.
    prefix = (root.resolve().name,) if any(p.parent == root for p in files) else ()
    for path in files:
        try:
            if path.stat().st_mtime < floor:
                continue  # last written before the window opened
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:  # unreadable, or deleted since the listing
            result.unreadable_files += 1
            continue
        result.files_read += 1
        bad_before = result.malformed_lines
        parts = prefix + path.relative_to(root).parts
        with handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    result.malformed_lines += 1
                    continue
                if not isinstance(entry, dict):
                    result.malformed_lines += 1
                    continue
                project, session, sidechain = _project_and_session(parts, path, entry)
                _note_label(result.labels, project, entry.get("cwd"))
                message = entry.get("message")
                if not isinstance(message, dict) or "usage" not in message:
                    continue
                tokens = _usage_tokens(message.get("usage"))
                when = parse_timestamp(entry.get("timestamp", ""))
                if tokens is None or when is None:
                    result.malformed_lines += 1
                    continue
                if not start <= when < end:
                    continue
                turn = Turn(project, session, sidechain, when, tokens, (when, str(path), line_no))
                key = message.get("id") or entry.get("requestId")
                if not isinstance(key, str) or not key:
                    loose.append(turn)
                    continue
                _merge(by_id, key, turn)
        if result.malformed_lines > bad_before:
            result.malformed_files += 1
    result.turns = sorted(list(by_id.values()) + loose, key=lambda t: t.owner)
    return result


def _note_label(labels: Dict[str, str], project: str, cwd: object) -> None:
    """Name a project folder after the directory Claude Code was started in.

    The folder name is that directory with every character other than a letter,
    digit or dash replaced by a dash. A session can cd elsewhere, so a working
    directory that encodes to the folder name wins over the first one seen.
    """
    if not isinstance(cwd, str) or not cwd:
        return
    name = Path(cwd).name or cwd
    if _NON_NAME.sub("-", cwd) == project:
        labels[project] = name
    else:
        labels.setdefault(project, name)


def _merge(by_id: Dict[str, Turn], key: str, turn: Turn) -> None:
    held = by_id.get(key)
    if held is None:
        by_id[key] = turn
        return
    if turn.owner < held.owner:  # keep the earliest copy's place and session
        turn, held = held, turn
        by_id[key] = held
    if (turn.tokens["output"], sum(turn.tokens.values())) > (held.tokens["output"], sum(held.tokens.values())):
        held.tokens = turn.tokens  # keep the final, largest counts


def _bucket() -> dict:
    return {"turns": 0, "weighted": 0.0, "tokens": {name: 0 for name in DEFAULT_WEIGHTS}}


def _add(bucket: dict, turn: Turn, weighted: float) -> None:
    bucket["turns"] += 1
    bucket["weighted"] += weighted
    for name, count in turn.tokens.items():
        bucket["tokens"][name] += count


def cache_hit(tokens: Dict[str, int]) -> Optional[float]:
    """Share of all input tokens served from cache: read / (fresh + write + read)."""
    total_in = tokens["input"] + cache_writes(tokens) + tokens["cache_read"]
    return tokens["cache_read"] / total_in if total_in else None


def reread_ratio(tokens: Dict[str, int]) -> Optional[float]:
    """How many times each token written to the cache was read back."""
    writes = cache_writes(tokens)
    return tokens["cache_read"] / writes if writes else None


def local_day(moment: datetime, tz: Optional[tzinfo]) -> date:
    """The calendar day of moment in tz; None means the machine's zone, DST included."""
    return (moment.astimezone(tz) if tz is not None else moment.astimezone()).date()


def summarize(result: Scan, weights: Dict[str, float], end_day: date, days_in_window: int,
              tz: Optional[tzinfo]) -> dict:
    """Roll the turns up by project, session and day."""
    total = _bucket()
    projects: Dict[str, dict] = {}
    sessions: Dict[Tuple[str, str], dict] = {}
    days = {(end_day - timedelta(days=n)).isoformat(): _bucket() for n in range(days_in_window)}
    for turn in result.turns:
        weighted = turn.weighted(weights)
        _add(total, turn, weighted)
        _add(projects.setdefault(turn.project, _bucket()), turn, weighted)
        _add(days.setdefault(local_day(turn.when, tz).isoformat(), _bucket()), turn, weighted)
        session = sessions.get((turn.project, turn.session))
        if session is None:
            session = sessions[(turn.project, turn.session)] = dict(
                _bucket(), subagent_turns=0, subagent_weighted=0.0, largest_turn=0.0)
        _add(session, turn, weighted)
        session["largest_turn"] = max(session["largest_turn"], weighted)
        if turn.sidechain:
            session["subagent_turns"] += 1
            session["subagent_weighted"] += weighted
    for (project, session_id), session in sessions.items():
        session.update(project=project, session=session_id)
        projects[project].setdefault("sessions", 0)
        projects[project]["sessions"] += 1
    readable = [result.labels.get(project, project) for project in projects]
    for project, bucket in projects.items():
        label = result.labels.get(project, project)
        # Two folders with the same last path component would print identically.
        bucket.update(project=project, label=label if readable.count(label) == 1 else project)
    return {
        "total": total,
        "projects": sorted(projects.values(), key=lambda b: (-b["weighted"], b["project"])),
        "sessions": sorted(sessions.values(), key=lambda b: (-b["weighted"], b["project"], b["session"])),
        "days": [dict(bucket, day=key) for key, bucket in sorted(days.items())],
    }


def short_id(session: str) -> str:
    return session[:8]


def find_flags(report: dict, thresholds: Dict[str, float]) -> List[dict]:
    """Every threshold the window crossed, one entry per session and rule."""
    flags = []
    labels = {p["project"]: p["label"] for p in report["projects"]}

    def flag(rule: str, session: dict, value: float, limit: float, message: str) -> None:
        flags.append({"rule": rule, "project": session["project"], "label": labels[session["project"]],
                      "session": session["session"], "value": value, "limit": limit, "message": message})

    judged = [s for s in report["sessions"] if s["weighted"] >= thresholds["min_session_weighted"]]
    for session in judged:
        hit = cache_hit(session["tokens"])
        if hit is not None and hit < thresholds["cache_hit_min"]:
            flag("low-cache-hit", session, hit, thresholds["cache_hit_min"],
                 f"{percent(hit)} of input came from cache, below {percent(thresholds['cache_hit_min'])}")
    for session in judged:
        ratio = reread_ratio(session["tokens"])
        if ratio is not None and ratio > thresholds["reread_max"]:
            flag("reread-heavy", session, ratio, thresholds["reread_max"],
                 f"each cached token was read back {plural(round(ratio), 'time')}, over {thresholds['reread_max']:g}")
    for session in report["sessions"]:
        # The largest call is also the largest share, so it alone decides the rule.
        largest = session["largest_turn"]
        share = _share(largest, session["weighted"])
        if largest > thresholds["turn_max"] and share >= thresholds["turn_share_min"]:
            flag("outsized-turn", session, largest, thresholds["turn_max"],
                 f"one call weighed {human(largest)}, over {human(thresholds['turn_max'])}, "
                 f"{percent(share)} of its session")
    total = report["total"]["weighted"]
    if total and judged and len(judged) >= thresholds["session_share_min_sessions"]:
        fair = thresholds["session_share_factor"] / len(judged)
        limit = max(fair, thresholds["session_share_min"])
        reason = (f"{thresholds['session_share_factor']:g}x a fair share of {plural(len(judged), 'session')}"
                  if fair >= thresholds["session_share_min"] else "the floor")
        for session in judged:
            share = session["weighted"] / total
            if share > limit:
                # "10% ... over (10%)" reads as a contradiction; a decimal shows the gap.
                places = 1 if percent(share) == percent(limit) else 0
                flag("session-share", session, share, limit,
                     f"{percent(share, places)} of the window's weighted total, over {reason} "
                     f"({percent(limit, places)})")
    return flags


def human(count: float) -> str:
    """Token counts the way people say them: 950, 12k, 3.4M, 1.2B."""
    if count >= 1e9:
        return f"{count / 1e9:.1f}B"
    if count >= 1e6:
        return f"{count / 1e6:.1f}M"
    if count >= 1e3:
        return f"{count / 1e3:.0f}k"
    return f"{count:.0f}"


class UsageError(Exception):
    """A bad flag, config file or folder: reported in one line, exit 2."""


def load_config(path: Optional[str]) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Defaults, overridden by a JSON file of the form {"weights": {...}, "thresholds": {...}}."""
    weights, thresholds = dict(DEFAULT_WEIGHTS), dict(DEFAULT_THRESHOLDS)
    if path is None:
        return weights, thresholds
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise UsageError(f"cannot read config {path}: {exc.strerror}")
    except ValueError as exc:
        raise UsageError(f"config {path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise UsageError(f"config {path} must be a JSON object")
    for section, target in (("weights", weights), ("thresholds", thresholds)):
        values = data.pop(section, {})
        if not isinstance(values, dict):
            raise UsageError(f"config {path}: {section} must be an object")
        for key, value in values.items():
            if key not in target:
                raise UsageError(f"config {path}: unknown {section} key {key!r}")
            # json accepts NaN and Infinity; NaN compares false with everything and
            # would silently switch a flag off, so both are refused.
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise UsageError(f"config {path}: {section}.{key} must be a finite number of 0 or more")
            target[key] = value
    if data:
        raise UsageError(f"config {path}: unknown key {sorted(data)[0]!r}")
    return weights, thresholds


def build_report(root: Path, end_day: date, days: int, weights: Dict[str, float],
                 thresholds: Dict[str, float], tz: Optional[tzinfo] = None) -> dict:
    if not root.is_dir():
        raise UsageError(f"no transcript folder at {root} (set --projects-dir or CLAUDE_CONFIG_DIR)")
    try:
        start, end = window_bounds(end_day, days, tz)
    except (OverflowError, ValueError):
        raise UsageError(f"a {days}-day window ending {end_day} reaches outside the calendar")
    result = scan(root, start, end)
    report = summarize(result, weights, end_day, days, tz)
    report["flags"] = find_flags(report, thresholds)
    report["window"] = {"first_day": report["days"][0]["day"], "last_day": end_day.isoformat(), "days": days}
    report["files_read"] = result.files_read
    report["malformed"] = {"lines": result.malformed_lines, "files": result.malformed_files}
    report["unreadable_files"] = result.unreadable_files
    report["unreadable_folders"] = result.unreadable_folders
    report["weights"], report["thresholds"] = weights, thresholds
    return report


def render_text(report: dict, top: int) -> str:
    total, window = report["total"], report["window"]
    lines = [f"token-watchdog: {plural(window['days'], 'day')} to {window['last_day']}, "
             f"{plural(len(report['projects']), 'project')}, {plural(len(report['sessions']), 'session')}, "
             f"{plural(total['turns'], 'call')}"]
    if not total["turns"]:
        lines.append("no API calls in this window")
        return "\n".join(lines + _skipped_notes(report)) + "\n"
    t, grand = total["tokens"], total["weighted"]
    sub = sum(s["subagent_weighted"] for s in report["sessions"])
    lines += [
        f"weighted {human(grand)} input-equivalent (fresh {human(t['input'])}, cache write "
        f"{human(cache_writes(t))}, cache read {human(t['cache_read'])}, output {human(t['output'])})",
        f"cache hit {percent(cache_hit(t))}, subagents {percent(_share(sub, grand))} of the weighted total",
        "", "by project",
    ]
    width = max(len(p["label"]) for p in report["projects"])
    for p in report["projects"]:
        lines.append(f"  {human(p['weighted']):>7}  {percent(_share(p['weighted'], grand)):>4}  {p['label']:<{width}}"
                     f"  {plural(p['sessions'], 'session')}")
    lines += ["", "by day"]
    peak = max(d["weighted"] for d in report["days"])
    for d in report["days"]:
        bar = "#" * round(20 * d["weighted"] / peak) if peak else ""
        weekday = WEEKDAYS[date.fromisoformat(d["day"]).weekday()]  # not strftime: locale-free
        lines.append(f"  {d['day']} {weekday}  {human(d['weighted']):>7}  {bar}".rstrip())
    lines += ["", f"top sessions ({min(top, len(report['sessions']))} of {len(report['sessions'])})"]
    labels = {p["project"]: p["label"] for p in report["projects"]}
    for s in report["sessions"][:top]:
        hit, ratio = cache_hit(s["tokens"]), reread_ratio(s["tokens"])
        lines.append(
            f"  {human(s['weighted']):>7}  {percent(_share(s['weighted'], grand)):>4}  {labels[s['project']]:<{width}}"
            f"  {short_id(s['session'])}  {s['turns']:>4} {'call ' if s['turns'] == 1 else 'calls'}"
            f"  hit {percent(hit):>4}"
            f"  reread {'-' if ratio is None else format(ratio, '.0f') + 'x':>4}"
            f"  subagents {percent(_share(s['subagent_weighted'], s['weighted']))}")
    lines.append("")
    flags = report["flags"]
    if flags:
        lines.append("flags")
        rule_width = max(len(f["rule"]) for f in flags)
        for f in flags:
            lines.append(f"  {f['rule']:<{rule_width}}  {f['label']} {short_id(f['session'])}")
            lines.append(f"  {'':<{rule_width}}  {f['message']}")
        lines.append("")
    lines += _skipped_notes(report)
    flagged = len({(f["project"], f["session"]) for f in flags})
    lines.append(f"FLAGGED: {plural(len(flags), 'flag')} in {plural(flagged, 'session')}"
                 if flags else "CLEAN: no flags")
    return "\n".join(lines) + "\n"


def percent(share: Optional[float], decimals: int = 0) -> str:
    """Round down, so a cache that missed even once never reads as 100%."""
    if share is None:
        return "-"
    scale = 10 ** decimals
    # The epsilon absorbs float error: 0.29 * 100 is 28.999999999999996.
    return f"{math.floor(share * 100 * scale + 1e-9) / scale:.{decimals}f}%"


def _share(part: float, whole: float) -> float:
    """part / whole, where a window of zero-usage calls has a whole of 0."""
    return part / whole if whole else 0.0


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _skipped_notes(report: dict) -> List[str]:
    notes = []
    bad = report["malformed"]
    if bad["lines"]:
        notes.append(f"skipped {plural(bad['lines'], 'malformed line')} in {plural(bad['files'], 'file')}")
    if report["unreadable_files"]:
        notes.append(f"skipped {plural(report['unreadable_files'], 'unreadable file')}")
    if report["unreadable_folders"]:
        notes.append(f"skipped {plural(report['unreadable_folders'], 'unreadable folder')}")
    return notes


def render_json(report: dict) -> str:
    def rounded(value: object) -> object:
        if isinstance(value, float):
            return round(value, 4) if value < 1 else round(value, 1)
        if isinstance(value, dict):
            return {k: rounded(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rounded(v) for v in value]
        return value

    out = dict(report, tool="token-watchdog", version=__version__)
    out["total"] = dict(report["total"], cache_hit=cache_hit(report["total"]["tokens"]))
    out["sessions"] = [dict(s, cache_hit=cache_hit(s["tokens"]), reread_ratio=reread_ratio(s["tokens"]))
                       for s in report["sessions"]]
    return json.dumps(rounded(out), indent=2) + "\n"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # one line on stderr, exit 2, no usage dump
        self.exit(2, f"{self.prog}: {message}\n")


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a whole number of 1 or more, got {text!r}")
    return value


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a date as YYYY-MM-DD, got {text!r}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _Parser(
        prog="token-watchdog",
        description="Audit Claude Code session logs for wasteful token use. "
                    "Exit 0 when no flag fires, 1 when one does, 2 on a usage or config error.")
    parser.add_argument("--days", type=_positive_int, default=7, help="calendar days in the window (default 7)")
    parser.add_argument("--now", type=_day, metavar="YYYY-MM-DD",
                        help="last day of the window, in local time (default today)")
    parser.add_argument("--projects-dir", metavar="DIR",
                        help="transcript folder (default $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)")
    parser.add_argument("--config", metavar="FILE", help="JSON file overriding weights and thresholds")
    parser.add_argument("--top", type=_positive_int, default=5, help="sessions to list (default 5)")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    try:
        weights, thresholds = load_config(args.config)
        root = Path(args.projects_dir).expanduser() if args.projects_dir else default_projects_dir()
        report = build_report(root, args.now or date.today(), args.days, weights, thresholds)
        text = render_json(report) if args.json else render_text(report, args.top)
    except UsageError as exc:
        print(f"{parser.prog}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # exit 1 means "a flag fired", so a crash must never produce it
        print(f"{parser.prog}: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    code = 1 if report["flags"] else 0
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader (head, a closed pager) left early. That is not an error of this
        # run: point stdout at devnull so the interpreter's own flush at exit stays
        # quiet, and keep the run's exit code.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return code
