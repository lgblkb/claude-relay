"""Offline tests for relay.usage: limits[] parsing, rotation thresholds, and the read cache.
No network calls — usage.fetch_usage is monkeypatched wherever a live GET would happen.
"""

from __future__ import annotations

import datetime as dt
import time
import unittest
from unittest import mock

from relay import usage


def _usage_json(
    session_percent: float = 49.0,
    session_severity: str = "normal",
    session_active: bool = True,
    weekly_all_percent: float = 20.0,
    weekly_all_severity: str = "normal",
) -> dict:
    return {
        "five_hour": {"utilization": session_percent, "resets_at": "2026-07-19T05:00:00Z"},
        "seven_day": {"utilization": weekly_all_percent, "resets_at": "2026-07-25T00:00:00Z"},
        "limits": [
            {
                "kind": "session",
                "group": "default",
                "percent": session_percent,
                "severity": session_severity,
                "resets_at": "2026-07-19T05:00:00Z",
                "scope": {"model": {"display_name": "Sonnet"}},
                "is_active": session_active,
            },
            {
                "kind": "weekly_all",
                "group": "all",
                "percent": weekly_all_percent,
                "severity": weekly_all_severity,
                "resets_at": "2026-07-25T00:00:00Z",
                "scope": {},
                "is_active": False,
            },
        ],
    }


class LimitParsingTests(unittest.TestCase):
    def test_from_json_extracts_all_fields(self) -> None:
        snapshot = usage.UsageSnapshot.from_json(_usage_json(), fetched_at=time.time())
        self.assertEqual(len(snapshot.limits), 2)
        session = usage.active_session_limit(snapshot)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.kind, "session")
        self.assertEqual(session.percent, 49.0)
        self.assertEqual(session.severity, "normal")
        self.assertTrue(session.is_active)
        self.assertEqual(session.model_display_name, "Sonnet")

    def test_active_limit_ignores_inactive_entries(self) -> None:
        snapshot = usage.UsageSnapshot.from_json(_usage_json(session_active=False), fetched_at=time.time())
        self.assertIsNone(usage.active_session_limit(snapshot))

    def test_malformed_limits_list_is_tolerated(self) -> None:
        snapshot = usage.UsageSnapshot.from_json({"limits": "not-a-list"}, fetched_at=time.time())
        self.assertEqual(snapshot.limits, [])


