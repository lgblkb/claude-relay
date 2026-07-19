"""The single place the wall-hit decision lives (DESIGN.md §2/§6). `gadkit.outcome()` produces
a disk-plus-live-usage bucket; `classify()` here is the ONE function that turns that bucket
into an `Action` the loop acts on, using the run's stdout tail ONLY as a backstop for the one
genuinely ambiguous case (an `AGENT_DEAD_NONLIMIT` bucket where the live usage reading was
itself unavailable, e.g. the post-run poll hit a network error) — never as the primary signal
(Invariant #2: disk + the usage endpoint decide; stdout is a backstop only).
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .usage import UsageSnapshot

# Action kinds the loop understands. Mirrors DESIGN.md §3's `handle(action)` comment exactly:
#   PROGRESSED           -> CONTINUE
#   HIT_WALL             -> CONTINUE_ROTATE   (cooldown already recorded; next pick_seat rotates)
#   AWAITING_HUMAN/BLOCKED -> NOTIFY_PARK
#   AGENT_DEAD_NONLIMIT  -> RETRY (or CONTINUE_ROTATE if the tail backstop overrides it)
#   NO_BACKLOG           -> DONE
CONTINUE = "CONTINUE"
CONTINUE_ROTATE = "CONTINUE_ROTATE"
NOTIFY_PARK = "NOTIFY_PARK"
RETRY = "RETRY"
DONE = "DONE"

# Case-insensitive substrings in stdout that plausibly indicate a usage/rate-limit wall was
# hit, for the backstop-only path above. Deliberately narrow and easy to extend — a false
# negative here just means one extra RETRY cycle before the next iteration's live usage poll
# catches the real state; a false positive just rotates one iteration early.
#
# The second group is gad-run.js's OWN limit-probe vocabulary, verified against the bundled
# workflow source (gad-kit 1.5.0, gad-run.js:120-149): when its post-death "pong" liveness probe
# also fails it sets status `LIMIT-SUSPECTED` and logs "usage-limit/platform outage" /
# "spending into a closed window". Those are the exact strings that reach our stdout tail, and
# NONE matched the generic first group ("usage-limit" is hyphenated, unlike "usage limit") — so
# without them the backstop silently missed gad-run's own diagnosis when the live endpoint was
# unreadable post-run. See uncertainty-ledger.jsonl (now resolved for these).
#
# Deliberately NOT included (reviewer finding: too broad, high false-positive risk in a
# 15-20-agent generation's own stdout): bare "429" (could be a line number, port, file name, or
# an unrelated HTTP call inside an agent's own Bash output) and "resets_at" (a plain JSON field
# name that can appear in totally unrelated debug/log output, e.g. an agent printing a fixture).
_LIMIT_SIGNATURES = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "5-hour limit",
    "usage_limit",
    "quota exceeded",
    # gad-run.js limit-probe markers (verified against the bundled workflow source):
    "limit-suspected",
    "usage-limit",
    "closed window",
)


@dataclasses.dataclass(frozen=True)
class Action:
    kind: str
    reason: str = ""


def tail_has_limit_signature(tail: list[str]) -> bool:
    """Backstop-only check: does the run's stdout tail contain a plausible usage/rate-limit
    signature? Never used to directly classify PROGRESSED/AWAITING_HUMAN/BLOCKED — only to
    resolve the HIT_WALL-vs-AGENT_DEAD-NONLIMIT ambiguity when the live usage endpoint itself
    could not be read after the run.
    """
    joined = "\n".join(tail).lower()
    return any(sig in joined for sig in _LIMIT_SIGNATURES)


def classify(outcome: str, usage: UsageSnapshot | None, tail: list[str]) -> Action:
    """Map a `gadkit.outcome()` bucket (+ the fresh usage reading + stdout tail backstop) to
    the one `Action` the loop takes next. This is the ONLY place that decision is made —
    nothing else in this package re-implements it (DESIGN.md §2).
    """
    if outcome == "PROGRESSED":
        return Action(CONTINUE, "new commit landed")
    if outcome == "HIT_WALL":
        return Action(CONTINUE_ROTATE, "active usage near cap — cooldown recorded, rotating")
    if outcome == "AWAITING_HUMAN":
        return Action(NOTIFY_PARK, "gated on an open owner decision")
    if outcome == "BLOCKED":
        return Action(NOTIFY_PARK, "consolidator left a handoff")
    if outcome == "NO_BACKLOG":
        return Action(DONE, "backlog exhausted")
    if outcome == "AGENT_DEAD_NONLIMIT":
        if usage is None and tail_has_limit_signature(tail):
            return Action(
                CONTINUE_ROTATE,
                "usage endpoint unreadable post-run, but stdout tail shows a limit signature "
                "(backstop signal, Invariant #2) — treating as a wall-hit rather than a dead agent",
            )
        return Action(RETRY, "no progress and the seat is not near its cap — transient failure")
    return Action(RETRY, f"unrecognized outcome {outcome!r} — retrying defensively")
