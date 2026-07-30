"""The single place the wall-hit decision lives (DESIGN.md §2/§6). `gadkit.outcome()` produces
a disk-plus-live-usage bucket; `classify()` here is the ONE function that turns that bucket
into an `Action` the loop acts on, consulting the run's stdout tail only for the `AGENT_DEAD_
NONLIMIT` bucket — never as the primary signal (Invariant #2: disk + the usage endpoint decide;
stdout is a backstop only).

Invariant #2 is NARROWED, not abandoned, in exactly two places, both genuinely structured
platform data rather than model prose:
  1. A `rate_limit_event` NDJSON envelope (see the "NDJSON tail" section below) — Anthropic's
     own CLI reporting a real rate-limit reading mid-run.
  2. gad-run.js's own limit probe, reported via the model's final `RESULT:` line (see
     `_extract_gad_result_status()`) — a diagnosis gad-kit reached by making a SECOND agent
     call and watching it die too, so it is a platform-level observation of the same kind as
     our usage poll, not model prose.
Both are authoritative even when the usage reading succeeded, because a platform-side limit (or
outage) can leave the seat's own `percent` far below its ceiling. Everything else in the tail
(the generic `_GENERIC_LIMIT_SIGNATURES`) remains strictly backstop-only: consulted solely when
the live usage reading was itself unavailable.

──────────────────────────────────────────────────────────────────────────────────────────────
NDJSON tail (2026-07-26 rewrite — "Blocker 1"): `gadkit.command()` invokes the child with
`--output-format stream-json --verbose`, which is **NDJSON**: one compact JSON object per
physical stdout line — never plain text. `runner.py` puts each RAW line into `RunResult.tail`
verbatim (it stays dumb on purpose; parsing lives here). Before this rewrite, every function in
this module matched literal substrings/regexes against those raw lines — e.g. `^\\s*RESULT\\s*:`
against `tail` itself. That can never match in production: a genuine transcript line looks like
`{"type":"assistant","message":{...,"content":[{"type":"text","text":"...RESULT: ...\\n"}]}, ...}`
— the model's own line-oriented text (including its prompted `RESULT: <status>` line) only
exists as a JSON-escaped SUBSTRING of an envelope's `message.content[].text` field, never as a
bare tail entry. Confirmed by running a real `claude -p --output-format stream-json --verbose`:
zero raw lines begin with `RESULT:`; every line begins with `{`. So the entire limit-detection
chain built on that premise was dead code — it could never fire outside a test fixture built
from the same false premise.

Fix: `_iter_envelopes()` best-effort `json.loads()`s each tail line (skipping ones that don't
parse — a partial/truncated line at the very end of a killed process, say). Everything else in
this module that needs actual text (the RESULT: line, the workflow/generic limit vocabularies)
now draws it from `_all_assistant_text()` — the DECODED `assistant`-envelope text, re-split on
real `\\n` characters — never from the raw tail lines directly.

Three envelope types observed live (2026-07-26 probe against a real `claude` 2.1.220 session) and
now specifically decoded:
  - `assistant`: `{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}` —
    the model's own output, including the final `RESULT:` line `gadkit.command()`'s Step 3
    instructs it to print.
  - `rate_limit_event`: `{"type":"rate_limit_event","rate_limit_info":{"status":
    "allowed_warning","resetsAt":1785290400,"rateLimitType":"seven_day","utilization":0.76,
    "isUsingOverage":false,"surpassedThreshold":0.75}, "uuid":"...","session_id":"..."}` — a
    first-class structured limit reading, pushed mid-run independent of anything the model says.
  - `result` (subtype `success`): `{"type":"result","subtype":"success","result":"...",
    "is_error":false,"num_turns":3,"duration_ms":1234,...}` — the terminal envelope's OWN flat
    `result` string field carries the same `RESULT:` line text a good `assistant` envelope would
    (2026-07-26 follow-up fix: an earlier version of this docstring listed this envelope's other
    keys — `api_error_status`, `is_error`, `num_turns`, `modelUsage`, `duration_ms` — but missed
    `result` itself, so `_result_line_content()` used to only ever look at decoded `assistant`
    text; `_terminal_result_text()` now decodes this field too and is preferred when present,
    since it is the run's own designated final-answer field rather than an arbitrary assistant
    turn — see `_result_line_content()`). `result.modelUsage`/`api_error_status` remain the
    structured token-cost fields DESIGN.md §4 names as Phase 2 batching's future calibration
    input (recorded there, not built here — see DESIGN.md's dated note).
Also observed: `system` (subtypes `hook_started`/`hook_response`/`init`) — not consulted here.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

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

# GENERIC, ambient limit vocabulary: case-insensitive substrings in stdout that plausibly indicate
# a usage/rate-limit wall was hit, but which any harness/API error text (or an agent quoting one)
# can produce. These stay STRICTLY backstop-only — consulted solely when the live usage poll itself
# failed — because they are prose and must never override a good live reading (Invariant #2).
# Deliberately narrow and easy to extend: a false negative here just means one extra RETRY cycle
# before the next iteration's live usage poll catches the real state.
#
# Deliberately NOT included (reviewer finding: too broad, high false-positive risk in a
# 15-20-agent generation's own stdout): bare "429" (could be a line number, port, file name, or
# an unrelated HTTP call inside an agent's own Bash output) and "resets_at" (a plain JSON field
# name that can appear in totally unrelated debug/log output, e.g. an agent printing a fixture).
_GENERIC_LIMIT_SIGNATURES = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "5-hour limit",
    "usage_limit",
    "quota exceeded",
)

# gad-run.js's OWN limit-probe vocabulary, verified against the bundled workflow source (gad-kit
# 2.0.0, `probeLimit` at gad-run.js:135-144): after retry exhaustion it fires a 1-line "pong"
# liveness agent, and if THAT also dies it sets `LIMIT_SUSPECTED` and logs "usage-limit/platform
# outage ... (LIMIT-SUSPECTED); stopping the crawl rather than spending into a closed window"
# (gad-run.js:140); once the flag is set, every subsequent null agent logs the same "closed
# window" phrasing at gad-run.js:151 ("not burning further retries into a closed window"). None of
# these match the generic set above ("usage-limit" is hyphenated, unlike "usage limit").
#
# These are treated as AUTHORITATIVE — enough to rotate EVEN WHEN the usage reading succeeded —
# and that is a deliberate NARROWING of Invariant #2, not an abandonment of it (see module
# docstring).
_WORKFLOW_LIMIT_SIGNATURES = (
    "limit-suspected",
    "usage-limit",
    "closed window",
)

# The union, kept as one name because `tail_has_limit_signature()` (public; used by tests and any
# other caller) keeps its original "any plausible limit signature" meaning.
_LIMIT_SIGNATURES = _GENERIC_LIMIT_SIGNATURES + _WORKFLOW_LIMIT_SIGNATURES

# The `status` field value gad-kit's own invoked script returns at the TOP LEVEL (E9 audit
# finding; `gadkit.command()`'s Step 3 instructs the model to print
# `RESULT: <the workflow's returned status field>` verbatim as its terminal line). Handled
# EXHAUSTIVELY: an unrecognized value must never be treated as a success signal (only ever used
# to decide RETRY-vs-park within the AGENT_DEAD_NONLIMIT ambiguity, never to override disk truth)
# — see `_extract_gad_result_status()`/`classify()`.
#
# WHICH script's status field this is depends on `gadkit.Plan.mode`, and it is NOT always
# gad-run.js despite this set's original name: a `gad_run`-mode invocation's RESULT line is
# gad-run.js's own `stopReason`/early-return status, but a `gad_generation`- or `gad_finish`-mode
# invocation (claude-relay's two RECOVERY tails, `triage()`'s FINISH/restart plans) calls
# gad-generation.js/gad-finish.js DIRECTLY — so its RESULT line is THEIR OWN top-level `status`
# instead (their `CONSOLIDATE_SCHEMA.status` enum, or their `deadAgentAbort()`'s `abortStatus`, or
# gad-generation.js's own preflight `AWAITING-OWNER`). `AGENT-DEAD`/`AWAITING-OWNER`/`COMMITTED`/
# `BLOCKED` below were missing from this set until the `InstalledGadKitStatusVocabularyContract-
# Tests` contract test (tests/test_gadkit.py, 2026-07-26) scraped the installed workflows/*.js and
# caught it — the exact class of drift this module's docstring warns has bitten silently 3 times
# before. None of the four need special `no_retry` handling (unlike DIRTY-TREE/REFUSED): a real
# COMMITTED/BLOCKED reaching this code at all means gad-run's own AGENT_DEAD_NONLIMIT bucket
# already disagrees with what the model claims (disk showed no new commit/handoff even though the
# model says it made one) — Invariant #2 already treats that as untrustworthy and safe to retry,
# which the plain (non-`no_retry`) RETRY branch below already does correctly.
_GAD_RUN_RESULT_STATUSES = frozenset(
    {
        "DIRTY-TREE",
        "SURVEY-FAILED",
        "LIMIT-SUSPECTED",
        "NO-PROGRESS",
        "BACKLOG-EXHAUSTED",
        "MAX-GENS-REACHED",
        "BUDGET-EXHAUSTED",
        "COMPLETED-BATCH",
        "IDEATED",
        "IDEATION-FAILED",
        # gad-kit's uncommitted 2.1.0 work (ship-blocker 2, 2026-07-26): gad-finish.js now
        # MECHANICALLY REFUSES to resume a generation whose reviews/adversarial-review.md is
        # missing (`status: 'REFUSED'`, no commit, no fix-loop spend) — the tail considers
        # itself the wrong route for that generation. See `classify()`'s `no_retry` handling and
        # `gadkit.artifact_census()`'s `verify_or_later` (which now ALSO requires
        # adversarial-review.md, the defense-in-depth fix that prevents this from ever being
        # reached in the first place).
        "REFUSED",
        # gad-generation.js's/gad-finish.js's OWN top-level status values (`deadAgentAbort()`'s
        # `abortStatus`, `CONSOLIDATE_SCHEMA.status`) — reachable ONLY for `gad_generation`/
        # `gad_finish`-mode invocations (see the set's docstring above), not gad-run.js's crawl.
        "AGENT-DEAD",
        "COMMITTED",
        "BLOCKED",
        # `gad-generation.js` only (its own preflight abort when an owner decision blocks the
        # generation it was asked to (re)run) — reachable in principle if claude-relay's own
        # `gadkit.blocking_decisions()` preflight gate and gad-generation.js's internal one ever
        # disagree on what counts as "still open."
        "AWAITING-OWNER",
        # gad-kit's soft usage ceiling (2026-07-30). `PAUSED` is gad-generation.js's/gad-finish.js's
        # own top-level status when a phase-boundary gate fired — or when runAgent() caught a hard
        # `budget` throw and reported it honestly rather than as a dead agent. `PAUSED-ON-BUDGET` is
        # gad-run.js's `stopReason` for the same event one level up. Neither is a failure: the
        # generation stopped ITSELF, with its artifacts written, and is resumable.
        # `GENERATION-THREW` is gad-run.js's new guard around `await workflow(...)` — a child
        # generation threw and the crawl reported it with earlier generations' accounting intact
        # instead of letting the throw destroy the whole batch.
        "PAUSED",
        "PAUSED-ON-BUDGET",
        "GENERATION-THREW",
    }
)

# The subset above meaning "this SEAT is spent but the WORK is healthy". Never retry these on the
# same seat: the pause fired precisely because headroom ran out, so an immediate retry re-hits the
# identical gate at the identical place, makes no progress, and burns the retry budget into a
# HARD_ERROR park — the exact outcome the soft ceiling exists to prevent.
#
# `BUDGET-EXHAUSTED` belongs here even though it predates this feature, and its inclusion is a FIX,
# not tidying. It is gad-run's own "I cannot afford to START the next generation" exit, which is
# semantically a pause — but until the soft ceiling landed it was unreachable dead code, because the
# gate behind it read `budget.total && budget.remaining() < perGenTokens` and `budget.total` is
# permanently null (see Config.tokens_per_percent). Rebuilding that gate on `budget.spent()` made it
# live for the first time, and as a bare member of `_GAD_RUN_RESULT_STATUSES` it classified as an
# ordinary RETRY: no cooldown (the `_force_cooldown` in run_once is keyed on CONTINUE_ROTATE), so
# pick_seat re-selects the same seat — whose percent has barely moved, since the run exited before
# doing any work — it exhausts again immediately, and three iterations later the HARD_ERROR breaker
# parks the whole repo. A healthy budget stop must never look like a crash loop.
_PAUSED_RESULT_STATUSES = frozenset({"PAUSED", "PAUSED-ON-BUDGET", "BUDGET-EXHAUSTED"})

# The model's own terminal line is `RESULT: <status>` (gadkit.command() Step 3, verbatim).
_RESULT_LINE_RE = re.compile(r"^\s*RESULT\s*:\s*(.*)$", re.IGNORECASE)
# The status token itself: leading run of identifier-ish characters, everything after (a colon,
# a trailing explanation) is not part of the token.
_RESULT_STATUS_TOKEN_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)")

# `rate_limit_event.rate_limit_info.status` values confirmed NON-limiting by live observation.
#
# 2026-07-26 probe: {"status":"allowed_warning","rateLimitType":"seven_day","utilization":0.76,
#                    "surpassedThreshold":0.75}
# 2026-07-27 probe: {"status":"allowed","rateLimitType":"five_hour","resetsAt":1785105000,
#                    "overageStatus":"rejected","overageDisabledReason":"org_level_disabled",
#                    "isUsingOverage":false}          <-- note: NO utilization field at all
#
# `allowed` was MISSING here until 2026-07-27, and its absence was a live bug rather than a
# theoretical gap. `allowed` is the ordinary, healthy, nothing-is-wrong status, so
# `_rate_limit_event_action()` classified it as "UNRECOGNIZED" and returned CONTINUE_ROTATE with a
# forced cooldown until the event's `resetsAt` (loop.py's `_force_cooldown`).
#
# SCOPE, stated precisely because the first write-up of this overstated it: `classify()` consults
# this function ONLY inside the `outcome == "AGENT_DEAD_NONLIMIT"` branch. PROGRESSED, HIT_WALL,
# AWAITING_HUMAN, BLOCKED and NO_BACKLOG all return earlier, so ordinary successful runs were never
# affected and the fleet did not stall on the happy path.
#
# What it DID do is convert every non-limit agent death — a crash, a hang, a timeout, a refusal —
# into a multi-hour seat outage: the run died for an unrelated reason, an `allowed` event was
# present as it almost always is, and the seat was cooled until its window reset instead of being
# retried. With a two-seat fleet, two unrelated crashes park everything for hours. Crash
# amplification, not immediate stall.
#
# And note what makes it a true inversion: the whole purpose of that branch is to distinguish "died
# because of a limit" from "died for some other reason." An `allowed` event is positive evidence of
# NOT-limit. Reading it as a limit did not merely fail to help, it flipped the signal's meaning.
#
# The mistake was reasoning that "unknown -> assume limited" is the conservative direction. It is
# not, and Invariant #2 is why: disk state and the usage endpoint are the PRIMARY deciders, and the
# endpoint's own `ceiling_pct` already catches real limits. This event is a supplementary signal, so
# a false positive here actively breaks rotation while a false negative merely defers to the
# mechanism that was already authoritative. Costs are asymmetric in the opposite direction to what
# the original comment assumed.
#
# Hence `_is_safe_rate_limit_status()` below also treats any `allowed*` status as safe: the prefix
# means the platform ALLOWED the request, which cannot simultaneously be a denial. A future
# `allowed_final_warning` must not stall the fleet on first sight.
_KNOWN_SAFE_RATE_LIMIT_STATUSES = frozenset({"allowed", "allowed_warning"})

# Prefix rule backing the reasoning above. Kept separate from the exact-match set so the set stays a
# literal record of what has actually been SEEN, while this encodes the semantic generalization.
_SAFE_RATE_LIMIT_STATUS_PREFIX = "allowed"


def _is_safe_rate_limit_status(status: str) -> bool:
    """True when `status` is known or structurally certain not to be a denial.

    Exact observations first, then the `allowed*` prefix — a status asserting the request was
    allowed is not a report that it was blocked, whatever suffix follows.
    """
    return status in _KNOWN_SAFE_RATE_LIMIT_STATUSES or status.startswith(_SAFE_RATE_LIMIT_STATUS_PREFIX)

# `utilization` is a 0..1 FRACTION (observed 0.76-0.99), NOT a percent. Even a KNOWN-safe status
# ("allowed_warning" literally means "the request was allowed") is still usable as a signal on its
# own once utilization is high enough.
#
# CALIBRATED 2026-07-27 by a Tier-2 burn (see relay/ratelimit_probe.py) that drove one seat's
# five-hour window from 84% to 100%, capturing 42 events across 42 successful calls:
#
#   * 0.9 is a REAL platform threshold, not just our guess. Every event carrying `utilization` also
#     carried `surpassedThreshold: 0.9`. The hand-picked value happened to match the platform's own.
#   * BUT `utilization` is ABSENT below 0.9 — 13 `allowed` events had no `utilization` field at all,
#     then 29 `allowed_warning` events carried 0.90 → 0.99. So `utilization >= 0.9` fires on the
#     very FIRST event that carries the field, which makes this threshold operationally equivalent
#     to `utilization is not None`. The number does no work beyond a presence check; lowering it
#     would change nothing, and raising it is the only edit with any effect.
#   * `surpassedThreshold` can be absent even when `utilization` is present (2 events at exactly
#     0.90 had no `surpassedThreshold`), so it must never be relied on as a presence proxy.
#   * utilization peaked at 0.99 and NEVER reached 1.0, and all 42 calls succeeded. Being past this
#     threshold does not mean requests are being denied — see `_KNOWN_SAFE_RATE_LIMIT_STATUSES`
#     above for why erring toward "rotate" is not the safe direction it appears to be.
#
# Kept at 0.9 anyway: it matches the platform's own warning threshold, and claude-relay's synthetic
# `ceiling_pct` (default 70) rotates far earlier via the usage endpoint regardless, so this is a
# backstop rather than the primary gate.
_RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD = 0.9


@dataclasses.dataclass(frozen=True)
class Action:
    kind: str
    reason: str = ""
    # ISO-8601 UTC string, when a genuinely structured reset time is known (from a
    # `rate_limit_event`'s `resetsAt`) — `run_once()` feeds this into `_force_cooldown()` instead
    # of the blind `_TIMEOUT_COOLDOWN_S` guess. `None` means "no real reset time available."
    resets_at: str | None = None
    # True iff retrying is KNOWN to be pointless (gad-run's own RESULT said it refused to even
    # start, e.g. DIRTY-TREE) — `loop.run()` trips the HARD_ERROR breaker on the first occurrence
    # instead of wasting `_MAX_CONSECUTIVE_AGENT_DEAD` cycles first.
    no_retry: bool = False
    # True iff gad-kit's own soft usage ceiling fired (RESULT: PAUSED / PAUSED-ON-BUDGET). This is
    # deliberately ORTHOGONAL to `kind`, because a pause can accompany EITHER bucket:
    #
    #   * `AGENT_DEAD_NONLIMIT` — the generation paused before committing anything (kind becomes
    #     CONTINUE_ROTATE), and
    #   * `PROGRESSED` — gad-run committed one or more generations and THEN paused on the next
    #     one (kind stays CONTINUE, because a commit did land and that is real progress).
    #
    # The second case is why this cannot be inferred from `kind` alone. Both cases mean the SAME
    # thing about the seat: its headroom is gone, so `run_once()` must cool it off, and
    # `loop.run()` must NOT count the rotation toward the HARD_ERROR breaker — a pause is the
    # mechanism working, and several seats pausing in a row is the expected steady state near a
    # fleet-wide ceiling, not a crash loop.
    paused: bool = False


@dataclasses.dataclass(frozen=True)
class RateLimitSignal:
    """One decoded `rate_limit_event` envelope's `rate_limit_info` payload — genuine structured
    platform data, not model prose (see module docstring)."""

    status: str | None
    rate_limit_type: str | None
    utilization: float | None
    resets_at: int | None  # unix epoch seconds, exactly as the CLI reports it
    surpassed_threshold: float | None
    raw: dict[str, Any]


# ─────────────────────────────────────────────────────────────────────────────
# NDJSON decoding
# ─────────────────────────────────────────────────────────────────────────────


def _iter_envelopes(tail: list[str]) -> Iterator[dict[str, Any]]:
    """Best-effort `json.loads()` of every tail line, yielding only the ones that parse to a
    JSON object. A line that fails to parse (a partial/truncated final line from a killed
    process, stderr interleaved via `stderr=STDOUT`, anything else non-JSON) is silently
    skipped — this module has no authoritative use for non-JSON lines at all.
    """
    for line in tail:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            yield obj


def _assistant_text_blocks(tail: list[str]) -> list[str]:
    """Every `text` content block's string, from every `assistant` envelope, in tail order."""
    blocks: list[str] = []
    for env in _iter_envelopes(tail):
        if env.get("type") != "assistant":
            continue
        message = env.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    blocks.append(text)
    return blocks


