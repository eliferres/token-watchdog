# Changelog

All notable changes to this project are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-10-02

### Added

- `token-watchdog` command that audits Claude Code session logs without calling a model.
- One weighted total in input-equivalent tokens, broken down by project, session and local day.
- Four flags with configurable thresholds: `low-cache-hit`, `reread-heavy`, `outsized-turn` and `session-share`.
- Exit code 1 when a flag fires, 0 when none does and 2 on a usage, configuration or unexpected error, for cron and CI.
- `--json` output carrying the full report, including every session's cache-hit share and re-read ratio.
- `--days`, `--now`, `--top` and `--projects-dir` options; `CLAUDE_CONFIG_DIR` is honored.
- JSON config file for weights and thresholds, with unknown keys and non-finite numbers rejected.
- Streamed replies, and messages that appear in more than one transcript file, are counted once.
- Subagent usage is credited to the session that started it.
- Malformed transcript lines and unreadable files are counted, skipped and reported.
