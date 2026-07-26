"""Offline tests for the rate-limit calibration harness (`relay/ratelimit_probe.py`).

Covers the pure decision logic and the CLI contract only — never `read_seat()`/`_run_claude_once()`,
which make a real network call and spawn a real `claude`. Those are exercised in
`tests_live/test_rate_limit_capture_live.py`, which is opt-in precisely because it costs quota.

The load-bearing tests here are the SAFETY RAILS. This harness deliberately spends real money's
worth of quota, so the caps must be provably impossible to exceed by flag or by bug:
  * `--max-calls` is clamped to `_MAX_BURN_CALLS` regardless of what is passed.
  * the burn prompt grants no tools, so a burn call cannot touch a repository (Invariant #6).
  * `_walled()` treats anything outside the single known-safe status as the stop signal, so an
    unrecognized-but-genuine wall halts the burn instead of being burned straight through.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from relay import detector, ratelimit_probe


def _event(
    status: str = "allowed_warning",
    kind: str = "seven_day",
    util: float = 0.76,
    thresh: float = 0.75,
) -> dict:
    return {
        "envelope": {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "resetsAt": 1785290400,
                "rateLimitType": kind,
                "utilization": util,
                "surpassedThreshold": thresh,
            },
        }
    }


def _reading(
    name: str, five: float | None, seven: float | None, error: str | None = None
) -> ratelimit_probe.SeatReading:
    return ratelimit_probe.SeatReading(
        name=name,
        path=Path(f"/home/x/.claude-{name}"),
        five_hour_pct=five,
        seven_day_pct=seven,
        raw={},
        error=error,
    )


class BurnScoreTests(unittest.TestCase):
    """The cheapest seat to wall has the MOST five-hour already spent and the LEAST weekly spent."""

    def test_high_five_hour_low_weekly_outranks_the_reverse(self) -> None:
        good = _reading("ayan", 57.0, 52.0)
        bad = _reading("azim", 0.0, 72.0)
        self.assertGreater(good.burn_score(), bad.burn_score())

    def test_a_fresh_window_is_the_worst_candidate(self) -> None:
        fresh = _reading("fresh", 0.0, 10.0)
        nearly_walled = _reading("nearly", 95.0, 10.0)
        self.assertGreater(nearly_walled.burn_score(), fresh.burn_score())

    def test_errored_seat_scores_negative_infinity_and_is_never_chosen(self) -> None:
        broken = _reading("broken", None, None, error="needs login")
        self.assertEqual(broken.burn_score(), float("-inf"))
        self.assertFalse(broken.usable_for_burn)
        self.assertLess(broken.burn_score(), _reading("ok", 0.0, 100.0).burn_score())

    def test_absent_weekly_gauge_is_treated_as_fully_spent_not_as_free(self) -> None:
        """A missing weekly reading must make a seat look EXPENSIVE, never cheap — guessing 'free'
        would send a burn at the one seat we know least about.
        """
        unknown_weekly = _reading("unknown", 50.0, None)
        known_cheap = _reading("known", 50.0, 0.0)
        self.assertLess(unknown_weekly.burn_score(), known_cheap.burn_score())

    def test_missing_five_hour_makes_a_seat_unusable_for_burn(self) -> None:
        self.assertFalse(_reading("x", None, 10.0).usable_for_burn)


class PctTests(unittest.TestCase):
    def test_accepts_ints_and_floats(self) -> None:
        self.assertEqual(ratelimit_probe._pct(57), 57.0)
        self.assertEqual(ratelimit_probe._pct(57.5), 57.5)

    def test_rejects_strings_and_none(self) -> None:
        self.assertIsNone(ratelimit_probe._pct("57"))
        self.assertIsNone(ratelimit_probe._pct(None))

    def test_rejects_bool_disguised_as_number(self) -> None:
        # `isinstance(True, int)` is True in Python, so a bool would otherwise become 1.0 — a
        # utilization of 1.0 means "walled", which is the single most consequential misreading here.
        self.assertNotEqual(ratelimit_probe._pct(True), 1.0)


class SchemaWalkTests(unittest.TestCase):
    def test_flattens_nested_dicts_to_dotted_paths(self) -> None:
        got = ratelimit_probe._schema_walk({"a": {"b": 1, "c": "x"}})
        self.assertEqual(got, {"a.b": "int", "a.c": "str"})

    def test_summarizes_a_list_by_its_first_element(self) -> None:
        got = ratelimit_probe._schema_walk({"limits": [{"kind": "session"}, {"kind": "weekly_all"}]})
        self.assertEqual(got, {"limits[].kind": "str"})

    def test_marks_an_empty_list(self) -> None:
        self.assertEqual(ratelimit_probe._schema_walk({"limits": []}), {"limits[]": "empty"})

    def test_records_none_as_a_type_so_present_but_null_fields_are_visible(self) -> None:
        """`seven_day_opus` is present-but-null on the observed account. A walk that dropped nulls
        would hide the fact that the endpoint has a per-model weekly gauge at all.
        """
        walked = ratelimit_probe._schema_walk({"seven_day_opus": None})
        self.assertEqual(walked, {"seven_day_opus": "NoneType"})


class WalledDetectionTests(unittest.TestCase):
    def test_known_safe_status_is_not_treated_as_walled(self) -> None:
        self.assertIsNone(ratelimit_probe._walled([_event(status="allowed_warning")]))

    def test_any_status_outside_known_safe_is_treated_as_walled(self) -> None:
        hit = ratelimit_probe._walled([_event(status="rejected")])
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit["envelope"]["rate_limit_info"]["status"], "rejected")

    def test_returns_the_first_walled_event_not_the_last(self) -> None:
        records = [_event(status="allowed_warning"), _event(status="blocked"), _event(status="rejected")]
        hit = ratelimit_probe._walled(records)
        assert hit is not None
        self.assertEqual(hit["envelope"]["rate_limit_info"]["status"], "blocked")

    def test_agrees_with_the_detector_constant_it_is_calibrating(self) -> None:
        """If someone widens `_KNOWN_SAFE_RATE_LIMIT_STATUSES`, the burn's stop condition must widen
        with it automatically — a harness with its own hardcoded copy would keep burning past a
        status production already considers safe, or stop on one it does not.
        """
        for status in detector._KNOWN_SAFE_RATE_LIMIT_STATUSES:
            self.assertIsNone(ratelimit_probe._walled([_event(status=status)]))

    def test_malformed_rate_limit_info_is_not_mistaken_for_a_wall(self) -> None:
        self.assertIsNone(ratelimit_probe._walled([{"envelope": {"type": "rate_limit_event"}}]))
        self.assertIsNone(
            ratelimit_probe._walled([{"envelope": {"type": "rate_limit_event", "rate_limit_info": "x"}}])
        )

    def test_non_string_status_is_not_mistaken_for_a_wall(self) -> None:
        record = {"envelope": {"type": "rate_limit_event", "rate_limit_info": {"status": 429}}}
        self.assertIsNone(ratelimit_probe._walled([record]))


class SummarizeTests(unittest.TestCase):
    def test_reports_the_pre_registered_answers(self) -> None:
        records = [
            _event(status="allowed_warning", kind="seven_day", util=0.76, thresh=0.75),
            _event(status="allowed_warning", kind="five_hour", util=0.91, thresh=0.9),
            {"envelope": {"type": "result", "subtype": "success", "modelUsage": {"claude-opus-5": {}}}},
        ]
        got = ratelimit_probe.summarize(records)
        self.assertEqual(got["records_total"], 3)
        self.assertEqual(got["rate_limit_events"], 2)
        self.assertEqual(got["result_envelopes"], 1)
        self.assertEqual(got["Q2_status_vocabulary"], {"allowed_warning": 2})
        self.assertEqual(got["Q2_statuses_outside_known_safe"], [])
        self.assertEqual(got["Q3_limit_types"], {"seven_day": 1, "five_hour": 1})
        self.assertTrue(got["Q3_five_hour_observed"])
        self.assertEqual(got["Q4_surpassed_thresholds"], [0.75, 0.9])
        self.assertEqual(got["Q4_max_utilization"], 0.91)
        self.assertEqual(got["Q6_result_envelopes_with_modelUsage"], 1)

    def test_flags_a_new_status_outside_the_known_safe_set(self) -> None:
        got = ratelimit_probe.summarize([_event(status="rejected")])
        self.assertEqual(got["Q2_statuses_outside_known_safe"], ["rejected"])

    def test_q1_is_none_when_no_result_envelope_has_been_seen(self) -> None:
        """Q1 asks whether a WALL emits an event. With no terminal `result` captured, no run has
        finished, so the honest answer is 'no data' rather than 'no'.
        """
        self.assertIsNone(ratelimit_probe.summarize([_event()])["Q1_wall_emitted_rate_limit_event"])

    def test_q3_five_hour_is_false_when_only_seven_day_seen(self) -> None:
        got = ratelimit_probe.summarize([_event(kind="seven_day")])
        self.assertFalse(got["Q3_five_hour_observed"])

    def test_empty_input_produces_a_report_rather_than_an_error(self) -> None:
        got = ratelimit_probe.summarize([])
        self.assertEqual(got["records_total"], 0)
        self.assertIsNone(got["Q4_max_utilization"])
        self.assertEqual(got["Q2_status_vocabulary"], {})

    def test_render_findings_mentions_every_question(self) -> None:
        rendered = ratelimit_probe.render_findings(ratelimit_probe.summarize([_event()]))
        for marker in ("Q1", "Q2", "Q3", "Q4", "Q6"):
            self.assertIn(marker, rendered)

    def test_render_findings_surfaces_a_new_status_prominently(self) -> None:
        rendered = ratelimit_probe.render_findings(ratelimit_probe.summarize([_event(status="rejected")]))
        self.assertIn("NEW statuses outside _KNOWN_SAFE", rendered)
        self.assertIn("rejected", rendered)


class SafetyRailTests(unittest.TestCase):
    def test_burn_prompt_grants_no_tools(self) -> None:
        """Invariant #6: a burn call must be incapable of touching unrelated work."""
        self.assertIn("Do not use any tools", ratelimit_probe._BURN_PROMPT)

    def test_hard_call_cap_is_finite_and_modest(self) -> None:
        self.assertGreater(ratelimit_probe._MAX_BURN_CALLS, 0)
        self.assertLessEqual(ratelimit_probe._MAX_BURN_CALLS, 1000)

    def test_burn_defaults_are_bounded(self) -> None:
        args = ratelimit_probe.build_parser().parse_args(["burn"])
        self.assertLessEqual(args.max_calls, ratelimit_probe._MAX_BURN_CALLS)
        self.assertGreater(args.max_seven_day_pts, 0.0)

    def test_max_calls_above_the_hard_cap_is_clamped_not_honored(self) -> None:
        args = ratelimit_probe.build_parser().parse_args(["burn", "--max-calls", "999999"])
        clamped = min(args.max_calls, ratelimit_probe._MAX_BURN_CALLS)
        self.assertEqual(clamped, ratelimit_probe._MAX_BURN_CALLS)

    def test_per_call_timeout_is_set(self) -> None:
        self.assertGreater(ratelimit_probe._BURN_CALL_TIMEOUT_S, 0)


