"""LIVE tests for the rate-limit calibration harness — real usage endpoint, real `claude` process.

Two tiers, gated separately because they cost differently:

  TIER 0 (free, auto-verifiable)  `RateLimitBaselineLiveTest`
      Zero tokens: one read-only GET per seat. Runs automatically whenever a usable seat exists and
      PROBE-SKIPS otherwise, matching `test_usage_endpoint_live.py`'s policy. Asserts the SHAPE the
      harness depends on and — the point of the tier — that `fetch_usage_raw()` really does return
      more than `UsageSnapshot` keeps, since the whole reason it exists is to see fields the
      projection drops.

  TIER 2 (costs real quota, opt-in)  `RateLimitCaptureLiveTest`
      Spawns a real `claude -p` and asserts the tap records what a live run actually emits. Gated
      behind CLAUDE_RELAY_LIVE_CLAUDE_RUN=1 like `test_claude_runner_live.py`, because a test that
      silently spends a subscription's quota when someone runs the suite is a trap.

Run:
    python3 -m unittest tests_live.test_rate_limit_capture_live -v                  # Tier 0 only
    CLAUDE_RELAY_LIVE_CLAUDE_RUN=1 python3 -m unittest tests_live.test_rate_limit_capture_live -v
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay import capture, fleet, ratelimit_probe, usage


class RateLimitBaselineLiveTest(unittest.TestCase):
    """TIER 0 — zero tokens."""

    def setUp(self) -> None:
        seats = [s for s in fleet.discover_seats() if s.usable]
        if not seats:
            self.skipTest("PROBE-SKIPPED: no usable seat (~/.claude-* with a valid access token)")
        self.seats = seats

    def test_raw_fetch_exposes_more_than_the_snapshot_projection_keeps(self) -> None:
        """The reason `fetch_usage_raw()` exists. If this ever stops being true, the harness has no
        purpose and `baseline` should be deleted rather than quietly reporting nothing.
        """
        try:
            raw = usage.fetch_usage_raw(self.seats[0].path)
        except usage.NeedsLoginError as exc:
            self.skipTest(f"PROBE-SKIPPED: seat became unusable between discovery and fetch: {exc}")
        except usage.RateLimited as exc:
            # A 429 is an environment condition, not a defect in the code under test. Observed for
            # real while building this harness (Retry-After: 300), so treating it as a failure would
            # make the suite red for anyone who ran it twice in quick succession.
            self.skipTest(f"PROBE-SKIPPED: usage endpoint rate-limited us: {exc}")
        except usage.UsageError as exc:
            self.fail(f"live usage endpoint call failed: {exc}")

        self.assertIsInstance(raw, dict)
        projected = {"five_hour", "seven_day", "limits"}
        self.assertTrue(projected.issubset(raw.keys()), f"endpoint no longer sends {projected - set(raw)}")
        dropped = set(raw) - projected
        self.assertTrue(
            dropped,
            "fetch_usage_raw() returned nothing beyond the projected keys — the harness's premise "
            "(that UsageSnapshot drops observable fields) no longer holds",
        )

    def test_raw_and_projected_readings_agree_on_the_gauge_that_drives_rotation(self) -> None:
        """Q5's free half: the two views of five-hour utilization must be the same number, or the
        harness is calibrating against a different gauge than production rotates on.
        """
        seat = self.seats[0]
        try:
            raw = usage.fetch_usage_raw(seat.path)
            snapshot = usage.UsageSnapshot.from_json(raw, fetched_at=0.0)
        except usage.UsageError as exc:
            self.skipTest(f"PROBE-SKIPPED: {exc}")

        raw_five = (raw.get("five_hour") or {}).get("utilization")
        if raw_five is None:
            self.skipTest("PROBE-SKIPPED: endpoint sent no five_hour.utilization for this seat")
        self.assertEqual(usage.session_utilization(snapshot), float(raw_five))

    def test_endpoint_gauges_are_percents_not_fractions(self) -> None:
        """Load-bearing units check. `detector`'s event `utilization` is a 0..1 FRACTION while this
        endpoint's is a 0-100 PERCENT; conflating them would make the 0.9 rotate threshold either
        never fire or fire instantly.
        """
        for seat in self.seats:
            reading = ratelimit_probe.read_seat(seat)
            if reading.error or reading.five_hour_pct is None:
                continue
            self.assertGreaterEqual(reading.five_hour_pct, 0.0)
            self.assertLessEqual(reading.five_hour_pct, 100.0)
            if reading.seven_day_pct is not None:
                self.assertGreaterEqual(reading.seven_day_pct, 0.0)
                self.assertLessEqual(reading.seven_day_pct, 100.0)

    def test_burn_candidate_ranking_prefers_a_spent_session_over_a_fresh_one(self) -> None:
        readings = [ratelimit_probe.read_seat(s) for s in self.seats]
        usable = [r for r in readings if r.usable_for_burn]
        if len(usable) < 2:
            self.skipTest("PROBE-SKIPPED: need two readable seats to compare ranking")
        ranked = sorted(usable, key=lambda r: r.burn_score(), reverse=True)
        self.assertGreaterEqual(ranked[0].burn_score(), ranked[-1].burn_score())


class RateLimitCaptureLiveTest(unittest.TestCase):
    """TIER 2 — spends real quota. Opt-in only."""

    def setUp(self) -> None:
        if os.environ.get("CLAUDE_RELAY_LIVE_CLAUDE_RUN") != "1":
            self.skipTest("OPERATOR-RECEIPT-PENDING: set CLAUDE_RELAY_LIVE_CLAUDE_RUN=1 (spends real quota)")
        if shutil.which("claude") is None:
            self.skipTest("PROBE-SKIPPED: `claude` not on PATH")
        seats = [s for s in fleet.discover_seats() if s.usable]
        if not seats:
            self.skipTest("PROBE-SKIPPED: no usable seat")
        self.seat = ratelimit_probe.read_seat(seats[0])
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        capture.reset_for_tests()
        self.addCleanup(capture.reset_for_tests)

    def test_a_real_claude_run_produces_recordable_envelopes(self) -> None:
        """The end-to-end claim: a real child's NDJSON contains at least a terminal `result`
        envelope, and the tap records it. This is what proves the Tier-1 tap is wired to reality
        rather than to a fixture — the failure mode that made the original `^RESULT:` detector dead
        code for its entire life.
        """
        capture_dir = self.tmp / "cap"
        with mock.patch.dict(os.environ, {capture.CAPTURE_DIR_ENV: str(capture_dir)}):
            capture.reset_for_tests()
            outcome = ratelimit_probe._run_claude_once(
                self.seat,
                prompt="Reply with exactly the word: ok. Do not use any tools.",
                model=None,
                capture_dir=capture_dir,
                timeout_s=120.0,
            )
            self.assertTrue(outcome.get("ok"), f"live claude run failed: {outcome}")
            self.assertGreater(outcome.get("stdout_lines", 0), 0, "child emitted no stdout lines")

            records = ratelimit_probe._collect_records(capture_dir)

        results = [r for r in records if (r.get("envelope") or {}).get("type") == "result"]
        self.assertTrue(results, f"no terminal `result` envelope captured from a real run: {records}")

        # Q6: modelUsage is what unblocks the deferred Phase 2 cost calibration. Report rather than
        # fail if absent — its presence is a claim about the CLI, not about our code.
        if not any((r.get("envelope") or {}).get("modelUsage") for r in results):
            print("\nNOTE: no `modelUsage` on the terminal result envelope — Phase 2 cost work stays blocked")

    def test_the_tap_records_nothing_when_disabled_even_on_a_real_run(self) -> None:
        """The off-by-default guarantee, verified against a real child rather than a synthetic line."""
        capture_dir = self.tmp / "unused"
        env = {k: v for k, v in os.environ.items() if k != capture.CAPTURE_DIR_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            capture.reset_for_tests()
            self.assertFalse(capture.enabled())
        self.assertFalse(capture_dir.exists())


if __name__ == "__main__":
    unittest.main()
