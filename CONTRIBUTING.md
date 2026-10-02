# Contributing

Useful contributions:

- Transcript entries the scan reads wrong. Claude Code's log format is
  not documented and changes; a redacted line that miscounts (numbers
  kept, text removed) is the most useful bug report there is.
- New flags, if the waste they catch is measurable from the usage
  records alone and the threshold is a named default that a config file
  can override.
- Better default thresholds, argued from measured sessions rather than
  taste.
- Fixes to anything the README claims that turns out not to be true.

Ground rules: the package stays standard-library only and runs on
Python 3.9. Every flag has a test that fires it and one that keeps it
quiet. Test fixtures are invented; never commit a real transcript, even
a redacted one, since prompts and file contents live in the same lines
as the usage. Keep `python3 -m unittest discover -s tests` green. If
you change what the demo prints, regenerate the record with
`UPDATE_DEMO_TRANSCRIPT=1 python3 -m unittest discover -s tests` and
update the README blocks to match.
