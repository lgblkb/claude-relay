"""Spawns `claude -p ...` against a chosen seat, streams stdout to a per-run logfile, and
keeps a bounded in-memory tail for the loop's backstop wall-hit signal (detector.py). This is
the ONLY module that spawns the `claude` process itself (gadkit.py separately shells out to
`git` for repo introspection/recovery — a different, much narrower subprocess surface).

`claude` is invoked with `CLAUDE_CONFIG_DIR` set to the seat's directory — this is also what
refreshes that seat's OAuth token on launch (confirmed live, DESIGN.md §0), so no standalone
refresh flow is needed in Phase 1.

The child is started in its own process group (`start_new_session=True`) and given a generous
wall-clock timeout (`run_timeout_s`, config.py default 7200s / 2h). If it hangs past that, the
WHOLE process group is killed (`os.killpg`) — a single `claude -p` turn can itself spawn Bash
subprocesses; killing only the direct child would leak them. A timed-out run is reported as a
normal (if unsuccessful) `RunResult` with `timed_out=True` so the loop can react (rotate off
this seat rather than crash-loop forever on a hung session) rather than raising.

Read-loop design (2026-07-26 audit fix, B3/B4/B6): the previous implementation alternated
`select()` on the raw pipe fd with `stdout.readline()` on the `BufferedReader` `Popen` wraps
around it. That combination is unsound three independent ways:

  * `select()` readable means "the fd has *some* unread bytes," not "a full line is available."
    `readline()` has no timeout parameter, so once `select` says ready it can block indefinitely
    waiting for a `\\n` that may never arrive within the deadline (measured 28s overrun on an 8s
    deadline in a reproduction; unbounded if the child never completes the line at all) — the
    exact hung-session profile the timeout exists for (B3).
  * `select()` polls the RAW fd while `readline()` drains the WRAPPER's own internal ~8KiB
    buffer. A single `readline()` call can pull several already-arrived lines' worth of bytes out
    of the kernel pipe into that buffer while only returning the first `\\n`-terminated one — the
    rest sit invisible to `select()`'s next poll (it only sees the *kernel* buffer, which is now
    empty) until the child's *next* write makes the fd ready again. Worse, the `remaining <= 0`
    timeout branch breaks the loop with no attempt to drain that internal buffer at all, silently
    discarding up to ~8KiB of already-received output — precisely the tail end where a limit
    signature would be (B4).
  * Exit was detected via stdout EOF only; `process.poll()` was never consulted. A leaked
    grandchild that inherits the write end of the pipe (common with backgrounded Bash jobs) keeps
    the pipe open long after the direct child has actually exited, so a *successful* run pays the
    full `run_timeout_s` before this function notices anything (B6).

Fix: read the raw fd directly with non-blocking `os.read()` (bypassing the `BufferedReader`
entirely — never call `.readline()`/`.read()` on `process.stdout`, only `os.read()` on its raw
fd number, so kernel-buffer state and `select()`'s view of it never diverge) and split lines out
of the accumulated byte buffer ourselves (`_split_lines()`). Every `select()` wait is capped at
`_POLL_INTERVAL_S` regardless of the remaining deadline, so the loop re-checks BOTH the deadline
and `process.poll()` at least once a second even while the child is silent — closing B3 and B6
at once. Whatever is left in the buffer with no trailing newline (at EOF *or* at the deadline) is
flushed as a final partial line rather than discarded, closing B4.

B5 (no exception between `Popen` and the kill sites may orphan the process group — ENOSPC on the
log write, a `KeyboardInterrupt`, anything): the entire read loop is now wrapped in a
`try/finally` whose `finally` unconditionally checks `process.poll()` and kills the process group
if it is still alive, no matter how control leaves the `try` (normal break, timeout break, or an
exception unwinding through it). The exception itself still propagates — this only guarantees the
child is never left running unsupervised.
"""

from __future__ import annotations

import collections
import dataclasses
import os
import select
import signal
import stat
import subprocess
import time
from pathlib import Path

from . import capture, plugins

DEFAULT_TAIL_LINES = 200
DEFAULT_RUN_TIMEOUT_S = 7200.0

# How often the read loop re-checks the deadline and `process.poll()` even while the child is
# silent — this bounds how late a hang or a leaked-grandchild-holds-the-pipe exit is noticed
# (B3/B6), independent of how far away the real deadline is. Uncertainty a reviewer should
# check: hand-picked for responsiveness, not derived from any measured production incident.
_POLL_INTERVAL_S = 1.0

# Chunk size for each non-blocking `os.read()` off the raw pipe fd. Uncertainty a reviewer
# should check: hand-picked, not derived from any specific throughput requirement — a single
# `claude -p --output-format stream-json` transcript line COULD in principle exceed this (e.g. a
# huge `TaskOutput` result embedded in one envelope); correctness does not depend on it (the
# read loop just does one more `os.read()` + `_split_lines()` iteration to get the rest), so this
# is a performance tuning choice only.
_READ_CHUNK_BYTES = 65536

