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
        seat = usable[0]

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with tempfile.TemporaryDirectory() as log_dir_str:
                log_dir = Path(log_dir_str)
                result = runner.run(
                    ["-p", "reply with exactly the single word: pong"],
                    repo=repo,
                    config_dir=seat.path,
                    log_dir=log_dir,
                    seat_name=seat.name,
                )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.log_path.exists())
        self.assertTrue(any("pong" in line.lower() for line in result.tail))


if __name__ == "__main__":
    unittest.main()
