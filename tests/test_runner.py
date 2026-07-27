"""Tests for `relay.runner`'s read loop: the B3/B4/B5/B6 audit-fix cluster (2026-07-26).

`_split_lines` is tested as a pure function. Everything else here spawns a REAL child process
(a tiny shell script standing in for `claude`, placed first on `PATH`) rather than mocking
`subprocess.Popen` — these bugs are specifically about the interaction between `select()`, the
kernel pipe buffer, and process/process-group lifecycle, which a mock cannot reproduce faithfully.
Each script is short-lived and bounded so the suite stays fast.
"""

from __future__ import annotations

import os
import select
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from relay import runner as runner_mod


def _write_fake_claude(bin_dir: Path, script_body: str) -> Path:
    """Write an executable `claude` script into `bin_dir` (which the caller has already put
    first on `PATH`), standing in for the real CLI so tests control timing precisely.
    """
    path = bin_dir / "claude"
    path.write_text(f"#!/bin/sh\n{script_body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class SplitLinesTests(unittest.TestCase):
    """Pure-function coverage for the line-splitting logic the read loop relies on."""

    def test_single_complete_line(self) -> None:
        lines, remainder = runner_mod._split_lines(b"", b"hello\n")
        self.assertEqual(lines, [b"hello"])
        self.assertEqual(remainder, b"")

    def test_partial_line_carried_as_remainder(self) -> None:
        lines, remainder = runner_mod._split_lines(b"", b"no newline yet")
        self.assertEqual(lines, [])
        self.assertEqual(remainder, b"no newline yet")

    def test_remainder_is_combined_with_the_next_chunk(self) -> None:
        lines, remainder = runner_mod._split_lines(b"no newline yet", b" - now it ends\n")
        self.assertEqual(lines, [b"no newline yet - now it ends"])
        self.assertEqual(remainder, b"")

    def test_multiple_lines_in_one_chunk(self) -> None:
        lines, remainder = runner_mod._split_lines(b"", b"one\ntwo\nthree\n")
        self.assertEqual(lines, [b"one", b"two", b"three"])
        self.assertEqual(remainder, b"")

    def test_multiple_lines_plus_a_trailing_partial(self) -> None:
        lines, remainder = runner_mod._split_lines(b"", b"one\ntwo\npart")
        self.assertEqual(lines, [b"one", b"two"])
        self.assertEqual(remainder, b"part")

    def test_empty_chunk_is_a_noop(self) -> None:
        lines, remainder = runner_mod._split_lines(b"carried", b"")
        self.assertEqual(lines, [])
        self.assertEqual(remainder, b"carried")


class _FakeClaudeTestCase(unittest.TestCase):
    """Common scaffolding: a temp `bin/` on PATH first, a temp repo/log/config dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.repo = root / "repo"
        self.repo.mkdir()
        self.log_dir = root / "logs"
        self.config_dir = root / "config"
        self.config_dir.mkdir()

    def _env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        env.update(extra)
        return env

    def _run(self, script_body: str, **kwargs) -> runner_mod.RunResult:  # noqa: ANN003
        _write_fake_claude(self.bin_dir, script_body)
        env = kwargs.pop("env", None) or self._env()
        return runner_mod.run(
            [],
            repo=self.repo,
            config_dir=self.config_dir,
            log_dir=self.log_dir,
            seat_name="test-seat",
            env=env,
            **kwargs,
        )


class NormalExitTests(_FakeClaudeTestCase):
    def test_ordinary_lines_are_captured_in_order(self) -> None:
        result = self._run('echo "line one"\necho "line two"\nexit 0')
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.tail, ["line one", "line two"])

    def test_log_file_contains_the_same_lines(self) -> None:
        result = self._run('echo "hello"\nexit 0')
        self.assertEqual(result.log_path.read_text(encoding="utf-8").splitlines(), ["hello"])

    def test_nonzero_exit_code_is_reported(self) -> None:
        result = self._run("exit 7")
        self.assertEqual(result.returncode, 7)
        self.assertFalse(result.timed_out)


class B3DeadlineOverrunTests(_FakeClaudeTestCase):
    """B3: `select()` readable must not let a subsequent blocking read overrun the deadline."""

    def test_a_hung_child_that_never_completes_a_line_does_not_overrun_the_deadline(self) -> None:
        # Prints a partial line (no trailing newline, ever) then sleeps far past the deadline.
        started = time.monotonic()
        result = self._run(
            "printf 'partial-no-newline'\nsleep 30",
            timeout_s=1.0,
            reap_grace_s=0.3,
        )
        elapsed = time.monotonic() - started
        self.assertTrue(result.timed_out)
        # The old readline()-based loop measured a 28s overrun on an 8s deadline; bound this
        # generously (deadline + one poll interval + reap grace + slack) so the test is robust
        # to CI scheduling jitter while still failing hard on a regression to unbounded blocking.
        self.assertLess(elapsed, 1.0 + runner_mod._POLL_INTERVAL_S + 0.3 + 2.0)

    def test_b4_partial_line_with_no_trailing_newline_is_not_discarded_on_timeout(self) -> None:
        """The exact B4 regression: the old loop's `remaining <= 0` break exited with no drain,
        discarding whatever was sitting in the wrapper's internal buffer — up to ~8KiB, the LAST
        bytes received, where a limit signature would be."""
        result = self._run(
            "printf 'RESULT: closed window, no newline follows'\nsleep 30",
            timeout_s=1.0,
            reap_grace_s=0.3,
        )
        self.assertTrue(result.timed_out)
        self.assertIn("RESULT: closed window, no newline follows", result.tail)


class B6EarlyExitDetectionTests(_FakeClaudeTestCase):
    """B6: exit must be detected via `process.poll()`, not stdout EOF alone."""

    def test_a_leaked_grandchild_holding_the_pipe_open_does_not_cost_the_full_deadline(self) -> None:
        # The direct child exits almost immediately; a detached grandchild inherits the write end
        # of the pipe and keeps it open for a few seconds. The old EOF-only loop would wait for
        # the pipe to actually close (or the deadline) before noticing the direct child was done.
        started = time.monotonic()
        result = self._run(
            '( sleep 2 >&1 & )\necho "done"\nexit 0',
            timeout_s=30.0,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.tail, ["done"])
        # Must return promptly via the poll()-detects-exit path, nowhere near the 30s deadline
        # or even the grandchild's own 2s hold.
        self.assertLess(elapsed, 2.0)

    def test_a_child_that_closes_stdout_but_keeps_running_is_reported_as_timed_out(self) -> None:
        """The B6 mirror case: closing stdout reaches EOF on our end (the read loop exits
        normally, `timed_out` stays False there), but the process itself is still alive. The old
        code reported a bare `returncode=-9` with `timed_out=False` — indistinguishable from an
        ordinary SIGKILL death with no explanation. `reap_grace_s` lets this test exercise the
        exact same code path as the real 10s grace period without a real 10s wait.
        """
        result = self._run(
            # Close BOTH fd 1 and fd 2 — `stderr=STDOUT` dup's the same pipe write-end onto both,
            # so closing only fd 1 would leave fd 2 still holding the pipe open (no real EOF).
            "exec 1>&- 2>&-\nsleep 30",
            timeout_s=30.0,
            reap_grace_s=0.2,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, -9)


class B5ProcessGroupOrphanTests(_FakeClaudeTestCase):
    """B5: no exception raised while draining output may leave the child (or its process group)
    running unsupervised."""

    def test_an_exception_raised_mid_read_loop_still_kills_the_process_group(self) -> None:
        heartbeat = Path(self._tmp.name) / "heartbeat"
        script = (
            'echo "one line before boom"\n'
            "i=0\n"
            "while true; do\n"
            '  i=$((i+1))\n'
            f'  echo "$i" >> "{heartbeat}"\n'
            "  sleep 0.05\n"
            "done\n"
        )
        _write_fake_claude(self.bin_dir, script)

        with mock.patch.object(runner_mod, "_emit_line", side_effect=OSError("simulated ENOSPC")):
            with self.assertRaises(OSError):
                runner_mod.run(
                    [],
                    repo=self.repo,
                    config_dir=self.config_dir,
                    log_dir=self.log_dir,
                    seat_name="test-seat",
                    env=self._env(),
                    timeout_s=30.0,
                )

        # Give the SIGKILL a brief moment to land, then confirm the heartbeat has genuinely
        # stopped growing — i.e. the process was actually terminated, not orphaned to keep
        # running in the background after `run()` raised.
        time.sleep(0.3)
        size_after_kill = heartbeat.stat().st_size if heartbeat.exists() else 0
        time.sleep(0.4)
        size_later = heartbeat.stat().st_size if heartbeat.exists() else 0
        self.assertEqual(
            size_after_kill,
            size_later,
            "heartbeat file kept growing after run() raised — the child was not killed (B5)",
        )


class MultiByteUtf8SplitAcrossChunksTests(_FakeClaudeTestCase):
    """Reviewer-verified-correct behavior with no regression test pinning it (2026-07-26
    review): a multi-byte UTF-8 character whose bytes are split across two SEPARATE `os.read()`
    calls (a real chunk boundary from two distinct child writes, not merely a `_split_lines()`
    unit-test artifact with two pre-chopped byte strings) must be reassembled and decoded
    correctly, never mangled or replaced. Correct by construction: `buf` accumulates RAW bytes
    across reads and is only ever `.decode()`d once a complete newline-terminated line (or a
    final EOF/timeout partial) is available — `_emit_line()` never runs on a fragment.
    """

    def test_a_character_split_across_two_separate_reads_decodes_correctly(self) -> None:
        # "日" (U+65E5) UTF-8-encodes as three bytes: 0xE6 0x97 0xA5 (octal \346 \227 \245 — the
        # portable `printf` escape `/bin/sh` (dash) actually supports; its `\xHH` form is NOT
        # interpreted by dash, confirmed empirically). Emit the first two bytes, sleep (forcing
        # a genuinely SEPARATE os.read() for the rest, not just two args to `_split_lines()`),
        # then the third byte plus the rest of the line and its newline.
        result = self._run(
            "printf '\\346\\227'\nsleep 0.2\nprintf '\\245 rest of line\\n'\nexit 0\n",
            timeout_s=5.0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.tail, ["日 rest of line"])

    def test_a_multi_line_transcript_with_a_split_character_mid_stream_stays_correct(self) -> None:
        """The split character is not necessarily the FIRST or LAST line — pin that ordinary
        lines before and after an in-flight split are unaffected."""
        result = self._run(
            'echo "before"\n'
            "printf 'jp: \\346\\227'\nsleep 0.2\nprintf '\\245\\n'\n"
            'echo "after"\n'
            "exit 0\n",
            timeout_s=5.0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.tail, ["before", "jp: 日", "after"])


class UnterminatedLineCapTests(_FakeClaudeTestCase):
    """Defensive cap (2026-07-26 audit): a child emitting an arbitrarily long line with no
    trailing newline must not grow `buf` without bound — a cheap memory-vector fix, low risk in
    practice (real `stream-json` lines are one compact JSON object each)."""

    def test_an_unterminated_line_past_the_cap_is_flushed_early_not_held_forever(self) -> None:
        with mock.patch.object(runner_mod, "_MAX_UNTERMINATED_LINE_BYTES", 100):
            result = self._run(
                "printf '%0.sA' $(seq 1 250)\nsleep 0.3\necho\nexit 0\n",
                timeout_s=5.0,
            )
        self.assertEqual(result.returncode, 0)
        # Flushed in more than one piece (the cap forced an early emit before the real newline
        # arrived) — not one single 250-byte line.
        self.assertGreater(len(result.tail), 1)
        self.assertTrue(any("cap" in line.lower() for line in result.tail), result.tail)
        # No bytes were silently dropped: every emitted 'A' across all flushed pieces adds up
        # (the trailing final empty `echo` line does not contribute any 'A's).
        total_a_count = sum(line.count("A") for line in result.tail)
        self.assertEqual(total_a_count, 250)


class FinalPipeDrainTests(_FakeClaudeTestCase):
    """2026-07-27 audit: two of the read loop's `break`s exit with bytes still unread in the
    KERNEL pipe, discarding the last output the child produced (`_drain_pipe()`'s docstring has
    the full analysis). B4's fix covered our own `buf` and the `BufferedReader`'s buffer; neither
    reaches the kernel pipe, so this is the same loss one layer further out.

    Motivation is a real observation, not a hypothetical: a live run once returned `returncode 0`
    with a written logfile and an EMPTY tail (tests_live/test_claude_runner_live.py). This is not
    the confirmed cause — the odds are far too low (order 1e-4/run) for a first-try sighting, and
    the leading explanation remains a first-launch OAuth refresh — but it IS an independent way to
    produce that exact signature, so it gets closed on its own merits rather than left as a
    candidate explanation for a flake.
    """

    def test_drain_pipe_recovers_bytes_already_sitting_in_the_pipe(self) -> None:
        """`_drain_pipe()` in isolation, on a real pipe: it returns buffered bytes, stops at
        not-ready instead of blocking, and honors its byte cap."""
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(read_fd))
        os.set_blocking(read_fd, False)
        os.write(write_fd, b"alpha\nbeta\npart")

        self.assertEqual(runner_mod._drain_pipe(read_fd), b"alpha\nbeta\npart")
        # Pipe is empty now and the write end is still OPEN, so there is no EOF to detect — a
        # blocking implementation would hang here instead of returning promptly.
        self.assertEqual(runner_mod._drain_pipe(read_fd), b"")

        os.write(write_fd, b"x" * 5000)
        capped = runner_mod._drain_pipe(read_fd, max_bytes=10)
        self.assertGreater(len(capped), 0)
        # One `os.read()` of up to _READ_CHUNK_BYTES may overshoot a small cap; the contract is
        # that it STOPS, not that it lands exactly on the boundary.
        self.assertLessEqual(len(capped), runner_mod._READ_CHUNK_BYTES)
        os.close(write_fd)

    def test_a_closed_fd_drains_to_empty_rather_than_raising(self) -> None:
        """Never raises: a drain failure must not turn a completed run into an exception."""
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        os.close(read_fd)
        self.assertEqual(runner_mod._drain_pipe(read_fd), b"")

    def _run_with_stale_select(self, script_body: str, **kwargs) -> runner_mod.RunResult:  # noqa: ANN003
        """Run a real child, but force the read loop's `select()` to report not-ready from the
        moment the child has produced its output onward — the state both loss windows share: bytes
        sitting in the pipe while the loop believes the fd is empty.

        Forced rather than raced because the real window is microseconds against a 1s poll
        interval: reproducing it by timing alone would need thousands of runs and still be flaky.
        The child, the pipe, the process group and the lifecycle are all real — only `select`'s
        answer is stubbed, and only for the loop's own polls.
        """
        _write_fake_claude(self.bin_dir, script_body)
        real_select = select.select
        state = {"settled": False}

        def fake_select(rlist, wlist, xlist, timeout=None):  # noqa: ANN001, ANN202
            # `_drain_pipe()` is the code under test and polls with `timeout=0` — that is what
            # makes it strictly non-blocking — so those calls must reach the REAL `select`.
            # Stubbing them too would disable the fix and make the test pass for the wrong reason.
            # Learned the hard way: the first draft patched both and reproduced the empty tail
            # WITH the fix in place.
            if timeout == 0:
                return real_select(rlist, wlist, xlist, timeout)
            if not state["settled"]:
                time.sleep(0.5)  # let the child write (and, where scripted, exit)
                state["settled"] = True
            return ([], [], [])

        with mock.patch.object(runner_mod.select, "select", fake_select):
            result = runner_mod.run(
                [],
                repo=self.repo,
                config_dir=self.config_dir,
                log_dir=self.log_dir,
                seat_name="test-seat",
                env=self._env(),
                **kwargs,
            )
        self.assertIs(select.select, real_select)  # patch fully undone
        return result

    def test_output_arriving_in_the_not_ready_then_exited_window_is_not_lost(self) -> None:
        """Window 1 — the not-ready-then-exited branch: the loop learns the fd was empty at that
        instant, then finds `poll()` non-None and breaks straight out, while the child's bytes are
        already in the pipe. Pre-fix this reports SUCCESS with an EMPTY tail — the exact signature
        of the live observation that motivated the audit.
        """
        result = self._run_with_stale_select(
            'echo "first"\necho "LIMIT SIGNATURE"\nexit 0',
            timeout_s=5.0,
            reap_grace_s=0.5,
        )

        # Exited on its own (not killed) => the loop left via the poll()-detects-exit branch.
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.tail, ["first", "LIMIT SIGNATURE"])
        self.assertEqual(
            result.log_path.read_text(encoding="utf-8").splitlines(),
            ["first", "LIMIT SIGNATURE"],
        )

    def test_output_written_before_the_deadline_break_is_not_lost(self) -> None:
        """Window 2 — the deadline branch, which is NOT rare: every timed-out run takes it.
        `remaining <= 0` is checked at the TOP of the loop, so bytes that arrived since the last
        `os.read()` are still in the pipe when it breaks. A timed-out run is exactly when the tail
        matters most — it is the evidence for why the session hung.

        The child stays alive (`sleep 30`, stdout never closed), so `poll()` stays None and the
        deadline is the ONLY way out of the loop; `timed_out` below confirms which branch ran.
        """
        result = self._run_with_stale_select(
            'echo "RESULT: last line before the hang"\nsleep 30',
            timeout_s=1.0,
            reap_grace_s=0.3,
        )

        self.assertTrue(result.timed_out)
        self.assertIn("RESULT: last line before the hang", result.tail)
        # Ordering contract: the child's real output precedes our synthetic TIMEOUT annotation.
        self.assertTrue(result.tail[-1].startswith("[claude-relay] TIMEOUT"), result.tail)


if __name__ == "__main__":
    unittest.main()
