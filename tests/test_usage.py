"""Offline tests for relay.usage: limits[] parsing, rotation thresholds, and the read cache.
No network calls — usage.fetch_usage is monkeypatched wherever a live GET would happen.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path
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

    def test_b11_extra_ttl_s_is_bounded_across_many_consecutive_429s(self) -> None:
        """B11: `extra_ttl_s` used to accumulate `retry_after_s` (or `ttl`) on EVERY consecutive
        429 with no ceiling — a long enough streak measured 11.1h of staleness in one
        reproduction, since `cached_at` is never refreshed when extending. Bounding it caps how
        stale a served reading can ever become regardless of how many 429s occur in a row.
        """
        cache = usage.UsageCache()
        snapshot = usage.UsageSnapshot.from_json(_usage_json(), fetched_at=1000.0)
        # A long streak of 429s, each honoring a generous Retry-After. `now` advances well past
        # whatever the CURRENT effective TTL is each time, so every call actually re-attempts a
        # fetch (and hits the next RateLimited) rather than being served straight from cache —
        # `cached_at` itself never moves, which is the non-refreshing accumulation bug.
        with mock.patch.object(
            usage,
            "fetch_usage",
            side_effect=[snapshot] + [usage.RateLimited(600.0)] * 20,
        ):
            cache.poll("dummy-seat-dir", ttl=90, now=1000.0)
            for i in range(1, 21):
                cache.poll("dummy-seat-dir", ttl=90, now=1000.0 + i * 5000.0)
        entry = cache._entries["dummy-seat-dir"]
        self.assertLessEqual(entry.extra_ttl_s, usage._MAX_EXTRA_TTL_S)
        # 20 * 600s = 12000s unbounded vs. the ~1800s cap — the bound must actually be binding
        # for this fixture, not just coincidentally under some huge accumulated value.
        self.assertEqual(entry.extra_ttl_s, usage._MAX_EXTRA_TTL_S)


class B7FetchUsageReadPhaseNetworkExceptionTests(unittest.TestCase):
    """B7 audit fix: `fetch_usage()`'s documented contract is "raises NeedsLoginError,
    RateLimited, or UsageFetchError — never anything else." A read-phase failure (after the
    connection succeeded — a mid-stream RST, a read timeout, a malformed HTTP status line) used
    to arrive as a raw `OSError` subclass or `http.client.HTTPException`, escaping that contract
    entirely and, via `loop.run()`'s exception-less `try/finally`, killing a multi-day run.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.seat_dir = Path(self._tmp.name) / "seat"
        self.seat_dir.mkdir()
        (self.seat_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "fake-token"}}), encoding="utf-8"
        )

    def _assert_wrapped_as_usage_fetch_error(self, exc: BaseException) -> None:
        with mock.patch.object(usage.urllib.request, "urlopen", side_effect=exc):
            with self.assertRaises(usage.UsageFetchError) as ctx:
                usage.fetch_usage(self.seat_dir)
        # Must not be some OTHER exception type re-raised bare — assert the contract's promise
        # explicitly, not just "some exception happened."
        self.assertNotIsInstance(ctx.exception, usage.RateLimited)

    def test_connection_reset_is_wrapped_as_usage_fetch_error(self) -> None:
        self._assert_wrapped_as_usage_fetch_error(ConnectionResetError("rst"))

    def test_bare_timeout_is_wrapped_as_usage_fetch_error(self) -> None:
        self._assert_wrapped_as_usage_fetch_error(TimeoutError("timed out"))

    def test_bad_status_line_is_wrapped_as_usage_fetch_error(self) -> None:
        self._assert_wrapped_as_usage_fetch_error(http.client.BadStatusLine("garbage"))


if __name__ == "__main__":
    unittest.main()


