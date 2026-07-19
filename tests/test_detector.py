"""Offline tests for relay.detector.classify(): the single wall-hit-decision locus. Covers
every outcome bucket plus the tail-backstop override for the AGENT_DEAD_NONLIMIT ambiguity.
"""

from __future__ import annotations

import unittest

from relay import detector
from relay import usage as usage_mod


def _usage(percent: float = 10.0) -> usage_mod.UsageSnapshot:
    return usage_mod.UsageSnapshot.from_json(
        {"limits": [{"kind": "session", "percent": percent, "severity": "normal", "is_active": True}]},
        fetched_at=0.0,
    )


class ClassifyTests(unittest.TestCase):
    def test_progressed_continues(self) -> None:
        action = detector.classify("PROGRESSED", usage=_usage(), tail=[])
        self.assertEqual(action.kind, detector.CONTINUE)

    def test_hit_wall_continues_and_rotates(self) -> None:
        action = detector.classify("HIT_WALL", usage=_usage(99.0), tail=[])
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)

    def test_awaiting_human_notifies_and_parks(self) -> None:
        action = detector.classify("AWAITING_HUMAN", usage=_usage(), tail=[])
        self.assertEqual(action.kind, detector.NOTIFY_PARK)

    def test_blocked_notifies_and_parks(self) -> None:
        action = detector.classify("BLOCKED", usage=_usage(), tail=[])
        self.assertEqual(action.kind, detector.NOTIFY_PARK)

    def test_no_backlog_is_done(self) -> None:
        action = detector.classify("NO_BACKLOG", usage=_usage(), tail=[])
        self.assertEqual(action.kind, detector.DONE)

    def test_agent_dead_nonlimit_with_readable_usage_retries(self) -> None:
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=["some ordinary log line"])
        self.assertEqual(action.kind, detector.RETRY)

    def test_agent_dead_nonlimit_without_usage_and_no_tail_signature_retries(self) -> None:
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=None, tail=["nothing special happened"])
        self.assertEqual(action.kind, detector.RETRY)

    def test_agent_dead_nonlimit_without_usage_but_tail_signature_rotates(self) -> None:
        action = detector.classify(
            "AGENT_DEAD_NONLIMIT", usage=None, tail=["error: you have hit your usage limit for this session"]
        )
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        self.assertIn("backstop", action.reason)

    def test_tail_backstop_is_case_insensitive(self) -> None:
        self.assertTrue(detector.tail_has_limit_signature(["RATE LIMIT hit, retry later"]))
        self.assertFalse(detector.tail_has_limit_signature(["all good, nothing to see"]))

    def test_tail_backstop_recognizes_gad_run_limit_markers(self) -> None:
        # gad-run.js's own limit-probe vocabulary (verified against the bundled source) — these are
        # hyphenated/compound and would NOT match the generic "usage limit" / "rate limit" set.
        self.assertTrue(detector.tail_has_limit_signature(["gad-run: G0 ... (LIMIT-SUSPECTED)"]))
        self.assertTrue(
            detector.tail_has_limit_signature(["treating as a usage-limit/platform outage"])
        )
        self.assertTrue(
            detector.tail_has_limit_signature(["stopping rather than spending into a closed window"])
        )

    def test_unrecognized_outcome_defaults_to_retry(self) -> None:
        action = detector.classify("SOMETHING_NEW", usage=_usage(), tail=[])
        self.assertEqual(action.kind, detector.RETRY)


if __name__ == "__main__":
    unittest.main()
