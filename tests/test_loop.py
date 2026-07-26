"""Offline tests for relay.loop: pick_seat() seat selection against mocked usage readings, and
SingleInstanceLock's stale-reclaim logic (dead-PID reclaim, alive-PID refusal, and the PID-
reuse regression finding #6 fixes). No network calls, no real `claude` subprocess.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from relay import cooldown, detector, fleet, gadkit, loop
from relay import runner as runner_mod
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


def _assistant_line(text: str) -> str:
    """One realistic `assistant` NDJSON envelope (Blocker 1, 2026-07-26): `RunResult.tail` is
    real `--output-format stream-json` output, one JSON object per physical line, never bare
    prose — a bare `"RESULT: ..."` string is not something production ever puts on the tail.
    """
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
            "session_id": "sess-1",
        }
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
    def test_disabled_seat_is_skipped_and_not_polled(self) -> None:
        good = _seat("good")
        off = _seat("off")
        # 'off' has the LOWER percent, so it would win if eligible — but it's disabled.
        cache = _FakeCache({str(good.path): _usage_at(30.0), str(off.path): _usage_at(5.0)})
        state = _state()
        cooldown.set_seat_disabled(state, "off", True)
        seat, _usage, notes = loop.pick_seat([off, good], state, cache, Config())
        self.assertIsNotNone(seat)
        self.assertEqual(seat.name, "good")
        self.assertNotIn(str(off.path), cache.polled)  # disabled -> skipped before any network poll
        self.assertIn("disabled: off", notes)

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

    def test_b11_the_most_stale_snapshot_no_longer_ranks_as_most_urgent(self) -> None:
        """B11: `seconds_to_reset` for a past `resets_at` (a stale/pinned 429-extended reading —
        see `UsageCache.extra_ttl_s`) is now floored at zero rather than going negative. Without
        the floor, the MORE-in-the-past entry produces a MORE-negative key and so perversely
        outranks a less-stale one — "the stalest seat sorts first," exactly backwards from "spend
        perishable capacity first" (which is meant to prefer genuinely-imminent resets, not
        arbitrarily-old readings). With the floor, every past-due entry ties at zero and the
        existing percent tie-break decides instead — so the LOWER-percent seat wins regardless of
        which one happens to be more stale.
        """
        very_stale = _seat("very-stale")  # resets_at ~11h in the past
        slightly_stale = _seat("slightly-stale")  # resets_at ~5min in the past
        cache = _FakeCache(
            {
                str(very_stale.path): _usage_reset(60.0, -11 * 3600),
                str(slightly_stale.path): _usage_reset(40.0, -5 * 60),
            }
        )
        config = Config(ceiling_pct=70.0, start_margin=5.0)
        seat, _usage, _notes = loop.pick_seat([very_stale, slightly_stale], _state(), cache, config)
        # Both are floored to the SAME (0.0) key, so the percent tie-break (lower wins) decides —
        # not which one happens to be more stale in wall-clock terms.
        self.assertEqual(seat, slightly_stale)


class _401ThenHealthyCache:
    """Stand-in for usage_mod.UsageCache: raises a 401 `UsageFetchError` for seats named in
    `failing`, otherwise returns `readings[seat_path]`. `failing` is a mutable set so a test can
    flip a seat from "401ing" to "healthy" mid-scenario (simulating either a successful bounded
    refresh-launch or a manual operator re-login) without swapping the whole cache object.
    """

    def __init__(self, readings: dict[str, usage_mod.UsageSnapshot], failing: set[str]):
        self._readings = readings
        self.failing = failing
        self.polled: list[str] = []

    def poll(self, seat_dir, ttl: float = 90.0, force: bool = False) -> usage_mod.UsageSnapshot:  # noqa: ANN001
        key = str(seat_dir)
        self.polled.append(key)
        if key in self.failing:
            raise usage_mod.UsageFetchError("token rejected", status_code=401)
        return self._readings[key]


class Auth401RecoveryTests(unittest.TestCase):
    """B8: a seat whose usage poll 401s must be given a bounded chance to be SELECTED anyway
    (the only way its token can refresh is a `claude` launch), but never at the expense of a
    genuinely healthy candidate, and the pool must not silently stall once exhausted.
    """

    def test_401_seat_is_offered_as_last_resort_when_it_is_the_only_seat(self) -> None:
        seat = _seat("locked-out-401")
        cache = _401ThenHealthyCache({}, failing={str(seat.path)})
        picked, seat_usage, notes = loop.pick_seat([seat], _state(), cache, Config())
        self.assertEqual(picked, seat)
        self.assertIsNone(seat_usage)  # no live reading was ever obtained
        self.assertTrue(any("auth-refresh-attempt" in n for n in notes))

    def test_401_seat_never_outcompetes_a_healthy_candidate(self) -> None:
        good = _seat("good")
        bad = _seat("bad-401")
        cache = _401ThenHealthyCache({str(good.path): _usage_at(30.0)}, failing={str(bad.path)})
        picked, seat_usage, _notes = loop.pick_seat([bad, good], _state(), cache, Config())
        self.assertEqual(picked, good)
        self.assertIsNotNone(seat_usage)

    def test_401_seat_stops_being_offered_and_notifies_once_past_the_budget(self) -> None:
        seat = _seat("locked-out-401")
        cache = _401ThenHealthyCache({}, failing={str(seat.path)})
        state = _state()
        config = Config(notify_sink="stdout")
        with mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch:
            for _ in range(loop._MAX_AUTH_REFRESH_ATTEMPTS):
                picked, _usage, notes = loop.pick_seat([seat], state, cache, config)
                self.assertEqual(picked, seat)  # still within budget
                self.assertTrue(any("auth-refresh-attempt" in n for n in notes))
            self.assertEqual(fake_dispatch.call_count, 0)  # not yet exhausted

            # One more failure crosses the budget: no longer offered, and notified exactly once.
            picked, _usage, notes = loop.pick_seat([seat], state, cache, config)
            self.assertIsNone(picked)
            self.assertTrue(any("auth-exhausted" in n for n in notes))
            self.assertEqual(fake_dispatch.call_count, 1)

            # Further iterations keep failing but must NOT spam — the dedupe key stays notified.
            for _ in range(3):
                picked, _usage, _notes = loop.pick_seat([seat], state, cache, config)
                self.assertIsNone(picked)
            self.assertEqual(fake_dispatch.call_count, 1)

    def test_a_successful_poll_at_any_time_resets_the_counter_and_the_notification(self) -> None:
        """Whether the recovery came from an automated refresh-launch (within budget) or a
        manual operator re-login (any time, even after exhaustion), the very next SUCCESSFUL
        poll must fully un-stick the seat: normal candidate selection resumes and a future 401
        streak can notify again (not permanently deduped by the first exhaustion)."""
        seat = _seat("recovers")
        state = _state()
        config = Config(notify_sink="stdout")
        cache = _401ThenHealthyCache({str(seat.path): _usage_at(10.0)}, failing={str(seat.path)})
        with mock.patch.object(loop.notify, "dispatch", return_value=True):
            for _ in range(loop._MAX_AUTH_REFRESH_ATTEMPTS + 1):
                loop.pick_seat([seat], state, cache, config)
        self.assertTrue(cooldown.was_notified(state, f"auth-exhausted:{seat.path}"))

        # The credentials got fixed (automated or manual) — next poll succeeds.
        cache.failing.discard(str(seat.path))
        picked, seat_usage, notes = loop.pick_seat([seat], state, cache, config)
        self.assertEqual(picked, seat)
        self.assertIsNotNone(seat_usage)
        self.assertFalse(any("auth-refresh-attempt" in n or "auth-exhausted" in n for n in notes))
        self.assertEqual(cooldown.get_seat_state(state, seat.path).get("consecutiveFailures"), 0)
        self.assertFalse(cooldown.was_notified(state, f"auth-exhausted:{seat.path}"))


class EarliestWaitAndWaitSecondsTests(unittest.TestCase):
    """B2 (zero-sleep busy loop) + B17 (unbounded wait): `_earliest_wait()` must filter out
    past/disabled-seat cooldowns exactly like `is_in_cooldown()`/`pick_seat()` already do, and
    `_wait_seconds()` must floor above zero and cap well below "parks for a year."
    """

    def test_past_cooldown_is_not_a_candidate(self) -> None:
        seat = _seat("stale")
        state = _state()
        past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
        cooldown.update_seat(state, seat.path, cooldown_until=past)
        # A past cooldownUntil must not win against nothing: no real future candidate at all.
        self.assertIsNone(loop._earliest_wait([seat], state))

    def test_past_cooldown_does_not_mask_a_real_future_one(self) -> None:
        stale = _seat("stale")
        live = _seat("live")
        state = _state()
        past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
        future = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)).isoformat()
        cooldown.update_seat(state, stale.path, cooldown_until=past)
        cooldown.update_seat(state, live.path, cooldown_until=future)
        result = loop._earliest_wait([stale, live], state)
        self.assertIsNotNone(result)
        self.assertEqual(result.isoformat(), future)

    def test_disabled_seats_cooldown_is_excluded(self) -> None:
        seat = _seat("off")
        state = _state()
        cooldown.set_seat_disabled(state, "off", True)
        future = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)).isoformat()
        cooldown.update_seat(state, seat.path, cooldown_until=future)
        # A disabled seat has no automatic path back to usable — its cooldown must not dictate
        # how long the loop sleeps.
        self.assertIsNone(loop._earliest_wait([seat], state))

    def test_needs_login_seats_cooldown_is_excluded(self) -> None:
        seat = fleet.Seat(name="locked", path=Path("/fake/.claude-locked"), has_creds=False, needs_login=True)
        state = _state()
        future = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)).isoformat()
        cooldown.update_seat(state, seat.path, cooldown_until=future)
        self.assertIsNone(loop._earliest_wait([seat], state))

    def test_b9_dead_band_seat_with_no_cooldown_still_yields_a_wait_from_its_reset_time(self) -> None:
        """B9: a seat between `start_cap` and `ceiling_pct` never gets a `cooldownUntil`
        (`rotate_off()` is false by construction) — but `pick_seat()` DOES record `lastResetsAt`
        for every seat it successfully polls, dead-band or not. If that's the only signal
        available, `_earliest_wait()` must use it rather than falling through to `None` (which
        `_wait_seconds()` turns into the bare ~90s default — a silent poll storm below
        `_LONG_WAIT_NOTIFY_S`)."""
        seat = _seat("dead-band")
        state = _state()
        future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)).isoformat()
        # No cooldown_until recorded at all — only lastResetsAt, exactly what a dead-band seat's
        # successful-but-above-start-cap poll produces via `_record_usage()`.
        cooldown.update_seat(state, seat.path, last_resets_at=future, last_percent=68.0)
        result = loop._earliest_wait([seat], state)
        self.assertIsNotNone(result)
        self.assertEqual(result.isoformat(), future)

    def test_b9_all_dead_band_seats_do_not_produce_a_below_notify_threshold_silent_wait(self) -> None:
        """The full B9 reproduction: every usable seat is simultaneously in the dead band. The
        resulting wait must be derived from a real future reset time, not collapse to the bare
        default retry wait that stays under the long-wait notify threshold forever."""
        a = _seat("a")
        b = _seat("b")
        state = _state()
        soon = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=20)).isoformat()
        later = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=3)).isoformat()
        cooldown.update_seat(state, a.path, last_resets_at=soon, last_percent=66.0)
        cooldown.update_seat(state, b.path, last_resets_at=later, last_percent=69.0)
        result = loop._earliest_wait([a, b], state)
        self.assertIsNotNone(result)
        self.assertEqual(result.isoformat(), soon)
        wait_s = loop._wait_seconds(result)
        self.assertGreater(wait_s, loop._LONG_WAIT_NOTIFY_S)

    def test_b9_past_last_resets_at_is_not_a_candidate(self) -> None:
        seat = _seat("stale-reset")
        state = _state()
        past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).isoformat()
        cooldown.update_seat(state, seat.path, last_resets_at=past, last_percent=68.0)
        self.assertIsNone(loop._earliest_wait([seat], state))

    def test_b9_cooldown_until_still_wins_when_it_is_sooner_than_last_resets_at(self) -> None:
        """Both signals coexist for an ordinary rotated-off seat (`cooldownUntil` IS derived
        from the same reset time) — this pins that adding `lastResetsAt` as a second candidate
        doesn't regress the ordinary cooldown path when the two happen to differ."""
        seat = _seat("mixed")
        state = _state()
        sooner = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat()
        later = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=5)).isoformat()
        cooldown.update_seat(state, seat.path, cooldown_until=sooner, last_resets_at=later)
        result = loop._earliest_wait([seat], state)
        self.assertEqual(result.isoformat(), sooner)

    def test_wait_seconds_floors_above_zero(self) -> None:
        almost_now = dt.datetime.now(dt.UTC) + dt.timedelta(milliseconds=1)
        self.assertGreaterEqual(loop._wait_seconds(almost_now), loop._MIN_WAIT_S)

    def test_wait_seconds_caps_a_far_future_wait(self) -> None:
        far = dt.datetime.now(dt.UTC) + dt.timedelta(days=7)
        self.assertLessEqual(loop._wait_seconds(far), loop._MAX_WAIT_S)

    def test_wait_seconds_none_uses_default(self) -> None:
        self.assertEqual(loop._wait_seconds(None), loop._DEFAULT_RETRY_WAIT_S)


