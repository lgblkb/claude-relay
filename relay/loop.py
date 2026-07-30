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

from . import cooldown, detector, fleet, gadkit, notify, plugins, runner
from . import usage as usage_mod
from .config import Config

_MAX_CONSECUTIVE_AGENT_DEAD = 3

# The `lastNotified` dedupe key for "every seat is exhausted or cooling". A NAMED constant, used by
# both the notify site and the clear site below, because the bug this fixes was born of a bare
# string literal that existed at exactly one of those two places (see `_clear_exhausted_notice()`).
_ALL_EXHAUSTED_KEY = "all-exhausted"
# Distinct from the key above because the two conditions call for opposite operator responses: an
# exhausted pool needs patience, a below-the-floor pool needs a config change and will never recover
# on its own. Sharing one dedupe key would also let whichever fired first silence the other.
_BELOW_FLOOR_KEY = "all-below-token-floor"

_LONG_WAIT_NOTIFY_S = 300.0  # notify the operator if an all-seats wait exceeds ~5 minutes
_SLEEP_CHUNK_S = 30.0  # bounded sleep chunks so the loop can save state/poll telegram
_PARK_RETRIAGE_INTERVAL_S = 60.0
_DEFAULT_RETRY_WAIT_S = 90.0  # fallback wait when no seat gives us a parseable resets_at
# Forced cooldown applied by `_force_cooldown()` to a seat whose run's outcome is untrustworthy
# as a usage signal: either the `claude` invocation timed out (runner.py's run_timeout_s — a
# hung session tells us nothing about the seat's real usage, finding #7), or (A1 audit fix) the
# run was classified AGENT_DEAD_NONLIMIT/CONTINUE_ROTATE via the probe-confirmed workflow-limit
# signature or the usage-unavailable backstop — a bucket that, by construction, never got a
# cooldown from the normal `_record_usage()`/`rotate_off()` path. Either way we must still
# rotate away from the seat rather than immediately re-selecting it. Guessed value, not
# empirically tuned — see uncertainty-ledger.jsonl.
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
        """B1 audit fix (reordered): liveness + start-time corroboration decide FIRST; the mtime
        AGE check is only a fallback for the one case neither can settle (non-Linux, or a /proc
        read race). The prior order checked age BEFORE liveness and short-circuited on it, so a
        healthy multi-day crawl (the exact workload this tool exists for) became unconditionally
        "stale" the moment it crossed `stale_after_s`, regardless of whether the holding process
        was still alive — reproduced with two concurrent instances. Pairs with `heartbeat()`
        (called once per outer loop iteration and per sleep/park chunk): a genuinely live
        instance's lock mtime is now refreshed far more often than `stale_after_s`, so the age
        fallback below only ever fires for a lock nothing is actively renewing.
        """
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
        # Can't corroborate via /proc (non-Linux, or the read raced/failed) — liveness alone
        # said the PID is alive, but PID reuse within the stale window could fool that too.
        # Fall back to the mtime age as the ONLY remaining signal, in this uncorroborated case
        # exclusively.
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        return age > self.stale_after_s

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

    def heartbeat(self) -> None:
        """Refresh the lockfile's mtime (B1 audit fix). Previously mtime was written exactly
        once, in `acquire()`, and never touched again — so `_is_stale()`'s age check made ANY
        run longer than `stale_after_s` (default 6h) unconditionally reclaimable, regardless of
        whether the holding process was still alive. Call this once per outer loop iteration
        AND once per sleep/park-poll chunk (`_sleep_and_poll()`/`_park_and_wait()` already loop
        every 30-60s to save state and poll Telegram — piggyback on that cadence) so a healthy
        instance's lock never goes more than about a minute without being renewed. No-op if we
        don't currently hold the lock.

        Uncertainty a reviewer should check (2026-07-26 review, recorded not fixed): this is
        NEVER called from INSIDE a single `run_once()` invocation — that call blocks
        synchronously on `runner.run()` for up to `config.run_timeout_s` (default 2h) with no
        heartbeat in between. On Linux this is harmless: `_is_stale()` checks liveness +
        `/proc`-start-time corroboration FIRST, and a live process passes that check regardless
        of how stale the mtime has gotten, so the age fallback below is never reached at all for
        a genuinely running instance. On a non-Linux host (where `_proc_start_time()` always
        returns `None`, per its own docstring), that corroboration is unavailable — liveness
        (`os.kill(pid, 0)`) alone cannot distinguish "our own live long-running instance" from
        "PID reuse," so `_is_stale()` falls through to the mtime AGE check as the SOLE signal,
        and a single `run_once()` longer than `stale_after_s` could then be wrongly reclaimed on
        such a host. Not reproduced (this environment is Linux); flagged as a known gap rather
        than fixed, since closing it would need heartbeating from inside `runner.run()`'s own
        read loop (a cross-module coupling out of this round's scope).
        """
        if not self._acquired:
            return
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def release(self) -> None:
        if self._acquired:
            # B1 audit fix: verify the file still names US (pid AND, when available, start-time)
            # before unlinking. Previously `release()` unlinked unconditionally — if this
            # instance's OWN stale-reclaim window had already elapsed (e.g. a long GC pause, or
            # simply the old age-first `_is_stale()` bug) and a second instance reclaimed the
            # lock in the meantime, this instance's own `release()` on exit would then delete
            # the SECOND instance's lock out from under it, letting a third instance start
            # concurrently with the second. Our own PID can never legitimately collide with
            # itself, so a pid mismatch alone is conclusive; a start-time mismatch (when both
            # sides are readable) is treated the same way, defensively.
            pid, recorded_starttime = self._read_lock_contents()
            still_ours = pid == os.getpid()
            if still_ours and recorded_starttime is not None:
                current_starttime = _proc_start_time(os.getpid())
                if current_starttime is not None and current_starttime != recorded_starttime:
                    still_ours = False  # shouldn't happen for our own live PID; stay conservative
            if still_ours:
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
        last_seen_at=cooldown.now_iso(),
    )


