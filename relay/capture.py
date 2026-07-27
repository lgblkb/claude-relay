"""Tier-1 rate-limit envelope tap: an append-only, opt-in recorder for the two NDJSON envelope
types that carry ground truth we cannot otherwise obtain.

WHY THIS EXISTS
---------------
`detector._KNOWN_SAFE_RATE_LIMIT_STATUSES` is a ONE-ELEMENT frozenset, and
`detector._RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD` is a hand-picked 0.9. Both encode a guess
about an enum whose only ever-observed value is `allowed_warning` at `utilization: 0.76`
(2026-07-26 probe). Rotation decisions — including forced multi-hour cooldowns — ride on that
guess.

The blocking problem is that `utilization` is a property of the ACCOUNT's window, not of any test
we can write: you cannot manufacture 90% utilization, you can only spend it. A seat's walled state
is therefore observable only when it actually occurs, which for the `seven_day` window means once
a week at most and never on demand.

So this tap makes the observation FREE and PASSIVE. `runner.py` already reads every NDJSON line
the child emits (the child always runs `--output-format stream-json`), so recording the handful of
envelopes that matter costs one `str.startswith` per line on the disabled path and one small
append on the enabled path. Normal claude-relay use then fills in the enum on its own, and the
next real wall — whenever it happens, on whichever seat — is captured instead of lost.

WHAT IS RECORDED
----------------
Only two envelope types, because only these two carry unobtainable-by-other-means truth:

  * `rate_limit_event` — the authoritative limit signal (DESIGN.md Invariant #2). The whole
    calibration target: `status`, `rateLimitType`, `utilization`, `surpassedThreshold`, `resetsAt`.
  * `result` (terminal) — carries `modelUsage`, which unblocks the Phase 2 cost calibration
    DESIGN.md has deferred since the beginning, AND reveals what a walled run's terminal envelope
    looks like when NO `rate_limit_event` precedes it (the single most important open question:
    if a wall emits no event, `detector._rate_limit_event_action()` is decorative exactly when it
    matters and the stdout backstop is the real path).

Deliberately NOT recorded: `assistant` envelopes. They carry model prose and, worse, arbitrary
tool output — the one category that can contain repository contents or secrets. Invariant #5 says
never leak secrets; the cheapest way to honour that in a capture file is to never write the
envelope type that can hold them.

OFF BY DEFAULT, ALWAYS
----------------------
Enabled only when `CLAUDE_RELAY_CAPTURE_DIR` names a writable directory. Absent that, every
public function here is a no-op returning immediately. A capture tap that could be switched on by
a config-file default is a capture tap that eventually writes somewhere the operator did not
expect, so the only switch is an explicit environment variable naming an explicit destination.

CRASH SAFETY
------------
Each envelope is one `json.dumps` line appended with a single `write()` under an O_APPEND handle,
so a torn write can lose the tail of the file but can never corrupt an earlier record (Invariant
#7: idempotent / crash-safe). Failures NEVER propagate: a capture tap that can break a multi-day
supervisor run is worse than no capture tap, so `record_line()` swallows every exception and
disables itself after the first failure rather than retrying on every subsequent line.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

# The env var that both enables the tap and names its destination directory.
CAPTURE_DIR_ENV = "CLAUDE_RELAY_CAPTURE_DIR"

# Envelope types worth recording. See module docstring for why `assistant` is excluded.
_CAPTURED_TYPES = frozenset({"rate_limit_event", "result"})

# Cheap pre-filter applied to the RAW line before any JSON parsing. Every captured type's name
# appears verbatim in the line's `"type":"..."` field, so a line containing none of these
# substrings cannot be a captured envelope. This keeps the hot path (thousands of `assistant`
# deltas per run) at a couple of substring scans instead of a full `json.loads`.
_PREFILTER = tuple(f'"{name}"' for name in sorted(_CAPTURED_TYPES))

# Hard cap on a single recorded line. A pathological `result` envelope (a huge `result` string) is
# not worth unbounded disk; the envelope is recorded truncated with an explicit marker so a reader
# can tell truncation from absence. Chosen well above any observed envelope (~2 KB) but far below
# anything that could fill a disk across a multi-day run.
_MAX_RECORD_BYTES = 64 * 1024


class _State:
    """Process-wide tap state. Resolved once on first use, not at import, so a test (or an
    operator) can set the env var after `relay.capture` is already imported.
    """

    def __init__(self) -> None:
        self.resolved = False
        self.path: Path | None = None
        self.disabled_reason: str | None = None

    def reset(self) -> None:
        self.resolved = False
        self.path = None
        self.disabled_reason = None


_STATE = _State()


def reset_for_tests() -> None:
    """Drop memoized state so a test can re-resolve against a different env var value."""
    _STATE.reset()
    _EXPLICIT_PATHS.clear()


def _resolve() -> Path | None:
    if _STATE.resolved:
        return _STATE.path
    _STATE.resolved = True
    raw = os.environ.get(CAPTURE_DIR_ENV)
    if not raw or not raw.strip():
        _STATE.disabled_reason = f"{CAPTURE_DIR_ENV} unset"
        return None
    try:
        directory = Path(raw).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        # One file per process, so concurrent supervisors/probes never interleave writes into the
        # same file and a reader can attribute every record to one run.
        stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        _STATE.path = directory / f"envelopes-{stamp}-{os.getpid()}.jsonl"
    except OSError as exc:
        _STATE.disabled_reason = f"could not prepare capture dir: {exc}"
        _STATE.path = None
    return _STATE.path


def enabled() -> bool:
    """True when the tap is on. Cheap after the first call."""
    return _resolve() is not None


def capture_path() -> Path | None:
    """The file this process records to, or None when the tap is off."""
    return _resolve()


def disabled_reason() -> str | None:
    """Why the tap is off, for a diagnostic line. None when it is on."""
    _resolve()
    return _STATE.disabled_reason


# Per-directory capture files for `record_to()`. Keyed by resolved directory so every line this
# process records into a given directory lands in one file, exactly as the env-gated tap does.
_EXPLICIT_PATHS: dict[str, Path] = {}


def _explicit_path(directory: Path) -> Path | None:
    key = str(directory)
    existing = _EXPLICIT_PATHS.get(key)
    if existing is not None:
        return existing
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        path = directory / f"envelopes-{stamp}-{os.getpid()}.jsonl"
    except OSError:
        return None
    _EXPLICIT_PATHS[key] = path
    return path


def record_to(directory: Path, line: str, *, seat: str | None = None) -> None:
    """Record `line` into `directory`, independent of `CLAUDE_RELAY_CAPTURE_DIR`.

    For callers that already KNOW where the capture belongs — the Tier-2 harness, which reads a
    child's stdout itself rather than going through `runner.py`.

    This exists because routing that case through the env var was a live bug: the harness set
    `CLAUDE_RELAY_CAPTURE_DIR` in the *child's* environment (correct-looking, since the child is the
    thing being observed) while calling `record_line()` in the *parent*, whose own `os.environ` had
    no such variable. Every call silently no-opped and the harness captured nothing across seven
    successful `claude -p` runs. Worse, the burn's wall detector reads those files, so it could never
    have stopped on a wall — it would have spent its entire cap for zero data.

    The same shape as the original `^RESULT:` detector defect: a code path that looks correct, is
    never exercised by a test that shares its assumptions, and does nothing at all. The unit test
    that "covered" it patched `os.environ` in-process, thereby creating the condition production
    lacked. An explicit destination removes the coupling instead of documenting it.
    """
    path = _explicit_path(directory)
    if path is None:
        return
    _record(path, line, seat=seat)


def record_line(line: str, *, seat: str | None = None) -> None:
    """Record `line` if it is one of the captured envelope types. No-op when the tap is off.

    The env-gated tap, for `runner.py` — the operator switches it on for a whole supervisor run.
    Callers that already know the destination should use `record_to()` instead.

    Never raises. On the first write failure the tap disables itself for the rest of the process
    rather than failing every subsequent line.
    """
    path = _resolve()
    if path is None:
        return
    _record(path, line, seat=seat)


def _record(path: Path, line: str, *, seat: str | None) -> None:
    """Shared filter-and-append. Never raises."""
    if not any(token in line for token in _PREFILTER):
        return
    try:
        envelope = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(envelope, dict) or envelope.get("type") not in _CAPTURED_TYPES:
        return
    record: dict[str, Any] = {
        "captured_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "captured_at_unix": time.time(),
        "pid": os.getpid(),
        "seat": seat,
        "envelope": envelope,
    }
    try:
        blob = json.dumps(record, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return
    if len(blob) > _MAX_RECORD_BYTES:
        # Preserve the calibration-relevant fields even when the envelope as a whole is too big:
        # `rate_limit_info` is small and is the entire point of the capture.
        record["envelope"] = {
            "type": envelope.get("type"),
            "rate_limit_info": envelope.get("rate_limit_info"),
            "subtype": envelope.get("subtype"),
            "is_error": envelope.get("is_error"),
            "modelUsage": envelope.get("modelUsage"),
        }
        record["truncated"] = True
        record["original_bytes"] = len(blob)
        try:
            blob = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(blob + "\n")
    except OSError as exc:
        # Only the env-gated tap self-disables here; an explicit `record_to()` destination belongs to
        # a caller that will see its own empty artifact, and silently disabling ITS writes because
        # one append failed would reintroduce exactly the silent-no-op class of bug this module was
        # just fixed for.
        if _STATE.path == path:
            _STATE.path = None
            _STATE.disabled_reason = f"write failed, tap disabled for this process: {exc}"


def read_records(path: Path) -> list[dict[str, Any]]:
    """Parse a capture file, skipping unparseable lines (a torn tail from a crash is expected and
    must not make the whole file unreadable).
    """
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return records
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def rate_limit_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Just the `rate_limit_event` records, in file order."""
    return [r for r in records if (r.get("envelope") or {}).get("type") == "rate_limit_event"]


