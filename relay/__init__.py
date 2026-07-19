"""claude-relay: an external supervisor that rotates a long-running, headless Claude Code
workload (currently: gad-kit generational runs) across authenticated "seats" (distinct
Anthropic accounts / CLAUDE_CONFIG_DIRs) as each seat's real 5-hour usage window fills up.

Package layout (see DESIGN.md, the authoritative spec, for the full rationale):
    fleet.py     seat discovery (~/.claude-* minus excludes) + credential state
    usage.py     Anthropic /api/oauth/usage client, limits[] parsing, rotation thresholds
    cooldown.py  ~/.claude-relay/state.json (atomic), cooldown math from real resets_at
    runner.py    spawns `claude -p ...` against a chosen seat, streams + tails output
    gadkit.py    the ONLY module that knows gad-kit's disk shape: triage/command/snapshot/outcome
    detector.py  classify(outcome, usage, tail) -> Action; the single wall-hit decision locus
    notify.py    notify-out sinks (telegram/stdout/command/webhook) + resolve-in (durable)
    config.py    config.toml + CLI merge; no hardcoded paths
    loop.py      the rotation loop that wires all of the above together

This package is stdlib-only. Nothing outside `usage.py`/`notify.py` performs network I/O, and
nothing outside `runner.py` spawns a subprocess. Never log or persist an OAuth access token.
"""

__version__ = "0.1.0"
