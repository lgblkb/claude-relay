"""Rate-limit calibration harness — the three-tier experiment that turns
`detector._KNOWN_SAFE_RATE_LIMIT_STATUSES` and `_RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD` from
guesses into measurements.

The whole design rests on one observation: `utilization` is a property of the ACCOUNT's window,
not of any test we can write. You cannot manufacture 90% utilization, you can only spend it. So
the harness is tiered by cost, and only the last tier spends anything.

  TIER 0  `baseline`  — zero tokens. Dumps the usage endpoint's VERBATIM payload for every seat
                        (`usage.fetch_usage_raw()`, not the lossy snapshot projection) so unknown
                        fields and unseen enum values are visible at all. Ranks seats by Tier-2
                        suitability and prints the two gauges the calibration cares about.

  TIER 1  (passive)   — see relay/capture.py. Not a subcommand: it rides on normal claude-relay
                        runs at ~zero marginal cost and is what eventually catches a real
                        `seven_day` wall, which is far too expensive to provoke deliberately.

  TIER 2  `burn`      — the only tier that spends. Drives one seat's FIVE-HOUR window to its wall
                        with small `claude -p` calls, capturing every envelope, and stops the
                        instant it sees a walled state.

WHY TIER 2 IS CHEAP IN THE ONLY SENSE THAT MATTERS
--------------------------------------------------
Unused five-hour capacity is not banked — the window resets and whatever was left is gone. So
spending the TAIL of a window on a seat you were not going to use costs nothing in opportunity
terms. The real cost is the weekly quota those same tokens also debit, which is exactly what
`exchange-rate` measures BEFORE you commit to a burn: it spends a deliberately tiny amount, re-
reads both gauges, and reports how much `seven_day` one point of `five_hour` actually costs. That
converts Tier 2 from an unbounded spend into a number you can approve or decline.

Tier 2 deliberately bypasses claude-relay's own supervisor: `config.Defaults.ceiling_pct` is 70.0,
so the relay would rotate away from the seat long before a wall. The harness therefore invokes
`claude -p` directly with `CLAUDE_CONFIG_DIR` pointed at the chosen seat, which is also what
refreshes that seat's OAuth token (DESIGN.md §0).

PRE-REGISTERED QUESTIONS (fixed before looking at any result; answers appended, never edited)
--------------------------------------------------------------------------------------------
  Q1  Does a five-hour wall emit a `rate_limit_event` AT ALL, or only a terminal `result` error?
      If the latter, `detector._rate_limit_event_action()` is decorative exactly when it matters
      and the stdout backstop is the real path. Highest-value question here.
      -> ANSWERED (2026-07-27), and NOT in either direction the question anticipated. The burn drove
         the window 84% -> 100% and every one of 42 calls SUCCEEDED: `is_error` false, subtype
         `success`, `api_error_status` null, `stop_reason` `end_turn`, throughout. The event stream
         warned continuously (utilization 0.90 -> 0.99) but never once emitted a blocking status,
         and utilization never reached 1.0. The wall then materialized in a DIFFERENT session
         minutes later.
         So the premise was wrong: there is no "walled run" whose envelopes we can inspect, because
         the run that gets refused is a run that never starts. The event stream is a WARNING channel
         only. The authoritative wall signal is the usage endpoint — `severity: "critical"` at
         `percent: 100` on the `limits[]` session entry — which is exactly what Invariant #2 already
         designates as primary. Verified directly against the walled seat: `session_utilization()`,
         `session_percent()`, `near_cap()` and `rotate_off(high=90)` all reported it correctly.
         This is the strongest available justification for the `_KNOWN_SAFE_RATE_LIMIT_STATUSES`
         fix: if the event channel cannot report a denial, then treating an unfamiliar status AS a
         denial can only ever produce false positives.
  Q2  Which `status` values exist? `allowed_warning` is the only one ever observed.
      -> ANSWERED as far as this channel goes: {`allowed`, `allowed_warning`} and nothing else,
         across 42 events spanning 84% -> 100% of a window. Per Q1 a blocking value may not exist in
         this channel at all. `allowed`'s absence from the known-safe set was a live HIGH-severity
         bug — see that constant's comment.
  Q3  Does `rateLimitType: "five_hour"` ever appear? Only `seven_day` has been seen — yet the
      five-hour window is what the rotation logic actually keys on.
      -> YES (2026-07-27), on all 42 events. The in-run signal does cover the window rotation keys
         on.
  Q4  Is there an event at ~0.9 utilization, and does `status` change there or does only
      `surpassedThreshold` move? `_RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD = 0.9` is hand-picked.
      -> ANSWERED. 0.9 is a REAL platform threshold: every event carrying `utilization` also carried
         `surpassedThreshold: 0.9`. The hand-picked value happened to match. But `utilization` is
         ABSENT below 0.9 entirely, so `>= 0.9` fires on the first event that carries the field —
         the constant is operationally a presence check and lowering it would change nothing.
         `status` does change at the boundary (`allowed` -> `allowed_warning`); `surpassedThreshold`
         can be absent even when `utilization` is present (2 events at exactly 0.90).
  Q5  Do the event's fractional `utilization` and the endpoint's percent `five_hour.utilization`
      agree at the same moment? Free cross-check that 0.9 is in the right units on the right gauge.
      -> CONSISTENT (2026-07-27): the burn's own progress log paired endpoint reads of 84/86/91/94/98
         percent against event utilizations of 0.90-0.99 over the same interval, and both saturated
         together at the end (endpoint 100.0, events 0.99). Same gauge, same window, differing only
         by the fraction-vs-percent scale already documented. Not a synchronized single-instant
         sample, so it is corroboration rather than proof.
  Q6  Is `modelUsage` present in the terminal `result`? Unblocks the deferred Phase 2 cost work.
      -> YES (2026-07-27), with per-model inputTokens / outputTokens / cacheReadInputTokens /
         costUSD / contextWindow, plus top-level `total_cost_usd`, `api_error_status`,
         `stop_reason`, `terminal_reason`.

STANDING PREDICTION (recorded so the data can refute it rather than be rationalized after)
------------------------------------------------------------------------------------------
`detector._rate_limit_event_action()` ignores `rate_limit_type` when deciding, and feeds the
event's `resetsAt` straight into `_force_cooldown()`. So a `seven_day` event at utilization >= 0.9
cools that seat until the WEEKLY reset — potentially days — discarding the remaining ~10% of weekly
AND a possibly-fresh five-hour window. I predict this is wrong and that both the threshold and the
cooldown horizon must become window-aware.

  -> STILL UNTESTED as of 2026-07-27, and I mispredicted twice about it.
     First I wrote "Q3 and Q4 settle it". They do not: Q3 came back YES but says nothing about the
     cooldown horizon. Then I wrote that Q4 might be unreachable because five-hour events omit
     `utilization` — also wrong, they carry it above 0.9; I had generalized from the single `allowed`
     event captured before the burn reached the warning band.
     The prediction still stands unexamined because it concerns a `seven_day` event at >= 0.9, and
     every high-utilization event captured has been `five_hour`. Reaching it needs a nearly-exhausted
     WEEKLY window, which the Tier-1 passive tap will eventually see and a five-hour burn never can.
     Both mispredictions are left in place rather than tidied away: the pattern in them is that I
     twice inferred a general rule from one observation, which is the same error the harness exists
     to prevent.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import capture, fleet, usage

# ---------------------------------------------------------------------------------------------
# Tier 2 safety rails. Every one of these is a HARD stop, not a warning: this harness deliberately
# spends real quota, so it must be impossible for a bug or a bad flag to spend more than intended.
# ---------------------------------------------------------------------------------------------

# Maximum `claude -p` calls a single burn may ever make, regardless of flags.
_MAX_BURN_CALLS = 400

# Burn stops when the five-hour gauge reaches this percent. 100 is the wall itself; stopping a
# shade under it is pointless (the whole objective is the walled envelope), so this exists only as
# an absolute upper bound for the loop.
_BURN_TARGET_PCT = 100.0

# Re-read the usage endpoint at most this often during a burn.
#
# Originally 20s, on the theory that frequent reads would catch the last few percent before the
# wall. That was empirically wrong: building this harness 429'd the endpoint with
# `Retry-After: 300` after only a couple of dozen reads across a few minutes (2026-07-27). The
# endpoint is free in TOKENS but distinctly not free in requests, and DESIGN.md §4 already settled
# on 90s as the supervisor's poll_ttl for exactly this reason. Matching it here rather than
# inventing a second, more aggressive cadence for the one tool most likely to be run alongside a
# live supervisor.
_USAGE_REREAD_INTERVAL_S = 90.0

# Per-call timeout for a burn's `claude -p`. Burn prompts are tiny; anything slower than this is a
# hung session, not a slow answer.
_BURN_CALL_TIMEOUT_S = 180.0

# The burn prompt. Deliberately asks for a bounded chunk of prose: it must consume enough tokens to
# move the gauge without being so large that one call overshoots the wall and we miss the
# threshold-crossing envelopes on the way up. It must NOT ask for tool use — a burn that touches
# the filesystem is a burn that can damage something.
_BURN_PROMPT = (
    "Write approximately 400 words of plain prose about the history of mechanical clocks. "
    "Do not use any tools. Do not ask questions. Output only the prose."
)


@dataclasses.dataclass
class SeatReading:
    """One seat's two gauges at one moment, plus the verbatim payload behind them."""

    name: str
    path: Path
    five_hour_pct: float | None
    seven_day_pct: float | None
    raw: dict[str, Any] | None
    error: str | None = None

    @property
    def usable_for_burn(self) -> bool:
        return self.error is None and self.five_hour_pct is not None

    def burn_score(self) -> float:
        """Higher is a better Tier-2 candidate: the cheapest seat to wall is the one with the MOST
        five-hour already spent (least left to buy) and the LEAST weekly spent (most headroom to
        absorb the cost).
        """
        if not self.usable_for_burn:
            return float("-inf")
        five = self.five_hour_pct or 0.0
        seven = self.seven_day_pct if self.seven_day_pct is not None else 100.0
        return five - seven


