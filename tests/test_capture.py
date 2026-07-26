"""Offline tests for the Tier-1 rate-limit envelope tap (`relay/capture.py`).

Fully offline and network-free like the rest of `tests/` — the tap itself never makes a network
call, so everything here is real behaviour against real temp directories, no mocking of the
subject.

The invariant-bearing tests (do NOT delete without replacing):
  * `assistant` envelopes are never recorded — Invariant #5, never leak secrets. Tool output rides
    in `assistant` envelopes and can contain repository contents or credentials.
  * a write failure disables the tap instead of raising — a capture tap that can kill a multi-day
    supervisor run is worse than no tap at all.
  * the tap is OFF unless the env var names a directory — no config-file path may enable it.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay import capture


def _event(status: str = "allowed_warning", kind: str = "seven_day", util: float = 0.76) -> str:
    return json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "resetsAt": 1785290400,
                "rateLimitType": kind,
                "utilization": util,
                "isUsingOverage": False,
                "surpassedThreshold": 0.75,
            },
        }
    )


class CaptureTapTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        capture.reset_for_tests()
        self.addCleanup(capture.reset_for_tests)

    def _enable(self) -> Path:
        target = self.tmp / "cap"
        patcher = mock.patch.dict(os.environ, {capture.CAPTURE_DIR_ENV: str(target)})
        patcher.start()
        self.addCleanup(patcher.stop)
        capture.reset_for_tests()
        return target

    def _records(self) -> list[dict]:
        found: list[dict] = []
        path = capture.capture_path()
        if path is not None and path.exists():
            found.extend(capture.read_records(path))
        return found

    # -- off by default ------------------------------------------------------------------------

    def test_tap_is_off_when_env_var_is_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            capture.reset_for_tests()
            self.assertFalse(capture.enabled())
            self.assertIsNone(capture.capture_path())
            self.assertIn(capture.CAPTURE_DIR_ENV, capture.disabled_reason() or "")
            # Must be a genuine no-op, not a silent failure.
            capture.record_line(_event())

    def test_tap_is_off_when_env_var_is_blank(self) -> None:
        with mock.patch.dict(os.environ, {capture.CAPTURE_DIR_ENV: "   "}):
            capture.reset_for_tests()
            self.assertFalse(capture.enabled())

    def test_enabling_creates_the_directory(self) -> None:
        target = self._enable()
        self.assertTrue(capture.enabled())
        self.assertTrue(target.is_dir())
        path = capture.capture_path()
        assert path is not None
        self.assertEqual(path.parent, target)
        self.assertTrue(path.name.startswith("envelopes-"))
        self.assertTrue(path.name.endswith(".jsonl"))

    def test_capture_file_name_includes_the_pid_so_concurrent_writers_never_share_a_file(self) -> None:
        self._enable()
        path = capture.capture_path()
        assert path is not None
        self.assertIn(str(os.getpid()), path.name)

    # -- what gets recorded --------------------------------------------------------------------

    def test_records_a_rate_limit_event(self) -> None:
        self._enable()
        capture.record_line(_event(), seat="ayan")
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["seat"], "ayan")
        info = records[0]["envelope"]["rate_limit_info"]
        self.assertEqual(info["status"], "allowed_warning")
        self.assertEqual(info["rateLimitType"], "seven_day")
        self.assertIn("captured_at", records[0])
        self.assertIn("captured_at_unix", records[0])

    def test_records_a_terminal_result_envelope(self) -> None:
        self._enable()
        capture.record_line(
            json.dumps({"type": "result", "subtype": "success", "modelUsage": {"claude-opus-5": {"in": 10}}})
        )
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["envelope"]["type"], "result")
        self.assertIn("modelUsage", records[0]["envelope"])

    def test_never_records_assistant_envelopes(self) -> None:
        """Invariant #5. `assistant` envelopes carry tool output, which can contain repository
        contents or secrets. The tap must not write them even though they are the bulk of a run.
        """
        self._enable()
        capture.record_line(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "AWS_SECRET_ACCESS_KEY=hunter2"}]},
                }
            )
        )
        self.assertEqual(self._records(), [])
        path = capture.capture_path()
        assert path is not None
        if path.exists():
            self.assertNotIn("hunter2", path.read_text(encoding="utf-8"))

    def test_never_records_system_envelopes(self) -> None:
        self._enable()
        capture.record_line(json.dumps({"type": "system", "subtype": "init"}))
        self.assertEqual(self._records(), [])

    def test_ignores_non_json_lines(self) -> None:
        self._enable()
        capture.record_line("this is not json but mentions rate_limit_event")
        self.assertEqual(self._records(), [])

    def test_ignores_a_line_whose_type_merely_mentions_a_captured_name(self) -> None:
        """The cheap substring prefilter must never be the final arbiter — a real `json.loads` and
        a `type` check decide, so prose that happens to contain the words is dropped.
        """
        self._enable()
        capture.record_line(json.dumps({"type": "assistant", "note": 'mentions "rate_limit_event" inline'}))
        self.assertEqual(self._records(), [])

    def test_ignores_a_json_array_line(self) -> None:
        self._enable()
        capture.record_line(json.dumps([{"type": "result"}]))
        self.assertEqual(self._records(), [])

    def test_appends_rather_than_overwriting(self) -> None:
        self._enable()
        for util in (0.76, 0.81, 0.9):
            capture.record_line(_event(util=util))
        records = self._records()
        self.assertEqual(len(records), 3)
        self.assertEqual(
            [r["envelope"]["rate_limit_info"]["utilization"] for r in records],
            [0.76, 0.81, 0.9],
        )

    # -- truncation ---------------------------------------------------------------------------

    def test_oversized_envelope_is_truncated_but_keeps_the_calibration_fields(self) -> None:
        self._enable()
        huge = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "modelUsage": {"claude-opus-5": {"in": 1}},
                "result": "x" * (capture._MAX_RECORD_BYTES + 5000),
            }
        )
        capture.record_line(huge)
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["truncated"])
        self.assertGreater(records[0]["original_bytes"], capture._MAX_RECORD_BYTES)
        # The point of truncating rather than dropping: the fields we came for survive.
        self.assertIn("modelUsage", records[0]["envelope"])
        self.assertEqual(records[0]["envelope"]["type"], "result")
        self.assertNotIn("result", records[0]["envelope"])

    def test_oversized_rate_limit_event_keeps_rate_limit_info(self) -> None:
        self._enable()
        payload = json.loads(_event(status="rejected"))
        payload["padding"] = "y" * (capture._MAX_RECORD_BYTES + 1000)
        capture.record_line(json.dumps(payload))
        records = self._records()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["truncated"])
        self.assertEqual(records[0]["envelope"]["rate_limit_info"]["status"], "rejected")

    # -- failure handling ---------------------------------------------------------------------

    def test_write_failure_disables_the_tap_and_never_raises(self) -> None:
        self._enable()
        path = capture.capture_path()
        assert path is not None
        with mock.patch.object(Path, "open", side_effect=OSError("disk full")):
            capture.record_line(_event())  # must not raise
        self.assertFalse(capture.enabled())
        self.assertIn("tap disabled", capture.disabled_reason() or "")
        # And stays disabled for subsequent lines rather than retrying every one.
        capture.record_line(_event())

    def test_unpreparable_directory_disables_the_tap_without_raising(self) -> None:
        blocker = self.tmp / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        with mock.patch.dict(os.environ, {capture.CAPTURE_DIR_ENV: str(blocker / "under-a-file")}):
            capture.reset_for_tests()
            self.assertFalse(capture.enabled())
            self.assertIn("could not prepare", capture.disabled_reason() or "")
            capture.record_line(_event())

    def test_unserializable_record_is_dropped_without_raising(self) -> None:
        self._enable()
        # Build the fixture BEFORE patching: `capture.json` is the shared stdlib module, so
        # patching its `dumps` also breaks this test's own helper (and `read_records`).
        line = _event()
        with mock.patch.object(capture.json, "dumps", side_effect=TypeError("nope")):
            capture.record_line(line)  # must not raise
        self.assertEqual(self._records(), [])


class ReadRecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_torn_tail_does_not_make_the_whole_file_unreadable(self) -> None:
        """A crash mid-append leaves a partial final line. Every earlier record must still parse —
        that is the entire reason each record is one self-contained line (Invariant #7).
        """
        path = self.tmp / "envelopes-x.jsonl"
        good = json.dumps({"envelope": json.loads(_event())})
        path.write_text(good + "\n" + good + "\n" + '{"envelope": {"type": "rate_li', encoding="utf-8")
        records = capture.read_records(path)
        self.assertEqual(len(records), 2)

    def test_missing_file_returns_empty_rather_than_raising(self) -> None:
        self.assertEqual(capture.read_records(self.tmp / "absent.jsonl"), [])

    def test_blank_lines_are_skipped(self) -> None:
        path = self.tmp / "envelopes-y.jsonl"
        path.write_text("\n\n" + json.dumps({"envelope": json.loads(_event())}) + "\n\n", encoding="utf-8")
        self.assertEqual(len(capture.read_records(path)), 1)


class VocabularyTests(unittest.TestCase):
    def _records(self, *events: str) -> list[dict]:
        return [{"envelope": json.loads(e)} for e in events]

    def test_status_vocabulary_counts_occurrences(self) -> None:
        records = self._records(
            _event(status="allowed_warning"),
            _event(status="allowed_warning"),
            _event(status="rejected"),
        )
        self.assertEqual(
            capture.observed_status_vocabulary(records),
            {"allowed_warning": 2, "rejected": 1},
        )

    def test_limit_types_counts_occurrences(self) -> None:
        records = self._records(_event(kind="seven_day"), _event(kind="five_hour"), _event(kind="five_hour"))
        self.assertEqual(capture.observed_limit_types(records), {"seven_day": 1, "five_hour": 2})

    def test_vocabularies_ignore_result_envelopes(self) -> None:
        records = [{"envelope": {"type": "result", "subtype": "success"}}]
        self.assertEqual(capture.observed_status_vocabulary(records), {})
        self.assertEqual(capture.observed_limit_types(records), {})

    def test_vocabularies_tolerate_a_missing_or_malformed_rate_limit_info(self) -> None:
        records = [
            {"envelope": {"type": "rate_limit_event"}},
            {"envelope": {"type": "rate_limit_event", "rate_limit_info": "not-a-dict"}},
            {"envelope": {"type": "rate_limit_event", "rate_limit_info": {"status": 42}}},
        ]
        self.assertEqual(capture.observed_status_vocabulary(records), {})
        self.assertEqual(capture.observed_limit_types(records), {})

    def test_rate_limit_records_preserves_file_order(self) -> None:
        records = self._records(_event(util=0.1), _event(util=0.2), _event(util=0.3))
        got = [r["envelope"]["rate_limit_info"]["utilization"] for r in capture.rate_limit_records(records)]
        self.assertEqual(got, [0.1, 0.2, 0.3])


if __name__ == "__main__":
    unittest.main()


class RecordToExplicitDestinationTests(unittest.TestCase):
    """`record_to()` must work with NO env var set anywhere.

    This is the regression guard for a live silent-no-op bug: the Tier-2 harness set
    CLAUDE_RELAY_CAPTURE_DIR in the CHILD's environment (the child being `claude`, which has never
    heard of relay.capture) while calling `record_line()` in the PARENT, whose own os.environ lacked
    it. Seven successful real runs recorded nothing, and the burn's wall detector reads those very
    records — so it could never have stopped on a wall.

    Every test here deliberately clears the env var. A test that patched it in would recreate the
    condition production lacked, which is exactly how the original bug survived its own test.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        without_tap = {k: v for k, v in os.environ.items() if k != capture.CAPTURE_DIR_ENV}
        self._env = mock.patch.dict(os.environ, without_tap, clear=True)
        self._env.start()
        self.addCleanup(self._env.stop)
        capture.reset_for_tests()
        self.addCleanup(capture.reset_for_tests)

    def test_records_with_the_env_gated_tap_completely_off(self) -> None:
        self.assertFalse(capture.enabled())
        target = self.tmp / "explicit"
        capture.record_to(target, _event(), seat="ayan")
        files = list(target.glob("envelopes-*.jsonl"))
        self.assertEqual(len(files), 1)
        records = capture.read_records(files[0])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["seat"], "ayan")

    def test_record_line_stays_a_noop_while_record_to_works(self) -> None:
        """The two entry points are independent: the env-gated tap being off must not disable the
        explicit one, and vice versa.
        """
        target = self.tmp / "explicit2"
        capture.record_line(_event())  # off — writes nowhere
        capture.record_to(target, _event())
        self.assertEqual(len(capture.read_records(next(target.glob("envelopes-*.jsonl")))), 1)

    def test_repeated_calls_append_to_one_file_per_directory(self) -> None:
        target = self.tmp / "explicit3"
        for util in (0.7, 0.8, 0.95):
            capture.record_to(target, _event(util=util))
        files = list(target.glob("envelopes-*.jsonl"))
        self.assertEqual(len(files), 1, "each call created its own file instead of appending")
        self.assertEqual(len(capture.read_records(files[0])), 3)

    def test_separate_directories_get_separate_files(self) -> None:
        a, b = self.tmp / "a", self.tmp / "b"
        capture.record_to(a, _event())
        capture.record_to(b, _event())
        self.assertEqual(len(list(a.glob("envelopes-*.jsonl"))), 1)
        self.assertEqual(len(list(b.glob("envelopes-*.jsonl"))), 1)

    def test_applies_the_same_assistant_exclusion(self) -> None:
        """Invariant #5 must hold on BOTH entry points, not just the env-gated one."""
        target = self.tmp / "explicit4"
        capture.record_to(target, json.dumps({"type": "assistant", "text": "SECRET=hunter2"}))
        files = list(target.glob("envelopes-*.jsonl"))
        if files:
            self.assertNotIn("hunter2", files[0].read_text(encoding="utf-8"))
        self.assertEqual(sum(len(capture.read_records(f)) for f in files), 0)

    def test_unwritable_destination_is_a_noop_rather_than_a_raise(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("", encoding="utf-8")
        capture.record_to(blocker / "under-a-file", _event())  # must not raise

    def test_an_explicit_write_failure_does_not_disable_the_env_gated_tap(self) -> None:
        with mock.patch.dict(os.environ, {capture.CAPTURE_DIR_ENV: str(self.tmp / "gated")}):
            capture.reset_for_tests()
            self.assertTrue(capture.enabled())
            with mock.patch.object(Path, "open", side_effect=OSError("nope")):
                capture.record_to(self.tmp / "other", _event())
            self.assertTrue(capture.enabled(), "an explicit-destination failure disabled the shared tap")