def _all_assistant_text(tail: list[str]) -> str:
    """The model's own decoded output, concatenated in order and re-joined with real `\\n`s —
    the ONE place both the authoritative RESULT-line check and the generic/workflow prose
    backstops draw actual human-readable text from, now that each raw tail line is a whole JSON
    envelope rather than plain text.
    """
    return "\n".join(_assistant_text_blocks(tail))


def _terminal_result_text(tail: list[str]) -> str | None:
    """The LAST `type: "result"` envelope's own `result` field — the CLI's terminal summary.

    2026-07-26 follow-up to the NDJSON rewrite: a real `claude -p --output-format stream-json
    --verbose` capture shows the model's `RESULT: <status>` line surviving in TWO places, not
    one — inside an `assistant` envelope's `message.content[].text` (what `_all_assistant_text()`
    decodes) AND, separately, as the terminal envelope's OWN flat `result` string field
    (`{"type":"result","subtype":"success","result":"...","is_error":false,...}`). The prior
    round's docstring listed this envelope's keys (`api_error_status`, `is_error`, `num_turns`,
    `modelUsage`, `duration_ms`) but missed `result` itself — this function is the fix.

    Preferred over assistant text (see `_result_line_content()`) because it is the run's own
    designated "this is the final answer" field — a single string, not nested message content —
    rather than an arbitrary assistant turn that happens to be last. `None` if no `result`
    envelope carries a string `result` field at all (an older/degenerate capture, or a process
    killed before the terminal envelope was ever written).
    """
    latest: str | None = None
    for env in _iter_envelopes(tail):
        if env.get("type") != "result":
            continue
        result = env.get("result")
        if isinstance(result, str):
            latest = result
    return latest