def observed_status_vocabulary(records: list[dict[str, Any]]) -> dict[str, int]:
    """`status` value -> occurrence count across every captured `rate_limit_event`.

    This is the direct calibration output: any key here that is not in
    `detector._KNOWN_SAFE_RATE_LIMIT_STATUSES` and is not a genuine blocked state means the
    detector's guess needs revising.
    """
    counts: dict[str, int] = {}
    for record in rate_limit_records(records):
        info = (record.get("envelope") or {}).get("rate_limit_info")
        if not isinstance(info, dict):
            continue
        status = info.get("status")
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + 1
    return counts


def observed_limit_types(records: list[dict[str, Any]]) -> dict[str, int]:
    """`rateLimitType` value -> occurrence count.

    Only `seven_day` has ever been observed, yet claude-relay rotates on the FIVE-HOUR window. A
    `five_hour` key appearing here is the confirmation that the in-run signal covers the window
    the rotation logic actually cares about.
    """
    counts: dict[str, int] = {}
    for record in rate_limit_records(records):
        info = (record.get("envelope") or {}).get("rate_limit_info")
        if not isinstance(info, dict):
            continue
        kind = info.get("rateLimitType")
        if isinstance(kind, str):
            counts[kind] = counts.get(kind, 0) + 1
    return counts