def _init_repo(repo: Path) -> None:
    """A throwaway local git repo (no network, no remotes) — the same fixture idiom
    tests/test_gadkit.py uses, duplicated so this module stays self-contained.
    """
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("placeholder\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial commit"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


class RunOnceIdeationHandbackTests(unittest.TestCase):
    """`run_once()`'s no-seat early return must hand back the backlog-refill attempt `triage()`
    booked at decision time. Without it, one all-seats-cooling window silently consumed a research
    repo's only refill and the next iteration ended the crawl with DONE having never spawned
    `claude` at all. Fully offline: seat discovery is stubbed out, so no `~/.claude*` dir is read
    and no subprocess beyond local `git` runs.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)

    def _seed_exhausted_backlog(self, *, research: bool) -> None:
        gad = self.repo / ".gad"
        gad.mkdir(parents=True, exist_ok=True)
        (gad / "generations-index.json").write_text(
            json.dumps({"project": "t", "nextGen": 2, "generations": [{"gen": 0}, {"gen": 1}]}, indent=2),
            encoding="utf-8",
        )
        marker = "<!-- gad-mode: research -->\n\n" if research else ""
        (gad / "backlog.md").write_text(
            f"# B\n\n{marker}## G0 — a\n- **Type**: eda\n\n## G1 — b\n- **Type**: experiment\n",
            encoding="utf-8",
        )
        for args in (("add", "-A"), ("commit", "-q", "-m", "seed .gad state")):
            subprocess.run(["git", *args], cwd=str(self.repo), check=True, capture_output=True, text=True)

    def _run_once_with_no_seats(self, state: dict) -> loop.IterationResult:
        with mock.patch.object(loop.fleet, "discover_seats", return_value=[]):
            return loop.run_once(self.repo, Config(), state, _FakeCache({}))

    def test_no_seat_hands_the_research_refill_attempt_back(self) -> None:
        self._seed_exhausted_backlog(research=True)
        state = _state()

        first = self._run_once_with_no_seats(state)
        self.assertEqual((first.plan.kind, first.plan.mode), ("RUN", "gad_run"))
        self.assertIsNone(first.seat)
        self.assertIsNone(first.action)  # nothing ran, so nothing to classify
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, self.repo))

        # ...and the very next iteration must still plan the refill rather than terminate.
        second = self._run_once_with_no_seats(state)
        self.assertEqual((second.plan.kind, second.plan.mode), ("RUN", "gad_run"))

    def test_hand_back_is_harmless_for_a_non_research_repo(self) -> None:
        """The same early return runs for every plan kind that reaches seat selection; on a build
        repo there is no marker to give back and it must not create one (or raise).
        """
        self._seed_exhausted_backlog(research=False)
        (self.repo / ".gad" / "backlog.md").write_text(
            "# B\n\n## G0 — a\n\n## G1 — b\n\n## G2 — pending\n", encoding="utf-8"
        )
        for args in (("add", "-A"), ("commit", "-q", "-m", "declare a pending generation")):
            subprocess.run(["git", *args], cwd=str(self.repo), check=True, capture_output=True, text=True)

        state = _state()
        result = self._run_once_with_no_seats(state)
        self.assertEqual((result.plan.kind, result.plan.mode), ("RUN", "gad_run"))
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, self.repo))
        self.assertEqual(state.get("repos", {}).get(str(self.repo), {}).get("ideationAttemptedAtHead"), None)

    def test_a_parked_plan_never_reaches_seat_selection(self) -> None:
        """Sanity boundary for the hand-back's placement: an AWAITING_HUMAN/BLOCKED repo returns
        before seat selection, so `discover_seats()` (a real filesystem scan of the operator's
        home) must not be called at all.
        """
        self._seed_exhausted_backlog(research=True)
        gen_dir = gadkit.generation_dir(self.repo, 2)
        gen_dir.mkdir(parents=True)
        (gen_dir / "handoff.md").write_text("BLOCKED: needs owner input\n", encoding="utf-8")
        state = _state()
        with mock.patch.object(loop.fleet, "discover_seats") as discover:
            result = loop.run_once(self.repo, Config(), state, _FakeCache({}))
        discover.assert_not_called()
        self.assertEqual(result.plan.kind, "BLOCKED")

    def test_no_seat_on_an_unrelated_recovery_plan_does_not_wipe_a_real_ideation_booking(self) -> None:
        """A6 audit fix regression: the no-seat hand-back must only clear
        `ideationAttemptedAtHead` when the CURRENT plan is the one that booked it
        (`plan.ideation_refill`). A completely unrelated no-seat iteration — here, a
        mid-generation recovery restart triggered by dirt that has nothing to do with
        ideation — must leave a real, still-binding ideation booking alone; wiping it let the
        same HEAD be re-attempted for auto-ideation indefinitely, since the very next no-seat
        iteration would just clear it again.
        """
        self._seed_exhausted_backlog(research=True)
        state = _state()
        gadkit.triage(self.repo, Config(), state)  # prime the clean baseline
        head = gadkit.git_head(self.repo)
        cooldown.set_ideation_attempt_head(state, self.repo, head)  # simulate a REAL prior booking

        # Dirty the tree with an unrelated mid-generation interruption (gen == nextGen, but this
        # plan is a `/gad-generation` restart, NOT the ideation-refill plan).
        gen_dir = gadkit.generation_dir(self.repo, 2)
        gen_dir.mkdir(parents=True)
        (gen_dir / "plan.md").write_text("# plan\n", encoding="utf-8")

        result = self._run_once_with_no_seats(state)
        self.assertEqual(result.plan.mode, "gad_generation")
        self.assertFalse(result.plan.ideation_refill)
        self.assertEqual(cooldown.get_ideation_attempt_head(state, self.repo), head)


class RunOnceForcedCooldownTests(unittest.TestCase):
    """A1 audit fix: `outcome()` is only AGENT_DEAD_NONLIMIT when the seat's OWN post-run usage
    reading did NOT justify a cooldown (`near_cap()`/`rotate_off()` false by that bucket's own
    premise) — so when `classify()` still resolves that to CONTINUE_ROTATE (the probe-confirmed
    workflow signature, or the usage-unavailable backstop), `run_once()` must force a cooldown
    onto the seat anyway. Without it, `pick_seat()` would happily re-select the exact same seat
    from the exact same cached reading next iteration: an uncapped, no-backoff respawn loop.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)
        gad = self.repo / ".gad"
        gad.mkdir(parents=True)
        (gad / "generations-index.json").write_text(
            json.dumps({"project": "t", "nextGen": 0, "generations": []}, indent=2), encoding="utf-8"
        )
        (gad / "backlog.md").write_text("# B\n\n## G0 — a\n- **Type**: build\n", encoding="utf-8")
        for args in (("add", "-A"), ("commit", "-q", "-m", "seed .gad state")):
            subprocess.run(["git", *args], cwd=str(self.repo), check=True, capture_output=True, text=True)

    def _run_once_with_fake_runner(
        self, state: dict, tail: list[str]
    ) -> tuple[loop.IterationResult, fleet.Seat]:
        seat = _seat("only")
        fake_result = runner_mod.RunResult(
            returncode=1, tail=tail, log_path=Path("/dev/null"), duration_s=1.0
        )
        cache = _FakeCache({str(seat.path): _usage_at(10.0)})
        with (
            mock.patch.object(loop.fleet, "discover_seats", return_value=[seat]),
            mock.patch.object(loop.runner, "run", return_value=fake_result),
        ):
            result = loop.run_once(self.repo, Config(), state, cache)
        return result, seat

    def test_workflow_probe_confirmed_death_forces_a_cooldown(self) -> None:
        state = _state()
        tail = [
            _assistant_line("gad-run: G0 agent returned null 3x and the pong probe also died"),
            _assistant_line("RESULT: LIMIT-SUSPECTED"),
        ]
        result, seat = self._run_once_with_fake_runner(state, tail)
        self.assertEqual(result.outcome, "AGENT_DEAD_NONLIMIT")
        assert result.action is not None
        self.assertEqual(result.action.kind, detector.CONTINUE_ROTATE)
        self.assertTrue(cooldown.is_in_cooldown(state, seat.path))

    def test_generic_backstop_death_also_forces_a_cooldown(self) -> None:
        """The SAME `_force_cooldown()` call site covers both CONTINUE_ROTATE flavors — the
        generic backstop (usage endpoint unreadable post-run) must not be left un-cooled either.
        """
        seat = _seat("only")
        state = _state()
        fake_result = runner_mod.RunResult(
            returncode=1,
            tail=[_assistant_line("error: you have hit your usage limit for this session")],
            log_path=Path("/dev/null"),
            duration_s=1.0,
        )

        class _FlakyPostRunCache:
            """Succeeds for pick_seat()'s pre-run poll, then raises for the post-run
            `force=True` poll — simulating the usage endpoint going unreadable mid-run."""

            def __init__(self) -> None:
                self._calls = 0

            def poll(self, seat_dir, ttl: float = 90.0, force: bool = False):  # noqa: ANN001
                self._calls += 1
                if force:
                    raise usage_mod.RateLimited("simulated post-run poll failure")
                return _usage_at(10.0)

        with (
            mock.patch.object(loop.fleet, "discover_seats", return_value=[seat]),
            mock.patch.object(loop.runner, "run", return_value=fake_result),
        ):
            result = loop.run_once(self.repo, Config(), state, _FlakyPostRunCache())
        self.assertEqual(result.outcome, "AGENT_DEAD_NONLIMIT")
        assert result.action is not None
        self.assertEqual(result.action.kind, detector.CONTINUE_ROTATE)
        self.assertTrue(cooldown.is_in_cooldown(state, seat.path))

    def test_ordinary_agent_death_does_not_force_a_cooldown(self) -> None:
        """Sanity boundary: an everyday transient failure (RETRY) must NOT get a forced
        cooldown — only the CONTINUE_ROTATE/AGENT_DEAD_NONLIMIT combination should.
        """
        state = _state()
        result, seat = self._run_once_with_fake_runner(state, [_assistant_line("nothing special happened")])
        self.assertEqual(result.outcome, "AGENT_DEAD_NONLIMIT")
        assert result.action is not None
        self.assertEqual(result.action.kind, detector.RETRY)
        self.assertFalse(cooldown.is_in_cooldown(state, seat.path))

    def test_repeated_workflow_probe_confirmed_deaths_still_trip_the_hard_error_breaker(self) -> None:
        """The other half of A1: `loop.run()`'s CONTINUE_ROTATE handling must not reset the
        HARD_ERROR breaker unconditionally — repeated probe-confirmed deaths (across however
        many different seats) is exactly the "platform outage, not one bad seat" signature the
        breaker exists to catch.
        """
        consecutive_agent_dead = 0
        tail = [
            _assistant_line("gad-run: not burning further retries into a closed window"),
            _assistant_line("RESULT: LIMIT-SUSPECTED"),
        ]
        for _ in range(loop._MAX_CONSECUTIVE_AGENT_DEAD):
            state = _state()
            result, _seat_used = self._run_once_with_fake_runner(state, tail)
            assert result.action is not None
            kind = result.action.kind
            # Exercises the SAME helper `loop.run()` itself calls, rather than re-deriving the
            # condition here — a divergence between this test and the real dispatch would
            # otherwise go unnoticed.
            if kind == detector.CONTINUE or loop._is_genuine_wall_hit_rotation(kind, result.outcome):
                consecutive_agent_dead = 0
            elif kind in (detector.RETRY, detector.CONTINUE_ROTATE):
                consecutive_agent_dead += 1
        self.assertGreaterEqual(consecutive_agent_dead, loop._MAX_CONSECUTIVE_AGENT_DEAD)

    def test_a_rate_limit_events_real_resets_at_is_used_for_the_forced_cooldown(self) -> None:
        """Blocker 1 item 2, end to end: `_force_cooldown()` must use the REAL structured
        `resetsAt` a `rate_limit_event` provides (via `detector.Action.resets_at`), not the
        blind `_TIMEOUT_COOLDOWN_S` guess, when one is available.
        """
        state = _state()
        rate_limit_envelope = json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed_warning",
                    "resetsAt": 1785290400,
                    "rateLimitType": "seven_day",
                    "utilization": 0.95,
                    "isUsingOverage": False,
                    "surpassedThreshold": 0.75,
                },
                "uuid": "u1",
                "session_id": "s1",
            }
        )
        result, seat = self._run_once_with_fake_runner(state, [rate_limit_envelope])
        assert result.action is not None
        self.assertEqual(result.action.kind, detector.CONTINUE_ROTATE)
        self.assertEqual(result.action.resets_at, "2026-07-29T02:00:00+00:00")
        entry = cooldown.get_seat_state(state, seat.path)
        self.assertEqual(entry["cooldownUntil"], "2026-07-29T02:00:00+00:00")