def _last_result_line_in(text: str) -> str | None:
    """The content after `RESULT:` on the LAST such line in `text` (already re-split on real
    `\\n`s by the caller's source), or `None` if no such line exists in this particular text.
    Factored out so `_result_line_content()` can apply the identical search to two different
    text sources without duplicating the reversed-scan logic.
    """
    for line in reversed(text.split("\n")):
        match = _RESULT_LINE_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def latest_rate_limit_signal(tail: list[str]) -> RateLimitSignal | None:
    """The LAST `rate_limit_event` envelope's `rate_limit_info`, decoded — the most recent
    structured reading is the one that matters (an earlier warning superseded by a later, less
    urgent one is stale). `None` if no such envelope is present.
    """
    latest: RateLimitSignal | None = None
    for env in _iter_envelopes(tail):
        if env.get("type") != "rate_limit_event":
            continue
        info = env.get("rate_limit_info")
        if not isinstance(info, dict):
            continue
        status = info.get("status")
        rate_limit_type = info.get("rateLimitType")
        latest = RateLimitSignal(
            status=status if isinstance(status, str) else None,
            rate_limit_type=rate_limit_type if isinstance(rate_limit_type, str) else None,
            utilization=_as_float(info.get("utilization")),
            resets_at=_as_int(info.get("resetsAt")),
            surpassed_threshold=_as_float(info.get("surpassedThreshold")),
            raw=info,
        )
    return latest