# Defensive cap (2026-07-26 audit): `buf` (the not-yet-newline-terminated remainder carried
# between `_split_lines()` calls) is otherwise UNBOUNDED — a child emitting an arbitrarily long
# line with no `\n` (a runaway progress bar, a single huge JSON envelope whose newline hasn't
# arrived yet) would grow it for the entire run, a memory vector with no ceiling. Once `buf`
# crosses this cap it is flushed as a synthetic (clearly tagged) line and reset, bounding worst-
# case growth to one cap's worth regardless of how long the real line turns out to be. Low risk
# in practice (real `stream-json` lines are one compact JSON object each, rarely anywhere near
# this size) but cheap to bound.
_MAX_UNTERMINATED_LINE_BYTES = 1_000_000

# How long, after the read loop ends (EOF or deadline), we give the process to actually exit
# before concluding it needs a SIGKILL. Parameterized (not just a literal `10`) so tests can
# exercise the exact same "EOF reached but the process itself hasn't exited yet" code path (the
# B6 mirror case: a child that closes stdout while continuing to work) without a real 10s wait.
_DEFAULT_REAP_GRACE_S = 10.0


def build_claude_argv(argv: list[str], plugin_dirs: list[str] | None = None) -> list[str]:
    """The full process argv: `claude [--plugin-dir <root> ...] <argv...>`. The `--plugin-dir`
    flags (resolved, absolute plugin roots) come FIRST so they apply to the whole session; they
    are empty by default (gad-kit needs none — see plugins.py). Pure/side-effect-free so it can
    be unit-tested and shown verbatim in `run --dry-run`.
    """
    return ["claude", *plugins.plugin_flags(plugin_dirs or []), *argv]


@dataclasses.dataclass(frozen=True)
class RunResult:
    returncode: int
    tail: list[str]  # last ~200 stdout lines (backstop signal only — never parsed for outcome)
    log_path: Path
    duration_s: float
    timed_out: bool = False


