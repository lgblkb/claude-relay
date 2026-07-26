"""LIVE test for the `LS-1-usage-endpoint` seam. Runs a REAL, read-only GET against
`https://api.anthropic.com/api/oauth/usage` using whichever usable seat this box already has
logged in — no mock, no fixture.

(This docstring previously said the seam was "declared in `.gad/live-seams.json`". That file has
never existed in this repo: claude-relay is the GAD *supervisor*, not a GAD-managed project. The
seam declaration is this docstring. Corrected 2026-07-27.)

This is the single most important ground-truth claim in the whole design (DESIGN.md §0's
"live-confirmed" endpoint shape), so it is `auto-verifiable`: it runs automatically whenever
at least one usable seat exists, and gracefully PROBE-SKIPS (not fails) when none does — e.g.
a fresh CI box with no `~/.claude-*` seats logged in yet.

Deliberately NOT part of `tests/` (that suite is 100% offline and network-free by
construction — see tests/README-equivalent note in the top-level README's Tests section).
Run explicitly:  python3 -m unittest tests_live.test_usage_endpoint_live -v
"""

from __future__ import annotations

import unittest

from relay import fleet, usage


class UsageEndpointLiveTest(unittest.TestCase):
    def test_fetch_usage_against_a_real_seat(self) -> None:
        seats = fleet.discover_seats()
        usable = [s for s in seats if s.usable]
        if not usable:
            self.skipTest("PROBE-SKIPPED: no usable seat (~/.claude-* with a valid access token) on this box")

        seat = usable[0]
        try:
            snapshot = usage.fetch_usage(seat.path)
        except usage.NeedsLoginError as exc:
            self.skipTest(f"PROBE-SKIPPED: seat became unusable between discovery and fetch: {exc}")
        except usage.RateLimited as exc:
            # `RateLimited` is NOT a `UsageFetchError`, so before 2026-07-27 a 429 escaped BOTH
            # handlers here and surfaced as a test ERROR — contradicting this module's own
            # "gracefully PROBE-SKIPS (not fails)" claim above. Not hypothetical: the endpoint
            # returns `Retry-After: ~300` after only a few dozen reads in a few minutes, so anyone
            # running this suite twice in quick succession saw a red seam for an environment
            # condition. A 429 says nothing about the shape assertions below.
            self.skipTest(f"PROBE-SKIPPED: usage endpoint rate-limited us: {exc}")
        except usage.UsageFetchError as exc:
            self.fail(f"live usage endpoint call failed: {exc}")

        # Structural assertions only — never assert a specific percent/severity (that's real
        # account state and changes over time); assert the SHAPE our code depends on.
        self.assertIsInstance(snapshot.five_hour, dict)
        self.assertIsInstance(snapshot.seven_day, dict)
        self.assertIsInstance(snapshot.limits, list)
        for limit in snapshot.limits:
            self.assertIn(limit.kind, ("session", "weekly_all", "weekly_scoped"))
            self.assertGreaterEqual(limit.percent, 0.0)
            self.assertLessEqual(limit.percent, 100.0)
            self.assertIsInstance(limit.severity, str)
            self.assertIsInstance(limit.is_active, bool)

        session = usage.active_session_limit(snapshot)
        if session is not None:
            # Exercise the two decision functions end-to-end against the real reading.
            usage.rotate_off(snapshot, high_pct=90.0)
            usage.near_cap(snapshot)


if __name__ == "__main__":
    unittest.main()