def _resets_at_iso(signal: RateLimitSignal) -> str | None:
    if signal.resets_at is None:
        return None
    try:
        return dt.datetime.fromtimestamp(signal.resets_at, tz=dt.UTC).isoformat()
    except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive against garbage input
        return None


def _rate_limit_event_action(tail: list[str]) -> Action | None:
    """The authoritative in-run limit signal (Blocker 1 item 2): a `rate_limit_event` is genuine
    platform-pushed structured data, so — unlike the prose backstops — it is trusted even when
    the post-run usage poll itself succeeded. Returns `None` when no event is present, or a
    present event does not clear the (conservative) bar for "treat this as a wall hit."
    """
    signal = latest_rate_limit_signal(tail)
    if signal is None:
        return None
    resets_at = _resets_at_iso(signal)
    if signal.status is not None and not _is_safe_rate_limit_status(signal.status):
        print(
            f"[claude-relay] rate_limit_event reported an UNRECOGNIZED status {signal.status!r} "
            f"(rateLimitType={signal.rate_limit_type!r}, utilization={signal.utilization!r}) — "
            "statuses confirmed non-limiting live are 'allowed' and 'allowed_warning' (plus any "
            "'allowed*'); treating this as a genuine limit signal (rotate + cool down). If this "
            "value is confirmed non-limiting, add it to "
            "detector._KNOWN_SAFE_RATE_LIMIT_STATUSES.",
            file=sys.stderr,
        )
        return Action(
            CONTINUE_ROTATE,
            f"rate_limit_event reported an unrecognized status {signal.status!r} — treated "
            "conservatively as a platform-reported limit (structured data, authoritative per "
            "Invariant #2's narrowing)",
            resets_at=resets_at,
        )
    if signal.utilization is not None and signal.utilization >= _RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD:
        # DELIBERATELY NO `resets_at` HERE — this is the rotation/cooldown split (2026-07-27).
        #
        # `loop.py`'s AGENT_DEAD_NONLIMIT branch is this Action's only consumer, and it passes
        # `action.resets_at` to `_force_cooldown()` as the cooldown BOUNDARY. Supplying it from a
        # high-utilization event marks the seat unusable until its window resets — which is
        # indefensible for this branch, because Q1 established that this channel only ever WARNS:
        # 42 events were captured from 0.90 to 0.99 utilization and every single call succeeded.
        # A warning is not a denial.
        #
        # The damage was worst on the weekly window: a `seven_day` event at 0.90 would have cooled
        # the seat for DAYS, discarding ~10% of still-spendable weekly quota, on the strength of a
        # warning about a seat that was demonstrably still serving requests. That was the standing
        # prediction recorded in relay/ratelimit_probe.py; Q1 converted it from a suspicion into a
        # straightforward consequence, so it is fixed here on the reasoning rather than waiting for
        # the `seven_day`-at-0.90 observation to arrive.
        #
        # Omitting `resets_at` does NOT weaken rotation. `_force_cooldown()` falls back to the short
        # `_TIMEOUT_COOLDOWN_S` guess, which is exactly what that branch needs: enough for
        # `pick_seat()` to move off this seat next iteration without declaring it dead for hours.
        # The seat's REAL exhaustion is decided by the usage endpoint — `severity: "critical"` at
        # `percent: 100`, verified against a genuinely walled seat — which is Invariant #2's primary
        # path and already records a proper cooldown via `_record_usage()`.
        #
        # The unrecognized-status branch above KEEPS `resets_at`, because a status this module
        # cannot vouch for is the one case where a genuine denial remains possible.
        return Action(
            CONTINUE_ROTATE,
            f"rate_limit_event: utilization {signal.utilization:.0%} of the "
            f"{signal.rate_limit_type or '?'} window at/above the rotate threshold "
            f"({_RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD:.0%}) — rotating on a WARNING, so no "
            "event-derived cooldown horizon is attached (see the comment above)",
        )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# The model's own `RESULT: <status>` line (gadkit.command() Step 3)
