"""Persistent state: `~/.claude-relay/state.json` (DESIGN.md §6), written atomically
(tmp file + `os.replace`) and `chmod 600`. Holds per-seat cooldown/percent bookkeeping and
notification dedupe — nothing else. No per-unit cost samples in v1 (calibration is Phase 2).

Schema (schemaVersion 1):
    {
      "schemaVersion": 1,
      "seats": {
        "<seatDir>": {
          "hasCreds": true, "cooldownUntil": "<resets_at ISO8601|null>",
          "lastPercent": 49, "lastResetsAt": "...",
          "consecutiveFailures": 0, "note": "needs-login|null"
        }
      },
      "lastNotified": {"all-exhausted": "...", "needs-login:<dir>": "..."},
      "disabledSeats": ["<seatName>", ...],
      "telegramUpdateOffset": 0,
      "repos": {
        "<repoPath>": {
          "cleanBaselineHead": "<git HEAD sha|null>", "cleanBaselineAt": "...",
          "lastStash": {"ref": "...", "at": "...", "files": ["..."]}
        }
      }
    }

`telegramUpdateOffset` is an additive field beyond DESIGN.md's literal §6 example — it is the
Telegram long-poll cursor the resolve-in poller needs to avoid re-processing old updates
(notify.py). `disabledSeats` (also additive) is the operator's dynamic on/off switch, keyed by
seat NAME (`~/.claude-<name>` suffix): a disabled seat still appears in the fleet (`seats` /
`login-check`) but `pick_seat()` skips it. It lives here (runtime state), NOT config.toml,
because claude-relay is stdlib-only and cannot cleanly rewrite TOML — so `disable`/`enable` are
one-command JSON edits. It is distinct from config `exclude`, which hides a dir entirely.
`repos` is also additive: `cleanBaselineHead` is the last git HEAD claude-relay
itself observed this repo clean at (Invariant #6/#7 — a dirty tree is only ever attributed to
claude-relay's own in-flight run when HEAD hasn't moved since that baseline; otherwise triage()
refuses with AWAITING_HUMAN rather than guessing). `lastStash` records the most recent
recovery stash (ref + the file list that was swept) for operator visibility. Both live in this
one atomically-written file rather than a second one, so state stays crash-safe as a unit
(Invariant #7).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def _empty_state() -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "seats": {},
        "lastNotified": {},
        "disabledSeats": [],
        "telegramUpdateOffset": 0,
        "repos": {},
    }


def load_state(state_path: Path) -> dict[str, Any]:
    """Load state.json, tolerating a missing file (fresh install) or corrupt JSON (crash mid-
    write, though the atomic writer below should prevent that) by falling back to empty state
    rather than raising — Invariant #7 (idempotent/crash-safe restart).
    """
    if not state_path.exists():
        return _empty_state()
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    data.setdefault("schemaVersion", SCHEMA_VERSION)
    data.setdefault("seats", {})
    data.setdefault("lastNotified", {})
    data.setdefault("disabledSeats", [])
    data.setdefault("telegramUpdateOffset", 0)
    data.setdefault("repos", {})
    return data


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    """Atomic write: write to a tmp file in the same directory, `os.replace` over the target,
    then `chmod 600` (state may reflect cooldown windows tied to account identity; not secret
    per se, but kept private per DESIGN.md §10 alongside logs).
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(state_path.parent), prefix=".state.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, state_path)
    finally:
        if os.path.exists(tmp_path):  # only if os.replace itself failed
            os.unlink(tmp_path)
    try:
        os.chmod(state_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - best-effort on filesystems without POSIX perms
        pass


def _seat_key(seat_dir: Path | str) -> str:
    return str(seat_dir)


def get_seat_state(state: dict[str, Any], seat_dir: Path | str) -> dict[str, Any]:
    return state["seats"].get(_seat_key(seat_dir), {})


def update_seat(
    state: dict[str, Any],
    seat_dir: Path | str,
    *,
    has_creds: bool | None = None,
    cooldown_until: str | None = "__unset__",
    last_percent: float | None = None,
    last_resets_at: str | None = None,
    last_seen_at: str | None = None,
    consecutive_failures: int | None = None,
    note: str | None = "__unset__",
) -> None:
    """Merge-update one seat's entry. Sentinel `"__unset__"` means "leave unchanged" for the
    two fields (`cooldown_until`, `note`) whose real value is legitimately `None`. `last_seen_at`
    (ISO-8601 of when this seat's usage was last actually observed) is set only when provided —
    it lets a read-only observer (the monitor) label a fallback reading with its age.
    """
    key = _seat_key(seat_dir)
    entry = state["seats"].setdefault(
        key,
        {
            "hasCreds": False,
            "cooldownUntil": None,
            "lastPercent": None,
            "lastResetsAt": None,
            "consecutiveFailures": 0,
            "note": None,
        },
    )
    if has_creds is not None:
        entry["hasCreds"] = has_creds
    if cooldown_until != "__unset__":
        entry["cooldownUntil"] = cooldown_until
    if last_percent is not None:
        entry["lastPercent"] = last_percent
    if last_resets_at is not None:
        entry["lastResetsAt"] = last_resets_at
    if last_seen_at is not None:
        entry["lastSeenAt"] = last_seen_at
    if consecutive_failures is not None:
        entry["consecutiveFailures"] = consecutive_failures
    if note != "__unset__":
        entry["note"] = note


def disabled_seats(state: dict[str, Any]) -> set[str]:
    """Seat NAMES the operator has switched off via `claude-relay disable` — still shown in the
    fleet but skipped by pick_seat(). Keyed by name (the `~/.claude-<name>` suffix), which is
    what both the CLI and pick_seat() work in. Distinct from config `exclude` (which hides a
    directory entirely).
    """
    raw = state.get("disabledSeats") or []
    return {str(x) for x in raw}


def set_seat_disabled(state: dict[str, Any], name: str, disabled: bool) -> bool:
    """Add/remove `name` from the disabled set, keeping it sorted. Returns True if the set
    actually changed, so the CLI can distinguish a real toggle from a no-op ("already disabled").
    """
    current = disabled_seats(state)
    changed = (name in current) != disabled
    if disabled:
        current.add(name)
    else:
        current.discard(name)
    state["disabledSeats"] = sorted(current)
    return changed


def is_in_cooldown(state: dict[str, Any], seat_dir: Path | str, now: dt.datetime | None = None) -> bool:
    """True if this seat's recorded `cooldownUntil` is still in the future. A missing or
    unparsable `cooldownUntil` is treated as "not in cooldown" (fail open toward usability;
    the live usage poll before actually running is the real gate).
    """
    entry = get_seat_state(state, seat_dir)
    until = entry.get("cooldownUntil")
    if not until:
        return False
    try:
        until_dt = dt.datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        return False
    now = now or dt.datetime.now(dt.UTC)
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    return now < until_dt


def clamp_future(
    resets_at: str | None, now: dt.datetime | None = None, min_seconds: float = 0.0
) -> str | None:
    """Clock skew / stale reading guard (DESIGN.md §9): if `resets_at` already lies in the
    past (or isn't parsable), don't trust it as a cooldown boundary — return None so the
    caller treats the seat as not-in-cooldown and re-polls fresh next time instead of
    computing a negative/zero wait from garbage input.
    """
    if not resets_at:
        return None
    try:
        parsed = dt.datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = now or dt.datetime.now(dt.UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    if (parsed - now).total_seconds() < min_seconds:
        return None
    return resets_at


def mark_notified(state: dict[str, Any], key: str, when: dt.datetime | None = None) -> None:
    when = when or dt.datetime.now(dt.UTC)
    state["lastNotified"][key] = when.isoformat()


def was_notified(state: dict[str, Any], key: str) -> bool:
    return key in state.get("lastNotified", {})


def clear_notified(state: dict[str, Any], key: str) -> None:
    state.get("lastNotified", {}).pop(key, None)


def clear_notified_prefix(state: dict[str, Any], prefix: str) -> None:
    """Clear every `lastNotified` key starting with `prefix` — used when a condition class
    (e.g. all `park:{repo}:...` keys) resolves, mirroring the single-key `clear_notified()`
    pattern already used for `needs-login:<seat>`. Without this, a park/error condition that
    recurs LATER with the exact same key (e.g. the same owner-decision id reopened after being
    resolved) would be silently swallowed by a stale dedupe entry from months ago.
    """
    last_notified = state.get("lastNotified", {})
    for key in [k for k in last_notified if k.startswith(prefix)]:
        last_notified.pop(key, None)


def get_telegram_offset(state: dict[str, Any]) -> int:
    return int(state.get("telegramUpdateOffset", 0) or 0)


def set_telegram_offset(state: dict[str, Any], offset: int) -> None:
    state["telegramUpdateOffset"] = int(offset)


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _repo_key(repo: Path | str) -> str:
    return str(repo)


def get_repo_entry(state: dict[str, Any], repo: Path | str) -> dict[str, Any]:
    return state.get("repos", {}).get(_repo_key(repo), {})


def get_clean_baseline(state: dict[str, Any], repo: Path | str) -> str | None:
    """The git HEAD claude-relay last itself observed this repo clean at (or None if it never
    has). Used by gadkit.triage() to decide whether a currently-dirty tree can be safely
    attributed to a claude-relay-initiated run (Invariant #6/#9).
    """
    return get_repo_entry(state, repo).get("cleanBaselineHead")


def set_clean_baseline(state: dict[str, Any], repo: Path | str, head: str | None) -> None:
    """Record `head` as the last-known-clean baseline for `repo`. A no-op if `head` is None
    (e.g. the repo has no commits yet / `git rev-parse HEAD` failed) — recording an unknown
    baseline would make every future dirty state falsely "unattributable."
    """
    if head is None:
        return
    entry = state.setdefault("repos", {}).setdefault(_repo_key(repo), {})
    entry["cleanBaselineHead"] = head
    entry["cleanBaselineAt"] = now_iso()


def record_stash(state: dict[str, Any], repo: Path | str, ref: str, files: list[str]) -> None:
    """Persist the most recent recovery stash's ref + swept file list, for operator visibility
    (`claude-relay status` / manual `git stash list` + state.json inspection).
    """
    entry = state.setdefault("repos", {}).setdefault(_repo_key(repo), {})
    entry["lastStash"] = {"ref": ref, "at": now_iso(), "files": list(files)}


def get_last_stash(state: dict[str, Any], repo: Path | str) -> dict[str, Any] | None:
    return get_repo_entry(state, repo).get("lastStash")