# B8 audit fix: a seat whose usage-endpoint poll fails with HTTP 401 (token present but
# rejected) can ONLY recover via a `claude` launch — that's what refreshes its token
# (runner.py's module docstring, confirmed live) — but a 401 poll failure is a `UsageError`
# like any other, and `pick_seat()` used to lump it into the generic "usage-poll-failed" skip,
# which declines the one launch that would fix it. Left unfixed, seats age out one at a time
# until the whole pool is 401'd (feeding B2's all-exhausted stall) with zero notification ever.
# Policy (a deliberate choice among the audit's two named options — this combines both): give a
# 401 seat up to `_MAX_AUTH_REFRESH_ATTEMPTS` bounded chances to be selected anyway, purely so
# the next `claude` launch can refresh its token — but ONLY as a last resort (never outcompeting
# a seat with a genuine live reading, so a speculative refresh-launch can't starve healthy work).
# Past the budget, stop offering it (an indefinitely-dead refresh_token would otherwise be
# retried forever) and notify the operator once that manual re-login is needed; a successful poll
# at ANY time afterward (automated recovery within budget, or the operator manually re-logging in
# after exhaustion) resets the counter and clears the notification, so this converges either way.
#
# RESIDUAL-GAP FIX (2026-07-30): B8's fix above closed the case the audit named — ALL seats 401,
# total stall. It left the PARTIAL-pool case open, and that case is the common one. The budget
# used to be charged during the per-seat scan, i.e. on every poll, whether or not the seat was
# ever handed back for a launch. Since a 401 seat is only ever RETURNED from the last-resort
# block (and a launch is the only thing that can refresh a token), any seat that 401'd while a
# healthy sibling existed was charged a strike for a recovery attempt that never happened —
# three iterations later it locked out permanently, with a "log in again" alert for an account
# that was perfectly healthy and one launch away from fine. Because `pick_seat` concentrates
# work on the seat with the soonest reset, idle/secondary seats are exactly the ones this hit,
# so every underused seat in a multi-seat fleet was on a deterministic path to false lockout.
# Field evidence: a 3-iteration overnight run charged an untouched, fully-healthy seat 3 strikes.
# The budget now measures FAILED RECOVERY ATTEMPTS (launches we actually handed out that still
# came back 401), not "times we noticed a stale token while busy with something better".
# Accepted tradeoff: a genuinely dead refresh_token now takes longer to reach the operator alert,
# because it must actually be needed as a last resort N times first — that is precisely the
# all-seats-down situation B8's fix already targets, so nothing is lost there. A stale token seen
# on a healthy-pool iteration is surfaced in `notes` (`auth-stale:`) instead of being charged.
_MAX_AUTH_REFRESH_ATTEMPTS = 3