# ─────────────────────────────────────────────────────────────────────────────


def _result_line_content(tail: list[str]) -> str | None:
    """The content after `RESULT:` on the model's own LAST such line.

    Tries TWO sources, in preference order (2026-07-26 follow-up — closes the prior round's
    self-flagged uncertainty that a future `RESULT:` line might not always land in an `assistant`
    envelope): first the terminal `result` envelope's own flat `result` field
    (`_terminal_result_text()` — the more authoritative, structurally simpler source, since it is
    the run's designated final-answer field rather than an arbitrary assistant turn), falling
    back to the DECODED assistant text (`_all_assistant_text()`) when no `result` envelope is
    present, OR when one is present but its text does not itself contain a `RESULT:` line (never
    treated as a reason to give up — Invariant #2's spirit of never silently missing a real
    status). Never the raw tail lines directly — see module docstring. Both text sources are
    re-split on real `\\n`s first, since the model's own line breaks only survive as `\\n`
    characters INSIDE a JSON string's decoded value, never as separate tail entries. Returns
    `None` if no such line exists in EITHER source: the model never reached Step 3.
    """
    terminal_text = _terminal_result_text(tail)
    if terminal_text:
        found = _last_result_line_in(terminal_text)
        if found is not None:
            return found
    assistant_text = _all_assistant_text(tail)
    if not assistant_text:
        return None
    return _last_result_line_in(assistant_text)


