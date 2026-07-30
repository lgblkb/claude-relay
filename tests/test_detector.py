"""Offline tests for relay.detector.classify(): the single wall-hit-decision locus. Covers
every outcome bucket plus the tail-backstop override for the AGENT_DEAD_NONLIMIT ambiguity.

Blocker 1 (2026-07-26): `gadkit.command()` invokes the child with `--output-format stream-json`,
which is NDJSON — one JSON object per physical line, never plain text. Every fixture here builds
REALISTIC envelope lines (via `_assistant_line()`/`_rate_limit_line()`/etc., matching shapes
observed live against a real `claude` 2.1.220 session) rather than bare prose strings — a bare
`"RESULT: ..."` string is not what production ever produces on `RunResult.tail`, so a test built
from one would pass against dead code without ever exercising the real decode path.
"""

from __future__ import annotations

import json
import unittest

from relay import detector
from relay import usage as usage_mod


def _usage(percent: float = 10.0) -> usage_mod.UsageSnapshot:
    return usage_mod.UsageSnapshot.from_json(
        {"limits": [{"kind": "session", "percent": percent, "severity": "normal", "is_active": True}]},
        fetched_at=0.0,
    )


def _assistant_line(text: str) -> str:
    """One realistic `assistant` NDJSON envelope carrying a single text content block — the
    shape observed live: `{"type":"assistant","message":{"content":[{"type":"text","text":...}]}}`.
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


def _system_line(subtype: str = "hook_started") -> str:
    return json.dumps({"type": "system", "subtype": subtype})


def _rate_limit_line(
    *,
    status: str = "allowed_warning",
    resets_at: int = 1785290400,
    rate_limit_type: str = "seven_day",
    utilization: float = 0.76,
    is_using_overage: bool = False,
    surpassed_threshold: float | None = 0.75,
) -> str:
    """The exact shape observed live (2026-07-26 probe): `{"type":"rate_limit_event",
    "rate_limit_info":{"status":"allowed_warning","resetsAt":1785290400,
    "rateLimitType":"seven_day","utilization":0.76,"isUsingOverage":false,
    "surpassedThreshold":0.75},"uuid":"...","session_id":"..."}`.
    """
    info = {
        "status": status,
        "resetsAt": resets_at,
        "rateLimitType": rate_limit_type,
        "utilization": utilization,
        "isUsingOverage": is_using_overage,
        "surpassedThreshold": surpassed_threshold,
    }
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info, "uuid": "u1", "session_id": "s1"})


def _result_envelope_line(is_error: bool = False, result: str | None = None) -> str:
    """The terminal `result` envelope (subtype success). When `result` is omitted (the default)
    this carries no text field at all — used by most fixtures purely for verisimilitude, where
    the RESULT: line is decoded from an `assistant` envelope instead. When `result` IS given, it
    is the envelope's own flat `result` string field — the second, more-authoritative place a
    real capture can carry the model's `RESULT: <status>` line (2026-07-26 follow-up fix; see
    `detector._terminal_result_text()`).
    """
    envelope: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "num_turns": 3,
        "duration_ms": 1234,
    }
    if result is not None:
        envelope["result"] = result
    return json.dumps(envelope)


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
        tail = [_system_line(), _assistant_line("doing some ordinary work"), _result_envelope_line()]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)

    def test_agent_dead_nonlimit_without_usage_and_no_tail_signature_retries(self) -> None:
        tail = [_assistant_line("nothing special happened")]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=None, tail=tail)
        self.assertEqual(action.kind, detector.RETRY)

    def test_agent_dead_nonlimit_without_usage_but_tail_signature_rotates(self) -> None:
        tail = [_assistant_line("error: you have hit your usage limit for this session")]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=None, tail=tail)
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        self.assertIn("backstop", action.reason)

    def test_tail_backstop_is_case_insensitive(self) -> None:
        self.assertTrue(detector.tail_has_limit_signature([_assistant_line("RATE LIMIT hit, retry later")]))
        self.assertFalse(detector.tail_has_limit_signature([_assistant_line("all good, nothing to see")]))

    def test_tail_backstop_recognizes_gad_run_limit_markers(self) -> None:
        # gad-run.js's own limit-probe vocabulary (verified against the bundled source) — these are
        # hyphenated/compound and would NOT match the generic "usage limit" / "rate limit" set.
        # They still count toward the GENERIC (usage-is-None-gated) backstop union.
        self.assertTrue(
            detector.tail_has_limit_signature([_assistant_line("gad-run: G0 ... (LIMIT-SUSPECTED)")])
        )
        self.assertTrue(
            detector.tail_has_limit_signature([_assistant_line("treating as a usage-limit/platform outage")])
        )
        self.assertTrue(
            detector.tail_has_limit_signature(
                [_assistant_line("stopping rather than spending into a closed window")]
            )
        )

    def test_unrecognized_outcome_defaults_to_retry(self) -> None:
        action = detector.classify("SOMETHING_NEW", usage=_usage(), tail=[])
        self.assertEqual(action.kind, detector.RETRY)


class NdjsonDecodingRobustnessTests(unittest.TestCase):
    """The tail is real `--output-format stream-json` NDJSON — one JSON object per line. These
    pin the decode seam itself against the failure modes a killed/hung/interleaved process can
    actually produce.
    """

    def test_a_bare_text_line_that_is_not_json_is_silently_skipped(self) -> None:
        """The exact false premise Blocker 1 fixes: a bare `"RESULT: ..."` string is NOT valid
        JSON and never appears on a real tail — it must be skipped, not crash, and must NOT be
        treated as a genuine RESULT line.
        """
        tail = ["RESULT: LIMIT-SUSPECTED", "not json at all {{{"]
        self.assertIsNone(detector._extract_gad_result_status(tail))
        self.assertFalse(detector.tail_has_workflow_limit_signature(tail))

    def test_a_truncated_final_line_is_skipped_without_crashing(self) -> None:
        tail = [_assistant_line("hello"), '{"type": "assistant", "message": {"content": [{"type": "te']
        # Must not raise, and the valid envelope's content is still recovered.
        self.assertEqual(detector._all_assistant_text(tail), "hello")

    def test_a_json_line_that_is_not_an_object_is_skipped(self) -> None:
        tail = ["[1, 2, 3]", '"just a string"', "42", _assistant_line("real content")]
        self.assertEqual(detector._all_assistant_text(tail), "real content")

    def test_multiple_assistant_envelopes_join_with_real_newlines(self) -> None:
        tail = [_assistant_line("line one"), _system_line(), _assistant_line("line two\nRESULT: NO-PROGRESS")]
        self.assertEqual(detector._all_assistant_text(tail), "line one\nline two\nRESULT: NO-PROGRESS")
        self.assertEqual(detector._extract_gad_result_status(tail), "NO-PROGRESS")

    def test_non_assistant_envelopes_contribute_no_text(self) -> None:
        tail = [_system_line(), _result_envelope_line(), _rate_limit_line()]
        self.assertEqual(detector._all_assistant_text(tail), "")


class RateLimitEventTests(unittest.TestCase):
    """Blocker 1 item 2: `rate_limit_event` is genuine structured platform data — authoritative
    even when the post-run usage poll succeeded (unlike the prose backstops)."""

    def test_known_safe_status_below_the_utilization_threshold_does_not_rotate(self) -> None:
        tail = [_rate_limit_line(status="allowed_warning", utilization=0.5)]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)

    def test_known_safe_status_at_high_utilization_rotates_without_a_resets_at(self) -> None:
        """EXPECTATION INVERTED 2026-07-27 by the rotation/cooldown split.

        This test previously asserted `action.resets_at == "2026-07-29T02:00:00+00:00"` — i.e. that a
        high-utilization WARNING sets the cooldown boundary to the window's reset. It was pinning the
        bug: `loop.py`'s AGENT_DEAD_NONLIMIT branch feeds that value to `_force_cooldown()`, so on a
        `seven_day` event this declared a still-working seat dead for DAYS.

        Q1 (see relay/ratelimit_probe.py) settled it: 42 events captured from 0.90 to 0.99
        utilization, every call succeeding. The channel warns; it never denies. So the event drives
        rotation and the usage endpoint decides exhaustion.

        Kept rather than deleted, with the old assertion quoted above, so the inversion is legible to
        anyone who wonders why this contract changed.
        """
        tail = [_rate_limit_line(status="allowed_warning", utilization=0.95, resets_at=1785290400)]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        self.assertIsNone(action.resets_at)

    def test_rotates_even_when_the_live_usage_reading_was_healthy(self) -> None:
        """The one deliberate Invariant #2 narrowing this item adds: authoritative regardless of
        `usage`, because it is structured platform data, not prose."""
        tail = [_rate_limit_line(utilization=0.95)]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(5.0), tail=tail)
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)

    def test_an_unrecognized_status_is_treated_conservatively_as_a_limit(self) -> None:
        """Only 'allowed_warning' has ever been observed live. Anything else must be treated as
        POTENTIALLY limiting (rotate + cool down), never silently assumed safe."""
        tail = [_rate_limit_line(status="denied", utilization=0.1)]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        self.assertIn("unrecognized", action.reason.lower())

    def test_the_latest_of_several_rate_limit_events_wins(self) -> None:
        tail = [
            _rate_limit_line(status="allowed_warning", utilization=0.95, resets_at=111),
            _rate_limit_line(status="allowed_warning", utilization=0.1, resets_at=222),
        ]
        # The LATEST event (low utilization) must win -> no rotate from this signal alone.
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)

    def test_no_rate_limit_event_at_all_is_not_mistaken_for_one(self) -> None:
        self.assertIsNone(detector.latest_rate_limit_signal([_assistant_line("all normal")]))

    def test_a_malformed_rate_limit_info_does_not_crash(self) -> None:
        tail = [json.dumps({"type": "rate_limit_event", "rate_limit_info": "not-a-dict"})]
        self.assertIsNone(detector.latest_rate_limit_signal(tail))
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)

    def test_resets_at_epoch_converts_to_iso8601_utc(self) -> None:
        signal = detector.latest_rate_limit_signal([_rate_limit_line(resets_at=1785290400)])
        assert signal is not None
        self.assertEqual(signal.resets_at, 1785290400)

    def test_utilization_is_a_fraction_not_a_percent(self) -> None:
        signal = detector.latest_rate_limit_signal([_rate_limit_line(utilization=0.76)])
        assert signal is not None
        self.assertEqual(signal.utilization, 0.76)


class GadResultStatusTests(unittest.TestCase):
    """Blocker 1 item 3 / E9: gad-run's own `RESULT: <status>` line, decoded from the real
    NDJSON assistant envelope (not a bare tail string)."""

    def test_extracts_the_status_from_a_realistic_assistant_envelope(self) -> None:
        tail = [_assistant_line("All done.\nRESULT: COMPLETED-BATCH")]
        self.assertEqual(detector._extract_gad_result_status(tail), "COMPLETED-BATCH")

    def test_no_result_line_anywhere_returns_none(self) -> None:
        tail = [_assistant_line("still working, no final line yet")]
        self.assertIsNone(detector._extract_gad_result_status(tail))

    def test_dirty_tree_is_non_retryable_and_trips_the_breaker_immediately(self) -> None:
        tail = [_assistant_line("Preflight failed.\nRESULT: DIRTY-TREE")]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)
        self.assertTrue(action.no_retry)

    def test_refused_is_non_retryable_and_trips_the_breaker_immediately(self) -> None:
        """gad-kit's uncommitted 2.1.0 work: gad-finish.js mechanically refuses (status:
        'REFUSED') to resume a generation whose reviews/adversarial-review.md is missing —
        retrying the identical FINISH would refuse identically every time (nothing on disk
        changes), so this must be non-retryable exactly like DIRTY-TREE, never a plain RETRY.
        """
        tail = [_assistant_line("gad-finish REFUSES to resume G7.\nRESULT: REFUSED")]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)
        self.assertTrue(action.no_retry)

    def test_every_other_recognized_status_is_a_plain_ordinary_retry(self) -> None:
        for status in (
            "SURVEY-FAILED",
            "NO-PROGRESS",
            "BACKLOG-EXHAUSTED",
            "MAX-GENS-REACHED",
            # BUDGET-EXHAUSTED deliberately MOVED OUT of this list (2026-07-30): it is gad-run's
            # "cannot afford to start the next generation" exit, which is a healthy budget stop
            # rather than a failure, so it now classifies as a CONTINUE_ROTATE pause. See
            # detector._PAUSED_RESULT_STATUSES for why leaving it here was a live bug (no cooldown,
            # the same seat re-picked, a HARD_ERROR park three iterations later), and
            # tests/test_soft_ceiling.py for its replacement coverage.
            "COMPLETED-BATCH",
            "IDEATED",
            "IDEATION-FAILED",
        ):
            with self.subTest(status=status):
                tail = [_assistant_line(f"RESULT: {status}")]
                action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
                self.assertEqual(action.kind, detector.RETRY)
                self.assertFalse(action.no_retry)

    def test_an_unrecognized_status_is_never_treated_as_success(self) -> None:
        tail = [_assistant_line("RESULT: SOME-FUTURE-STATUS-NOT-YET-KNOWN")]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)
        self.assertFalse(action.no_retry)
        self.assertIn("UNRECOGNIZED", action.reason)

    def test_the_last_result_line_wins_over_an_earlier_quoted_one(self) -> None:
        tail = [
            _assistant_line("earlier I said RESULT: DIRTY-TREE but that was a quoted example"),
            _assistant_line("RESULT: COMPLETED-BATCH"),
        ]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)
        self.assertFalse(action.no_retry)  # the LAST line (COMPLETED-BATCH) governs, not DIRTY-TREE

    def test_result_line_found_only_in_the_terminal_result_envelope_is_still_detected(self) -> None:
        """2026-07-26 follow-up: a real capture can carry the model's `RESULT:` line in the
        terminal `result` envelope's own flat `result` field, NOT (or not only) inside an
        `assistant` envelope's decoded text. A realistic tail: a system init line, an ordinary
        `assistant` turn that does NOT itself contain the RESULT: line (the model narrating its
        work before the tool-blocked wait resolves), and only the terminal `result` envelope
        carries the final `RESULT: <status>` line. This closes the previous round's self-flagged
        uncertainty ("assumes a future RESULT: line always lands in an assistant envelope").
        """
        tail = [
            _system_line("init"),
            _assistant_line("Calling the Workflow tool and blocking on TaskOutput..."),
            _result_envelope_line(result="RESULT: BACKLOG-EXHAUSTED\n"),
        ]
        # Confirm the premise: no assistant envelope anywhere contains a RESULT: line.
        self.assertNotIn("RESULT:", detector._all_assistant_text(tail))
        self.assertEqual(detector._extract_gad_result_status(tail), "BACKLOG-EXHAUSTED")
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(10.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)
        self.assertFalse(action.no_retry)

    def test_terminal_result_envelope_is_preferred_over_a_stale_assistant_line(self) -> None:
        """When BOTH sources carry a RESULT: line, the terminal `result` envelope wins — it is
        the run's own designated final-answer field, more authoritative than an arbitrary
        (possibly earlier, possibly quoted) assistant turn."""
        tail = [
            _assistant_line("an earlier draft said RESULT: DIRTY-TREE before I re-checked"),
            _result_envelope_line(result="RESULT: COMPLETED-BATCH"),
        ]
        self.assertEqual(detector._extract_gad_result_status(tail), "COMPLETED-BATCH")

    def test_result_envelope_present_but_textless_falls_back_to_assistant_text(self) -> None:
        """A `result` envelope with no usable `result` line inside it (e.g. an error summary, or
        simply absent) must not be treated as "give up" — the assistant text is still searched.
        """
        tail = [
            _assistant_line("All done.\nRESULT: MAX-GENS-REACHED"),
            _result_envelope_line(),  # no `result` field at all
        ]
        self.assertEqual(detector._extract_gad_result_status(tail), "MAX-GENS-REACHED")


class LimitSignatureSplitTests(unittest.TestCase):
    """The two tail vocabularies are NOT interchangeable (2026-07-26 audit):

    * GENERIC markers are ambient prose any harness/API error text can produce, so they stay
      strictly backstop-only — consulted solely when the live usage poll itself failed. A generic
      marker must NEVER override a good reading (Invariant #2).
    * gad-run.js's OWN probe vocabulary is authoritative ONLY as the content of the model's own
      terminal `RESULT:` line (attribution, not proximity — A2/A2b) — it is authoritative even
      when the reading succeeded, because a platform outage leaves the seat far below its
      ceiling — precisely the case the reading is blind to.
    """

    # Realistic lines, not bare tokens: each is text that actually shows up in a run's stdout,
    # wrapped in a realistic `assistant` NDJSON envelope (Blocker 1 — this is what the tail
    # ACTUALLY contains in production, never bare prose).
    GENERIC_TAILS = (
        "error: you have hit your usage limit for this session",
        "RATE LIMIT hit, retry later",
        '{"type":"rate_limit_error","message":"..."}',
        "your 5-hour limit will reset at 14:00",
        "api error: usage_limit reached for this organization",
        "quota exceeded",
    )
    WORKFLOW_TAILS = (
        "gad-run: G0 agent returned null 3x and the pong probe also died (LIMIT-SUSPECTED)",
        "gad-run: G0 hit a probe-confirmed usage-limit/platform outage — stopping the crawl.",
        "gad-run: not burning further retries into a closed window",
    )

    def test_generic_marker_does_not_rotate_when_the_usage_reading_succeeded(self) -> None:
        """The bug this split fixes in the other direction: an ambient "quota exceeded" (or an
        agent quoting one) must not rotate a seat that the live endpoint says is at 12%.
        """
        for tail_line in self.GENERIC_TAILS:
            with self.subTest(tail=tail_line):
                action = detector.classify(
                    "AGENT_DEAD_NONLIMIT", usage=_usage(12.0), tail=[_assistant_line(tail_line)]
                )
                self.assertEqual(action.kind, detector.RETRY)

    def test_generic_marker_still_rotates_when_the_usage_reading_is_unavailable(self) -> None:
        """The pre-existing backstop path, kept green so the split cannot silently regress it."""
        for tail_line in self.GENERIC_TAILS:
            with self.subTest(tail=tail_line):
                tail = [_assistant_line(tail_line)]
                action = detector.classify("AGENT_DEAD_NONLIMIT", usage=None, tail=tail)
                self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
                self.assertIn("backstop", action.reason)

    def test_workflow_marker_rotates_even_with_a_healthy_usage_reading(self) -> None:
        """Authoritative only when `LIMIT-SUSPECTED` is the CONTENT of the model's own RESULT:
        line — each fixture pairs plausible narration with a genuine RESULT: line."""
        tail = [_assistant_line(self.WORKFLOW_TAILS[0]), _assistant_line("RESULT: LIMIT-SUSPECTED")]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(12.0), tail=tail)
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        self.assertIn("LIMIT-SUSPECTED", action.reason)

    def test_workflow_marker_rotates_when_the_usage_reading_is_unavailable_too(self) -> None:
        tail = [_assistant_line(self.WORKFLOW_TAILS[0]), _assistant_line("RESULT: LIMIT-SUSPECTED")]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=None, tail=tail)
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)

    def test_workflow_marker_is_matched_case_insensitively_on_the_result_line(self) -> None:
        tail = [
            _assistant_line("consolidator: wrote .gad/generation-3/plan.md"),
            _assistant_line("gad-run: G3 LIMIT-SUSPECTED after the pong probe also returned null"),
            _assistant_line("RESULT: LIMIT-SUSPECTED"),
        ]
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(30.0), tail=tail)
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        lowered = [_assistant_line(json.loads(t)["message"]["content"][0]["text"].lower()) for t in tail]
        self.assertEqual(
            detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(30.0), tail=lowered).kind,
            detector.CONTINUE_ROTATE,
        )

    def test_workflow_predicate_ignores_the_generic_vocabulary_on_the_result_line(self) -> None:
        for tail_line in self.GENERIC_TAILS:
            with self.subTest(tail=tail_line):
                self.assertFalse(
                    detector.tail_has_workflow_limit_signature([_assistant_line(f"RESULT: {tail_line}")])
                )

    def test_workflow_predicate_only_fires_on_the_exact_limit_suspected_token(self) -> None:
        # "usage-limit" / "closed window" (the other two old WORKFLOW markers) are no longer
        # independently authoritative substrings — only the canonical LIMIT-SUSPECTED status is
        # (they remain part of the generic backstop vocabulary instead, see _LIMIT_SIGNATURES).
        for marker in ("usage-limit", "closed window"):
            with self.subTest(marker=marker):
                tail = [_assistant_line(f"RESULT: {marker}")]
                self.assertFalse(detector.tail_has_workflow_limit_signature(tail))
        suspected_tail = [_assistant_line("RESULT: LIMIT-SUSPECTED")]
        self.assertTrue(detector.tail_has_workflow_limit_signature(suspected_tail))

    def test_workflow_predicate_does_not_fire_when_marker_is_only_prose_with_no_result_line(
        self,
    ) -> None:
        """A2b regression: the exact scenario the fix targets — no RESULT: line at all (the
        model never reached Step 3), so nothing in the tail can be authoritative no matter how
        close to the end it sits."""
        for tail_line in self.WORKFLOW_TAILS:
            with self.subTest(tail=tail_line):
                self.assertFalse(detector.tail_has_workflow_limit_signature([_assistant_line(tail_line)]))

    def test_union_predicate_still_covers_both_vocabularies(self) -> None:
        """`tail_has_limit_signature()` is public and keeps its original "any plausible limit
        signature" meaning — the split must not have narrowed it for existing callers.
        """
        for tail_line in self.GENERIC_TAILS + self.WORKFLOW_TAILS:
            with self.subTest(tail=tail_line):
                self.assertTrue(detector.tail_has_limit_signature([_assistant_line(tail_line)]))

    def test_neither_predicate_fires_on_ordinary_output(self) -> None:
        ordinary = [
            _assistant_line("verifier: GATE GREEN (184 passed, 2 skipped)"),
            _assistant_line("implementer: wrote src/limits.py  # nothing limit-y about this"),
            _assistant_line(""),
        ]
        self.assertFalse(detector.tail_has_limit_signature(ordinary))
        self.assertFalse(detector.tail_has_workflow_limit_signature(ordinary))
        self.assertEqual(
            detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(12.0), tail=ordinary).kind, detector.RETRY
        )

    def test_empty_tail_is_handled_by_both_predicates(self) -> None:
        self.assertFalse(detector.tail_has_limit_signature([]))
        self.assertFalse(detector.tail_has_workflow_limit_signature([]))
        self.assertEqual(detector.classify("AGENT_DEAD_NONLIMIT", usage=None, tail=[]).kind, detector.RETRY)

    def test_workflow_marker_buried_early_in_a_long_tail_does_not_hijack_a_healthy_reading(self) -> None:
        """A2 audit fix regression: these exact literals appear verbatim in gad-kit's OWN
        `tests/run-tests.mjs` scenario titles, and self-hosting gad-kit's own test suite (a Bash
        tool call) is a documented use case. A scenario title printed early in a long, otherwise
        healthy transcript must NOT be mistaken for a genuine probe-confirmed diagnosis just
        because it shares the same substring — the authoritative check is anchored to the
        model's own final `RESULT:` line's content, not the whole decoded transcript.
        """
        tail = (
            [_assistant_line("node tests/run-tests.mjs output:"), _assistant_line(self.WORKFLOW_TAILS[0])]
            + [_assistant_line(f"unrelated log line {i}") for i in range(20)]
            + [_assistant_line("RESULT: COMPLETED-BATCH")]
        )
        self.assertFalse(detector.tail_has_workflow_limit_signature(tail))
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(12.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)

    def test_workflow_marker_as_the_result_lines_own_content_still_fires(self) -> None:
        """The complementary case: a genuine diagnosis IS the model's own terminal RESULT line
        content and must still be caught, no matter how much unrelated prose precedes it."""
        tail = [_assistant_line(f"unrelated log line {i}") for i in range(20)] + [
            _assistant_line(self.WORKFLOW_TAILS[0]),
            _assistant_line("RESULT: LIMIT-SUSPECTED"),
        ]
        self.assertTrue(detector.tail_has_workflow_limit_signature(tail))

    def test_a2b_regression_closed_window_with_healthy_usage_and_no_result_line_does_not_rotate(
        self,
    ) -> None:
        """The exact scenario the audit's Priority 0 fix names: a payload containing "closed
        window" (workflow vocabulary) with a healthy usage reading and NO `RESULT:` line at all
        must NOT rotate.
        """
        tail = [_assistant_line(f"benign tool output line {i}") for i in range(5)] + [
            _assistant_line("the model quoted a bug report mentioning a closed window before giving up")
        ]
        self.assertFalse(detector.tail_has_workflow_limit_signature(tail))
        action = detector.classify("AGENT_DEAD_NONLIMIT", usage=_usage(12.0), tail=tail)
        self.assertEqual(action.kind, detector.RETRY)

    def test_a_workflow_marker_does_not_hijack_the_other_outcome_buckets(self) -> None:
        """The narrowing applies to the AGENT_DEAD_NONLIMIT ambiguity ONLY — a disk-visible
        outcome must keep its meaning no matter what the tail says (Invariant #2).
        """
        tail = [_assistant_line(self.WORKFLOW_TAILS[0]), _assistant_line("RESULT: LIMIT-SUSPECTED")]
        for outcome, expected in (
            ("PROGRESSED", detector.CONTINUE),
            ("AWAITING_HUMAN", detector.NOTIFY_PARK),
            ("BLOCKED", detector.NOTIFY_PARK),
            ("NO_BACKLOG", detector.DONE),
        ):
            with self.subTest(outcome=outcome):
                self.assertEqual(detector.classify(outcome, usage=_usage(12.0), tail=tail).kind, expected)


if __name__ == "__main__":
    unittest.main()


class RateLimitEventSafeStatusTests(unittest.TestCase):
    """`status: "allowed"` must never trigger a rotation.

    Live HIGH-severity regression, found 2026-07-27 by capturing a real envelope:

      {"status":"allowed","resetsAt":1785105000,"rateLimitType":"five_hour",
       "overageStatus":"rejected","overageDisabledReason":"org_level_disabled",
       "isUsingOverage":false}

    `_KNOWN_SAFE_RATE_LIMIT_STATUSES` held only `allowed_warning`, so this ordinary healthy event
    was classified UNRECOGNIZED and returned CONTINUE_ROTATE with a forced cooldown until
    `resetsAt` — up to five hours on a perfectly usable seat. With a small fleet every seat is
    cooled almost immediately and the supervisor stalls itself, the exact opposite of the
    availability it exists to provide.

    The original reasoning ("unknown -> assume limited is the conservative direction") was
    backwards. Per Invariant #2 disk state and the usage endpoint are the PRIMARY deciders and
    `ceiling_pct` already catches real limits, so a false positive here breaks rotation outright
    while a false negative merely defers to the mechanism that was already authoritative.
    """

    @staticmethod
    def _event(status: str, **extra: object) -> list[str]:
        info: dict[str, object] = {"status": status, "rateLimitType": "five_hour", "resetsAt": 1785105000}
        info.update(extra)
        return [json.dumps({"type": "rate_limit_event", "rate_limit_info": info})]

    def test_allowed_does_not_trigger_a_rotation(self) -> None:
        self.assertIsNone(detector._rate_limit_event_action(self._event("allowed")))

    def test_the_exact_live_envelope_does_not_trigger_a_rotation(self) -> None:
        tail = self._event(
            "allowed",
            overageStatus="rejected",
            overageDisabledReason="org_level_disabled",
            isUsingOverage=False,
        )
        self.assertIsNone(detector._rate_limit_event_action(tail))

    def test_allowed_warning_without_high_utilization_does_not_rotate(self) -> None:
        self.assertIsNone(detector._rate_limit_event_action(self._event("allowed_warning", utilization=0.76)))

    def test_allowed_warning_at_high_utilization_still_rotates(self) -> None:
        action = detector._rate_limit_event_action(self._event("allowed_warning", utilization=0.95))
        self.assertIsNotNone(action)

    def test_a_future_allowed_variant_is_safe_by_prefix(self) -> None:
        self.assertIsNone(detector._rate_limit_event_action(self._event("allowed_final_warning")))

    def test_a_genuinely_blocking_status_still_rotates(self) -> None:
        for status in ("rejected", "blocked", "exceeded", "denied", "throttled"):
            with self.subTest(status=status):
                self.assertIsNotNone(detector._rate_limit_event_action(self._event(status)))

    def test_both_observed_statuses_are_recorded_in_the_constant(self) -> None:
        """The set is a literal record of what has been SEEN; the prefix rule is the generalization.
        Keep them distinct so shrinking the record is visible.
        """
        self.assertIn("allowed", detector._KNOWN_SAFE_RATE_LIMIT_STATUSES)
        self.assertIn("allowed_warning", detector._KNOWN_SAFE_RATE_LIMIT_STATUSES)

    def test_an_event_with_no_utilization_field_does_not_crash_or_rotate(self) -> None:
        """The live five_hour event carried no `utilization` at all — that field appears only once a
        warning threshold is crossed.
        """
        signal = detector.latest_rate_limit_signal(self._event("allowed"))
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertIsNone(signal.utilization)
        self.assertEqual(signal.rate_limit_type, "five_hour")


class RateLimitUtilizationCalibrationTests(unittest.TestCase):
    """Pins what the 2026-07-27 Tier-2 burn measured about `utilization`.

    42 events captured while driving one five-hour window from 84% to 100%, across 42 SUCCESSFUL
    calls. The shape of that data constrains the threshold constant in ways the original guess did
    not anticipate.
    """

    @staticmethod
    def _event(**info: object) -> list[str]:
        base: dict[str, object] = {"rateLimitType": "five_hour", "resetsAt": 1785105000}
        base.update(info)
        return [json.dumps({"type": "rate_limit_event", "rate_limit_info": base})]

    def test_utilization_is_absent_below_the_warning_threshold(self) -> None:
        """13 `allowed` events carried NO `utilization`. So the rotate check must tolerate its
        absence rather than defaulting it to anything.
        """
        signal = detector.latest_rate_limit_signal(self._event(status="allowed"))
        assert signal is not None
        self.assertIsNone(signal.utilization)
        self.assertIsNone(detector._rate_limit_event_action(self._event(status="allowed")))

    def test_the_first_event_carrying_utilization_already_meets_the_threshold(self) -> None:
        """The operational consequence of the above: the lowest `utilization` ever observed is
        exactly 0.90, so `>= 0.9` fires on the FIRST event that carries the field. The constant is
        equivalent to a presence check — worth pinning, because someone lowering it to "be safer"
        would be changing nothing at all.
        """
        self.assertLessEqual(detector._RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD, 0.9)
        action = detector._rate_limit_event_action(self._event(status="allowed_warning", utilization=0.9))
        self.assertIsNotNone(action)

    def test_surpassed_threshold_may_be_absent_even_when_utilization_is_present(self) -> None:
        """2 of 29 events at exactly 0.90 carried no `surpassedThreshold`, so it must never be used
        as a proxy for "is this a warning event".
        """
        tail = self._event(status="allowed_warning", utilization=0.9)
        self.assertNotIn("surpassedThreshold", tail[0])
        self.assertIsNotNone(detector._rate_limit_event_action(tail))

    def test_utilization_below_the_threshold_does_not_rotate(self) -> None:
        """The original 2026-07-26 observation: seven_day at 0.76. Must stay non-rotating."""
        tail = [
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "allowed_warning",
                        "rateLimitType": "seven_day",
                        "utilization": 0.76,
                        "surpassedThreshold": 0.75,
                        "resetsAt": 1785290400,
                    },
                }
            )
        ]
        self.assertIsNone(detector._rate_limit_event_action(tail))

    def test_ninety_nine_percent_still_only_rotates_never_treated_as_a_denial(self) -> None:
        """utilization peaked at 0.99 with every call SUCCEEDING. High utilization is a rotate
        signal, not evidence the request was refused.
        """
        action = detector._rate_limit_event_action(self._event(status="allowed_warning", utilization=0.99))
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)


class RotationCooldownSplitTests(unittest.TestCase):
    """The rotation/cooldown split (2026-07-27).

    `loop.py`'s AGENT_DEAD_NONLIMIT branch passes `action.resets_at` to `_force_cooldown()` as the
    cooldown BOUNDARY. Q1 established that this event channel only ever WARNS — 42 events from 0.90
    to 0.99 utilization, every call succeeding — so a high-utilization event must drive ROTATION
    (move off this seat now) without declaring the seat dead until its window resets.

    Worst case before the fix: a `seven_day` event at 0.90 cooled the seat for DAYS, discarding ~10%
    of still-spendable weekly quota, on the strength of a warning about a seat that was
    demonstrably still serving requests.
    """

    @staticmethod
    def _tail(**info: object) -> list[str]:
        base: dict[str, object] = {"rateLimitType": "five_hour", "resetsAt": 1785105000}
        base.update(info)
        return [json.dumps({"type": "rate_limit_event", "rate_limit_info": base})]

    def test_a_high_utilization_warning_rotates_without_a_cooldown_horizon(self) -> None:
        action = detector._rate_limit_event_action(self._tail(status="allowed_warning", utilization=0.95))
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        self.assertIsNone(action.resets_at, "a warning must not set the cooldown boundary")

    def test_a_weekly_warning_never_yields_a_multi_day_cooldown(self) -> None:
        """The specific harm the split exists to prevent."""
        tail = self._tail(
            status="allowed_warning",
            rateLimitType="seven_day",
            utilization=0.9,
            resetsAt=1785290400,
        )
        action = detector._rate_limit_event_action(tail)
        self.assertIsNotNone(action)
        assert action is not None
        self.assertIsNone(action.resets_at)

    def test_an_unrecognized_status_still_carries_its_reset_time(self) -> None:
        """The one case where a genuine denial remains possible keeps the conservative behaviour."""
        action = detector._rate_limit_event_action(self._tail(status="rejected"))
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        self.assertIsNotNone(action.resets_at, "an unvouched-for status must keep its reset horizon")

    def test_the_reason_string_says_why_no_horizon_was_attached(self) -> None:
        """An operator reading the log must be able to tell this apart from a missing resetsAt."""
        action = detector._rate_limit_event_action(self._tail(status="allowed_warning", utilization=0.99))
        assert action is not None
        self.assertIn("WARNING", action.reason)

    def test_rotation_still_happens_so_the_seat_is_not_hammered(self) -> None:
        """Dropping the horizon must not drop the rotation — `_force_cooldown()` still applies its
        short default guess, which is what stops `pick_seat()` re-selecting this seat immediately.
        """
        for util in (0.9, 0.95, 0.99):
            with self.subTest(utilization=util):
                tail = self._tail(status="allowed_warning", utilization=util)
                action = detector._rate_limit_event_action(tail)
                self.assertIsNotNone(action)
                assert action is not None
                self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