def pick_seat(
    seats: list[fleet.Seat],
    state: dict[str, Any],
    cache: usage_mod.UsageCache,
    config: Config,
    *,
    dry_run: bool = False,
) -> tuple[fleet.Seat | None, usage_mod.UsageSnapshot | None, list[str]]:
    """Among usable, not-in-cooldown seats, prefer the lowest active-session `percent` with
    `percent < ceiling_pct(seat) - start_margin`; if none qualifies, return (None, None, notes)
    and the caller waits for the soonest `resets_at` (DESIGN.md §4). Only non-cooldown
    candidates are polled at all — a seat already known-exhausted (`cooldownUntil` in the
    future) is skipped without a network call ("exhausted seats are known-unavailable until
    their captured resets_at, no polling"). `ceiling_pct` is the SYNTHETIC per-seat rotation
    ceiling (default 70%, config.py `resolve_seat_ceiling()`) — deliberately lower than
    Claude's real 100%, and resolved per-seat so different seats can carry different ceilings.

    See the `_MAX_AUTH_REFRESH_ATTEMPTS` comment above for the B8 401-recovery policy: a seat
    within its bounded retry budget is offered as a LAST-RESORT candidate (returned with
    `usage=None`) only when no seat with a genuine live reading is available. The retry budget is
    charged ONLY for the seat this function actually hands back for a launch, never merely for
    observing a 401 during the scan (see the RESIDUAL-GAP FIX note above).

    `dry_run=True` makes this genuinely side-effect-free: no `consecutiveFailures` write and, the
    part that actually mattered, no `notify()` — `dry_run_preview()` passes a `copy.deepcopy` of
    `state`, which already discarded the counter write AND the notify dedupe bookkeeping, so an
    operator sitting at the exhaustion boundary got a real, never-deduplicated Telegram message on
    EVERY `--dry-run` from something documented as having zero side effects.
    """
    notes: list[str] = []
    disabled = cooldown.disabled_seats(state)
    candidates: list[tuple[fleet.Seat, usage_mod.UsageSnapshot]] = []
    # Seats that 401'd on this scan. Deliberately NOT charged here — see the last-resort block.
    auth_401_seats: list[fleet.Seat] = []
    for seat in seats:
        if seat.name in disabled:
            notes.append(f"disabled: {seat.name}")
            continue
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
            if usage_mod.is_auth_error(exc):
                # Collect only. The budget is charged in the last-resort block below, for the ONE
                # seat actually handed back — a strike must mean "a launch was spent on this seat
                # and it still 401'd", not "we polled it while a better seat was available".
                auth_401_seats.append(seat)
                continue
            notes.append(f"usage-poll-failed: {seat.name}: {exc}")
            continue
        # A live 200 proves the token is fine again — reset the 401 streak and let a NEW streak
        # (if this seat ever 401s again later) notify again rather than staying deduped forever.
        cooldown.update_seat(state, seat.path, consecutive_failures=0)
        cooldown.clear_notified(state, f"auth-exhausted:{seat.path}")
        ceiling = config.resolve_seat_ceiling(seat.name)
        _record_usage(state, seat, seat_usage, ceiling)
        if usage_mod.rotate_off(seat_usage, ceiling):
            notes.append(f"rotate-off: {seat.name} (ceiling={ceiling}%)")
            continue
        percent = usage_mod.session_percent(seat_usage)
        start_cap = ceiling - config.start_margin
        if percent >= start_cap:
            notes.append(f"above-start-cap: {seat.name} ({percent}% >= {start_cap}%)")
            continue
        # The soft usage ceiling's LAUNCH GATE (2026-07-30). `start_margin` asks the coarse
        # question "is this seat far enough below its ceiling to bother starting?"; this asks the
        # quantitative one, "does the headroom that is left convert to enough output tokens to fund
        # a phase?" — and a seat that fails it must be SKIPPED, not started with a tiny allowance.
        # Starting it anyway is the livelock `Config.launch_budget_for()` documents: gad-kit pauses
        # before its first gated phase, relay rotates, the next seat does the same, and the pool
        # burns one launch per seat forever while committing nothing.
        #
        # Inert unless `derive_token_target` is on, in which case `startable` is always True and
        # this is a no-op — the selection order and outcome are bit-for-bit unchanged.
        launch_budget = config.launch_budget_for(
            seat.name,
            percent,
            learned=cooldown.learned_tokens_per_percent(state, seat.path),
        )
        if not launch_budget.startable:
            notes.append(f"below-token-floor: {seat.name}: {launch_budget.reason}")
            continue
        candidates.append((seat, seat_usage))

    if not candidates:
        # Last resort: nothing has a live reading, so offer a 401 seat purely so the next `claude`
        # launch can refresh its token. This is the ONLY place the budget is charged, and only for
        # the seat we actually return — one strike per launch handed out.
        for seat in auth_401_seats:
            charged = int(cooldown.get_seat_state(state, seat.path).get("consecutiveFailures", 0))
            attempts = charged + 1
            if attempts > _MAX_AUTH_REFRESH_ATTEMPTS:
                notes.append(
                    f"auth-exhausted: {seat.name} ({charged} refresh launches spent, all still 401)"
                )
                # Not force=True: this must fire once, then stay deduped (mirrors the needs-login
                # pattern) — the failure recurs every iteration a permanently dead refresh_token
                # keeps 401ing, and force=True here would spam. Suppressed entirely under dry_run:
                # a preview must not message the operator.
                if not dry_run:
                    notify.notify(
                        config,
                        state,
                        f"auth-exhausted:{seat.path}",
                        f"claude-relay: seat {seat.name} was launched "
                        f"{charged} times to refresh its token and still fails usage-endpoint "
                        f"auth (HTTP 401), so it will no longer be auto-retried. Restore it by "
                        f"logging in again for this seat (CLAUDE_CONFIG_DIR={seat.path}) — or, if "
                        f"this seat was created by `claude-relay adopt`, re-run `claude-relay "
                        f"adopt` instead, since its credentials are a copy that never sees the "
                        f"source account's token refreshes.",
                    )
                continue
            if dry_run:
                # Report what WOULD be charged. Without this the preview printed `charged + 1` as
                # though it were persisted, which reads as one strike more than reality.
                notes.append(
                    f"auth-refresh-attempt: {seat.name} (would be attempt "
                    f"{attempts}/{_MAX_AUTH_REFRESH_ATTEMPTS}; not charged by a preview)"
                )
            else:
                cooldown.update_seat(state, seat.path, consecutive_failures=attempts)
                notes.append(
                    f"auth-refresh-attempt: {seat.name} ({attempts}/{_MAX_AUTH_REFRESH_ATTEMPTS})"
                )
            return seat, None, notes
        return None, None, notes

    # A healthy seat won, so every 401 seat above goes UNCHARGED — surface it so a stale token is
    # still visible to the operator (in `seats`/`--dry-run` notes) without consuming its budget.
    for seat in auth_401_seats:
        notes.append(f"auth-stale: {seat.name} (401 on poll; budget not charged, a live seat won)")

    # Spend PERISHABLE capacity first (DESIGN.md §4): among seats that pass the start-cap gate,
    # prefer the one whose 5h window resets SOONEST — its remaining headroom is about to refresh
    # anyway, so using it now costs nothing future, whereas pushing a seat that resets hours from
    # now keeps it elevated (eating its synthetic reservation) for those hours. Tie-break by
    # lowest percent (more headroom => less chance of overshooting the ceiling mid-generation). A
    # seat with no parseable reset time sorts last (treated as maximally far off, least urgent).
    now = dt.datetime.now(dt.UTC)

    def _sort_key(pair: tuple[fleet.Seat, usage_mod.UsageSnapshot]) -> tuple[float, float]:
        resets = usage_mod.session_resets_at(pair[1])
        # B11 audit fix: floor at zero. `resets` can be a STALE/pinned reading (see
        # `UsageCache.extra_ttl_s`) that is already in the past by the time this sorts — an
        # unfloored `(resets - now)` goes NEGATIVE, which then sorts BEFORE every genuinely
        # imminent seat under "spend perishable capacity first," inverting the very policy this
        # sort implements (a stale snapshot is the least trustworthy signal, not the most urgent).
        seconds_to_reset = (
            max(0.0, (resets - now).total_seconds()) if resets is not None else float("inf")
        )
        # Uncertainty a reviewer should check: this floor fixes RELATIVE staleness ranking among
        # MULTIPLE past-due entries (the most-stale one no longer sorts first among them) — it
        # does NOT guarantee "a stale reading never beats a genuinely-future one." Two floored-
        # to-zero seats tie on `seconds_to_reset`, so the percent tie-break decides, which could
        # still let a stale-but-low-percent seat outrank a real-but-imminent higher-percent one.
        return (seconds_to_reset, usage_mod.session_percent(pair[1]))

    candidates.sort(key=_sort_key)
    best_seat, best_usage = candidates[0]
    return best_seat, best_usage, notes


