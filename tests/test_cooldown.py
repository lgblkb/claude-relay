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

    def test_b14_corrupt_file_is_quarantined_not_silently_discarded(self) -> None:
        """B14 audit fix: an existing-but-unreadable state file must not vanish silently — it is
        renamed aside (never deleted) so the operator can inspect what was lost, distinct from
        the genuinely-missing-file (fresh install) case."""
        self.state_path.write_text("{not json", encoding="utf-8")
        cooldown.load_state(self.state_path)
        self.assertFalse(self.state_path.exists())  # moved aside, not left in place
        quarantined = list(self.state_path.parent.glob(f"{self.state_path.name}.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{not json")

    def test_b14_a_valid_json_non_object_is_also_quarantined(self) -> None:
        """`load_state()`'s contract is "a JSON OBJECT with this schema" — valid JSON that is
        merely the wrong shape (e.g. a bare list) is exactly as corrupt for our purposes as
        unparseable JSON, and must be quarantined the same way, not silently accepted-then-
        replaced."""
        self.state_path.write_text("[1, 2, 3]", encoding="utf-8")
        state = cooldown.load_state(self.state_path)
        self.assertEqual(state["seats"], {})
        quarantined = list(self.state_path.parent.glob(f"{self.state_path.name}.corrupt-*"))
        self.assertEqual(len(quarantined), 1)

    def test_b14_a_missing_file_is_not_treated_as_corrupt(self) -> None:
        """The fresh-install case must stay silent — no warning, no quarantine file, nothing to
        preserve, since there was never anything there in the first place."""
        cooldown.load_state(self.state_path)  # file never existed
        quarantined = list(self.state_path.parent.glob("*.corrupt-*"))
        self.assertEqual(quarantined, [])

    def test_b14_save_state_fsyncs_before_returning(self) -> None:
        """Direct unit coverage that `save_state()` actually calls `os.fsync` on the tmp file's
        fd (not just that a plain round-trip happens to work, which would pass even with no
        fsync at all)."""
        from unittest import mock

        state = cooldown.load_state(self.state_path)
        with mock.patch.object(cooldown.os, "fsync", wraps=cooldown.os.fsync) as fake_fsync:
            cooldown.save_state(self.state_path, state)
        self.assertGreaterEqual(fake_fsync.call_count, 1)

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

    def test_clamp_future_rejects_implausibly_far_future(self) -> None:
        """B17: clamp_future() must be symmetric — an implausibly-far-future `resets_at` (a
        corrupt value, or a clock skewed forward by e.g. an RTC error) is just as untrustworthy
        as a past one and must not be recorded as a real cooldown boundary."""
        garbage_future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=400)).isoformat()
        self.assertIsNone(cooldown.clamp_future(garbage_future))

    def test_clamp_future_accepts_a_legitimate_weekly_reset(self) -> None:
        """The longest REAL window this tool waits on is a ~7-day weekly usage-limit reset
        (usage.py's earliest_reset()) — the far-future guard must not reject that."""
        weekly_reset = (dt.datetime.now(dt.UTC) + dt.timedelta(days=6, hours=23)).isoformat()
        self.assertEqual(cooldown.clamp_future(weekly_reset), weekly_reset)


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


