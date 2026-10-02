"""Builds synthetic Claude Code transcripts in a temporary directory.

Every name, path and number here is invented. The entry shape matches what Claude
Code writes: usage sits on assistant entries under message.usage.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def assistant(msg_id: str, when: str, session: str, inp: int = 0, write: int = 0,
              read: int = 0, out: int = 0, sidechain: bool = False,
              cwd: str = "/home/dev/demo-app") -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "isSidechain": sidechain,
        "cwd": cwd,
        "timestamp": when,
        "requestId": "req_" + msg_id,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
                "output_tokens": out,
            },
        },
    }


def user(when: str, session: str) -> dict:
    return {"type": "user", "sessionId": session, "timestamp": when,
            "message": {"role": "user", "content": "synthetic prompt"}}


def write_log(root: Path, project: str, name: str, entries: list,
              subagent_of: Optional[str] = None, raw_lines: tuple = ()) -> Path:
    """Write entries as JSONL; raw_lines are appended verbatim (for malformed input)."""
    folder = root / project
    if subagent_of:
        folder = folder / subagent_of / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    lines = [json.dumps(e) for e in entries] + list(raw_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