def _earliest_wait(seats: list[fleet.Seat], state: dict[str, Any]) -> dt.datetime | None:
    """The soonest time among usable, non-disabled seats that could plausibly become selectable
    again on their own: a disabled seat's timing alone should never dictate what we sleep for
    (there is no automatic path back to usable), and a timestamp that has already elapsed is not
    a real future wait at all.

    B2 audit fix: this used to take `min()` over EVERY seat's `cooldownUntil` verbatim — with no
    "usable" exclusion for disabled seats and, crucially, no check that the timestamp was still
    in the future. A stale/past `cooldownUntil` (a disabled seat's old reading, a poll-failed
    seat's last-known value, or simple clock skew) then fed `_wait_seconds()` a non-positive
    delta, which `_sleep_and_poll()`'s `while remaining > 0:` treats as "nothing to sleep" —
    a zero-sleep busy loop that also stops Telegram polling (since that only happens INSIDE the
    sleep chunks) and suppresses the long-wait notification (`0 < _LONG_WAIT_NOTIFY_S`).

    B9 audit fix: a seat sitting in the "dead band" between `start_cap` and `ceiling_pct` (below
    the rotate-off ceiling, but above the start-cap margin `pick_seat()` requires to actually
    select it) never gets a `cooldownUntil` at all — `rotate_off()` is false by construction, so
    `_record_usage()` never sets one. If EVERY usable seat is simultaneously in this dead band,
    the old cooldown-only version returned `None` here regardless, which `_wait_seconds()` turns
    into the bare ~90s default retry wait — a silent poll storm (400 usage polls per 5h window,
    each with a full `triage()`) that also stays under `_LONG_WAIT_NOTIFY_S`, so the operator
    never hears about it. `pick_seat()` records `lastResetsAt` for EVERY seat it successfully
    polls, dead-band or not (see `_record_usage()`), so that reading — the seat's own 5h window
    reset, a real future time after which its percent very plausibly drops back under the start
    cap — is now ALSO a candidate wait target, not just an explicit `cooldownUntil`.
    """
    now = dt.datetime.now(dt.UTC)
    disabled = cooldown.disabled_seats(state)
    candidates: list[dt.datetime] = []
    for seat in seats:
        if not seat.usable or seat.name in disabled:
            continue
        entry = cooldown.get_seat_state(state, seat.path)
        for raw in (entry.get("cooldownUntil"), entry.get("lastResetsAt")):
            if not raw:
                continue
            try:
                parsed = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.UTC)
            if parsed <= now:
                continue  # already elapsed — mirrors is_in_cooldown()'s own `now < until_dt` check
            candidates.append(parsed)
    return min(candidates) if candidates else None


# B2 fix: a floor so a cooldown that is technically still in the future but only by a hair (a
# race between _earliest_wait()'s `now` and this function's own `now`, or a resets_at a few
# milliseconds out) can never compute to an effectively-zero sleep and spin the loop hot.
_MIN_WAIT_S = 5.0
# B17 fix: a ceiling so a legitimately distant `cooldownUntil` (a weekly-limit reset can be up to
# ~7 days out — see usage.py's `earliest_reset()`) or an outright-corrupt one (bad RTC, garbage
# `resets_at`) never turns into a single multi-day-or-longer sleep. Capping here (rather than
# only in `clamp_future()`) means the loop always re-polls Telegram / re-checks seat state at
# least this often regardless of WHY the wait looked long, closing the "parks the supervisor for
# up to a year" failure mode DESIGN.md §9 promises clamping against.
_MAX_WAIT_S = 5.0 * 3600.0


def _wait_seconds(wait_until: dt.datetime | None, default_s: float = _DEFAULT_RETRY_WAIT_S) -> float:
    if wait_until is None:
        return default_s
    now = dt.datetime.now(dt.UTC)
    if wait_until.tzinfo is None:
        wait_until = wait_until.replace(tzinfo=dt.UTC)
    return max(_MIN_WAIT_S, min(_MAX_WAIT_S, (wait_until - now).total_seconds()))