class ParkAndWaitRetryNotifyTests(unittest.TestCase):
    """B21 audit fix: a transient notify failure (Telegram down for a moment) must be retried on
    a LATER re-triage cycle, not lost for the whole park — which can last indefinitely
    (AWAITING_HUMAN/BLOCKED have no timeout).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config = Config(state_dir=root / "state", state_path=root / "state" / "state.json")
        (root / "state").mkdir(parents=True)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.lock = loop.SingleInstanceLock(self.config.state_dir / "claude-relay.lock")
        self.lock.acquire()
        self.addCleanup(self.lock.release)

    def test_a_transient_send_failure_is_retried_on_the_next_retriage_cycle(self) -> None:
        state = _state()
        plans = iter(
            [
                gadkit.Plan(kind="AWAITING_HUMAN", repo=self.repo, detail="d"),
                gadkit.Plan(kind="AWAITING_HUMAN", repo=self.repo, detail="d"),
                gadkit.Plan(kind="RUN", repo=self.repo, detail="d"),  # unparks on the 3rd cycle
            ]
        )
        with (
            mock.patch.object(loop.gadkit, "triage", side_effect=lambda *a, **k: next(plans)),
            mock.patch.object(loop.time, "sleep", return_value=None),
            mock.patch.object(loop.notify, "poll_telegram_updates", return_value=[]),
            mock.patch.object(loop.notify, "dispatch", side_effect=[False, True]) as fake_dispatch,
        ):
            loop._park_and_wait(
                self.repo, self.config, state, self.lock, notify_key="park:test", notify_message="hello"
            )
        # First retriage cycle's send failed; the second retried and succeeded. A THIRD attempt
        # never happens because `notify()`'s own dedupe sees it already marked sent.
        self.assertEqual(fake_dispatch.call_count, 2)
        self.assertTrue(cooldown.was_notified(state, "park:test"))

    def test_no_retry_attempted_when_no_notify_key_is_given(self) -> None:
        """Backward-compatible default: omitting `notify_key`/`notify_message` (as any FUTURE
        caller not tied to a park notification might) must not call dispatch at all."""
        state = _state()
        plans = iter([gadkit.Plan(kind="RUN", repo=self.repo, detail="d")])
        with (
            mock.patch.object(loop.gadkit, "triage", side_effect=lambda *a, **k: next(plans)),
            mock.patch.object(loop.notify, "poll_telegram_updates", return_value=[]),
            mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch,
        ):
            loop._park_and_wait(self.repo, self.config, state, self.lock)
        fake_dispatch.assert_not_called()

    def test_a_successfully_sent_notification_is_not_resent_on_later_cycles(self) -> None:
        state = _state()
        plans = iter(
            [
                gadkit.Plan(kind="AWAITING_HUMAN", repo=self.repo, detail="d"),
                gadkit.Plan(kind="AWAITING_HUMAN", repo=self.repo, detail="d"),
                gadkit.Plan(kind="RUN", repo=self.repo, detail="d"),
            ]
        )
        with (
            mock.patch.object(loop.gadkit, "triage", side_effect=lambda *a, **k: next(plans)),
            mock.patch.object(loop.time, "sleep", return_value=None),
            mock.patch.object(loop.notify, "poll_telegram_updates", return_value=[]),
            mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch,
        ):
            loop._park_and_wait(
                self.repo, self.config, state, self.lock, notify_key="park:test", notify_message="hello"
            )
        self.assertEqual(fake_dispatch.call_count, 1)


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

    def test_liveness_beats_a_large_mtime_age(self) -> None:
        """B1 core regression: the OLD `_is_stale()` checked mtime age BEFORE liveness and
        short-circuited on it, so ANY run longer than `stale_after_s` (a healthy multi-day
        crawl, exactly the workload this tool exists for) became unconditionally reclaimable
        regardless of whether the holding process was still alive. A genuinely alive process
        whose recorded start-time matches its real one must NOT be reclaimed no matter how old
        the lockfile's mtime looks.
        """
        my_pid = os.getpid()
        my_starttime = loop._proc_start_time(my_pid)
        if my_starttime is None:
            self.skipTest("/proc start-time unavailable on this platform — nothing to regress-test")
        self.lock_path.write_text(f"{my_pid}:{my_starttime}")
        old = time.time() - 10 * 3600  # 10h old, past the default 6h stale_after_s
        os.utime(self.lock_path, (old, old))
        lock = loop.SingleInstanceLock(self.lock_path, stale_after_s=6 * 3600)
        self.assertFalse(lock._is_stale())

    def test_is_stale_falls_back_to_age_only_when_uncorroboratable(self) -> None:
        """The mtime age check is a FALLBACK, reached only when start-time corroboration is
        impossible (non-Linux, or a /proc read race) — pin both sides of that fallback."""
        my_pid = os.getpid()
        self.lock_path.write_text(f"{my_pid}:12345")
        lock = loop.SingleInstanceLock(self.lock_path, stale_after_s=100.0)
        with mock.patch.object(loop, "_proc_start_time", return_value=None):
            old = time.time() - 1000  # older than stale_after_s
            os.utime(self.lock_path, (old, old))
            self.assertTrue(lock._is_stale())
            os.utime(self.lock_path, None)  # fresh mtime (what heartbeat() does)
            self.assertFalse(lock._is_stale())

    def test_heartbeat_refreshes_mtime(self) -> None:
        lock = loop.SingleInstanceLock(self.lock_path)
        lock.acquire()
        old = time.time() - 10_000
        os.utime(self.lock_path, (old, old))
        lock.heartbeat()
        self.assertGreater(self.lock_path.stat().st_mtime, old)
        lock.release()

    def test_heartbeat_before_acquire_is_a_noop(self) -> None:
        lock = loop.SingleInstanceLock(self.lock_path)
        lock.heartbeat()  # must not raise or create the lockfile
        self.assertFalse(self.lock_path.exists())

    def test_release_does_not_unlink_a_lock_reclaimed_by_someone_else(self) -> None:
        """B1: if this instance's lock was reclaimed by another instance in the meantime (its
        file now names a different pid/starttime), `release()` must NOT delete that OTHER
        instance's lock out from under it."""
        lock = loop.SingleInstanceLock(self.lock_path)
        lock.acquire()
        self.lock_path.write_text("424242:999999")  # simulate a reclaim by someone else
        lock.release()
        self.assertEqual(self.lock_path.read_text(), "424242:999999")

    def test_release_unlinks_when_still_genuinely_ours(self) -> None:
        lock = loop.SingleInstanceLock(self.lock_path)
        lock.acquire()
        lock.release()
        self.assertFalse(self.lock_path.exists())