class BoolUtilizationRejectionTests(unittest.TestCase):
    """A boolean in `five_hour.utilization` must never be read as a percent.

    `isinstance(True, int)` is True in Python, so a bare `float(raw)` turns True into 1.0. On this
    0-100 percent gauge that reads as a nearly-EMPTY window, so claude-relay would keep dispatching
    work to a seat it should have rotated off — the missed-rotation direction Invariant #2 exists to
    prevent. Found 2026-07-27 while building the rate-limit calibration harness.
    """

    def _snapshot(self, utilization: object) -> usage.UsageSnapshot:
        return usage.UsageSnapshot.from_json({"five_hour": {"utilization": utilization}}, fetched_at=0.0)

    def test_true_is_rejected_rather_than_read_as_one_percent(self) -> None:
        self.assertIsNone(usage.session_utilization(self._snapshot(True)))

    def test_false_is_rejected_rather_than_read_as_zero_percent(self) -> None:
        self.assertIsNone(usage.session_utilization(self._snapshot(False)))

    def test_a_rejected_bool_does_not_become_a_zero_percent_reading_downstream(self) -> None:
        """`session_percent()` falls back to 0.0 when the gauge is unreadable. That is correct for
        ranking, but assert it explicitly: the dangerous outcome would be 1.0 (a real-looking
        percent) rather than the honest 0.0-plus-absent-session fallback.
        """
        self.assertEqual(usage.session_percent(self._snapshot(True)), 0.0)

    def test_a_genuine_numeric_reading_still_works(self) -> None:
        self.assertEqual(usage.session_utilization(self._snapshot(57)), 57.0)
        self.assertEqual(usage.session_utilization(self._snapshot(57.5)), 57.5)


class WalledSeatEndpointTests(unittest.TestCase):
    """The endpoint payload of a seat that had ACTUALLY walled, captured 2026-07-27.

    `ayan` hit its five-hour session limit for real; this is the first observation of a walled
    seat's payload. It matters more than the NDJSON event, because Invariant #2 makes the endpoint a
    PRIMARY decider while the event is supplementary.

    `severity: "critical"` was a new value here (previously only `normal` and `warning` at 82%).
    `rotate_off()` gates on `severity != "normal"`, so it routes correctly with no enum change —
    this pins that, so a future refactor to an explicit severity allowlist cannot silently drop it.
    """

    WALLED = {
        "five_hour": {"utilization": 100.0, "resets_at": "2026-07-26T22:30:00.743308+00:00"},
        "seven_day": {"utilization": 57.0, "resets_at": "2026-07-28T16:00:00.743341+00:00"},
        "limits": [
            {
                "kind": "session",
                "group": "session",
                "percent": 100,
                "severity": "critical",
                "resets_at": "2026-07-26T22:30:00.743308+00:00",
                "is_active": True,
            },
            {
                "kind": "weekly_all",
                "group": "weekly",
                "percent": 57,
                "severity": "normal",
                "resets_at": "2026-07-28T16:00:00.743341+00:00",
                "is_active": False,
            },
        ],
    }

    def _snapshot(self) -> usage.UsageSnapshot:
        return usage.UsageSnapshot.from_json(self.WALLED, fetched_at=0.0)

    def test_a_walled_seat_is_rotated_off(self) -> None:
        self.assertTrue(usage.rotate_off(self._snapshot(), high_pct=90.0))

    def test_a_walled_seat_reads_as_near_cap(self) -> None:
        self.assertTrue(usage.near_cap(self._snapshot()))

    def test_session_utilization_reports_the_full_hundred(self) -> None:
        self.assertEqual(usage.session_utilization(self._snapshot()), 100.0)
        self.assertEqual(usage.session_percent(self._snapshot()), 100.0)

    def test_critical_severity_is_parsed_and_preserved(self) -> None:
        session = usage.active_session_limit(self._snapshot())
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.severity, "critical")

    def test_critical_severity_alone_rotates_even_below_the_percent_gate(self) -> None:
        """The severity tier must be able to rotate a seat on its own. If a future change made
        rotation percent-only, a `critical` seat below `high_pct` would keep receiving work.
        """
        payload = json.loads(json.dumps(self.WALLED))
        payload["five_hour"]["utilization"] = 50.0
        payload["limits"][0]["percent"] = 50
        snapshot = usage.UsageSnapshot.from_json(payload, fetched_at=0.0)
        self.assertTrue(usage.rotate_off(snapshot, high_pct=90.0))

    def test_the_session_reset_time_is_recoverable_for_the_cooldown(self) -> None:
        """The supervisor cools a walled seat until this instant, so it must parse."""
        resets = usage.session_resets_at(self._snapshot())
        self.assertIsNotNone(resets)
        assert resets is not None
        self.assertEqual(resets.hour, 22)
