"""Offline tests for relay.cooldown: atomic state I/O, cooldown-window math, clock-skew
clamping, and notification dedupe bookkeeping.
"""

from __future__ import annotations

import datetime as dt
import stat
import tempfile
import unittest
from pathlib import Path

from relay import cooldown


class StateIOTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = Path(self._tmp.name) / "state.json"

    def test_load_missing_file_returns_empty_schema(self) -> None:
        state = cooldown.load_state(self.state_path)
        self.assertEqual(state["schemaVersion"], 1)
        self.assertEqual(state["seats"], {})

    def test_load_corrupt_file_falls_back_to_empty_rather_than_raising(self) -> None:
        self.state_path.write_text("{not json", encoding="utf-8")
        state = cooldown.load_state(self.state_path)
        self.assertEqual(state["seats"], {})

    def test_save_then_load_round_trips_and_chmods_600(self) -> None:
        state = cooldown.load_state(self.state_path)
        cooldown.update_seat(state, "/seat/a", has_creds=True, last_percent=42.0)
        cooldown.save_state(self.state_path, state)
        reloaded = cooldown.load_state(self.state_path)
        self.assertEqual(reloaded["seats"]["/seat/a"]["lastPercent"], 42.0)
        mode = stat.S_IMODE(self.state_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_update_seat_unset_sentinel_preserves_existing_value(self) -> None:
        state = cooldown.load_state(self.state_path)
        cooldown.update_seat(state, "/seat/a", cooldown_until="2026-01-01T00:00:00+00:00")
        cooldown.update_seat(state, "/seat/a", last_percent=5.0)  # cooldown_until left alone
        entry = cooldown.get_seat_state(state, "/seat/a")
        self.assertEqual(entry["cooldownUntil"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(entry["lastPercent"], 5.0)


class CooldownWindowTests(unittest.TestCase):
    def test_is_in_cooldown_true_for_future_timestamp(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        future = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).isoformat()
        cooldown.update_seat(state, "/seat/a", cooldown_until=future)
        self.assertTrue(cooldown.is_in_cooldown(state, "/seat/a"))

    def test_is_in_cooldown_false_for_past_timestamp(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        past = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
        cooldown.update_seat(state, "/seat/a", cooldown_until=past)
        self.assertFalse(cooldown.is_in_cooldown(state, "/seat/a"))

    def test_is_in_cooldown_false_when_unset(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        self.assertFalse(cooldown.is_in_cooldown(state, "/seat/never-seen"))

    def test_clamp_future_rejects_past_resets_at(self) -> None:
        past = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).isoformat()
        self.assertIsNone(cooldown.clamp_future(past))

    def test_clamp_future_accepts_future_resets_at(self) -> None:
        future = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat()
        self.assertEqual(cooldown.clamp_future(future), future)

    def test_clamp_future_none_input(self) -> None:
        self.assertIsNone(cooldown.clamp_future(None))


class NotifyDedupeTests(unittest.TestCase):
    def test_mark_then_was_notified(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        self.assertFalse(cooldown.was_notified(state, "all-exhausted"))
        cooldown.mark_notified(state, "all-exhausted")
        self.assertTrue(cooldown.was_notified(state, "all-exhausted"))

    def test_clear_notified_resets_dedupe(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.mark_notified(state, "needs-login:/seat/a")
        cooldown.clear_notified(state, "needs-login:/seat/a")
        self.assertFalse(cooldown.was_notified(state, "needs-login:/seat/a"))

    def test_clear_notified_prefix_clears_only_matching_keys(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.mark_notified(state, "park:/repo:AWAITING_HUMAN:2:abc")
        cooldown.mark_notified(state, "park:/repo:AWAITING_HUMAN:2:def")
        cooldown.mark_notified(state, "done:/repo")
        cooldown.clear_notified_prefix(state, "park:/repo:")
        self.assertFalse(cooldown.was_notified(state, "park:/repo:AWAITING_HUMAN:2:abc"))
        self.assertFalse(cooldown.was_notified(state, "park:/repo:AWAITING_HUMAN:2:def"))
        self.assertTrue(cooldown.was_notified(state, "done:/repo"))  # unrelated key untouched


class TelegramOffsetTests(unittest.TestCase):
    def test_offset_defaults_to_zero_and_round_trips(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        self.assertEqual(cooldown.get_telegram_offset(state), 0)
        cooldown.set_telegram_offset(state, 42)
        self.assertEqual(cooldown.get_telegram_offset(state), 42)


class RepoBaselineAndStashTests(unittest.TestCase):
    def test_clean_baseline_defaults_to_none(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        self.assertIsNone(cooldown.get_clean_baseline(state, "/repo/a"))

    def test_set_then_get_clean_baseline_round_trips(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_clean_baseline(state, "/repo/a", "deadbeef")
        self.assertEqual(cooldown.get_clean_baseline(state, "/repo/a"), "deadbeef")

    def test_set_clean_baseline_with_none_head_is_a_noop(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_clean_baseline(state, "/repo/a", "deadbeef")
        cooldown.set_clean_baseline(state, "/repo/a", None)  # must not overwrite with None
        self.assertEqual(cooldown.get_clean_baseline(state, "/repo/a"), "deadbeef")

    def test_record_and_get_last_stash(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        self.assertIsNone(cooldown.get_last_stash(state, "/repo/a"))
        cooldown.record_stash(state, "/repo/a", "stash@{0}", ["src/foo.py", ".gad/generation-2/"])
        recorded = cooldown.get_last_stash(state, "/repo/a")
        assert recorded is not None
        self.assertEqual(recorded["ref"], "stash@{0}")
        self.assertEqual(recorded["files"], ["src/foo.py", ".gad/generation-2/"])
        self.assertIn("at", recorded)

    def test_baseline_and_stash_round_trip_through_save_and_load(self) -> None:
        import tempfile

        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_clean_baseline(state, "/repo/a", "deadbeef")
        cooldown.record_stash(state, "/repo/a", "stash@{0}", ["x.py"])
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            cooldown.save_state(state_path, state)
            reloaded = cooldown.load_state(state_path)
        self.assertEqual(cooldown.get_clean_baseline(reloaded, "/repo/a"), "deadbeef")
        recorded = cooldown.get_last_stash(reloaded, "/repo/a")
        assert recorded is not None
        self.assertEqual(recorded["ref"], "stash@{0}")


if __name__ == "__main__":
    unittest.main()