class RunTopLevelExceptionTests(unittest.TestCase):
    """B7/B12 audit fix: `loop.run()`'s literal source (not just its decision helpers) is
    exercised here — a stubbed `run_once()` lets the real `while True:`/lock/notify/save-state
    machinery run for one iteration without needing a real `claude` subprocess or repo.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config = Config(
            state_dir=root / "state",
            state_path=root / "state" / "state.json",
            log_dir=root / "logs",
            notify_sink="stdout",  # irrelevant here — dispatch is mocked directly
        )
        (root / "state").mkdir(parents=True)
        self.repo = root / "repo"
        self.repo.mkdir()

    def test_an_uncaught_exception_notifies_before_it_propagates(self) -> None:
        boom = RuntimeError("simulated gadkit_plugin_root() FileNotFoundError stand-in")
        with (
            mock.patch.object(loop, "run_once", side_effect=boom),
            mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch,
        ):
            with self.assertRaises(RuntimeError):
                loop.run(self.repo, self.config, once=True)

        self.assertEqual(fake_dispatch.call_count, 1)
        (_config_arg, message), _kwargs = fake_dispatch.call_args
        self.assertIn("CRASHED", message)
        self.assertIn(str(self.repo), message)

    def test_state_is_still_saved_when_an_exception_propagates(self) -> None:
        """The pre-existing `finally: cooldown.save_state(...)` must still run — this is a
        regression guard that adding the new `except` clause didn't accidentally swallow it or
        change execution order."""
        boom = RuntimeError("boom")
        with (
            mock.patch.object(loop, "run_once", side_effect=boom),
            mock.patch.object(loop.notify, "dispatch", return_value=True),
        ):
            with self.assertRaises(RuntimeError):
                loop.run(self.repo, self.config, once=True)
        self.assertTrue(self.config.state_path.exists())

    def test_a_failing_notify_does_not_mask_the_original_exception(self) -> None:
        """The notify-before-reraise call is itself best-effort — if dispatch() somehow raises
        (a bug in a sink, not just an ordinary network failure that notify.py now catches
        internally), the ORIGINAL exception must still be what propagates, not a notify error."""
        boom = ValueError("original failure")
        with (
            mock.patch.object(loop, "run_once", side_effect=boom),
            mock.patch.object(loop.notify, "dispatch", side_effect=RuntimeError("notify itself blew up")),
        ):
            with self.assertRaises(ValueError):
                loop.run(self.repo, self.config, once=True)

    def test_b12_gadkit_plugin_root_file_not_found_error_notifies_before_propagating(self) -> None:
        """B12's named reproduction: `gadkit.gadkit_plugin_root()` (called from
        `gadkit.command()`, reached from inside `run_once()`) raises a bare `FileNotFoundError`
        when the gad-kit plugin isn't installed — previously this reached `main()` uncaught with
        zero notification while the monitor kept painting a healthy table."""
        boom = FileNotFoundError("gad-kit plugin workflows not found under ~/.claude/plugins/...")
        with (
            mock.patch.object(loop, "run_once", side_effect=boom),
            mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch,
        ):
            with self.assertRaises(FileNotFoundError):
                loop.run(self.repo, self.config, once=True)
        self.assertEqual(fake_dispatch.call_count, 1)

    def test_b12_git_recovery_error_notifies_before_propagating(self) -> None:
        """B12's other named reproduction: `gadkit.git_stash_push()`'s `GitRecoveryError`."""
        boom = gadkit.GitRecoveryError("git stash push -u failed: <simulated git failure>")
        with (
            mock.patch.object(loop, "run_once", side_effect=boom),
            mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch,
        ):
            with self.assertRaises(gadkit.GitRecoveryError):
                loop.run(self.repo, self.config, once=True)
        self.assertEqual(fake_dispatch.call_count, 1)

    def test_keyboard_interrupt_is_not_treated_as_a_crash_to_notify(self) -> None:
        """Deliberately `except Exception`, not `BaseException`: an operator-issued Ctrl-C must
        propagate silently (no alarming 'CRASHED' notification for their own deliberate action)."""
        with (
            mock.patch.object(loop, "run_once", side_effect=KeyboardInterrupt),
            mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch,
        ):
            with self.assertRaises(KeyboardInterrupt):
                loop.run(self.repo, self.config, once=True)
        fake_dispatch.assert_not_called()


class NoRetryBreakerTests(unittest.TestCase):
    """Blocker 1 item 3 (E9 DIRTY-TREE) / the REFUSED-status regression fix:
    `action.no_retry=True` must trip the HARD_ERROR breaker on the FIRST occurrence, not after
    the usual `_MAX_CONSECUTIVE_AGENT_DEAD` retries — retrying a KNOWN-non-retryable failure
    (gad-run refused to even start; gad-finish mechanically refused) is pure waste. Drives the
    REAL `loop.run()` dispatch (not a re-derivation of its condition), with `run_once()` stubbed.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config = Config(
            state_dir=root / "state",
            state_path=root / "state" / "state.json",
            log_dir=root / "logs",
            notify_sink="stdout",
        )
        (root / "state").mkdir(parents=True)
        self.repo = root / "repo"
        self.repo.mkdir()

    def _iteration(self, reason: str) -> loop.IterationResult:
        plan = gadkit.Plan(kind="FINISH", repo=self.repo, gen=7, mode="gad_finish", tier="budget")
        action = detector.Action(detector.RETRY, reason, no_retry=True)
        return loop.IterationResult(
            plan=plan,
            action=action,
            seat=_seat("only"),
            run_result=runner_mod.RunResult(
                returncode=1, tail=[], log_path=Path("/dev/null"), duration_s=1.0
            ),
            outcome="AGENT_DEAD_NONLIMIT",
        )

    def test_no_retry_trips_the_breaker_on_the_first_occurrence_not_the_third(self) -> None:
        iteration = self._iteration("gad-finish's own RESULT reported REFUSED")
        with (
            mock.patch.object(loop, "run_once", return_value=iteration) as fake_run_once,
            mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch,
        ):
            rc = loop.run(self.repo, self.config, once=False)
        self.assertEqual(rc, 1)
        self.assertEqual(fake_run_once.call_count, 1)  # NOT 3 — tripped immediately
        self.assertEqual(fake_dispatch.call_count, 1)
        message = fake_dispatch.call_args.args[1]
        self.assertIn("non-retryable", message)
        self.assertIn("REFUSED", message)

    def test_an_ordinary_retryable_failure_still_needs_three_before_hard_error(self) -> None:
        """Sanity boundary: an everyday RETRY (no_retry=False) must NOT trip the breaker on the
        first occurrence — only the genuinely non-retryable case gets the fast path."""
        iteration = self._iteration("transient failure")
        iteration.action = detector.Action(detector.RETRY, "transient failure", no_retry=False)
        with (
            mock.patch.object(loop, "run_once", return_value=iteration) as fake_run_once,
            mock.patch.object(loop.notify, "dispatch", return_value=True),
        ):
            rc = loop.run(self.repo, self.config, once=False)
        self.assertEqual(rc, 1)
        self.assertEqual(fake_run_once.call_count, loop._MAX_CONSECUTIVE_AGENT_DEAD)