def _pct(raw: Any) -> float | None:
    """The endpoint reports these gauges as 0-100 percents (see usage.py's module docstring).

    `bool` is excluded explicitly: `isinstance(True, int)` is True in Python, so a boolean would
    otherwise coerce to 1.0 — and on a 0-1 fractional gauge 1.0 reads as "fully walled," the single
    most consequential misreading available here. The endpoint has many boolean fields
    (`spend.enabled`, `extra_usage.is_enabled`, ...), so a future field rename putting a bool where
    a gauge used to be is a realistic way to hit this.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def read_usage(name: str, path: Path) -> SeatReading:
    """One zero-token usage read for the seat at `path`, keeping the payload verbatim.

    Takes name+path rather than a `fleet.Seat` because every re-read site inside a burn already
    holds a `SeatReading`, not a `Seat`. Reconstructing a `Seat` there is impossible anyway —
    `Seat.usable` is a derived property, not a field, so `Seat(..., usable=True)` is a TypeError
    (which is exactly how this was found: the burn's re-read path would have crashed at runtime).
    """
    try:
        raw = usage.fetch_usage_raw(path)
    except usage.UsageError as exc:
        return SeatReading(name, path, None, None, None, error=str(exc))
    five = raw.get("five_hour") if isinstance(raw.get("five_hour"), dict) else {}
    seven = raw.get("seven_day") if isinstance(raw.get("seven_day"), dict) else {}
    return SeatReading(
        name=name,
        path=path,
        five_hour_pct=_pct(five.get("utilization")),
        seven_day_pct=_pct(seven.get("utilization")),
        raw=raw,
    )


def read_seat(seat: fleet.Seat) -> SeatReading:
    """`read_usage()` for a discovered `fleet.Seat`."""
    return read_usage(seat.name, seat.path)


def reread(reading: SeatReading) -> SeatReading:
    """Refresh an existing reading in place-ish — the burn's polling path."""
    return read_usage(reading.name, reading.path)


def read_all_seats() -> list[SeatReading]:
    return [read_seat(s) for s in fleet.discover_seats() if s.usable]


def _write_artifact(out_dir: Path, name: str, payload: Any) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{name}-{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def _schema_walk(obj: Any, prefix: str = "") -> dict[str, str]:
    """Flatten a payload into dotted-path -> type-name, so a diff against
    `usage.UsageSnapshot.from_json()`'s four known keys shows exactly what the projection drops.
    """
    found: dict[str, str] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                found.update(_schema_walk(value, path))
            else:
                found[path] = type(value).__name__
    elif isinstance(obj, list):
        # Only the first element: these arrays are homogeneous (`limits[]`), so every element
        # repeats the same shape and listing all of them just adds noise.
        if obj:
            found.update(_schema_walk(obj[0], f"{prefix}[]"))
        else:
            found[f"{prefix}[]"] = "empty"
    return found


# ---------------------------------------------------------------------------------------------
# Tier 0
# ---------------------------------------------------------------------------------------------


def cmd_baseline(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    readings = read_all_seats()
    if not readings:
        print("no usable seats (~/.claude-* with a valid access token) — nothing to read")
        return 1

    print(f"TIER 0 baseline — {len(readings)} usable seat(s), 0 tokens spent\n")
    header = f"{'seat':<12} {'five_hour%':>11} {'seven_day%':>11} {'burn score':>11}"
    print(header)
    print("-" * len(header))
    for reading in sorted(readings, key=lambda r: r.burn_score(), reverse=True):
        if reading.error:
            print(f"{reading.name:<12} {'ERROR':>11} {'-':>11} {'-':>11}   {reading.error}")
            continue
        five = "?" if reading.five_hour_pct is None else f"{reading.five_hour_pct:.1f}"
        seven = "?" if reading.seven_day_pct is None else f"{reading.seven_day_pct:.1f}"
        print(f"{reading.name:<12} {five:>11} {seven:>11} {reading.burn_score():>11.1f}")

    payload = {
        "captured_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "tier": 0,
        "tokens_spent": 0,
        "seats": [
            {
                "name": r.name,
                "five_hour_pct": r.five_hour_pct,
                "seven_day_pct": r.seven_day_pct,
                "burn_score": r.burn_score() if r.usable_for_burn else None,
                "error": r.error,
                "raw": r.raw,
            }
            for r in readings
        ],
    }
    artifact = _write_artifact(out_dir, "tier0-baseline", payload)
    print(f"\nverbatim payloads -> {artifact}")

    # What the production projection throws away. This is a real finding, not decoration: a field
    # the endpoint sends that `UsageSnapshot` drops is a field no rotation decision can ever use.
    known = {"five_hour", "seven_day", "limits"}
    for reading in readings:
        if not reading.raw:
            continue
        schema = _schema_walk(reading.raw)
        dropped = sorted({p for p in schema if p.split(".")[0].split("[")[0] not in known})
        print(f"\nendpoint schema for seat {reading.name!r}: {len(schema)} leaf field(s)")
        if dropped:
            print("  fields UsageSnapshot.from_json() drops entirely:")
            for path in dropped:
                print(f"    {path}: {schema[path]}")
        else:
            print("  (no top-level fields outside five_hour/seven_day/limits)")
        break  # one seat is enough to establish the schema; they are the same endpoint

    best = max(readings, key=lambda r: r.burn_score())
    if best.usable_for_burn:
        print(
            f"\ncheapest Tier-2 candidate: seat {best.name!r} "
            f"(five_hour={best.five_hour_pct:.1f}%, seven_day={best.seven_day_pct or 0.0:.1f}%)"
        )
        print("  next: `exchange-rate --seat " + best.name + "` to price a burn before running one")
    return 0


# ---------------------------------------------------------------------------------------------
# Shared `claude -p` driver
# ---------------------------------------------------------------------------------------------


def _claude_binary() -> str | None:
    return shutil.which("claude")


def _run_claude_once(
    seat: SeatReading,
    *,
    prompt: str,
    model: str | None,
    capture_dir: Path,
    timeout_s: float = _BURN_CALL_TIMEOUT_S,
) -> dict[str, Any]:
    """One real `claude -p` call against `seat`, with stream-json output so the capture tap sees
    every envelope. Returns a summary dict; never raises on a non-zero exit.

    Runs with cwd set to a scratch dir and NO tool permissions granted, so a burn call cannot
    touch a repository (Invariant #6: never destroy unrelated work).
    """
    binary = _claude_binary()
    if binary is None:
        return {"ok": False, "error": "`claude` not found on PATH"}

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(seat.path)
    # Deliberately NOT setting `capture.CAPTURE_DIR_ENV` here. Doing so was a live bug: it sets the
    # variable for the CHILD, which is `claude` and has never heard of relay.capture, while the
    # recording below happens in THIS process, whose own os.environ still lacks it. Every record
    # silently no-opped. The parent records via `capture.record_to()` with an explicit destination.

    argv = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if model:
        argv += ["--model", model]

    scratch = capture_dir / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    started = time.time()
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            cwd=str(scratch),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout_s}s", "elapsed_s": time.time() - started}

    # The env-gated tap only sees lines that pass through runner.py; this direct subprocess call does
    # not use runner.py at all, so feed this run's stdout to the recorder with an EXPLICIT
    # destination. `record_to()` rather than `record_line()` — see that function's docstring for the
    # silent-no-op bug this replaced.
    lines = proc.stdout.splitlines()
    for line in lines:
        capture.record_to(capture_dir, line, seat=seat.name)

    recorded = len(_collect_records(capture_dir))
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "elapsed_s": time.time() - started,
        "stdout_lines": len(lines),
        "records_after_call": recorded,
        "stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
    }


def _resolve_seat(name: str | None) -> SeatReading | None:
    readings = read_all_seats()
    if not readings:
        return None
    if name:
        for reading in readings:
            if reading.name == name:
                return reading
        return None
    best = max(readings, key=lambda r: r.burn_score())
    return best if best.usable_for_burn else None


# ---------------------------------------------------------------------------------------------
# Tier 0.5 — price a burn before committing to one
# ---------------------------------------------------------------------------------------------


def cmd_exchange_rate(args: argparse.Namespace) -> int:
    """Spend a deliberately tiny amount, then report how much `seven_day` one point of `five_hour`
    costs. This is what makes Tier 2's price knowable in advance.
    """
    out_dir = Path(args.out).expanduser()
    capture_dir = out_dir / "envelopes"
    seat = _resolve_seat(args.seat)
    if seat is None:
        print(f"no usable seat matching {args.seat!r}" if args.seat else "no usable seats")
        return 1

    before = reread(seat)
    if before.error is not None or before.five_hour_pct is None:
        # FAIL CLOSED before spending. The entire output of this subcommand is a DELTA between two
        # readings, so without a baseline there is nothing to compute and every call made would be
        # quota spent for no measurement. Observed for real on the first live run (2026-07-27): a
        # lingering 429 made both readings None, three calls were spent anyway, and the summary
        # then reported "five_hour did not move measurably" — a misleading conclusion drawn from an
        # absent measurement, which is strictly worse than refusing to run.
        detail = before.error or "endpoint sent no five_hour.utilization"
        print(f"refusing to measure: could not read seat {seat.name!r} usage ({detail})")
        print("this subcommand reports a DELTA between two readings; without a baseline it can only mislead")
        return 1

    print(f"seat {seat.name!r} before: five_hour={before.five_hour_pct}%  seven_day={before.seven_day_pct}%")
    print(f"spending {args.calls} small call(s) to measure the exchange rate...")

    calls: list[dict[str, Any]] = []
    for index in range(args.calls):
        result = _run_claude_once(seat, prompt=_BURN_PROMPT, model=args.model, capture_dir=capture_dir)
        calls.append(result)
        state = "ok" if result.get("ok") else f"FAILED ({result.get('error') or result.get('returncode')})"
        print(f"  call {index + 1}/{args.calls}: {state} in {result.get('elapsed_s', 0):.1f}s")
        if not result.get("ok"):
            break

    # The endpoint lags a little behind a just-finished call; give it a moment before re-reading.
    time.sleep(args.settle)
    after = reread(seat)
    if after.error is not None or after.five_hour_pct is None:
        # The spend already happened, so unlike the baseline guard this cannot be undone — but it
        # must not be reported as a measurement. Say plainly that the quota is spent and the
        # exchange rate is unknown, rather than subtracting None and calling the result 0.00.
        detail = after.error or "endpoint sent no five_hour.utilization"
        print(f"seat {seat.name!r} after:  UNREADABLE ({detail})")
        print(f"\n{len(calls)} call(s) were spent but the result CANNOT be measured — no exchange rate.")
        print("re-run once the endpoint is readable again; the spend is not recoverable.")
        _write_artifact(
            out_dir,
            "tier0.5-exchange-rate-UNMEASURED",
            {
                "tier": 0.5,
                "seat": seat.name,
                "calls_attempted": args.calls,
                "calls": calls,
                "before": {"five_hour_pct": before.five_hour_pct, "seven_day_pct": before.seven_day_pct},
                "after_error": detail,
                "outcome": "spent-but-unmeasured",
            },
        )
        return 1

    print(f"seat {seat.name!r} after:  five_hour={after.five_hour_pct}%  seven_day={after.seven_day_pct}%")

    d_five = (after.five_hour_pct or 0.0) - (before.five_hour_pct or 0.0)
    d_seven = (after.seven_day_pct or 0.0) - (before.seven_day_pct or 0.0)
    print(f"\ndelta: five_hour {d_five:+.2f} pts, seven_day {d_seven:+.2f} pts")

    verdict: dict[str, Any] = {
        "tier": 0.5,
        "seat": seat.name,
        "calls_attempted": args.calls,
        "calls": calls,
        "before": {"five_hour_pct": before.five_hour_pct, "seven_day_pct": before.seven_day_pct},
        "after": {"five_hour_pct": after.five_hour_pct, "seven_day_pct": after.seven_day_pct},
        "delta_five_hour_pts": d_five,
        "delta_seven_day_pts": d_seven,
    }

    if d_five > 0.0:
        ratio = d_seven / d_five
        remaining = max(0.0, _BURN_TARGET_PCT - (after.five_hour_pct or 0.0))
        projected = ratio * remaining
        per_pt_calls = args.calls / d_five
        verdict["seven_day_pts_per_five_hour_pt"] = ratio
        verdict["projected_seven_day_cost_pts_to_wall"] = projected
        verdict["projected_calls_to_wall"] = per_pt_calls * remaining
        print(f"exchange rate: 1 pt of five_hour costs {ratio:.3f} pts of seven_day")
        print(
            f"PROJECTED COST of a full Tier-2 burn on {seat.name!r}: "
            f"{projected:.1f} pts of weekly quota over ~{per_pt_calls * remaining:.0f} more call(s)"
        )
        if d_seven <= 0.0:
            # The endpoint reports both gauges as INTEGER percents, so any weekly movement under
            # one full point rounds to zero. A literal reading of `ratio == 0.0` therefore says a
            # burn is FREE in weekly terms, which is false and is exactly the kind of number
            # someone would approve a spend against. Observed on the first real measurement
            # (2026-07-27): 4 calls moved five_hour +2 and seven_day +0, while a wider sample over
            # the same session moved five_hour 57->70 alongside seven_day 52->54 — i.e. a true
            # ratio near 0.15, not 0. Say so instead of implying zero cost.
            floor_ratio = 1.0 / d_five  # one full weekly point was NOT yet observed, so this bounds it
            verdict["below_gauge_resolution"] = True
            verdict["seven_day_pts_per_five_hour_pt_upper_bound"] = floor_ratio
            verdict["projected_seven_day_cost_pts_upper_bound"] = floor_ratio * remaining
            print(
                "  NOTE: weekly moved less than the endpoint's 1-point resolution, so that 0.0 is "
                "'below measurable', NOT 'free'."
            )
            print(
                f"  Upper bound from this sample: <{floor_ratio:.3f} weekly pts per five_hour pt, "
                f"so <{floor_ratio * remaining:.1f} weekly pts to the wall."
            )
            print("  Widen --calls for a tighter estimate, or read the burn's own before/after delta.")
    else:
        verdict["note"] = (
            "five_hour did not move measurably — either the spend was too small to register at the "
            "endpoint's resolution, or the gauge updates on a delay longer than --settle"
        )
        print("five_hour did not move measurably; cannot project a burn cost from this sample")

    artifact = _write_artifact(out_dir, "tier0.5-exchange-rate", verdict)
    print(f"\nartifact -> {artifact}")
    return 0


# ---------------------------------------------------------------------------------------------
# Tier 2 — the burn
# ---------------------------------------------------------------------------------------------


def _novel_status(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first captured `rate_limit_event` whose status is not known-safe — a candidate wall.

    Named `_novel_status`, NOT `_walled`, because the first live run proved the difference matters.
    It stopped the burn announcing "WALLED STATE CAPTURED: status='allowed'" — and `allowed` is the
    ordinary healthy status, the opposite of a wall. Calling every unfamiliar value a wall
    overclaimed the finding and cut the burn short at 80% of the window.

    Deliberately delegates to `detector._is_safe_rate_limit_status()` rather than keeping a private
    copy: the burn's stop condition and production's rotate condition must move together, or the
    harness stops on values production ignores (and vice versa). The `allowed` miss WAS the
    production bug this run uncovered, so the shared predicate is the fix in both places at once.
    """
    from . import detector

    for record in capture.rate_limit_records(records):
        info = (record.get("envelope") or {}).get("rate_limit_info")
        if not isinstance(info, dict):
            continue
        status = info.get("status")
        if isinstance(status, str) and not detector._is_safe_rate_limit_status(status):
            return record
    return None


def cmd_burn(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    capture_dir = out_dir / "envelopes"
    seat = _resolve_seat(args.seat)
    if seat is None:
        print(f"no usable seat matching {args.seat!r}" if args.seat else "no usable seats")
        return 1

    max_calls = min(args.max_calls, _MAX_BURN_CALLS)
    if args.max_calls > _MAX_BURN_CALLS:
        print(f"--max-calls {args.max_calls} exceeds the hard cap; clamped to {_MAX_BURN_CALLS}")

    before = reread(seat)
    if before.error is not None:
        # Fail closed BEFORE spending anything: without a starting reading there is no baseline to
        # measure the weekly-spend cap against, so the cap could not be enforced at all.
        print(f"refusing to burn: could not read seat {seat.name!r} usage ({before.error})")
        print("the weekly-spend cap is enforced against this reading; without it there is no cap")
        return 1
    print(f"TIER 2 burn on seat {seat.name!r}")
    print(f"  start: five_hour={before.five_hour_pct}%  seven_day={before.seven_day_pct}%")
    print(f"  caps:  max_calls={max_calls}  max_seven_day_pts={args.max_seven_day_pts}")
    print(f"  stops on: first non-'allowed_warning' status, five_hour>={_BURN_TARGET_PCT}, or a cap\n")

    calls: list[dict[str, Any]] = []
    stop_reason = "max-calls-reached"
    last_usage_read = time.time()
    current = before

    for index in range(max_calls):
        result = _run_claude_once(seat, prompt=_BURN_PROMPT, model=args.model, capture_dir=capture_dir)
        calls.append(result)

        records = _collect_records(capture_dir)
        hit = _novel_status(records)
        state = "ok" if result.get("ok") else f"exit={result.get('returncode')} {result.get('error') or ''}"
        line = f"  call {index + 1}/{max_calls}: {state}  records={len(records)}"
        if current.five_hour_pct is not None:
            line += f"  five_hour~{current.five_hour_pct:.1f}%"
        # flush=True because a burn runs for ~15 minutes and is almost always redirected to a log
        # that someone (or a monitor) is tailing. Python buffers its own stdout when stdout is not a
        # tty, and `stdbuf` does NOT override that — so without this the entire progress stream
        # appears only at exit, which is indistinguishable from a hung run.
        print(line, flush=True)

        if index == 0 and result.get("ok") and not records:
            # SILENCE IS NOT SUCCESS. Wall detection reads these records, so a burn that records
            # nothing cannot possibly stop on a wall — it would spend its entire cap and report a
            # tidy "max-calls-reached" having learned nothing. That is not hypothetical: the first
            # real burn ran exactly this way because the recorder was a silent no-op (see
            # `capture.record_to()`), and only an out-of-band check of the artifact directory caught
            # it. Checking after the FIRST successful call bounds the waste at one call.
            stop_reason = "recording-broken-nothing-captured"
            print(
                "\nABORTING: the first call succeeded but recorded ZERO envelopes.\n"
                "  Wall detection reads those records, so this burn could never stop on a wall and\n"
                "  every further call would be spent for no data. Fix the recorder, then re-run."
            )
            break

        if hit is not None:
            info = (hit.get("envelope") or {}).get("rate_limit_info") or {}
            # "NOVEL", not "WALLED": this is a status production does not recognize as safe, which
            # is a candidate wall and worth stopping for — but whether it IS a wall is a judgement
            # for whoever reads the payload, not a claim this function can make.
            print(f"\n*** NOVEL (non-safe) STATUS CAPTURED: status={info.get('status')!r} ***")
            print(json.dumps(info, indent=2))
            print("  Stopping here so the payload can be judged before more quota is spent.")
            stop_reason = "novel-status-captured"
            break

        if not result.get("ok"):
            # A failed call is itself informative — it may BE the wall, expressed as a non-zero
            # exit with no rate_limit_event (pre-registered question Q1). Keep the evidence and
            # stop rather than hammering a seat that is already refusing.
            stop_reason = "call-failed"
            tail_text = result.get("stderr_tail", "")
            print(f"    stderr tail: {tail_text[-400:]}")
            break

        if time.time() - last_usage_read >= _USAGE_REREAD_INTERVAL_S:
            current = reread(seat)
            last_usage_read = time.time()
            if current.error is not None:
                # FAIL CLOSED. The weekly-budget cap is enforced purely by re-reading this gauge,
                # so an unreadable gauge means the only guard against unbounded spend is gone. A
                # 429 here is not hypothetical — building this harness triggered one with
                # `Retry-After: 300` (2026-07-27) — and "keep burning while blind" is precisely the
                # behaviour a deliberately-spending tool must never have.
                stop_reason = "usage-unreadable-fail-closed"
                print(f"\nstopping: usage unreadable ({current.error}) — cannot enforce the spend cap")
                break
            if current.five_hour_pct is not None and current.five_hour_pct >= _BURN_TARGET_PCT:
                stop_reason = "five-hour-target-reached"
                break
            spent = (current.seven_day_pct or 0.0) - (before.seven_day_pct or 0.0)
            if spent >= args.max_seven_day_pts:
                stop_reason = "seven-day-budget-reached"
                print(f"\nstopping: spent {spent:.1f} pts of weekly quota (cap {args.max_seven_day_pts})")
                break

    time.sleep(args.settle)
    after = reread(seat)
    records = _collect_records(capture_dir)

    print(f"\n  end:   five_hour={after.five_hour_pct}%  seven_day={after.seven_day_pct}%")
    print(f"  stop reason: {stop_reason}")

    findings = summarize(records)
    print("\n" + render_findings(findings))

    payload = {
        "tier": 2,
        "seat": seat.name,
        "stop_reason": stop_reason,
        "calls_made": len(calls),
        "calls": calls,
        "before": {"five_hour_pct": before.five_hour_pct, "seven_day_pct": before.seven_day_pct},
        "after": {"five_hour_pct": after.five_hour_pct, "seven_day_pct": after.seven_day_pct},
        "weekly_pts_spent": (after.seven_day_pct or 0.0) - (before.seven_day_pct or 0.0),
        "findings": findings,
    }
    artifact = _write_artifact(out_dir, "tier2-burn", payload)
    print(f"\nartifact -> {artifact}")
    return 0


# ---------------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------------


def _collect_records(capture_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not capture_dir.exists():
        return records
    for path in sorted(capture_dir.glob("envelopes-*.jsonl")):
        records.extend(capture.read_records(path))
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Answer the pre-registered questions from whatever has been captured so far."""
    from . import detector

    rate_events = capture.rate_limit_records(records)
    statuses = capture.observed_status_vocabulary(records)
    limit_types = capture.observed_limit_types(records)

    results = [r for r in records if (r.get("envelope") or {}).get("type") == "result"]
    with_model_usage = [r for r in results if (r.get("envelope") or {}).get("modelUsage")]

    unknown = sorted(set(statuses) - set(detector._KNOWN_SAFE_RATE_LIMIT_STATUSES))
    utilizations = []
    thresholds = set()
    for record in rate_events:
        info = (record.get("envelope") or {}).get("rate_limit_info") or {}
        if isinstance(info.get("utilization"), (int, float)):
            utilizations.append(float(info["utilization"]))
        if isinstance(info.get("surpassedThreshold"), (int, float)):
            thresholds.add(float(info["surpassedThreshold"]))

    return {
        "records_total": len(records),
        "rate_limit_events": len(rate_events),
        "result_envelopes": len(results),
        "Q1_wall_emitted_rate_limit_event": bool(unknown) if results else None,
        "Q2_status_vocabulary": statuses,
        "Q2_statuses_outside_known_safe": unknown,
        "Q3_limit_types": limit_types,
        "Q3_five_hour_observed": "five_hour" in limit_types,
        "Q4_surpassed_thresholds": sorted(thresholds),
        "Q4_max_utilization": max(utilizations) if utilizations else None,
        "Q6_result_envelopes_with_modelUsage": len(with_model_usage),
    }


def render_findings(findings: dict[str, Any]) -> str:
    lines = ["PRE-REGISTERED QUESTIONS — current answers"]
    lines.append(
        f"  records={findings['records_total']}  rate_limit_events={findings['rate_limit_events']}  "
        f"result_envelopes={findings['result_envelopes']}"
    )
    q1 = findings["Q1_wall_emitted_rate_limit_event"]
    lines.append(
        "  Q1 wall emitted a rate_limit_event: "
        + ("no data yet" if q1 is None else ("YES" if q1 else "NOT YET — no non-safe status seen"))
    )
    lines.append(f"  Q2 status vocabulary: {findings['Q2_status_vocabulary'] or '(none captured)'}")
    if findings["Q2_statuses_outside_known_safe"]:
        lines.append(f"     NEW statuses outside _KNOWN_SAFE: {findings['Q2_statuses_outside_known_safe']}")
    lines.append(f"  Q3 rateLimitType values: {findings['Q3_limit_types'] or '(none captured)'}")
    lines.append(f"     five_hour observed: {findings['Q3_five_hour_observed']}")
    lines.append(f"  Q4 surpassedThreshold values: {findings['Q4_surpassed_thresholds'] or '(none)'}")
    lines.append(f"     max utilization seen: {findings['Q4_max_utilization']}")
    with_usage = findings["Q6_result_envelopes_with_modelUsage"]
    lines.append(f"  Q6 result envelopes carrying modelUsage: {with_usage}")
    return "\n".join(lines)


def cmd_report(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    records = _collect_records(out_dir / "envelopes")
    if not records:
        print(f"no captured envelopes under {out_dir / 'envelopes'}")
        print("run `baseline`, then `exchange-rate`, then `burn` — or set CLAUDE_RELAY_CAPTURE_DIR")
        print("to that directory and let normal claude-relay runs fill it (Tier 1).")
        return 1
    print(render_findings(summarize(records)))
    return 0


# ---------------------------------------------------------------------------------------------


_DEFAULT_OUT = "~/.claude-relay/ratelimit-calibration"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rate-limit-probe",
        description="Three-tier rate-limit calibration harness (see relay/ratelimit_probe.py).",
    )
    parser.add_argument("--out", default=_DEFAULT_OUT, help=f"artifact directory (default: {_DEFAULT_OUT})")
    sub = parser.add_subparsers(dest="command", required=True)

    p0 = sub.add_parser("baseline", help="TIER 0: zero-token usage dump for every seat")
    p0.set_defaults(func=cmd_baseline)

    p05 = sub.add_parser("exchange-rate", help="TIER 0.5: spend a little to price a full burn")
    p05.add_argument("--seat", default=None, help="seat name (default: cheapest candidate)")
    p05.add_argument("--calls", type=int, default=3, help="small calls to spend (default: 3)")
    p05.add_argument("--model", default=None, help="--model passed to claude (default: seat default)")
    p05.add_argument("--settle", type=float, default=8.0, help="seconds to wait before re-reading usage")
    p05.set_defaults(func=cmd_exchange_rate)

    p2 = sub.add_parser("burn", help="TIER 2: drive one seat's five-hour window to its wall")
    p2.add_argument("--seat", default=None, help="seat name (default: cheapest candidate)")
    p2.add_argument("--max-calls", type=int, default=60, help=f"hard-capped at {_MAX_BURN_CALLS}")
    p2.add_argument(
        "--max-seven-day-pts",
        type=float,
        default=8.0,
        help="abort once this many percentage points of WEEKLY quota have been spent (default: 8)",
    )
    p2.add_argument("--model", default=None, help="--model passed to claude (default: seat default)")
    p2.add_argument("--settle", type=float, default=8.0, help="seconds to wait before the final usage read")
    p2.set_defaults(func=cmd_burn)

    pr = sub.add_parser("report", help="answer the pre-registered questions from captured envelopes")
    pr.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
