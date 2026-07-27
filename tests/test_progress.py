"""Tests for `relay.progress` — the operator-facing projection of a run's NDJSON stream.

`project()` is a pure function of one decoded envelope plus the caller's `seen` set, so every rule
is testable offline. Two of these tests exist because the corresponding rule was WRONG on the first
real supervised run (DESIGN.md §4b) and produced actively misleading output:

  * concurrent agents interleave their `task_progress` events, so "changed since last event" reads
    as a stuck A/B/A/B loop (`ConcurrentAgentInterleaveTests`);
  * not every `task_started` is a workflow (`TaskStartLabellingTests`).

Both were caught by watching a live run, not by reading the projection — which is precisely why they
are pinned here now.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay import progress


def _progress_event(description: str, *, tokens: int = 0, tools: int = 0) -> dict:
    return {
        "type": "system",
        "subtype": "task_progress",
        "description": description,
        "usage": {"total_tokens": tokens, "tool_uses": tools},
        "workflow_progress": [
            {"type": "workflow_phase", "index": 1, "title": "Survey"},
            {"type": "workflow_phase", "index": 2, "title": "Generations"},
        ],
    }


class ConcurrentAgentInterleaveTests(unittest.TestCase):
    """gad-run runs agents concurrently, so their progress events interleave. Each distinct agent
    must be reported exactly ONCE — reporting on "description differs from the previous event"
    turned two healthy parallel reviewers into an alternating feed indistinguishable from a hang.
    """

    def test_each_distinct_agent_is_reported_exactly_once(self) -> None:
        seen: set[str] = set()
        interleaved = ["review:framing", "review:combined"] * 4
        emitted = [progress.project(_progress_event(d), seen) for d in interleaved]

        reported = [line for line in emitted if line is not None]
        self.assertEqual(len(reported), 2, f"expected one line per distinct agent, got {reported}")
        self.assertIn("AGENT[1] review:framing", reported[0])
        self.assertIn("AGENT[2] review:combined", reported[1])

    def test_the_counter_counts_distinct_agents_not_events(self) -> None:
        seen: set[str] = set()
        for name in ("survey", "preflight", "plan G0"):
            for _ in range(3):  # each agent emits many progress events
                progress.project(_progress_event(name), seen)
        self.assertEqual(len(seen), 3)

    def test_a_repeated_label_after_others_still_stays_silent(self) -> None:
        seen: set[str] = set()
        progress.project(_progress_event("implement"), seen)
        progress.project(_progress_event("adversarial"), seen)
        self.assertIsNone(progress.project(_progress_event("implement"), seen))

    def test_an_event_with_no_description_is_ignored(self) -> None:
        seen: set[str] = set()
        self.assertIsNone(progress.project(_progress_event(""), seen))
        self.assertEqual(seen, set())

    def test_progress_line_carries_tokens_tools_and_plan(self) -> None:
        line = progress.project(_progress_event("implement", tokens=324728, tools=169), set())
        assert line is not None
        self.assertIn("tokens=324728", line)
        self.assertIn("tools=169", line)
        self.assertIn("Survey>Generations", line)


class TaskStartLabellingTests(unittest.TestCase):
    """`task_started` covers both workflows AND the `local_bash` background tasks agents launch.
    Labelling everything a workflow rendered real filesystem searches as "WORKFLOW-START None".
    """

    def test_a_workflow_task_is_labelled_by_workflow_name(self) -> None:
        line = progress.project(
            {
                "type": "system",
                "subtype": "task_started",
                "task_type": "local_workflow",
                "workflow_name": "gad-run",
                "task_id": "w31r9xgpk",
            },
            set(),
        )
        assert line is not None
        self.assertIn("TASK-START[local_workflow]", line)
        self.assertIn("gad-run", line)
        self.assertNotIn("None", line)

    def test_a_local_bash_task_falls_back_to_its_description(self) -> None:
        line = progress.project(
            {
                "type": "system",
                "subtype": "task_started",
                "task_type": "local_bash",
                "workflow_name": None,
                "description": "Search filesystem for claude-relay or capture-tap references",
                "task_id": "bbaff8g9f",
            },
            set(),
        )
        assert line is not None
        self.assertIn("TASK-START[local_bash]", line)
        self.assertIn("Search filesystem", line)
        self.assertNotIn("None", line)


class RateLimitAndResultProjectionTests(unittest.TestCase):
    def test_rate_limit_event_without_utilization_renders_a_dash(self) -> None:
        # Below 0.9 the platform omits `utilization` entirely (DESIGN.md §4a Q4); the projection
        # must not print "None" as though the field were present and null.
        line = progress.project(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
            },
            set(),
        )
        assert line is not None
        self.assertIn("status=allowed", line)
        self.assertIn("utilization=-", line)

    def test_rate_limit_event_with_utilization_shows_the_number(self) -> None:
        line = progress.project(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed_warning",
                    "rateLimitType": "five_hour",
                    "utilization": 0.9,
                },
            },
            set(),
        )
        assert line is not None
        self.assertIn("utilization=0.9", line)

    def test_result_envelope_reports_outcome_cost_and_models(self) -> None:
        line = progress.project(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "stop_reason": "end_turn",
                "api_error_status": None,
                "num_turns": 10,
                "total_cost_usd": 14.728279,
                "duration_ms": 3829000,
                "modelUsage": {"claude-sonnet-5": {}, "claude-haiku-4-5-20251001": {}},
            },
            set(),
        )
        assert line is not None
        self.assertIn("is_error=False", line)
        self.assertIn("cost=$14.728279", line)
        self.assertIn("dur=3829s", line)
        self.assertIn("claude-sonnet-5", line)


class AssistantProjectionTests(unittest.TestCase):
    def test_workflow_tool_use_names_the_script(self) -> None:
        line = progress.project(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Workflow",
                            "input": {"scriptPath": "/long/path/to/workflows/gad-run.js"},
                        }
                    ]
                },
            },
            set(),
        )
        self.assertEqual(line, "tool:Workflow script=gad-run.js")

    def test_taskoutput_reports_whether_it_blocks(self) -> None:
        line = progress.project(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "TaskOutput", "input": {"block": True}}
                    ]
                },
            },
            set(),
        )
        self.assertEqual(line, "tool:TaskOutput block=True")

    def test_text_containing_an_error_marker_is_flagged(self) -> None:
        line = progress.project(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Traceback (most recent call last):"}]},
            },
            set(),
        )
        assert line is not None
        self.assertTrue(line.startswith("TEXT-ERROR:"), line)

    def test_ordinary_text_is_not_flagged_as_an_error(self) -> None:
        line = progress.project(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Still running."}]}},
            set(),
        )
        self.assertEqual(line, "text: Still running.")

    def test_an_assistant_envelope_with_no_content_stays_silent(self) -> None:
        self.assertIsNone(progress.project({"type": "assistant", "message": {"content": []}}, set()))


class ToolResultFailureTests(unittest.TestCase):
    """Tool results arrive as `user` envelopes. A successful one is noise; a failed one is where a
    workflow death becomes visible, so only failures are surfaced.
    """

    def test_a_failed_tool_result_is_surfaced(self) -> None:
        line = progress.project(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "is_error": True, "content": "workflow died"}
                    ]
                },
            },
            set(),
        )
        assert line is not None
        self.assertIn("TOOL-ERROR", line)
        self.assertIn("workflow died", line)

    def test_a_successful_tool_result_stays_silent(self) -> None:
        line = progress.project(
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "is_error": False, "content": "ok"}]},
            },
            set(),
        )
        self.assertIsNone(line)


class UnknownEnvelopeTests(unittest.TestCase):
    def test_noisy_system_subtypes_stay_silent(self) -> None:
        for sub in ("hook_started", "hook_response", "thinking_tokens", "background_tasks_changed"):
            with self.subTest(sub=sub):
                self.assertIsNone(progress.project({"type": "system", "subtype": sub}, set()))

    def test_an_unrecognized_envelope_type_stays_silent(self) -> None:
        self.assertIsNone(progress.project({"type": "something_new_from_the_platform"}, set()))


class NewestLogTests(unittest.TestCase):
    def test_newest_log_picks_the_most_recently_modified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            old = log_dir / "run-old.log"
            new = log_dir / "run-new.log"
            old.write_text("{}\n", encoding="utf-8")
            new.write_text("{}\n", encoding="utf-8")
            import os

            os.utime(old, (1_600_000_000, 1_600_000_000))
            os.utime(new, (1_700_000_000, 1_700_000_000))
            with mock.patch.object(progress, "LOG_DIR", log_dir):
                self.assertEqual(progress.newest_log(), new)

    def test_newest_log_returns_none_for_an_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            progress, "LOG_DIR", Path(tmp)
        ):
            self.assertIsNone(progress.newest_log())


class MainTailTests(unittest.TestCase):
    def test_main_projects_a_completed_log_and_stops_at_the_result_envelope(self) -> None:
        lines = [
            {"type": "system", "subtype": "init", "model": "claude-opus-5[1m]", "cwd": "/repo"},
            {"type": "system", "subtype": "hook_started"},  # noise, must not appear
            _progress_event("implement", tokens=100, tools=3),
            {"type": "result", "subtype": "success", "is_error": False, "modelUsage": {}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "after"}]}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            with mock.patch("builtins.print") as printed:
                rc = progress.main(["run-progress", str(log)])

        self.assertEqual(rc, 0)
        out = [call.args[0] for call in printed.call_args_list]
        self.assertTrue(any("init model=claude-opus-5[1m]" in line for line in out))
        self.assertTrue(any("AGENT[1] implement" in line for line in out))
        self.assertTrue(any("RESULT subtype=success" in line for line in out))
        # Stopped at the terminal envelope: nothing after it was projected.
        self.assertFalse(any("after" in line for line in out))
        self.assertFalse(any("hook_started" in line for line in out))

    def test_a_malformed_line_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text(
                "{not json at all\n" + json.dumps({"type": "result", "modelUsage": {}}) + "\n",
                encoding="utf-8",
            )
            with mock.patch("builtins.print") as printed:
                rc = progress.main(["run-progress", str(log)])
        self.assertEqual(rc, 0)
        self.assertTrue(any("RESULT" in call.args[0] for call in printed.call_args_list))

    def test_a_non_json_line_carrying_an_error_marker_is_surfaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text(
                'Traceback (most recent call last):\n{"type":"result","modelUsage":{}}\n',
                encoding="utf-8",
            )
            with mock.patch("builtins.print") as printed:
                progress.main(["run-progress", str(log)])
        out = [call.args[0] for call in printed.call_args_list]
        self.assertTrue(any("NON-JSON-ERROR" in line for line in out), out)

    def test_a_missing_logfile_argument_path_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.log"
            with mock.patch("builtins.print"):
                self.assertEqual(progress.main(["run-progress", str(missing)]), 2)

    def test_no_logs_at_all_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            progress, "LOG_DIR", Path(tmp)
        ), mock.patch("builtins.print"):
            self.assertEqual(progress.main(["run-progress"]), 1)


if __name__ == "__main__":
    unittest.main()