def _clear_exhausted_notice(config: Config, state: dict[str, Any]) -> bool:
    """Re-arm the `all-exhausted` notification because a seat became available again. Returns True
    if the key was actually present and cleared.

    Persisting is guarded on a real change: this is called on every iteration that selects a seat
    — the common, healthy path — and an unconditional `save_state()` there would add a disk write
    per generation to accomplish nothing.
    """
    if not cooldown.was_notified(state, _ALL_EXHAUSTED_KEY):
        return False
    cooldown.clear_notified(state, _ALL_EXHAUSTED_KEY)
    cooldown.save_state(config.state_path, state)
    return True


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
        # `triage()` must book a research repo's one-per-HEAD backlog-refill attempt at decision
        # time (it is the only place holding `state`), but nothing was spent here — every seat is
        # cooling. Hand the attempt back, or one all-seats-exhausted window would silently consume
        # it and the next triage would end the crawl with DONE having never invoked gad-run at all.
        #
        # A6 audit fix: gated on `plan.ideation_refill` — the plan produced by
        # `gadkit._exhausted_backlog_plan()`'s ideation branch, and the ONLY plan that actually
        # booked this attempt. Previously this ran for EVERY no-seat iteration regardless of
        # which plan triage had just returned (a mid-generation recovery restart, an ordinary
        # pending-backlog RUN, anything) — wiping a still-binding marker from an entirely
        # unrelated iteration let the same HEAD be re-attempted for auto-ideation indefinitely,
        # since the very next no-seat iteration (for whatever plan) would clear it right back out
        # again the moment triage re-booked it.
        if plan.ideation_refill:
            cooldown.clear_ideation_attempt_head(state, repo)
        return IterationResult(
            plan=plan, action=None, seat_notes=notes, wait_until=_earliest_wait(seats, state)
        )

    # Size this launch's output-token allowance to the seat we are actually about to use, and hand
    # it to the plan. This is the single line that makes the soft usage ceiling real: `triage()`
    # built `plan` before any seat was chosen, so its allowance is necessarily None until here —
    # `gadkit.command()` then threads a non-None value through as the `tokenAllowance` workflow arg,
    # which is what gad-kit's `shouldPause()` compares `budget.spent()` against.
    #
    # `pick_seat()` already refused any seat whose budget was not `startable`, so a non-startable
    # answer at this point would be an internal inconsistency (the only way to reach it is a seat
    # offered by the last-resort 401 path, which carries `usage=None` and therefore always resolves
    # to the unbounded-but-startable case). Handled defensively rather than trusted: rotate the seat
    # away and take the wait path, never launch with a budget we just judged unfundable.
    seat_percent = None if _seat_usage is None else usage_mod.session_percent(_seat_usage)
    launch_budget = config.launch_budget_for(
        seat.name, seat_percent, learned=cooldown.learned_tokens_per_percent(state, seat.path)
    )
    if not launch_budget.startable:
        _force_cooldown(state, seat)
        notes.append(f"below-token-floor at launch: {seat.name}: {launch_budget.reason}")
        return IterationResult(
            plan=plan, action=None, seat_notes=notes, wait_until=_earliest_wait(seats, state)
        )
    if launch_budget.allowance is not None:
        plan = dataclasses.replace(
            plan,
            token_allowance=launch_budget.allowance,
            # Keep the prompt's human-readable budget sentence consistent with what is enforced.
            token_target=f"+{launch_budget.allowance}",
        )
        print(
            f"[claude-relay] seat={seat.name} tokenAllowance={launch_budget.allowance} "
            f"({launch_budget.reason})",
            flush=True,
        )

    pre = gadkit.snapshot(repo)
    calibration_before = gadkit.calibration_record_count(repo)
    argv = gadkit.command(plan)
    plugin_dirs = [str(p) for p in plugins.resolve_plugin_dirs(config.plugin_dirs)]
    result = runner.run(
        argv,
        repo=repo,
        config_dir=seat.path,
        log_dir=config.log_dir,
        seat_name=seat.name,
        timeout_s=config.run_timeout_s,
        plugin_dirs=plugin_dirs,
    )

    ceiling = config.resolve_seat_ceiling(seat.name)
    try:
        post_usage = cache.poll(seat.path, ttl=config.poll_ttl, force=True)
    except usage_mod.UsageError:
        post_usage = None
    if post_usage is not None:
        _record_usage(state, seat, post_usage, ceiling)

    post = gadkit.snapshot(repo)
    outcome_bucket = gadkit.outcome(pre, post, post_usage, ceiling)
    action = detector.classify(outcome_bucket, post_usage, result.tail)

    if result.timed_out:
        # A hung `claude` process tells us nothing reliable about this seat's real usage — but
        # we must still rotate away from it rather than immediately re-selecting the same seat
        # (finding #7: "the loop treats [a timeout] as rotate/retry"). Force a short cooldown so
        # pick_seat() naturally avoids it next iteration regardless of what outcome() concludes.
        _force_cooldown(state, seat)
    elif outcome_bucket == "AGENT_DEAD_NONLIMIT" and action.kind == detector.CONTINUE_ROTATE:
        # A1 audit fix: `outcome()` can only be AGENT_DEAD_NONLIMIT when `near_cap()` was FALSE
        # for this seat's own post-run usage reading — i.e. `rotate_off()` is false BY THE
        # BUCKET'S OWN PREMISE, so the normal cooldown-recording path (`_record_usage()`) never
        # gave this seat a cooldown. Yet `classify()` still resolved this to CONTINUE_ROTATE,
        # via either gad-run's own probe-confirmed workflow-limit signature or (when the usage
        # poll itself failed) the generic tail backstop. Left unpatched, `pick_seat()` would
        # happily re-select the SAME seat from the SAME (still-headroom-showing) cached reading
        # next iteration: an uncapped, no-backoff respawn that also bypasses the HARD_ERROR
        # breaker entirely, since `loop.run()`'s CONTINUE_ROTATE handling resets
        # `consecutive_agent_dead` to 0 unconditionally with no sleep in between. Force the same
        # short cooldown the timeout case uses, so this seat is at least rotated away from
        # rather than hammered; `loop.run()` additionally treats THIS specific flavor of
        # CONTINUE_ROTATE like RETRY for breaker-counting purposes (see there), since repeated
        # probe-confirmed deaths across DIFFERENT seats is exactly the "platform outage, not one
        # bad seat" signature the breaker exists to catch. `action.resets_at` (Blocker 1 item 2)
        # carries a REAL structured reset time when the rate_limit_event signal produced this
        # CONTINUE_ROTATE; `_force_cooldown()` prefers it over the blind timeout guess.
        _force_cooldown(state, seat, resets_at=action.resets_at)

    if action.paused:
        # gad-kit's soft ceiling fired: it spent its whole allowance and stopped itself at a unit
        # boundary. That allowance WAS this seat's headroom below its ceiling, so the seat is now
        # done for this window by construction — and, unlike a wall-hit, its raw percent may still
        # look startable (the ceiling is synthetic and sits well below 100%). Nothing in the normal
        # path would cool it: `_record_usage()`/`rotate_off()` only fire at the real ceiling, and
        # the AGENT_DEAD_NONLIMIT branch above does not cover the PROGRESSED flavour of a pause.
        # Left uncooled, the very next iteration re-selects the same seat, whose fresh allowance is
        # now near zero, and it pauses again at the same gate — a launch spent per iteration.
        #
        # Cool it to the seat's OWN reported reset time when we have one; that is when its headroom
        # genuinely returns, and it is a real platform figure rather than a guess.
        resets_at = None
        if post_usage is not None:
            reset_dt = usage_mod.session_resets_at(post_usage)
            if reset_dt is not None:
                resets_at = reset_dt.isoformat()
        _force_cooldown(state, seat, resets_at=resets_at)
        _learn_tokens_per_percent(state, seat, repo, calibration_before, _seat_usage, post_usage)
    elif outcome_bucket == "PROGRESSED":
        # A completed, committed run is also a valid calibration sample — in fact the best kind,
        # since it spans a whole generation rather than a truncated one.
        _learn_tokens_per_percent(state, seat, repo, calibration_before, _seat_usage, post_usage)

    return IterationResult(
        plan=plan, action=action, seat=seat, run_result=result, outcome=outcome_bucket, seat_notes=notes
    )


# How much of a new observation to fold into a seat's learned tokens-per-percent. Low on purpose:
# individual runs are noisy (a generation that stalled on one long agent burns wall-clock window
# with little output; a wide fan-out burns output with little wall-clock), and the number this feeds
# decides whether seats are startable at all — so it must drift toward the truth over several runs
# rather than lurch to whatever the last one happened to look like.
_TOKENS_PER_PCT_EWMA_ALPHA = 0.3
# Reject observations outside this band as measurement error rather than learning from them. The
# span is deliberately wide (three orders of magnitude): its job is to catch a nonsense pairing —
# a percent delta of ~0 with real spend, a stale calibration record, two seats' runs interleaved —
# not to encode an opinion about what a plausible rate is, which is the very thing being measured.
_TOKENS_PER_PCT_MIN = 50.0
_TOKENS_PER_PCT_MAX = 50_000.0
# A percent delta below this is treated as unmeasurable: the usage gauge is coarse, so dividing real
# spend by a delta of 0.2% manufactures a rate ten times too large from pure quantization noise.
_MIN_LEARNABLE_PCT_DELTA = 2.0


