# token-watchdog

Reads the session logs Claude Code keeps on your machine and tells you which sessions and projects burned tokens wastefully, and why: a cache that kept missing, a context read back hundreds of times, one call that cost as much as a whole afternoon. It never calls a model, so the audit itself costs nothing, and it exits 1 when something needs a look, so it can run from cron or CI.