def _seat_env(base_env: dict[str, str], config_dir: Path) -> dict[str, str]:
    env = dict(base_env)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def build_log_path(log_dir: Path, seat_name: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return log_dir / f"run-{stamp}-{seat_name}.log"


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    """Kill the ENTIRE process group the child started (`start_new_session=True` made it the
    group leader), not just the direct child — a `claude -p` turn commonly spawns its own Bash
    subprocesses, which a plain `process.kill()` would leave orphaned/running.
    """
    try:
        pgid = os.getpgid(process.pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            process.kill()
        except OSError:  # pragma: no cover - process already gone
            pass


def _split_lines(buf: bytes, chunk: bytes) -> tuple[list[bytes], bytes]:
    """Append `chunk` to `buf` and split out every complete (`\\n`-terminated) line. Returns
    `(complete_lines, remainder)` — `remainder` has no trailing newline and is meant to be carried
    into the next call, or flushed as a final partial line once the caller knows no more data is
    coming (EOF or deadline — this is the B4 audit fix: that final partial line is exactly where
    a limit signature would sit, and the old implementation discarded it silently on timeout).
    Pure/side-effect-free so it can be unit-tested without spawning a process.
    """
    data = buf + chunk
    parts = data.split(b"\n")
    return parts[:-1], parts[-1]


def _emit_line(log_file, tail: collections.deque[str], raw: bytes, *, seat: str | None = None) -> None:  # noqa: ANN001
    """Decode one raw line (never raising on bad bytes — a non-UTF-8 locale in the child's
    output must not crash the supervisor), write it to the logfile, and record it in the
    in-memory tail. Module-level (not nested in `run()`) so tests can patch it to simulate a
    write failure partway through a real run, exercising the B5 kill-on-exception path.

    Also offers the line to the Tier-1 rate-limit capture tap (`capture.record_line()`). That call
    is a no-op unless `CLAUDE_RELAY_CAPTURE_DIR` is set, and it never raises — see relay/capture.py
    for why the tap lives on this path (it is the one place every child NDJSON line is already
    guaranteed to pass through) and why it can never break a run.

    `seat` is keyword-only WITH a default so the existing tests that patch this function with a
    3-positional-arg replacement keep working unchanged.
    """
    text = raw.decode("utf-8", errors="replace")
    log_file.write(text + "\n")
    log_file.flush()
    tail.append(text)
    capture.record_line(text, seat=seat)


def run(
    argv: list[str],
    *,
    repo: Path,
    config_dir: Path,
    log_dir: Path,
    seat_name: str,
    tail_lines: int = DEFAULT_TAIL_LINES,
    env: dict[str, str] | None = None,
    timeout_s: float | None = DEFAULT_RUN_TIMEOUT_S,
    plugin_dirs: list[str] | None = None,
    reap_grace_s: float = _DEFAULT_REAP_GRACE_S,
) -> RunResult:
    """Run `claude <argv...>` with cwd=`repo` and `CLAUDE_CONFIG_DIR=config_dir`. Blocks until
    the process exits OR `timeout_s` elapses (None disables the timeout — not recommended for
    unattended use). Streams every stdout line to a fresh logfile under `log_dir` AND keeps the
    last `tail_lines` lines in memory for the caller (loop.py passes this tail to
    detector.classify() as a backstop signal only — never authoritative, per Invariant #2).

    See the module docstring for the B3/B4/B5/B6 read-loop fix this implements.
    """
    log_path = build_log_path(log_dir, seat_name)
    tail: collections.deque[str] = collections.deque(maxlen=tail_lines)
    start = time.monotonic()
    deadline = start + timeout_s if timeout_s is not None else None

    process = subprocess.Popen(  # noqa: S603 - argv is built by gadkit.command(), not user shell input
        build_claude_argv(argv, plugin_dirs),
        cwd=str(repo),
        env=_seat_env(env if env is not None else dict(os.environ), config_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,  # unbuffered: we do our own line splitting on raw, non-blocking reads
        start_new_session=True,  # own process group -> killpg can reach any Bash grandchildren
    )

    if process.stdout is None:  # pragma: no cover - defensive; PIPE was requested above
        raise RuntimeError("subprocess.Popen did not attach a stdout pipe despite stdout=PIPE")

    stdout_fd = process.stdout.fileno()
    os.set_blocking(stdout_fd, False)

    timed_out = False
    buf = b""
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            while True:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    wait_for = min(remaining, _POLL_INTERVAL_S)
                else:
                    wait_for = _POLL_INTERVAL_S

                ready, _, _ = select.select([stdout_fd], [], [], wait_for)
                if not ready:
                    # No new bytes right now. If the direct child has already exited, whatever
                    # is still holding the pipe open is a leaked grandchild (B6), not the process
                    # we are supervising — stop waiting rather than burning the rest of the
                    # deadline on it.
                    if process.poll() is not None:
                        break
                    continue

                try:
                    chunk = os.read(stdout_fd, _READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if chunk == b"":  # EOF: the pipe's write end is fully closed
                    break
                lines, buf = _split_lines(buf, chunk)
                for raw_line in lines:
                    _emit_line(log_file, tail, raw_line, seat=seat_name)
                if len(buf) > _MAX_UNTERMINATED_LINE_BYTES:
                    # Defensive cap: flush the oversized unterminated remainder now rather than
                    # letting it grow without bound for the rest of the run (see the constant's
                    # docstring above). Clearly tagged so it is never mistaken for a genuine
                    # newline-terminated line.
                    _emit_line(
                        log_file,
                        tail,
                        buf + b" [claude-relay: line exceeded the unterminated-line cap, flushed early]",
                        seat=seat_name,
                    )
                    buf = b""

            if buf:  # final partial line with no trailing newline — do not discard it (B4)
                _emit_line(log_file, tail, buf, seat=seat_name)
                buf = b""

            if timed_out:
                note = f"[claude-relay] TIMEOUT after {timeout_s}s — process group killed"
                tail.append(note)
                log_file.write(note + "\n")
                # Deadline hit: kill right away, same as the pre-fix behavior — no reason to
                # wait out `reap_grace_s` below when we already know the child overran.
                _kill_process_group(process)
    except BaseException:
        # B5 safety net: an exception unwinding through the read loop above (ENOSPC on the log
        # write, a KeyboardInterrupt, anything else) must not leave the child — or its whole
        # process group — running unsupervised. Deliberately NOT a blanket `finally`: the normal,
        # no-exception exit path (including "EOF reached but the process hasn't exited yet," the
        # B6 mirror case) must fall through to the `process.wait(timeout=reap_grace_s)` grace
        # period below rather than being killed immediately here.
        if process.poll() is None:
            _kill_process_group(process)
        raise

    try:
        returncode = process.wait(timeout=reap_grace_s)
    except subprocess.TimeoutExpired:
        # The pipe reached EOF (or we hit the deadline) but the process itself has not actually
        # exited within the grace period — e.g. it closed stdout while continuing to work. This
        # IS the hung-session profile the timeout exists for even though we didn't take the
        # `timed_out` branch above (B6's mirror case) — report it as a timeout, not a bare `-9`
        # with no explanation.
        timed_out = True
        _kill_process_group(process)
        returncode = process.poll()
        if returncode is None:
            returncode = -9

    try:
        process.stdout.close()
    except OSError:  # pragma: no cover - best-effort fd hygiene
        pass

    try:
        os.chmod(log_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # pragma: no cover - best-effort on filesystems without POSIX perms
        pass

    return RunResult(
        returncode=returncode,
        tail=list(tail),
        log_path=log_path,
        duration_s=time.monotonic() - start,
        timed_out=timed_out,
    )