def _extract_gad_result_status(tail: list[str]) -> str | None:
    """The canonical (uppercased, first-token) status gad-run's own RESULT line reports, or
    `None` if there is no such line at all. ALWAYS returns the raw token even when it is not one
    of `_GAD_RUN_RESULT_STATUSES` (E9 audit note: an unrecognized value must never be silently
    treated as success) — callers decide what to do with an unrecognized token; this function
    only decodes it.
    """
    content = _result_line_content(tail)
    if content is None:
        return None
    match = _RESULT_STATUS_TOKEN_RE.match(content.strip())
    if not match:
        return None
    return match.group(1).upper()


def tail_has_limit_signature(tail: list[str]) -> bool:
    """Does the run's DECODED assistant text contain ANY plausible usage/rate-limit signature
    (generic or gad-kit-workflow-specific)? Never used to directly classify PROGRESSED/
    AWAITING_HUMAN/BLOCKED — only to resolve the HIT_WALL-vs-AGENT_DEAD-NONLIMIT ambiguity, and
    (for the generic markers) only when the live usage endpoint itself could not be read after
    the run.
    """
    text = _all_assistant_text(tail)
    if not text:
        return False
    return _tail_matches([text], _LIMIT_SIGNATURES)


def tail_has_workflow_limit_signature(tail: list[str]) -> bool:
    """Is gad-run.js's own PROBE-CONFIRMED limit diagnosis the CONTENT of the model's own
    terminal `RESULT:` line — i.e. does it canonicalize to exactly `LIMIT-SUSPECTED`?

    Deliberately ATTRIBUTION, not proximity/substring-anywhere (A2/A2b, preserved here): a
    broad "does `_WORKFLOW_LIMIT_SIGNATURES` vocabulary appear ANYWHERE in the decoded assistant
    text" check was tried and reverted twice — it false-positives on gad-kit's own
    `tests/run-tests.mjs` scenario titles (byte-for-byte identical to this vocabulary) printed
    anywhere in a long, otherwise-healthy transcript (self-hosting gad-kit's own test suite is a
    documented use case), or on the model merely quoting a bug report. So this checks ONLY the
    RESULT line's own canonical status — the one place the workflow's ACTUAL returned status
    ends up, per `gadkit.command()`'s Step 3 instruction. If there is no RESULT line at all (the
    model never reached Step 3), this is never authoritative for that run; the generic,
    `usage is None`-gated backstop (which still scans the raw vocabulary, including the
    `_WORKFLOW_LIMIT_SIGNATURES` phrases, as part of `_LIMIT_SIGNATURES`) is the only fallback.

    Unlike the generic markers this is consulted even when the post-run usage poll succeeded,
    because it is a second-agent-call observation of the platform rather than model prose about
    it (module docstring).
    """
    return _extract_gad_result_status(tail) == "LIMIT-SUSPECTED"