class RotateOffTests(unittest.TestCase):
    def test_below_high_pct_and_normal_severity_does_not_rotate(self) -> None:
        snapshot = usage.UsageSnapshot.from_json(_usage_json(session_percent=49.0), fetched_at=time.time())
        self.assertFalse(usage.rotate_off(snapshot, high_pct=90.0))

    def test_at_or_above_high_pct_rotates(self) -> None:
        snapshot = usage.UsageSnapshot.from_json(_usage_json(session_percent=91.0), fetched_at=time.time())
        self.assertTrue(usage.rotate_off(snapshot, high_pct=90.0))

    def test_nonnormal_severity_rotates_even_at_low_percent(self) -> None:
        snapshot = usage.UsageSnapshot.from_json(
            _usage_json(session_percent=10.0, session_severity="critical"), fetched_at=time.time()
        )
        self.assertTrue(usage.rotate_off(snapshot, high_pct=90.0))

    def test_weekly_all_saturation_rotates_even_if_session_is_fine(self) -> None:
        snapshot = usage.UsageSnapshot.from_json(
            _usage_json(session_percent=10.0, weekly_all_percent=95.0), fetched_at=time.time()
        )
        self.assertTrue(usage.rotate_off(snapshot, high_pct=90.0))

    def test_low_5h_ceiling_does_not_rotate_on_normal_weekly(self) -> None:
        # Regression (smoke test, 2026-07-19): a low SYNTHETIC 5h ceiling (15%) must NOT rotate a
        # seat off just because its WEEKLY usage (33%) exceeds that 5h ceiling — the synthetic
        # ceiling is 5h-session-only; weekly is gated near its own real cap (WEEKLY_CEILING_PCT).
        ok = usage.UsageSnapshot.from_json(
            _usage_json(session_percent=0.0, weekly_all_percent=33.0), fetched_at=time.time()
        )
        self.assertFalse(usage.rotate_off(ok, high_pct=15.0))
        # ...but a weekly genuinely near its real cap still rotates, even at a low 5h ceiling.
        saturated = usage.UsageSnapshot.from_json(
            _usage_json(session_percent=0.0, weekly_all_percent=96.0), fetched_at=time.time()
        )
        self.assertTrue(usage.rotate_off(saturated, high_pct=15.0))

    def test_rotates_on_raw_5h_utilization_when_no_active_session_limit(self) -> None:
        # Regression (live smoke test, 2026-07-19): the endpoint only emits an *active* `limits[]`
        # "session" entry once the 5h window is a BINDING constraint. A seat sitting at 28% had no
        # active session limit at all, so `active_session_limit()` was None — yet it was 28% into
        # its 5h window. Gating on the normalized limit alone silently returned rotate_off=False
        # and never rotated it. The synthetic ceiling MUST read the raw `five_hour.utilization`.
        snap = usage.UsageSnapshot.from_json(
            _usage_json(session_percent=28.0, session_active=False, weekly_all_percent=36.0),
            fetched_at=time.time(),
        )
        self.assertIsNone(usage.active_session_limit(snap))  # models the real observed shape
        self.assertEqual(usage.session_utilization(snap), 28.0)
        self.assertTrue(usage.rotate_off(snap, high_pct=15.0))
        self.assertFalse(usage.rotate_off(snap, high_pct=90.0))  # 28% < a normal 90% ceiling

    def test_near_cap_threshold(self) -> None:
        near = usage.UsageSnapshot.from_json(_usage_json(session_percent=99.5), fetched_at=time.time())
        far = usage.UsageSnapshot.from_json(_usage_json(session_percent=90.0), fetched_at=time.time())
        self.assertTrue(usage.near_cap(near))
        self.assertFalse(usage.near_cap(far))

    def test_near_cap_is_not_triggered_by_severity_alone(self) -> None:
        # Regression test for a real bug caught by a live probe (2026-07-19): the real endpoint
        # returned severity="warning" at only 82% utilization. near_cap() must stay percent-only
        # so a merely-elevated (but not actually exhausted) seat is never misreported as HIT_WALL.
        snapshot = usage.UsageSnapshot.from_json(
            _usage_json(session_percent=82.0, session_severity="warning"), fetched_at=time.time()
        )
        self.assertFalse(usage.near_cap(snapshot))
        # rotate_off, by contrast, SHOULD react to that same non-normal severity early.
        self.assertTrue(usage.rotate_off(snapshot, high_pct=90.0))

    def test_earliest_reset_picks_the_soonest_datetime(self) -> None:
        snapshot = usage.UsageSnapshot.from_json(_usage_json(), fetched_at=time.time())
        earliest = usage.earliest_reset(snapshot)
        self.assertIsInstance(earliest, dt.datetime)
        assert earliest is not None
        self.assertEqual(earliest.year, 2026)
        self.assertEqual(earliest.month, 7)
        self.assertEqual(earliest.day, 19)

    def test_earliest_reset_uses_raw_5h_when_no_active_session_limit(self) -> None:
        # Regression (live smoke test, 2026-07-19): with NO active `limits[]` session entry, this
        # must still return the 5h `five_hour.resets_at` (today), not fall through to the weekly
        # reset (days away) — otherwise a seat rotated off at the synthetic 5h ceiling is benched
        # for days instead of until its 5h window reopens.
        snap = usage.UsageSnapshot.from_json(
            {
                "five_hour": {"utilization": 28.0, "resets_at": "2026-07-19T05:29:00Z"},
                "limits": [
                    {"kind": "weekly_all", "percent": 36.0, "severity": "normal",
                     "resets_at": "2026-07-25T00:00:00Z", "is_active": False},
                ],
            },
            fetched_at=time.time(),
        )
        self.assertIsNone(usage.active_session_limit(snap))
        earliest = usage.earliest_reset(snap)
        assert earliest is not None
        self.assertEqual((earliest.month, earliest.day, earliest.hour), (7, 19, 5))


class UsageCacheTests(unittest.TestCase):
    def test_poll_uses_cache_within_ttl(self) -> None:
        cache = usage.UsageCache()
        snapshot = usage.UsageSnapshot.from_json(_usage_json(), fetched_at=1000.0)
        with mock.patch.object(usage, "fetch_usage", return_value=snapshot) as fetch:
            first = cache.poll("dummy-seat-dir", ttl=90, now=1000.0)
            second = cache.poll("dummy-seat-dir", ttl=90, now=1050.0)  # within TTL
        self.assertIs(first, snapshot)
        self.assertIs(second, snapshot)
        fetch.assert_called_once()

    def test_poll_refetches_after_ttl_expires(self) -> None:
        cache = usage.UsageCache()
        snap1 = usage.UsageSnapshot.from_json(_usage_json(session_percent=10.0), fetched_at=1000.0)
        snap2 = usage.UsageSnapshot.from_json(_usage_json(session_percent=80.0), fetched_at=1200.0)
        with mock.patch.object(usage, "fetch_usage", side_effect=[snap1, snap2]):
            cache.poll("dummy-seat-dir", ttl=90, now=1000.0)
            second = cache.poll("dummy-seat-dir", ttl=90, now=1200.0)  # 200s later, past TTL
        self.assertIs(second, snap2)

    def test_rate_limited_falls_back_to_cache(self) -> None:
        cache = usage.UsageCache()
        snapshot = usage.UsageSnapshot.from_json(_usage_json(), fetched_at=1000.0)
        with mock.patch.object(usage, "fetch_usage", side_effect=[snapshot, usage.RateLimited(30.0)]):
            first = cache.poll("dummy-seat-dir", ttl=1, now=1000.0)
            second = cache.poll("dummy-seat-dir", ttl=1, now=1002.0)  # past TTL, but 429
        self.assertIs(first, snapshot)
        self.assertIs(second, snapshot)

    def test_rate_limited_with_no_cache_propagates(self) -> None:
        cache = usage.UsageCache()
        with mock.patch.object(usage, "fetch_usage", side_effect=usage.RateLimited(30.0)):
            with self.assertRaises(usage.RateLimited):
                cache.poll("dummy-seat-dir", ttl=90, now=1000.0)


if __name__ == "__main__":
    unittest.main()
