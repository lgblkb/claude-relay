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

DEFAULT_TAIL_LINES = 200
DEFAULT_RUN_TIMEOUT_S = 7200.0


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
) -> RunResult:
    """Run `claude <argv...>` with cwd=`repo` and `CLAUDE_CONFIG_DIR=config_dir`. Blocks until
    the process exits OR `timeout_s` elapses (None disables the timeout — not recommended for
    unattended use). Streams every stdout line to a fresh logfile under `log_dir` AND keeps the
    last `tail_lines` lines in memory for the caller (loop.py passes this tail to
    detector.classify() as a backstop signal only — never authoritative, per Invariant #2).
    """
    log_path = build_log_path(log_dir, seat_name)
    tail: collections.deque[str] = collections.deque(maxlen=tail_lines)
    start = time.monotonic()
    deadline = start + timeout_s if timeout_s is not None else None

    process = subprocess.Popen(  # noqa: S603 - argv is built by gadkit.command(), not user shell input
        ["claude", *argv],
        cwd=str(repo),
        env=_seat_env(env if env is not None else dict(os.environ), config_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,  # own process group -> killpg can reach any Bash grandchildren
    )

    if process.stdout is None:  # pragma: no cover - defensive; PIPE was requested above
        raise RuntimeError("subprocess.Popen did not attach a stdout pipe despite stdout=PIPE")

    timed_out = False
    stdout = process.stdout
    with log_path.open("w", encoding="utf-8") as log_file:
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                # select() lets us poll for readability without blocking past the deadline;
                # readline() itself has no timeout parameter.
                ready, _, _ = select.select([stdout], [], [], min(remaining, 1.0))
                if not ready:
                    continue
            line = stdout.readline()
            if line == "":  # EOF: the process closed stdout (normal exit path)
                break
            log_file.write(line)
            log_file.flush()
            tail.append(line.rstrip("\n"))

        if timed_out:
            tail.append(f"[claude-relay] TIMEOUT after {timeout_s}s — process group killed")
            log_file.write(tail[-1] + "\n")
            _kill_process_group(process)

    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - only if SIGKILL itself didn't land
        _kill_process_group(process)
        returncode = process.poll()
        if returncode is None:
            returncode = -9

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