def _learn_tokens_per_percent(
    state: dict[str, Any],
    seat: fleet.Seat,
    repo: Path,
    calibration_before: int,
    pre_usage: usage_mod.UsageSnapshot | None,
    post_usage: usage_mod.UsageSnapshot | None,
) -> None:
    """Fold this run into `seat`'s learned output-tokens-per-percent, if it is measurable.

    Pairs the two halves of the observation that no single component can see on its own: how many
    OUTPUT TOKENS the run spent (only gad-kit knows — it reads `budget.spent()` from inside the
    workflow and appends it to `.gad/calibration.jsonl`) against how much of the five-hour WINDOW
    that consumed (only relay knows, from its pre/post usage polls of this seat).

    Silently does nothing whenever the observation is not trustworthy — a missing or stale
    calibration record, an unreadable usage poll, a percent delta too small to divide by, or a
    resulting rate outside `_TOKENS_PER_PCT_MIN.._MAX`. Calibration is advisory: a run that teaches
    us nothing is normal and must never be an error, and a bad sample is far worse than no sample,
    because too LOW a learned rate makes the seat unstartable.
    """
    calibration = gadkit.read_calibration(repo)
    if calibration is None or calibration.records <= calibration_before:
        return  # no record, or only the stale one an earlier launch left behind
    if pre_usage is None or post_usage is None:
        return
    pct_delta = usage_mod.session_percent(post_usage) - usage_mod.session_percent(pre_usage)
    if pct_delta < _MIN_LEARNABLE_PCT_DELTA:
        # Also catches the negative case: the window reset mid-run, so post is a fresh window and
        # the delta is meaningless rather than merely small.
        return
    observed = calibration.spent_output_tokens / pct_delta
    if not (_TOKENS_PER_PCT_MIN <= observed <= _TOKENS_PER_PCT_MAX):
        print(
            f"[claude-relay] seat={seat.name} discarding an implausible calibration sample "
            f"({calibration.spent_output_tokens} output tokens / {pct_delta:.1f}% = "
            f"{observed:.0f}/pct, outside {_TOKENS_PER_PCT_MIN:.0f}..{_TOKENS_PER_PCT_MAX:.0f})",
            flush=True,
        )
        return
    previous = cooldown.learned_tokens_per_percent(state, seat.path)
    blended = (
        observed
        if previous is None
        else (1 - _TOKENS_PER_PCT_EWMA_ALPHA) * previous + _TOKENS_PER_PCT_EWMA_ALPHA * observed
    )
    cooldown.update_seat(state, seat.path, tokens_per_percent=blended)
    print(
        f"[claude-relay] seat={seat.name} tokens-per-pct observed={observed:.0f} "
        f"(spent={calibration.spent_output_tokens} over {pct_delta:.1f}%, "
        f"status={calibration.status!r}, gens={calibration.gens_committed}) "
        f"learned={blended:.0f}" + ("" if previous is None else f" (was {previous:.0f})"),
        flush=True,
    )


def _is_genuine_wall_hit_rotation(kind: str, outcome: str | None) -> bool:
    """True iff this iteration's action is a CONTINUE_ROTATE driven by an ACTUAL usage-near-cap
    reading (`gadkit.outcome()`'s HIT_WALL bucket) rather than the AGENT_DEAD_NONLIMIT
    ambiguity's probe-confirmed/backstop override (A1 audit fix). Only the former already has a
    real, `_record_usage()`-recorded cooldown behind it and represents unambiguous progress
    toward a fresh seat — HARD_ERROR-breaker resets require that. The latter must count toward
    the breaker exactly like RETRY, since `run_once()`'s `_force_cooldown()` there is a
    defensive rotation, not evidence anything actually progressed.
    """
    return kind == detector.CONTINUE_ROTATE and outcome == "HIT_WALL"