class IdeationAttemptHeadTests(unittest.TestCase):
    """The one-attempt-per-HEAD bookkeeping that bounds a research repo's backlog refill
    (`gadkit._exhausted_backlog_plan()`). Every accessor must degrade on absent/partial state:
    `triage()` calls them on repos that have never been seen before.
    """

    def test_absent_key_reads_as_none(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, "/repo/a"))

    def test_set_then_get_round_trips_and_records_a_timestamp(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_ideation_attempt_head(state, "/repo/a", "deadbeef")
        self.assertEqual(cooldown.get_ideation_attempt_head(state, "/repo/a"), "deadbeef")
        self.assertIn("ideationAttemptedAt", cooldown.get_repo_entry(state, "/repo/a"))

    def test_set_with_no_head_is_a_noop_and_creates_no_entry(self) -> None:
        """A repo with no commits cannot be bounded by HEAD identity at all — recording an
        unbounded attempt would end the crawl on the next triage for the wrong reason.
        """
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_ideation_attempt_head(state, "/repo/a", None)
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, "/repo/a"))
        self.assertEqual(state.get("repos", {}), {})

    def test_set_with_no_head_does_not_overwrite_a_recorded_attempt(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_ideation_attempt_head(state, "/repo/a", "deadbeef")
        cooldown.set_ideation_attempt_head(state, "/repo/a", None)
        self.assertEqual(cooldown.get_ideation_attempt_head(state, "/repo/a"), "deadbeef")

    def test_clear_removes_the_marker_and_leaves_sibling_bookkeeping_intact(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_clean_baseline(state, "/repo/a", "basehead")
        cooldown.record_stash(state, "/repo/a", "stash@{0}", ["x.py"])
        cooldown.set_ideation_attempt_head(state, "/repo/a", "deadbeef")
        cooldown.clear_ideation_attempt_head(state, "/repo/a")
        entry = cooldown.get_repo_entry(state, "/repo/a")
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, "/repo/a"))
        self.assertNotIn("ideationAttemptedAt", entry)
        self.assertEqual(cooldown.get_clean_baseline(state, "/repo/a"), "basehead")
        self.assertIsNotNone(cooldown.get_last_stash(state, "/repo/a"))

    def test_clear_on_a_never_seen_repo_is_a_noop(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.clear_ideation_attempt_head(state, "/repo/never-seen")
        self.assertEqual(state.get("repos", {}), {})

    def test_clear_is_idempotent(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_ideation_attempt_head(state, "/repo/a", "deadbeef")
        for _ in range(3):
            cooldown.clear_ideation_attempt_head(state, "/repo/a")
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, "/repo/a"))

    def test_partial_state_with_only_a_timestamp_reads_as_none(self) -> None:
        """Hand-edited / older state.json: the timestamp without the head must not be mistaken
        for a booked attempt (that would terminate a research crawl at every HEAD).
        """
        state = cooldown.load_state(Path("/nonexistent"))
        state.setdefault("repos", {})["/repo/a"] = {"ideationAttemptedAt": "2026-07-26T00:00:00+00:00"}
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, "/repo/a"))

    def test_state_without_a_repos_table_at_all_still_reads_and_clears(self) -> None:
        state = {"schemaVersion": 1, "seats": {}}  # a pre-`repos` state.json
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, "/repo/a"))
        cooldown.clear_ideation_attempt_head(state, "/repo/a")  # must not raise
        cooldown.set_ideation_attempt_head(state, "/repo/a", "deadbeef")
        self.assertEqual(cooldown.get_ideation_attempt_head(state, "/repo/a"), "deadbeef")

    def test_path_and_string_repo_keys_are_interchangeable(self) -> None:
        """`gadkit.triage()` passes a `Path` while other call sites pass strings — a key mismatch
        would silently hand out an unlimited number of refills.
        """
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_ideation_attempt_head(state, Path("/repo/a"), "deadbeef")
        self.assertEqual(cooldown.get_ideation_attempt_head(state, "/repo/a"), "deadbeef")
        cooldown.clear_ideation_attempt_head(state, Path("/repo/a"))
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, "/repo/a"))

    def test_attempts_are_tracked_per_repo(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_ideation_attempt_head(state, "/repo/a", "aaa")
        self.assertIsNone(cooldown.get_ideation_attempt_head(state, "/repo/b"))
        cooldown.clear_ideation_attempt_head(state, "/repo/b")
        self.assertEqual(cooldown.get_ideation_attempt_head(state, "/repo/a"), "aaa")

    def test_marker_survives_the_state_file_round_trip(self) -> None:
        state = cooldown.load_state(Path("/nonexistent"))
        cooldown.set_ideation_attempt_head(state, "/repo/a", "deadbeef")
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            cooldown.save_state(state_path, state)
            reloaded = cooldown.load_state(state_path)
        self.assertEqual(cooldown.get_ideation_attempt_head(reloaded, "/repo/a"), "deadbeef")


if __name__ == "__main__":
    unittest.main()
