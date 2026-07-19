"""The rotation loop (DESIGN.md §3): wires fleet/usage/cooldown/runner/gadkit/detector/notify
together. Owns the single-instance lockfile, seat selection, and the park/wait/notify
plumbing around one triage-run-classify iteration.

This module is the "outer loop" — it is deliberately the least unit-tested part of the
package (it spawns real subprocesses, sleeps in real wall-clock time, and makes real network
calls through usage.py/notify.py). The PURE decision logic it calls (fleet discovery, usage
parsing/thresholds, gadkit.triage's decision order, detector.classify) is covered by
`tests/`; this module is exercised by `claude-relay run --dry-run` (zero side effects) and,
ultimately, by a real run against a real repo.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import cooldown, detector, fleet, gadkit, notify, runner
from . import usage as usage_mod
from .config import Config

_MAX_CONSECUTIVE_AGENT_DEAD = 3
_LONG_WAIT_NOTIFY_S = 300.0  # notify the operator if an all-seats wait exceeds ~5 minutes
_SLEEP_CHUNK_S = 30.0  # bounded sleep chunks so the loop can save state/poll telegram
_PARK_RETRIAGE_INTERVAL_S = 60.0
_DEFAULT_RETRY_WAIT_S = 90.0  # fallback wait when no seat gives us a parseable resets_at
# Forced cooldown applied to a seat whose `claude` invocation timed out (runner.py's
# run_timeout_s) — a hung session tells us nothing about the seat's real usage, but we must
# still rotate away from it (finding #7) rather than immediately re-selecting the same seat.
# Guessed value, not empirically tuned — see uncertainty-ledger.jsonl.
_TIMEOUT_COOLDOWN_S = 900.0


class LockError(RuntimeError):
    """Raised when another claude-relay `run` instance already holds the lock."""


def _proc_start_time(pid: int) -> int | None:
    """Field 22 (`starttime`, clock ticks since boot) of `/proc/<pid>/stat`. Returns None on
    any non-Linux platform or if the process is gone — callers MUST degrade gracefully (treat
    None as "can't corroborate," not as "confirmed different process").

    Why: a bare `os.kill(pid, 0)` liveness check alone is fooled by PID reuse — if the PID
    recorded in the lockfile has since exited and the OS handed that same number to an
    unrelated process within the lock's stale-after window, `os.kill(pid, 0)` succeeds and the
    lock is wrongly treated as still held by our own prior instance. Comparing the recorded
    start-time against the CURRENT process holding that PID detects this: a different process
    (even one that reused the PID) will have a different start-time almost always.
    """
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:  # noqa: PTH123 - matches os.kill's pid-based API
            content = f.read()
    except OSError:
        return None
    try:
        # comm (field 2) is parenthesized and may itself contain spaces/parens; split on the
        # LAST ')' so we land after it regardless. Fields 3.. follow, space-separated, so
        # index 19 in that remainder is field 3+19=22 (starttime).
        after_comm = content.rsplit(")", 1)[1]
        starttime = int(after_comm.split()[19])
    except (IndexError, ValueError):
        return None
    return starttime


class SingleInstanceLock:
    """A simple exclusive-create lockfile with age+liveness+start-time-based stale reclaim
    (DESIGN.md §9: "Two instances -> lockfile (stale reclaimed by age)"). Not a distributed
    lock — this tool is explicitly single-repo/single-instance by design (§11).

    Lockfile content is `"<pid>:<starttime>"` (starttime empty if `/proc` is unavailable, e.g.
    non-Linux) so a later reclaim check can distinguish "still our own prior instance" from
    "the OS reused this PID for something else" (finding #6).
    """

    def __init__(self, path: Path, stale_after_s: float = 6 * 3600):
        self.path = path
        self.stale_after_s = stale_after_s
        self._acquired = False

    def _read_lock_contents(self) -> tuple[int | None, int | None]:
        try:
            raw = self.path.read_text().strip()
        except OSError:
            return None, None
        pid_str, _, starttime_str = raw.partition(":")
        try:
            pid = int(pid_str)
        except ValueError:
            return None, None
        starttime = int(starttime_str) if starttime_str else None
        return pid, starttime

    def _read_pid(self) -> int | None:
        return self._read_lock_contents()[0]

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        if age > self.stale_after_s:
            return True
        pid, recorded_starttime = self._read_lock_contents()
        if pid is None:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True  # the PID is gone entirely — unambiguously stale
        except PermissionError:
            return False  # process exists (owned by someone else); assume it's alive
        except OSError:  # pragma: no cover - platform without signal 0 support
            return False
        # The PID exists — but is it genuinely OUR prior instance, or did the OS reuse the PID
        # for an unrelated process within the stale window? Compare start-times when we can.
        current_starttime = _proc_start_time(pid)
        if recorded_starttime is not None and current_starttime is not None:
            return current_starttime != recorded_starttime
        return False  # can't corroborate (non-Linux, or /proc read failed) — fall back to alive

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if self._is_stale():
                try:
                    self.path.unlink()
                except OSError:
                    pass
            else:
                raise LockError(
                    f"another claude-relay run appears active (lock={self.path}, "
                    f"pid={self._read_pid()}) — refusing to start a second instance"
                )
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise LockError(f"lockfile {self.path} appeared concurrently") from exc
        my_pid = os.getpid()
        my_starttime = _proc_start_time(my_pid)
        with os.fdopen(fd, "w") as f:
            f.write(f"{my_pid}:{my_starttime if my_starttime is not None else ''}")
        self._acquired = True

    def release(self) -> None:
        if self._acquired:
            try:
                self.path.unlink()
            except OSError:
                pass
            self._acquired = False

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.release()


# ─────────────────────────────────────────────────────────────────────────────
# Seat bookkeeping + selection
# ─────────────────────────────────────────────────────────────────────────────


def sync_seat_login_state(state: dict[str, Any], seats: list[fleet.Seat]) -> None:
    """Update state.json's per-seat `hasCreds`/`note` from a fresh discovery pass, and clear a
    stale `needs-login` notification once a seat re-logs-in (Invariant #3: a re-logged-in seat
    rejoins the pool automatically, and we stop nagging about it).
    """
    for seat in seats:
        if seat.usable:
            existing = cooldown.get_seat_state(state, seat.path)
            if existing.get("note") == "needs-login":
                cooldown.update_seat(state, seat.path, has_creds=True, note=None)
            cooldown.clear_notified(state, f"needs-login:{seat.path}")
        else:
            cooldown.update_seat(state, seat.path, has_creds=False, note="needs-login")


def _record_usage(
    state: dict[str, Any], seat: fleet.Seat, usage: usage_mod.UsageSnapshot, ceiling_pct: float
) -> None:
    cooldown_until: str | None = None
    if usage_mod.rotate_off(usage, ceiling_pct):
        earliest = usage_mod.earliest_reset(usage)
        cooldown_until = cooldown.clamp_future(earliest.isoformat() if earliest else None)
    # Record the raw 5h gauge (session_percent / session_resets_at), not the normalized session
    # limit — the latter is frequently absent, which is why almas (28% but no active session
    # entry) previously recorded lastPercent=None. These feed status_report and the cooldown key.
    resets = usage_mod.session_resets_at(usage)
    cooldown.update_seat(
        state,
        seat.path,
        has_creds=True,
        cooldown_until=cooldown_until,
        last_percent=usage_mod.session_percent(usage),
        last_resets_at=resets.isoformat() if resets is not None else None,
    )


def pick_seat(
    seats: list[fleet.Seat],
    state: dict[str, Any],
    cache: usage_mod.UsageCache,
    config: Config,
) -> tuple[fleet.Seat | None, usage_mod.UsageSnapshot | None, list[str]]:
    """Among usable, not-in-cooldown seats, prefer the lowest active-session `percent` with
    `percent < ceiling_pct(seat) - start_margin`; if none qualifies, return (None, None, notes)
    and the caller waits for the soonest `resets_at` (DESIGN.md §4). Only non-cooldown
    candidates are polled at all — a seat already known-exhausted (`cooldownUntil` in the
    future) is skipped without a network call ("exhausted seats are known-unavailable until
    their captured resets_at, no polling"). `ceiling_pct` is the SYNTHETIC per-seat rotation
    ceiling (default 70%, config.py `resolve_seat_ceiling()`) — deliberately lower than
    Claude's real 100%, and resolved per-seat so different seats can carry different ceilings.
    """
    notes: list[str] = []
    candidates: list[tuple[fleet.Seat, usage_mod.UsageSnapshot]] = []
    for seat in seats:
        if not seat.usable:
            notes.append(f"needs-login: {seat.name}")
            continue
        if cooldown.is_in_cooldown(state, seat.path):
            notes.append(f"in-cooldown: {seat.name}")
            continue
        try:
            seat_usage = cache.poll(seat.path, ttl=config.poll_ttl)
        except usage_mod.NeedsLoginError:
            notes.append(f"needs-login: {seat.name}")
            continue
        except usage_mod.UsageError as exc:
            notes.append(f"usage-poll-failed: {seat.name}: {exc}")
            continue
        ceiling = config.resolve_seat_ceiling(seat.name)
        _record_usage(state, seat, seat_usage, ceiling)
        if usage_mod.rotate_off(seat_usage, ceiling):
            notes.append(f"rotate-off: {seat.name} (ceiling={ceiling}%)")
            continue
        percent = usage_mod.session_percent(seat_usage)
        start_cap = ceiling - config.start_margin
        if percent < start_cap:
            candidates.append((seat, seat_usage))
        else:
            notes.append(f"above-start-cap: {seat.name} ({percent}% >= {start_cap}%)")

    if not candidates:
        return None, None, notes

    # Spend PERISHABLE capacity first (DESIGN.md §4): among seats that pass the start-cap gate,
    # prefer the one whose 5h window resets SOONEST — its remaining headroom is about to refresh
    # anyway, so using it now costs nothing future, whereas pushing a seat that resets hours from
    # now keeps it elevated (eating its synthetic reservation) for those hours. Tie-break by
    # lowest percent (more headroom => less chance of overshooting the ceiling mid-generation). A
    # seat with no parseable reset time sorts last (treated as maximally far off, least urgent).
    now = dt.datetime.now(dt.UTC)

    def _sort_key(pair: tuple[fleet.Seat, usage_mod.UsageSnapshot]) -> tuple[float, float]:
        resets = usage_mod.session_resets_at(pair[1])
        seconds_to_reset = (resets - now).total_seconds() if resets is not None else float("inf")
        return (seconds_to_reset, usage_mod.session_percent(pair[1]))

    candidates.sort(key=_sort_key)
    best_seat, best_usage = candidates[0]
    return best_seat, best_usage, notes


def _earliest_wait(seats: list[fleet.Seat], state: dict[str, Any]) -> dt.datetime | None:
    candidates: list[dt.datetime] = []
    for seat in seats:
        if not seat.usable:
            continue
        entry = cooldown.get_seat_state(state, seat.path)
        until = entry.get("cooldownUntil")
        if not until:
            continue
        try:
            parsed = dt.datetime.fromisoformat(str(until).replace("Z", "+00:00"))
        except ValueError:
            continue
        candidates.append(parsed)
    return min(candidates) if candidates else None


def _wait_seconds(wait_until: dt.datetime | None, default_s: float = _DEFAULT_RETRY_WAIT_S) -> float:
    if wait_until is None:
        return default_s
    now = dt.datetime.now(dt.UTC)
    if wait_until.tzinfo is None:
        wait_until = wait_until.replace(tzinfo=dt.UTC)
    return max(0.0, (wait_until - now).total_seconds())


# ─────────────────────────────────────────────────────────────────────────────
# One iteration (triage -> [pick seat -> run -> classify])
# ─────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class IterationResult:
    plan: gadkit.Plan
    action: detector.Action | None
    seat: fleet.Seat | None = None
    run_result: runner.RunResult | None = None
    outcome: str | None = None
    seat_notes: list[str] = dataclasses.field(default_factory=list)
    wait_until: dt.datetime | None = None


def run_once(
    repo: Path, config: Config, state: dict[str, Any], cache: usage_mod.UsageCache
) -> IterationResult:
    """Exactly one iteration of DESIGN.md §3's loop body: triage, and for RUN/FINISH plans,
    pick a seat, run `claude`, take a fresh post-run usage reading, and classify the outcome.
    """
    plan = gadkit.triage(repo, config, state)

    if plan.kind == "DONE":
        return IterationResult(plan=plan, action=detector.Action(detector.DONE, plan.detail))
    if plan.kind in ("AWAITING_HUMAN", "BLOCKED"):
        return IterationResult(plan=plan, action=detector.Action(detector.NOTIFY_PARK, plan.detail))

    seats = fleet.discover_seats(config.effective_exclude())
    sync_seat_login_state(state, seats)
    seat, _seat_usage, notes = pick_seat(seats, state, cache, config)
    if seat is None:
        return IterationResult(
            plan=plan, action=None, seat_notes=notes, wait_until=_earliest_wait(seats, state)
        )

    pre = gadkit.snapshot(repo)
    argv = gadkit.command(plan)
    result = runner.run(
        argv,
        repo=repo,
        config_dir=seat.path,
        log_dir=config.log_dir,
        seat_name=seat.name,
        timeout_s=config.run_timeout_s,
    )

    ceiling = config.resolve_seat_ceiling(seat.name)
    try:
        post_usage = cache.poll(seat.path, ttl=config.poll_ttl, force=True)
    except usage_mod.UsageError:
        post_usage = None
    if post_usage is not None:
        _record_usage(state, seat, post_usage, ceiling)

    if result.timed_out:
        # A hung `claude` process tells us nothing reliable about this seat's real usage — but
        # we must still rotate away from it rather than immediately re-selecting the same seat
        # (finding #7: "the loop treats [a timeout] as rotate/retry"). Force a short cooldown so
        # pick_seat() naturally avoids it next iteration regardless of what outcome() concludes.
        timeout_cooldown_until = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=_TIMEOUT_COOLDOWN_S)
        cooldown.update_seat(state, seat.path, cooldown_until=timeout_cooldown_until.isoformat())

    post = gadkit.snapshot(repo)
    outcome_bucket = gadkit.outcome(pre, post, post_usage, ceiling)
    action = detector.classify(outcome_bucket, post_usage, result.tail)
    return IterationResult(
        plan=plan, action=action, seat=seat, run_result=result, outcome=outcome_bucket, seat_notes=notes
    )


# ─────────────────────────────────────────────────────────────────────────────
# dry-run preview (zero side effects: no stash, no subprocess, no claude invocation)
# ─────────────────────────────────────────────────────────────────────────────


def dry_run_preview(repo: Path, config: Config, state: dict[str, Any]) -> dict[str, Any]:
    """Preview exactly what `run` would do next: the triage Plan, the chosen seat (still a
    real, read-only usage poll — not a repo/process side effect), and the exact argv. Never
    stashes (triage is called with `dry_run=True`) and never spawns `claude`.
    """
    plan = gadkit.triage(repo, config, copy.deepcopy(state), dry_run=True)
    preview: dict[str, Any] = {
        "repo": str(repo),
        "plan_kind": plan.kind,
        "gen": plan.gen,
        "detail": plan.detail,
    }
    if plan.kind in ("RUN", "FINISH"):
        preview["argv"] = gadkit.command(plan)
        seats = fleet.discover_seats(config.effective_exclude())
        cache = usage_mod.UsageCache()
        seat, seat_usage, notes = pick_seat(seats, copy.deepcopy(state), cache, config)
        preview["seat"] = seat.name if seat else None
        preview["seat_notes"] = notes
        if seat_usage is not None:
            preview["seat_percent"] = usage_mod.session_percent(seat_usage)
    return preview


def status_report(config: Config, state: dict[str, Any]) -> dict[str, Any]:
    """Offline status snapshot (no live usage poll — reads only state.json's last-known
    readings) plus, if a repo is configured, the current triage plan (in dry-run mode, so
    `status` itself never mutates the repo).
    """
    seats = fleet.discover_seats(config.effective_exclude())
    seat_rows = []
    for seat in seats:
        entry = cooldown.get_seat_state(state, seat.path)
        seat_rows.append(
            {
                "name": seat.name,
                "path": str(seat.path),
                "usable": seat.usable,
                "needs_login": seat.needs_login,
                "cooldownUntil": entry.get("cooldownUntil"),
                "lastPercent": entry.get("lastPercent"),
                "consecutiveFailures": entry.get("consecutiveFailures", 0),
                "note": entry.get("note"),
            }
        )
    report: dict[str, Any] = {"seats": seat_rows}
    if config.repo:
        plan = gadkit.triage(Path(config.repo), config, copy.deepcopy(state), dry_run=True)
        report["plan"] = {"kind": plan.kind, "gen": plan.gen, "detail": plan.detail}
    return report


# ─────────────────────────────────────────────────────────────────────────────
# The outer driver: sleep/park/notify plumbing around run_once()
# ─────────────────────────────────────────────────────────────────────────────


def _sleep_and_poll(total_s: float, config: Config, state: dict[str, Any], repo: Path) -> None:
    remaining = total_s
    while remaining > 0:
        chunk = min(_SLEEP_CHUNK_S, remaining)
        time.sleep(chunk)
        remaining -= chunk
        notify.poll_telegram_updates(
            config, state, repo, status_provider=lambda: f"claude-relay: waiting on seat cooldowns for {repo}"
        )
        cooldown.save_state(config.state_path, state)


def _park_and_wait(repo: Path, config: Config, state: dict[str, Any]) -> None:
    """Block until the repo is no longer AWAITING_HUMAN/BLOCKED, opportunistically polling
    Telegram (so `resolve <id> <answer>` / `status` keep working while parked) and re-triaging
    periodically. Single-repo v1 has no other work to serve while parked (DESIGN.md §11). Uses
    `dry_run=True` (never stashes while merely re-checking a parked repo — the real stash
    decision, if any, happens the next time `run_once()` triages for real) but passes the REAL
    `state` object so clean-baseline bookkeeping legitimately updates while parked.
    """
    while True:
        notify.poll_telegram_updates(
            config, state, repo, status_provider=lambda: f"claude-relay: parked on {repo}"
        )
        cooldown.save_state(config.state_path, state)
        plan = gadkit.triage(repo, config, state, dry_run=True)
        if plan.kind not in ("AWAITING_HUMAN", "BLOCKED"):
            return
        time.sleep(_PARK_RETRIAGE_INTERVAL_S)


def _park_notify_key(repo: Path, plan: gadkit.Plan) -> str:
    """Build the notify-dedupe key for a NOTIFY_PARK action. MUST change whenever the actual
    blocking condition changes (finding #2): a different owner-decision id (or, lacking one, a
    different `detail` string) always yields a fresh key, so a genuinely new reason to park is
    never silently swallowed by a stale dedupe entry from a previous, different park.
    """
    if plan.blocking_decision_ids:
        distinguishing = ",".join(sorted(plan.blocking_decision_ids))
    else:
        distinguishing = hashlib.sha256(plan.detail.encode("utf-8")).hexdigest()[:16]
    return f"park:{repo}:{plan.kind}:{plan.gen}:{distinguishing}"


def _park_message(repo: Path, plan: gadkit.Plan) -> str:
    """The human-facing park notification. For a gated-owner-decision park, spell out each
    decision's QUESTION and the exact `resolve <id> <answer>` line to reply — so the operator
    reacts to a complete prompt instead of just an id list they'd have to look up (matches the
    Telegram help fallback). Falls back to the concise `kind: detail` for handoff/other parks.
    """
    if plan.blocking_decision_ids:
        ids = set(plan.blocking_decision_ids)
        decisions = [d for d in gadkit.open_owner_decisions(repo) if str(d.get("id")) in ids]
        block = gadkit.format_decisions_for_operator(decisions, gen=plan.gen)
        if block:
            return f"claude-relay parked {repo.name} — needs your decision.\n{block}"
    return f"{plan.kind}: {plan.detail}"


def run(
    repo: Path,
    config: Config,
    *,
    once: bool = False,
) -> int:
    """The full rotation loop. Acquires the single-instance lock, then repeats `run_once()`
    until DONE, a hard error, `max_units` completed units, or (with `once=True`) exactly one
    iteration. Returns a process exit code.
    """
    lock = SingleInstanceLock(config.state_dir / "claude-relay.lock")
    with lock:
        state = cooldown.load_state(config.state_path)
        cache = usage_mod.UsageCache()
        consecutive_agent_dead = 0
        units_completed = 0
        try:
            while True:
                iteration = run_once(repo, config, state, cache)
                cooldown.save_state(config.state_path, state)

                # A genuinely completed RUN/FINISH unit (an actual `claude` invocation
                # happened) counts toward `max_units`, regardless of its outcome — even a
                # HIT_WALL run consumed real effort (finding #8).
                if iteration.run_result is not None:
                    units_completed += 1

                # Once we're not currently parked, any stale `park:{repo}:...` dedupe entries
                # from a PRIOR, different blocking condition are no longer relevant — clear
                # them so a recurrence of that same condition later re-notifies (mirrors the
                # needs-login clear in sync_seat_login_state; finding #2).
                if iteration.plan.kind not in ("AWAITING_HUMAN", "BLOCKED"):
                    cooldown.clear_notified_prefix(state, f"park:{repo}:")

                # Recovery stash notification (low priority, finding #5b): every stash gets a
                # unique key (the message embeds a timestamp), so this never dedupes-forever.
                if iteration.plan.kind == "RUN" and iteration.plan.stashed_ref is not None:
                    notify.notify(
                        config,
                        state,
                        f"stash:{repo}:{iteration.plan.stashed_ref}",
                        f"claude-relay parked a partial generation via git stash "
                        f"(ref={iteration.plan.stashed_ref!r}) before restarting gen "
                        f"{iteration.plan.gen} on {repo}.",
                    )
                    cooldown.save_state(config.state_path, state)

                if config.max_units > 0 and units_completed >= config.max_units:
                    notify.notify(
                        config,
                        state,
                        f"max-units-reached:{repo}",
                        f"claude-relay: reached configured max_units={config.max_units} for "
                        f"{repo}; stopping cleanly ({units_completed} unit(s) run this session).",
                        force=True,
                    )
                    cooldown.save_state(config.state_path, state)
                    return 0

                if iteration.action is None:
                    wait_s = _wait_seconds(iteration.wait_until)
                    if wait_s > _LONG_WAIT_NOTIFY_S:
                        notify.notify(
                            config,
                            state,
                            "all-exhausted",
                            f"All seats exhausted or in cooldown for {repo}; "
                            f"waiting ~{int(wait_s // 60)}m (until {iteration.wait_until}).",
                        )
                        cooldown.save_state(config.state_path, state)
                    if once:
                        return 0
                    _sleep_and_poll(wait_s, config, state, repo)
                    continue

                kind = iteration.action.kind
                if kind in (detector.CONTINUE, detector.CONTINUE_ROTATE):
                    consecutive_agent_dead = 0
                    if once:
                        return 0
                    continue

                if kind == detector.RETRY:
                    consecutive_agent_dead += 1
                    if consecutive_agent_dead >= _MAX_CONSECUTIVE_AGENT_DEAD:
                        # force=True (finding #2): a HARD_ERROR must never be permanently
                        # swallowed by a static dedupe key — the operator needs to see EVERY
                        # occurrence, not just the first one ever.
                        notify.notify(
                            config,
                            state,
                            f"hard-error:{repo}",
                            f"HARD_ERROR: {consecutive_agent_dead} consecutive non-limit "
                            f"failures on {repo}; parking rather than burning tokens in a "
                            f"crash loop. Last reason: {iteration.action.reason}",
                            force=True,
                        )
                        cooldown.save_state(config.state_path, state)
                        return 1
                    if once:
                        return 0
                    continue

                if kind == detector.NOTIFY_PARK:
                    key = _park_notify_key(repo, iteration.plan)
                    notify.notify(config, state, key, _park_message(repo, iteration.plan))
                    cooldown.save_state(config.state_path, state)
                    if once:
                        return 0
                    _park_and_wait(repo, config, state)
                    consecutive_agent_dead = 0
                    continue

                if kind == detector.DONE:
                    notify.notify(config, state, f"done:{repo}", f"DONE: {iteration.plan.detail}", force=True)
                    cooldown.save_state(config.state_path, state)
                    return 0

                print(f"[claude-relay] unrecognized action kind {kind!r}; stopping", file=sys.stderr)
                return 1
        finally:
            cooldown.save_state(config.state_path, state)