def _force_cooldown(state: dict[str, Any], seat: fleet.Seat, resets_at: str | None = None) -> None:
    """Force a cooldown onto `seat` regardless of what the live usage reading said — used when a
    run's outcome is untrustworthy as a usage signal (a hung process, or a probe-confirmed/
    backstop-inferred wall-hit that the seat's own percent doesn't reflect) but we still must not
    let `pick_seat()` re-select the same seat next iteration.

    `resets_at` (Blocker 1 item 2, an ISO-8601 UTC string): when a `rate_limit_event` gave us a
    REAL structured reset time (its `resetsAt` field, via `detector.Action.resets_at`), use that
    as the actual cooldown boundary instead of the blind `_TIMEOUT_COOLDOWN_S` guess —
    `cooldown.clamp_future()` still guards against a garbage/implausible value. Falls back to the
    guess when no `resets_at` is given, or `clamp_future()` rejects it.
    """
    clamped = cooldown.clamp_future(resets_at) if resets_at else None
    if clamped is not None:
        cooldown.update_seat(state, seat.path, cooldown_until=clamped)
        return
    until = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=_TIMEOUT_COOLDOWN_S)
    cooldown.update_seat(state, seat.path, cooldown_until=until.isoformat())


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
        resolved = [str(p) for p in plugins.resolve_plugin_dirs(config.plugin_dirs)]
        preview["plugin_dirs"] = resolved
        preview["argv"] = runner.build_claude_argv(gadkit.command(plan), resolved)[1:]  # drop "claude"
        seats = fleet.discover_seats(config.effective_exclude())
        cache = usage_mod.UsageCache()
        seat, seat_usage, notes = pick_seat(
            seats, copy.deepcopy(state), cache, config, dry_run=True
        )
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
    disabled = cooldown.disabled_seats(state)
    seat_rows = []
    for seat in seats:
        entry = cooldown.get_seat_state(state, seat.path)
        seat_rows.append(
            {
                "name": seat.name,
                "path": str(seat.path),
                "usable": seat.usable,
                "needs_login": seat.needs_login,
                "disabled": seat.name in disabled,
                "cooldownUntil": entry.get("cooldownUntil"),
                "lastPercent": entry.get("lastPercent"),
                "lastSeenAt": entry.get("lastSeenAt"),
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


def _sleep_and_poll(
    total_s: float, config: Config, state: dict[str, Any], repo: Path, lock: SingleInstanceLock
) -> None:
    remaining = total_s
    while remaining > 0:
        chunk = min(_SLEEP_CHUNK_S, remaining)
        time.sleep(chunk)
        remaining -= chunk
        notify.poll_telegram_updates(
            config, state, repo, status_provider=lambda: f"claude-relay: waiting on seat cooldowns for {repo}"
        )
        cooldown.save_state(config.state_path, state)
        # B1 fix: this loop can run for hours (a capped wait is still up to _MAX_WAIT_S, and
        # this chunk-loop is the ONLY code running during that whole span) — refresh the
        # lockfile's mtime every chunk so `_is_stale()`'s age fallback never mistakes a live,
        # merely-waiting instance for a dead one.
        lock.heartbeat()


def _park_and_wait(
    repo: Path,
    config: Config,
    state: dict[str, Any],
    lock: SingleInstanceLock,
    *,
    notify_key: str | None = None,
    notify_message: str | None = None,
) -> None:
    """Block until the repo is no longer AWAITING_HUMAN/BLOCKED, opportunistically polling
    Telegram (so `resolve <id> <answer>` / `status` keep working while parked) and re-triaging
    periodically. Single-repo v1 has no other work to serve while parked (DESIGN.md §11). Uses
    `dry_run=True` (never stashes while merely re-checking a parked repo — the real stash
    decision, if any, happens the next time `run_once()` triages for real) but passes the REAL
    `state` object so clean-baseline bookkeeping legitimately updates while parked.

    B21 audit fix: `notify()` correctly does not mark a key as sent when `dispatch()` fails (a
    transient Telegram outage, say), but the ORIGINAL park notification was only ever attempted
    ONCE, right before this function was entered — so a single transient failure lost the park
    message PERMANENTLY: this loop can run for the entire duration of an AWAITING_HUMAN/BLOCKED
    park (unbounded), with no other path that ever retries it. `notify_key`/`notify_message`
    (when given) are retried on every re-triage cycle via the SAME `notify.notify()` call the
    original send used — its own dedupe (`was_notified`) means this is a genuine no-op once the
    send has actually succeeded (no re-spam), but keeps trying for as long as it keeps failing.
    """
    while True:
        notify.poll_telegram_updates(
            config, state, repo, status_provider=lambda: f"claude-relay: parked on {repo}"
        )
        if notify_key is not None and notify_message is not None:
            notify.notify(config, state, notify_key, notify_message)
        cooldown.save_state(config.state_path, state)
        lock.heartbeat()  # B1 fix: a park can last indefinitely (AWAITING_HUMAN has no timeout)
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
                # B1 fix: refresh the lockfile's mtime once per outer iteration too (in addition
                # to the per-chunk heartbeats inside `_sleep_and_poll()`/`_park_and_wait()`) so a
                # crawl that never hits either of those (e.g. back-to-back CONTINUE/RETRY
                # iterations with no waiting at all) still renews it at least once per
                # `run_once()` call.
                lock.heartbeat()
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

                # A seat WAS available this iteration, so the all-exhausted condition has resolved:
                # re-arm that notification for the next exhaustion event.
                #
                # Placed HERE — beside the park-prefix clear, immediately after `run_once()` — and
                # NOT further down beside the action handling, which is where it first went. MEASURED
                # (live-run-02): everything below the `max_units` gate is skipped on the final unit
                # of a bounded run, so with `max_units = 1` a clear placed down there never executed
                # at all. The generation completed, the supervisor exited cleanly, and the key was
                # still set. Its unit tests passed the whole time because they called the helper
                # directly and never established that the call site was reachable.
                #
                # Gated on `seat`, not on `action`: "a seat was available" is literally the condition
                # whose resolution re-arms the alert.
                if iteration.seat is not None:
                    _clear_exhausted_notice(config, state)

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
                    # An unconditional, flushed heartbeat for every wait — NOT gated on
                    # `_LONG_WAIT_NOTIFY_S` and NOT routed through `notify()`'s dedupe. MEASURED
                    # 2026-07-27 (DESIGN.md §4c): a supervisor waiting out a cooldown printed
                    # NOTHING for its entire wait, so "healthy, sleeping until 19:40Z" and "hung"
                    # and "crashed" were indistinguishable in the logfile. The deduped notification
                    # is the right shape for an ALERT (say it once) and the wrong shape for a
                    # liveness signal (say it every time), so this is a separate line, deliberately.
                    print(
                        f"[claude-relay] no seat available for {repo}; waiting {int(wait_s)}s "
                        f"(until {iteration.wait_until if iteration.wait_until else 'unknown'})"
                        + (f" — {'; '.join(iteration.seat_notes)}" if iteration.seat_notes else ""),
                        flush=True,
                    )
                    # A pool idle because every seat is BELOW THE TOKEN FLOOR is a configuration
                    # fault dressed up as ordinary exhaustion, and it does not heal by waiting: when
                    # the windows reset, the seats come back at 0% and are measured against the same
                    # `tokens_per_percent`, so they are below the floor again. Worse, a seat that
                    # never launches never writes a calibration record, so it can never learn the
                    # better rate that would make it startable. `_validate()` rejects the case that
                    # is arithmetically impossible for ANY seat; this catches what only shows up
                    # against live readings, and it says the actual diagnosis instead of letting
                    # "all seats exhausted or in cooldown" imply the pool is merely busy.
                    below_floor = [
                        n for n in iteration.seat_notes if n.startswith("below-token-floor")
                    ]
                    if below_floor:
                        notify.notify(
                            config,
                            state,
                            _BELOW_FLOOR_KEY,
                            f"No seat for {repo} can fund a generation under its usage ceiling — "
                            f"{len(below_floor)} seat(s) skipped by the min_token_target floor. "
                            "This will NOT clear on its own when the windows reset. Either the "
                            "learned tokens_per_percent is too low for these accounts, or "
                            "min_token_target is too high. Set derive_token_target = false to run "
                            "unbounded again while you recalibrate. "
                            + "; ".join(below_floor),
                        )
                        cooldown.save_state(config.state_path, state)
                    elif wait_s > _LONG_WAIT_NOTIFY_S:
                        notify.notify(
                            config,
                            state,
                            _ALL_EXHAUSTED_KEY,
                            f"All seats exhausted or in cooldown for {repo}; "
                            f"waiting ~{int(wait_s // 60)}m (until {iteration.wait_until}).",
                        )
                        cooldown.save_state(config.state_path, state)
                    if once:
                        return 0
                    _sleep_and_poll(wait_s, config, state, repo, lock)
                    continue

                kind = iteration.action.kind
                if (
                    kind == detector.CONTINUE
                    or _is_genuine_wall_hit_rotation(kind, iteration.outcome)
                    # A soft-ceiling pause must NOT count toward the HARD_ERROR breaker. It is the
                    # mechanism working exactly as designed — the generation stopped itself at a
                    # unit boundary with its artifacts on disk and is resumable — and `run_once()`
                    # has already cooled the seat, so this is genuine progress toward a fresh one,
                    # the same as a wall-hit rotation. Counting it would be actively harmful:
                    # several seats pausing in a row is the EXPECTED steady state once a fleet is
                    # near its ceilings, so the breaker would trip on healthy operation and park
                    # the repo, which is precisely the outcome the soft ceiling exists to avoid.
                    or iteration.action.paused
                ):
                    # A genuine wall-hit rotation (outcome() only returns HIT_WALL when the
                    # seat's OWN usage reading is at/near its ceiling) already got a real
                    # cooldown recorded by `_record_usage()`/`rotate_off()` — unambiguous
                    # progress toward a fresh seat, so reset the breaker.
                    consecutive_agent_dead = 0
                    if once:
                        return 0
                    continue

                if kind in (detector.RETRY, detector.CONTINUE_ROTATE):
                    # A1 audit fix: a CONTINUE_ROTATE reaching here (the HIT_WALL case above
                    # already `continue`d) is the AGENT_DEAD_NONLIMIT flavor — `run_once()`'s
                    # `_force_cooldown()` already rotated THIS iteration's seat away, but
                    # treating it as unconditional "progress" (resetting the breaker to 0, as
                    # this used to) bypassed the HARD_ERROR breaker entirely: an uncapped,
                    # no-backoff respawn that could cycle every seat in the pool forever.
                    # Repeated probe-confirmed/backstop-inferred deaths — however many
                    # DIFFERENT seats they land on — is exactly the "platform outage, not one
                    # bad seat" signature this breaker exists to catch, so it counts the same
                    # as RETRY.
                    consecutive_agent_dead += 1
                    # Blocker 1 item 3 (E9): `action.no_retry` means detector.classify() already
                    # determined retrying is KNOWN to be pointless (e.g. gad-run's own RESULT
                    # reported DIRTY-TREE — it refused to even start) — trip the breaker on THIS
                    # occurrence rather than wasting `_MAX_CONSECUTIVE_AGENT_DEAD` cycles first.
                    if iteration.action.no_retry or consecutive_agent_dead >= _MAX_CONSECUTIVE_AGENT_DEAD:
                        # force=True (finding #2): a HARD_ERROR must never be permanently
                        # swallowed by a static dedupe key — the operator needs to see EVERY
                        # occurrence, not just the first one ever.
                        message = (
                            f"HARD_ERROR: {repo} received a non-retryable RESULT on the first "
                            f"attempt (retrying would waste cycles for nothing); parking rather "
                            f"than burning tokens. Reason: {iteration.action.reason}"
                            if iteration.action.no_retry
                            else (
                                f"HARD_ERROR: {consecutive_agent_dead} consecutive non-limit "
                                f"failures on {repo}; parking rather than burning tokens in a "
                                f"crash loop. Last reason: {iteration.action.reason}"
                            )
                        )
                        notify.notify(
                            config,
                            state,
                            f"hard-error:{repo}",
                            message,
                            force=True,
                        )
                        cooldown.save_state(config.state_path, state)
                        return 1
                    if once:
                        return 0
                    continue

                if kind == detector.NOTIFY_PARK:
                    key = _park_notify_key(repo, iteration.plan)
                    message = _park_message(repo, iteration.plan)
                    notify.notify(config, state, key, message)
                    cooldown.save_state(config.state_path, state)
                    if once:
                        return 0
                    # B21 audit fix: pass key/message through so a transient send failure gets
                    # retried on every re-triage cycle instead of being lost for the whole park.
                    _park_and_wait(repo, config, state, lock, notify_key=key, notify_message=message)
                    consecutive_agent_dead = 0
                    continue

                if kind == detector.DONE:
                    # B29 audit fix: `iteration.plan.detail` describes the PLAN triage() produced
                    # (e.g., for the auto-ideation exhaustion path, "handing the exhausted backlog
                    # to /gad-run ... instead of terminating" — a description of what was ABOUT to
                    # be attempted), not what actually happened. `iteration.action.reason` is the
                    # right field in every DONE path: `run_once()` sets it to `plan.detail`
                    # verbatim for the direct "no run needed" case (so nothing regresses there),
                    # and to `detector.classify()`'s own "backlog exhausted" for the case where a
                    # RUN plan was genuinely executed and its post-run `outcome()` came back
                    # NO_BACKLOG — the actual terminal OUTCOME, not the pre-run plan's own text.
                    done_message = f"DONE: {iteration.action.reason}"
                    notify.notify(config, state, f"done:{repo}", done_message, force=True)
                    cooldown.save_state(config.state_path, state)
                    return 0

                print(f"[claude-relay] unrecognized action kind {kind!r}; stopping", file=sys.stderr)
                return 1
        except Exception as exc:
            # B7/B12 audit fix: previously this `try` had a `finally` and NO `except` at all, so
            # an uncaught exception anywhere in the loop body (a raw network exception escaping
            # notify.py/usage.py before that fix, `gadkit.gadkit_plugin_root()`'s bare
            # `FileNotFoundError`, `gadkit.git_stash_push()`'s `GitRecoveryError`, anything else)
            # unwound straight past every "never raises" contract and killed the supervisor with
            # ZERO notification — the monitor keeps painting a healthy table (B24) while
            # supervision has actually stopped. Deliberately `Exception`, not `BaseException`:
            # `KeyboardInterrupt`/`SystemExit` are the operator's own deliberate action and should
            # not read as an alarming crash. The notify call itself is best-effort — a failure to
            # notify must never hide or replace the ORIGINAL exception's traceback.
            try:
                notify.notify(
                    config,
                    state,
                    f"crash:{repo}",
                    f"claude-relay CRASHED on {repo}: {exc!r}. Supervision has STOPPED — "
                    "it will not resume until the process is restarted.",
                    force=True,
                )
            except Exception as notify_exc:  # pragma: no cover - defensive: must never mask a crash
                print(
                    f"[claude-relay] failed to send crash notification (original error: {exc!r}): "
                    f"{notify_exc!r}",
                    file=sys.stderr,
                )
            raise
        finally:
            cooldown.save_state(config.state_path, state)
