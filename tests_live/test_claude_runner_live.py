"""LIVE test for the `LS-3-claude-subprocess` seam. Spawns
a REAL `claude` process (via `runner.run()`) against a real seat and a throwaway git repo —
no mock.

Classified `operator-receipt` (metered/interactive, not auto-verifiable): this is the highest
blast-radius seam claude-relay has — it consumes real usage quota and, with
`--dangerously-skip-permissions`, can execute real Bash/Write tool calls. It NEVER runs
unattended and requires explicit opt-in:
  CLAUDE_RELAY_LIVE_CLAUDE_RUN=1 python3 -m unittest tests_live.test_claude_runner_live -v

This is also where DESIGN.md's still-open questions must be settled for real (NOT during this
generation's implementation, per the task's explicit no-live-`claude -p` constraint):
  - whether a full ~15-20-agent `/gad-run` generation completes headlessly without hitting an
    undocumented per-turn ceiling (feasibility #4);
  - the exact effect of the `token_target` directive appended to the prompt.
This test only exercises the runner's OWN contract (spawn, stream, tail, exit code) with a
trivial one-line prompt — it deliberately does NOT attempt a real gad-kit generation (that
would burn a large amount of real usage quota just to run a unit test).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from relay import fleet, runner


class ClaudeRunnerLiveTest(unittest.TestCase):
    def test_trivial_headless_invocation(self) -> None:
        if os.environ.get("CLAUDE_RELAY_LIVE_CLAUDE_RUN") != "1":
            self.skipTest(
                "operator-receipt seam: set CLAUDE_RELAY_LIVE_CLAUDE_RUN=1 to actually spawn a "
                "real `claude` process (consumes real usage quota; never run automatically)"
            )
        seats = fleet.discover_seats()
        usable = [s for s in seats if s.usable]
        if not usable:
            self.skipTest("PROBE-SKIPPED: no usable seat on this box")

        # Try each usable seat rather than hard-coding `usable[0]`.
        #
        # 2026-07-27: this test failed against `usable[0]` and passed on retry moments later, with
        # the seat reporting returncode 0, a written logfile, and an EMPTY tail. The seat's five-hour
        # window had reset shortly before, after having been genuinely walled. The plausible cause is
        # the first-launch OAuth refresh (DESIGN.md §0: a `claude` launch is what refreshes a seat's
        # token) returning success with no output — but that is a HYPOTHESIS, not a confirmed finding:
        # reproducing it needs another freshly-unwalled seat, and it has been observed exactly once.
        #
        # Either way the subject under test is the RUNNER's contract — spawn, stream, tail, exit code
        # — not any particular seat's health, so one uncooperative seat must not turn this seam red.
        # A genuinely broken runner fails on every seat and still reports failure below.
        failures: list[str] = []
        for seat in usable:
            with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as log_dir_str:
                result = runner.run(
                    ["-p", "reply with exactly the single word: pong"],
                    repo=Path(tmp),
                    config_dir=seat.path,
                    log_dir=Path(log_dir_str),
                    seat_name=seat.name,
                )
                log_existed = result.log_path.exists()
            if result.returncode == 0 and log_existed and any("pong" in line.lower() for line in result.tail):
                return  # runner contract satisfied
            failures.append(
                f"seat {seat.name!r}: returncode={result.returncode} log_exists={log_existed} "
                f"tail_lines={len(result.tail)}"
            )

        self.fail("no usable seat produced a well-formed run:\n  " + "\n  ".join(failures))


if __name__ == "__main__":
    unittest.main()
