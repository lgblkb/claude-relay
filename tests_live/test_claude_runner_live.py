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

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from relay import fleet, runner

# Where a failing attempt's evidence is preserved. The 2026-07-27 empty-tail sighting was
# undiagnosable for one reason only: this test ran with `log_dir` inside a `TemporaryDirectory`
# and asserted on `log_path.exists()` without ever reading the bytes, so the single artifact that
# could discriminate the candidate explanations was deleted microseconds before the failure was
# reported. Production does not have this problem (loop.py uses config.log_dir under
# ~/.claude-relay/logs, which persists), so a recurrence in a real run is already observable — this
# closes the gap for the test itself. Overridable so a CI box can redirect it.
_ARTIFACT_DIR_ENV = "CLAUDE_RELAY_LIVE_ARTIFACT_DIR"

# Bound on the excerpt inlined into the failure message. The full bytes always go to the artifact
# file; this only keeps a 20MB transcript from drowning the assertion output.
_EXCERPT_BYTES = 4000


def _artifact_dir() -> Path:
    override = os.environ.get(_ARTIFACT_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude-relay" / "live-test-artifacts"


def _oauth_clock(seat_dir: Path) -> dict[str, Any]:
    """The seat's OAuth *timestamps* — never its tokens (Invariant #5: no artifact this test writes
    may contain a secret, and these files hold live credentials). Only key NAMES and numeric
    expiries are extracted.

    This is the discriminator for the leading explanation of the empty-tail sighting: a launch is
    what refreshes a seat's token (DESIGN.md §0), so if the hypothesis holds, `expiresAt` was in
    the past before the run and moved afterwards. Sampling it either way turns a recurrence from an
    anecdote into a decidable observation.
    """
    path = seat_dir / ".credentials.json"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"readable": False, "error": type(exc).__name__}
    oauth = parsed.get("claudeAiOauth") if isinstance(parsed, dict) else None
    if not isinstance(oauth, dict):
        return {"readable": True, "oauth_block": False}
    clock: dict[str, Any] = {"readable": True, "oauth_block": True, "keys": sorted(oauth)}
    for field in ("expiresAt", "refreshTokenExpiresAt"):
        value = oauth.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            clock[field] = value
    try:
        clock["mtime"] = path.stat().st_mtime
    except OSError:  # pragma: no cover - best effort
        pass
    return clock


def _preserve(
    *,
    seat_name: str,
    log_bytes: bytes,
    result: runner.RunResult,
    oauth_before: dict[str, Any],
    oauth_after: dict[str, Any],
) -> Path:
    """Write a failing attempt's evidence somewhere that outlives the tempdir, and return the path.

    Two files: the child's raw stdout+stderr bytes verbatim, and a sidecar `.json` with the
    RunResult fields plus the before/after OAuth clock. Best-effort — a preservation failure must
    never mask the real assertion failure, so every error here is swallowed and reported as a path
    that simply says so.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = _artifact_dir() / f"claude-runner-live-{stamp}-{seat_name}"
    try:
        base.parent.mkdir(parents=True, exist_ok=True)
        log_copy = base.with_suffix(".log")
        log_copy.write_bytes(log_bytes)
        base.with_suffix(".json").write_text(
            json.dumps(
                {
                    "seat": seat_name,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "duration_s": result.duration_s,
                    "tail_lines": len(result.tail),
                    "tail": result.tail[:50],
                    "log_bytes": len(log_bytes),
                    "log_excerpt": log_bytes[:_EXCERPT_BYTES].decode("utf-8", errors="replace"),
                    "oauth_before": oauth_before,
                    "oauth_after": oauth_after,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return Path(f"<preservation failed: {type(exc).__name__}>")
    return log_copy


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
        # A recurrence is now self-diagnosing rather than anecdotal (`_preserve()` / `_oauth_clock()`):
        # the raw bytes survive the tempdir, and the before/after OAuth clock says whether a refresh
        # actually happened during the launch, which is the whole content of the hypothesis. Two other
        # candidates are already closed: stderr is merged into the same stream, so an empty log cannot
        # be a stream mix-up; and the runner's two kernel-pipe loss windows now drain on exit
        # (runner._drain_pipe, tests/test_runner.py::FinalPipeDrainTests) — those could produce this
        # exact signature, though at ~1e-4/run they are too rare to be a first-try sighting.
        #
        # Either way the subject under test is the RUNNER's contract — spawn, stream, tail, exit code
        # — not any particular seat's health, so one uncooperative seat must not turn this seam red.
        # A genuinely broken runner fails on every seat and still reports failure below.
        failures: list[str] = []
        artifacts: list[Path] = []
        for seat in usable:
            oauth_before = _oauth_clock(seat.path)
            with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as log_dir_str:
                result = runner.run(
                    ["-p", "reply with exactly the single word: pong"],
                    repo=Path(tmp),
                    config_dir=seat.path,
                    log_dir=Path(log_dir_str),
                    seat_name=seat.name,
                )
                log_existed = result.log_path.exists()
                # Read the BYTES, not just `exists()`, and do it inside the `with` — the logfile is
                # about to be deleted with the tempdir, and on the empty-tail path it is the only
                # evidence there is. `stderr` is merged into this same stream (runner.py's
                # `stderr=subprocess.STDOUT`), so an empty log means the child printed nothing on
                # EITHER stream, which is what rules out a stream mix-up as the explanation.
                log_bytes = result.log_path.read_bytes() if log_existed else b""
            oauth_after = _oauth_clock(seat.path)

            if result.returncode == 0 and log_existed and any("pong" in line.lower() for line in result.tail):
                return  # runner contract satisfied

            failures.append(
                f"seat {seat.name!r}: returncode={result.returncode} timed_out={result.timed_out} "
                f"duration_s={result.duration_s:.1f} log_exists={log_existed} "
                f"log_bytes={len(log_bytes)} tail_lines={len(result.tail)} "
                f"oauth_refreshed={oauth_before.get('expiresAt') != oauth_after.get('expiresAt')}"
            )
            artifacts.append(
                _preserve(
                    seat_name=seat.name,
                    log_bytes=log_bytes,
                    result=result,
                    oauth_before=oauth_before,
                    oauth_after=oauth_after,
                )
            )

        detail = "\n  ".join(failures)
        excerpts = "\n".join(f"  evidence: {path}" for path in artifacts)
        self.fail(f"no usable seat produced a well-formed run:\n  {detail}\n{excerpts}")


if __name__ == "__main__":
    unittest.main()