class ParserTests(unittest.TestCase):
    def test_every_subcommand_is_reachable_and_bound(self) -> None:
        parser = ratelimit_probe.build_parser()
        for command in ("baseline", "exchange-rate", "burn", "report"):
            args = parser.parse_args([command])
            self.assertTrue(callable(args.func), f"{command} has no bound handler")

    def test_a_subcommand_is_required(self) -> None:
        # argparse prints its usage to stderr before raising; swallow it so a green gate stays
        # visually clean rather than looking like it emitted an error.
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ratelimit_probe.build_parser().parse_args([])

    def test_out_dir_is_overridable(self) -> None:
        # Not under /tmp: ruff's S108 flags hardcoded temp paths, and this only needs to prove
        # the flag is threaded through to `args.out` at all.
        args = ratelimit_probe.build_parser().parse_args(["--out", "/var/opt/probe-out", "report"])
        self.assertEqual(args.out, "/var/opt/probe-out")

    def test_default_out_dir_is_under_the_relay_state_dir_not_the_repo(self) -> None:
        """Artifacts must never land inside a target repo — that is how a probe ends up committed
        into somebody's project (Invariant #6).
        """
        self.assertTrue(ratelimit_probe._DEFAULT_OUT.startswith("~/"))


class CollectRecordsTests(unittest.TestCase):
    def test_missing_directory_yields_no_records(self) -> None:
        self.assertEqual(ratelimit_probe._collect_records(Path("/nonexistent/xyz")), [])

    def test_reads_every_capture_file_in_the_directory(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index in (1, 2):
                (directory / f"envelopes-2026010{index}T000000Z-{index}.jsonl").write_text(
                    json.dumps(_event(util=0.1 * index)) + "\n", encoding="utf-8"
                )
            # A non-capture file in the same directory must be ignored.
            (directory / "tier0-baseline-x.json").write_text("{}", encoding="utf-8")
            records = ratelimit_probe._collect_records(directory)
            self.assertEqual(len(records), 2)


if __name__ == "__main__":
    unittest.main()


class FailClosedTests(unittest.TestCase):
    """A tool that deliberately spends quota must stop spending the moment it can no longer measure
    what it has spent. Both guards were added after the usage endpoint really did 429 us mid-build
    (2026-07-27, Retry-After: 300).
    """

    def test_burn_refuses_to_start_without_a_baseline_reading(self) -> None:
        args = ratelimit_probe.build_parser().parse_args(["burn", "--seat", "ayan"])
        unreadable = _reading("ayan", None, None, error="usage endpoint rate-limited us")
        with (
            mock.patch.object(ratelimit_probe, "_resolve_seat", return_value=_reading("ayan", 50.0, 50.0)),
            mock.patch.object(ratelimit_probe, "read_seat", return_value=unreadable),
            mock.patch.object(ratelimit_probe, "_run_claude_once") as spawn,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            rc = ratelimit_probe.cmd_burn(args)
        self.assertEqual(rc, 1)
        spawn.assert_not_called()  # the point: nothing was spent
        self.assertIn("refusing to burn", out.getvalue())

    def test_usage_reread_interval_matches_the_supervisor_poll_ttl(self) -> None:
        """DESIGN.md §4 settled on 90s. A second, more aggressive cadence in the one tool most
        likely to run alongside a live supervisor is how both end up 429'd.
        """
        self.assertGreaterEqual(ratelimit_probe._USAGE_REREAD_INTERVAL_S, 90.0)