class DoneMessageReportsTheOutcomeTests(unittest.TestCase):
    """B29 audit fix: `DONE`'s notification must report the actual terminal OUTCOME
    (`iteration.action.reason`, what `detector.classify()` concluded from a genuinely executed
    run's post-run `outcome()`), not the pre-run PLAN's own description
    (`iteration.plan.detail`) — which, on the auto-ideation-exhaustion path, reads like "handing
    the backlog to /gad-run instead of terminating" even when the crawl is now, in fact,
    terminating.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config = Config(
            state_dir=root / "state",
            state_path=root / "state" / "state.json",
            log_dir=root / "logs",
            notify_sink="stdout",
        )
        (root / "state").mkdir(parents=True)
        self.repo = root / "repo"
        self.repo.mkdir()

    def test_done_message_uses_the_action_reason_not_the_plan_detail(self) -> None:
        plan = gadkit.Plan(
            kind="RUN",
            repo=self.repo,
            mode="gad_run",
            detail=(
                "backlog exhausted on a RESEARCH repo — handing it to /gad-run for one "
                "auto-ideation attempt instead of terminating"
            ),
            ideation_refill=True,
        )
        iteration = loop.IterationResult(
            plan=plan,
            action=detector.Action(detector.DONE, "backlog exhausted"),
            outcome="NO_BACKLOG",
        )
        with (
            mock.patch.object(loop, "run_once", return_value=iteration),
            mock.patch.object(loop.notify, "dispatch", return_value=True) as fake_dispatch,
        ):
            result = loop.run(self.repo, self.config, once=True)
        self.assertEqual(result, 0)
        self.assertEqual(fake_dispatch.call_count, 1)
        (_config_arg, message), _kwargs = fake_dispatch.call_args
        self.assertEqual(message, "DONE: backlog exhausted")
        self.assertNotIn("instead of terminating", message)


if __name__ == "__main__":
    unittest.main()
