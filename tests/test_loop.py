"""Offline tests for relay.loop: pick_seat() seat selection against mocked usage readings, and
SingleInstanceLock's stale-reclaim logic (dead-PID reclaim, alive-PID refusal, and the PID-
reuse regression finding #6 fixes). No network calls, no real `claude` subprocess.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import time
import unittest
from pathlib import Path

from relay import cooldown, fleet, loop
from relay import usage as usage_mod
from relay.config import Config, SeatConfig


def _state() -> dict:
    return cooldown.load_state(Path("/nonexistent-claude-relay-state.json"))


def _seat(name: str, path: str = "") -> fleet.Seat:
    seat_path = Path(path or f"/fake/.claude-{name}")
    return fleet.Seat(name=name, path=seat_path, has_creds=True, needs_login=False)


def _usage_at(percent: float, severity: str = "normal") -> usage_mod.UsageSnapshot:
    return usage_mod.UsageSnapshot.from_json(
        {"limits": [{"kind": "session", "percent": percent, "severity": severity, "is_active": True}]},
        fetched_at=time.time(),
    )


def _usage_reset(
    percent: float, resets_in_seconds: float, resets_at: str | None = None
) -> usage_mod.UsageSnapshot:
    """A reading whose 5h window resets `resets_in_seconds` from now (or at an explicit
    `resets_at` ISO string, so two seats can share an EXACT reset time for tie-break tests) — for
    reset-time selection tests. Sets the raw `five_hour` gauge (utilization + resets_at), which is
    what `session_percent`/`session_resets_at` read."""
    when = resets_at or (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=resets_in_seconds)).isoformat()
    return usage_mod.UsageSnapshot.from_json(
        {"five_hour": {"utilization": percent, "resets_at": when}},
        fetched_at=time.time(),
    )


class _FakeCache:
    """Stand-in for usage_mod.UsageCache: returns a canned reading per seat path, and records
    which seats were actually polled (so tests can assert cooled-down seats are skipped
    WITHOUT a poll, per DESIGN.md §4).
    """

    def __init__(self, readings: dict[str, usage_mod.UsageSnapshot]):
        self._readings = readings
        self.polled: list[str] = []

    def poll(self, seat_dir, ttl: float = 90.0, force: bool = False) -> usage_mod.UsageSnapshot:  # noqa: ANN001
        key = str(seat_dir)
        self.polled.append(key)
        if key not in self._readings:
            raise usage_mod.NeedsLoginError(f"no fixture reading for {key}")
        return self._readings[key]


class PickSeatTests(unittest.TestCase):
    def test_prefers_lowest_percent_under_start_cap(self) -> None:
        low = _seat("low")
        high = _seat("high")
        cache = _FakeCache(
            {
                str(low.path): _usage_at(30.0),
                str(high.path): _usage_at(50.0),
            }
        )
        config = Config(ceiling_pct=70.0, start_margin=5.0)
        seat, seat_usage, notes = loop.pick_seat([low, high], _state(), cache, config)
        self.assertEqual(seat, low)
        assert seat_usage is not None
        self.assertEqual(usage_mod.active_session_limit(seat_usage).percent, 30.0)

    def test_prefers_soonest_reset_even_at_higher_percent(self) -> None:
        # Perishable-capacity-first (DESIGN.md §4): a seat at 50% resetting in 30 min must beat a
        # seat at 40% resetting in 4 h. If selection went by percent alone, the 40% seat would
        # win — so this proves reset-time dominates the (reset, percent) sort. Both pass the
        # start-cap gate (start_cap = 70 - 5 = 65; 50 and 40 are both < 65).
        soon = _seat("soon")
        later = _seat("later")
        cache = _FakeCache(
            {
                str(soon.path): _usage_reset(50.0, 30 * 60),
                str(later.path): _usage_reset(40.0, 4 * 3600),
            }
        )
        config = Config(ceiling_pct=70.0, start_margin=5.0)
        seat, _seat_usage, _notes = loop.pick_seat([later, soon], _state(), cache, config)
        self.assertEqual(seat, soon)

    def test_reset_tie_breaks_by_lowest_percent(self) -> None:
        # Equal reset times => fall back to the original policy: prefer the seat with more headroom.
        a = _seat("a")
        b = _seat("b")
        shared_reset = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).isoformat()
        cache = _FakeCache(
            {
                str(a.path): _usage_reset(55.0, 0, resets_at=shared_reset),
                str(b.path): _usage_reset(20.0, 0, resets_at=shared_reset),
            }
        )
        config = Config(ceiling_pct=70.0, start_margin=5.0)
        seat, _seat_usage, _notes = loop.pick_seat([a, b], _state(), cache, config)
        self.assertEqual(seat, b)

    def test_seat_at_or_above_ceiling_is_rotated_off(self) -> None:
        seat = _seat("almost-full")
        cache = _FakeCache({str(seat.path): _usage_at(85.0)})
        config = Config(ceiling_pct=70.0, start_margin=5.0)
        picked, _usage, notes = loop.pick_seat([seat], _state(), cache, config)
        self.assertIsNone(picked)
        self.assertTrue(any("rotate-off" in n for n in notes))

    def test_seat_above_start_cap_but_below_ceiling_is_not_picked(self) -> None:
        # ceiling=70, start_margin=5 -> start_cap=65; a seat at 68% is below the real ceiling
        # (not rotated off) but still not preferred to START a fresh unit on.
        seat = _seat("borderline")
        cache = _FakeCache({str(seat.path): _usage_at(68.0)})
        config = Config(ceiling_pct=70.0, start_margin=5.0)
        picked, _usage, notes = loop.pick_seat([seat], _state(), cache, config)
        self.assertIsNone(picked)
        self.assertTrue(any("above-start-cap" in n for n in notes))

    def test_cooled_down_seat_is_skipped_without_polling(self) -> None:
        seat = _seat("cooling")
        state = _state()
        future = "2999-01-01T00:00:00+00:00"
        cooldown.update_seat(state, seat.path, cooldown_until=future)
        cache = _FakeCache({str(seat.path): _usage_at(10.0)})
        picked, _usage, notes = loop.pick_seat([seat], state, cache, Config())
        self.assertIsNone(picked)
        self.assertEqual(cache.polled, [])  # never polled — DESIGN.md §4: no network for known-cooled seats
        self.assertTrue(any("in-cooldown" in n for n in notes))

    def test_needs_login_seat_is_skipped(self) -> None:
        seat = fleet.Seat(
            name="locked-out", path=Path("/fake/.claude-locked-out"), has_creds=False, needs_login=True
        )
        cache = _FakeCache({})
        picked, _usage, notes = loop.pick_seat([seat], _state(), cache, Config())
        self.assertIsNone(picked)
        self.assertTrue(any("needs-login" in n for n in notes))

    def test_per_seat_ceiling_override_lets_a_seat_be_picked_at_a_percent_that_would_otherwise_rotate_it_off(
        self,
    ) -> None:
        seat = _seat("sam")
        cache = _FakeCache({str(seat.path): _usage_at(80.0)})
        config = Config(
            ceiling_pct=70.0, start_margin=5.0, seat_configs={"sam": SeatConfig(ceiling_pct=90.0)}
        )
        picked, seat_usage, _notes = loop.pick_seat([seat], _state(), cache, config)
        self.assertEqual(picked, seat)

    def test_no_candidates_returns_none(self) -> None:
        picked, seat_usage, notes = loop.pick_seat([], _state(), _FakeCache({}), Config())
        self.assertIsNone(picked)
        self.assertIsNone(seat_usage)
        self.assertEqual(notes, [])


class SingleInstanceLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = Path(self._tmp.name) / "claude-relay.lock"

    def _dead_pid(self) -> int:
        for candidate in (999999, 999998, 999997, 899999):
            try:
                os.kill(candidate, 0)
            except ProcessLookupError:
                return candidate
            except PermissionError:  # pragma: no cover - exists but not ours; try the next one
                continue
        self.skipTest("could not find an unused PID to simulate a dead process")

    def test_acquire_reclaims_a_lock_held_by_a_dead_pid(self) -> None:
        dead_pid = self._dead_pid()
        self.lock_path.write_text(f"{dead_pid}:12345")
        lock = loop.SingleInstanceLock(self.lock_path)
        lock.acquire()  # must not raise — the recorded PID is dead, so the lock is stale
        lock.release()

    def test_acquire_refuses_when_lock_is_held_by_a_genuinely_alive_pid(self) -> None:
        my_pid = os.getpid()
        my_starttime = loop._proc_start_time(my_pid)
        starttime_field = str(my_starttime) if my_starttime is not None else ""
        self.lock_path.write_text(f"{my_pid}:{starttime_field}")
        lock = loop.SingleInstanceLock(self.lock_path)
        with self.assertRaises(loop.LockError):
            lock.acquire()

    def test_acquire_reclaims_when_pid_was_reused_by_a_different_process(self) -> None:
        """Finding #6 regression: a bare os.kill(pid, 0) liveness check alone would treat this
        as "still alive" (the PID exists) — the start-time mismatch must catch that it is NOT
        the same process that originally held the lock.
        """
        my_pid = os.getpid()
        my_starttime = loop._proc_start_time(my_pid)
        if my_starttime is None:
            self.skipTest("/proc start-time unavailable on this platform — nothing to regress-test")
        bogus_starttime = my_starttime + 999999  # guaranteed not to match the real one
        self.lock_path.write_text(f"{my_pid}:{bogus_starttime}")
        lock = loop.SingleInstanceLock(self.lock_path)
        lock.acquire()  # must reclaim: recorded start-time doesn't match this PID's real one
        lock.release()

    def test_acquire_then_release_round_trips(self) -> None:
        lock = loop.SingleInstanceLock(self.lock_path)
        lock.acquire()
        self.assertTrue(self.lock_path.exists())
        lock.release()
        self.assertFalse(self.lock_path.exists())

    def test_stale_by_age_alone_is_reclaimed(self) -> None:
        dead_pid = self._dead_pid()
        self.lock_path.write_text(f"{dead_pid}:1")
        lock = loop.SingleInstanceLock(self.lock_path, stale_after_s=0.0)
        lock.acquire()
        lock.release()


if __name__ == "__main__":
    unittest.main()
