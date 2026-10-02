# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-10-02

### Added

- `token-watchdog` command that audits Claude Code session logs without calling a model.
- One weighted total in input-equivalent tokens, broken down by project, session and local day, with one-hour cache writes weighted at 2 and five-minute writes at 1.25.
- Four flags with configurable thresholds: `low-cache-hit`, `reread-heavy`, `outsized-turn` and `session-share`.
- `session-share` fires for a session above twice its fair share and above 10% of the window, once five sessions pass the size floor.
- Exit code 1 when a flag fires, 0 when none does and 2 on a usage, configuration or unexpected error, for cron and CI.
- `--json` output carrying the full report, including every session's cache-hit share and re-read ratio.
- `--days`, `--now`, `--top` and `--projects-dir` options; `--projects-dir` also takes one project's folder, and `CLAUDE_CONFIG_DIR` is honored.
- JSON config file for weights and thresholds, with unknown keys and non-finite numbers rejected.
- Streamed replies, and messages that appear in more than one transcript file, are counted once.
- Subagent usage is credited to the session that started it.
- Malformed transcript lines and unreadable files and folders are counted, skipped and reported.