def _tail_matches(tail: list[str], signatures: tuple[str, ...]) -> bool:
    joined = "\n".join(tail).lower()
    return any(sig in joined for sig in signatures)


def classify(outcome: str, usage: UsageSnapshot | None, tail: list[str]) -> Action:
    """Map a `gadkit.outcome()` bucket (+ the fresh usage reading + stdout tail backstop) to
    the one `Action` the loop takes next. This is the ONLY place that decision is made —
    nothing else in this package re-implements it (DESIGN.md §2).
    """
    if outcome == "PROGRESSED":
        # A commit landed, so this is unambiguously progress and `kind` stays CONTINUE. But
        # gad-run can commit generation N and then pause on N+1 (its soft ceiling is checked at
        # every phase boundary of every generation in the crawl, not once per launch), and that
        # combination reaches here — the AGENT_DEAD_NONLIMIT branch below never runs. Without
        # this check the loop would happily re-select the SAME just-exhausted seat for the
        # resume, which pauses again immediately at the same gate: a launch spent per iteration
        # for nothing. Flagging it lets `run_once()` cool the seat while still crediting the
        # commit.
        if _extract_gad_result_status(tail) in _PAUSED_RESULT_STATUSES:
            return Action(
                CONTINUE,
                "new commit landed, and gad-kit's own RESULT then reported a soft-ceiling PAUSE — "
                "real progress, but this seat's headroom is spent; cooling it off so the resume "
                "lands on a seat that can actually finish the next generation",
                paused=True,
            )
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
        # Most authoritative first: genuine structured platform data (Blocker 1 item 2).
        rate_limit_action = _rate_limit_event_action(tail)
        if rate_limit_action is not None:
            return rate_limit_action
        if tail_has_workflow_limit_signature(tail):
            return Action(
                CONTINUE_ROTATE,
                "gad-run's own RESULT: line reported LIMIT-SUSPECTED — a probe-confirmed "
                "platform-level diagnosis (a second agent call that also died), authoritative "
                "even though the live usage reading was fine; rotating rather than spending "
                "further seats into a window gad-run already proved closed",
            )
        if usage is None and tail_has_limit_signature(tail):
            return Action(
                CONTINUE_ROTATE,
                "usage endpoint unreadable post-run, but the model's own decoded output shows a "
                "limit signature (backstop signal, Invariant #2) — treating as a wall-hit rather "
                "than a dead agent",
            )
        # Blocker 1 item 3 (E9): disambiguate using gad-run's own returned RESULT status, never
        # to override disk truth (outcome() already ran) — only to decide RETRY vs. "retrying is
        # known to be pointless."
        status = _extract_gad_result_status(tail)
        if status == "DIRTY-TREE":
            return Action(
                RETRY,
                "gad-run's own RESULT reported DIRTY-TREE — it refused to even start against a "
                "dirty working tree; retrying would waste cycles for nothing (E9 audit note), so "
                "this is marked non-retryable and the breaker trips immediately rather than after "
                "the usual 3 attempts",
                no_retry=True,
            )
        if status == "REFUSED":
            return Action(
                RETRY,
                "gad-finish's own RESULT reported REFUSED — it mechanically refused to resume "
                "this generation (reviews/adversarial-review.md missing; Guardrails never fully "
                "completed) and wrote nothing; retrying the SAME FINISH would refuse identically "
                "every time (nothing on disk changes), so this is marked non-retryable — the "
                "NEXT triage() should re-route to a full /gad-generation restart instead (the "
                "artifact-census fix in gadkit.py's `verify_or_later` is the primary defense; "
                "this is the backstop for whenever that gate is somehow still wrong)",
                no_retry=True,
            )
        if status in _PAUSED_RESULT_STATUSES:
            return Action(
                CONTINUE_ROTATE,
                f"gad-kit's own RESULT reported {status!r} — its soft usage ceiling fired at a "
                "phase boundary, so the generation stopped itself deliberately with its artifacts "
                "written and is resumable. This is healthy, NOT a failure: no commit is expected "
                "yet, and retrying on THIS seat would re-hit the same gate at the same place. "
                "Rotate to a seat with headroom and let the next triage() resume the generation.",
                paused=True,
            )
        if status is not None:
            if status in _GAD_RUN_RESULT_STATUSES:
                return Action(
                    RETRY,
                    f"gad-run's own RESULT reported {status!r} with no disk-visible progress — "
                    "retrying (a disk-visible commit is required before this is ever treated as "
                    "success, regardless of what RESULT says)",
                )
            return Action(
                RETRY,
                f"gad-run's own RESULT reported an UNRECOGNIZED status {status!r} with no "
                "disk-visible progress — treated as unsuccessful and safe to retry, never as "
                "success (E9 audit note)",
            )
        return Action(RETRY, "no progress and the seat is not near its cap — transient failure")
    return Action(RETRY, f"unrecognized outcome {outcome!r} — retrying defensively")
