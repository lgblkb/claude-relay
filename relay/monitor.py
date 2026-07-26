"""Phase 2 monitor: a read-only tmux cockpit for a running (or idle) claude-relay.

**Observe-only by design.** `monitor` NEVER launches a `claude-relay run` — you start the loop
yourself (another pane, another terminal, or SSH from your phone). This module only *visualizes*:

  - an all-seats usage table (left/main pane, auto-refreshing),
  - the live supervisor log — the newest per-run `claude -p` stream under `log_dir` (top-right),
  - the target repo's git log + `.gad/BUILD_STATUS.md` (bottom-right).

The usage table is **live + fallback**: each seat is polled live against the OAuth usage endpoint
via the same `usage.UsageCache` / `poll_ttl` discipline the loop uses (the endpoint self-rate-
limits ~5 min); a seat whose stored token has expired (`NeedsLoginError`) or that is momentarily
rate-limited/unreachable falls back to the last-known reading recorded in `state.json`, labeled
with its age. This process cannot share the loop's in-memory cache (the loop is a *separate*
process), so it keeps its own — and that is fine: correctness of *seat selection* lives entirely
in the loop's `pick_seat()` (which polls live at decision time). This is a dashboard, not an input
to any decision.

Everything network/tmux-facing is factored so the pure parts (row building against an injected
cache, table rendering, `tmux_commands()`) are unit-testable with no sockets and no tmux.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from relay import cooldown, fleet, loop
from relay import usage as usage_mod
from relay.config import Config

DEFAULT_SESSION = "claude-relay"
DEFAULT_SEATS_INTERVAL_S = 60.0
DEFAULT_REPO_INTERVAL_S = 30.0
_CLEAR = "\033[2J\033[3J\033[H"  # clear screen + scrollback + home cursor


class MonitorError(RuntimeError):
    """A precondition for the monitor is unmet (e.g. tmux is not installed)."""


# ─────────────────────────────────────────────────────────────────────────────
# The all-seats usage table (live + fallback)
# ─────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class SeatRow:
    name: str
    percent: float | None  # best-available 5h session percent
    resets_at: dt.datetime | None  # when the 5h window reopens
    weekly: float | None  # highest weekly-limit percent, if any
    state_label: str  # seat condition: needs-login / cooling Xm / ← latest run / idle
    source: str  # data provenance: "live" | "stale" | "none"
    age_s: float | None  # age of a "stale" reading (seconds); None for live/none


def _parse_dt(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed


def _max_weekly_percent(usage: usage_mod.UsageSnapshot) -> float | None:
    percents = [w.percent for w in usage_mod.weekly_limits(usage)]
    return max(percents) if percents else None


def _is_auth_error(exc: usage_mod.UsageError) -> bool:
    """Thin re-export: the shared predicate now lives in `usage.is_auth_error()` (B8 audit fix)
    so `loop.pick_seat()` can use the exact same logic — kept here under its original name so
    every existing call site in this module is unchanged.
    """
    return usage_mod.is_auth_error(exc)


def supervisor_liveness(config: Config) -> tuple[bool, int | None]:
    """`(is_alive, pid)` for the supervisor (`claude-relay run`) process, read-only — this
    module NEVER acquires the lock, only inspects it, using the SAME pid+starttime corroboration
    `SingleInstanceLock._is_stale()` applies internally rather than a bare `os.kill(pid, 0)`
    (which PID reuse can fool).

    B24 audit fix: the monitor's only liveness-adjacent signal used to be `latest_run_seat()`'s
    run-log mtime, which persists forever after a crash — a supervisor dead for hours renders
    IDENTICALLY to a healthy one (the same "← latest run" label, forever). Combined with B12 (an
    uncaught exception used to exit with zero notification), the operator had no signal at all
    that supervision had stopped. Returns `(False, None)` if no lockfile exists (never started,
    or a clean shutdown that released it) — that is a legitimate "not running," not an error.
    """
    lock_path = config.state_dir / "claude-relay.lock"
    lock = loop.SingleInstanceLock(lock_path)
    pid, recorded_starttime = lock._read_lock_contents()
    if pid is None:
        return False, None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, pid
    except PermissionError:
        return True, pid  # alive, just owned by someone else — can't corroborate further
    except OSError:  # pragma: no cover - platform without signal 0 support
        return False, pid
    if recorded_starttime is not None:
        current_starttime = loop._proc_start_time(pid)
        if current_starttime is not None and current_starttime != recorded_starttime:
            return False, pid  # PID reused by an unrelated process — the recorded one is dead
    return True, pid


def latest_run_seat(log_dir: Path) -> str | None:
    """The seat name of the most recent `run-<stamp>-<seat>.log` under `log_dir` — a best-effort
    "which seat is (or just was) running" marker, since the monitor is observe-only and has no
    other channel to the loop. Returns None if `log_dir` has no run logs yet.

    B24 audit fix note: this is a "most recent activity" marker, NOT a liveness signal — its
    underlying mtime persists forever after a crash. Use `supervisor_liveness()` for "is the
    supervisor actually still running right now."
    """
    newest = _newest_run_log(log_dir)
    if newest is None:
        return None
    stem = newest.name.removeprefix("run-").removesuffix(".log")
    # filename is run-<stamp>-<seat>.log; the stamp (%Y%m%dT%H%M%S) never contains a '-', so the
    # seat name is everything after the last '-'.
    _, sep, seat = stem.rpartition("-")
    return seat if sep else None


def _newest_run_log(log_dir: Path) -> Path | None:
    try:
        logs = [p for p in log_dir.glob("run-*.log") if p.is_file()]
    except OSError:
        return None
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime)


def build_seat_rows(
    config: Config,
    state: dict[str, Any],
    cache: usage_mod.UsageCache,
    *,
    now: dt.datetime | None = None,
    log_dir: Path | None = None,
) -> list[SeatRow]:
    """One row per discovered seat. Polls live (TTL-cached) for every seat that still has a
    token; on `NeedsLoginError` (expired token — cannot poll without a launch/refresh) or any
    other `UsageError` (rate-limit / network), falls back to `state.json`'s last-known reading.
    """
    now = now or dt.datetime.now(dt.UTC)
    log_dir = log_dir if log_dir is not None else config.log_dir
    seats = fleet.discover_seats(config.effective_exclude())
    disabled = cooldown.disabled_seats(state)
    latest = latest_run_seat(log_dir)
    rows: list[SeatRow] = []
    for seat in seats:
        entry = cooldown.get_seat_state(state, seat.path)
        cooling = cooldown.is_in_cooldown(state, seat.path, now=now)

        percent: float | None = None
        resets_at: dt.datetime | None = None
        weekly: float | None = None
        source = "none"
        age_s: float | None = None
        auth_expired = False

        if not seat.needs_login:
            try:
                snap = cache.poll(seat.path, ttl=config.poll_ttl)
                percent = usage_mod.session_percent(snap)
                resets_at = usage_mod.session_resets_at(snap)
                weekly = _max_weekly_percent(snap)
                source = "live"
            except usage_mod.UsageError as exc:
                auth_expired = _is_auth_error(exc)  # 401: token present but rejected

        if source != "live":
            last_percent = entry.get("lastPercent")
            if last_percent is not None:
                percent = float(last_percent)
                source = "stale"  # the % is last-known; a 401 is implied by its age
            elif auth_expired:
                source = "auth"  # no history AND can't poll: say why it's blank
            resets_at = _parse_dt(entry.get("lastResetsAt")) or resets_at
            seen = _parse_dt(entry.get("lastSeenAt"))
            if seen is not None:
                age_s = max(0.0, (now - seen).total_seconds())

        rows.append(
            SeatRow(
                name=seat.name,
                percent=percent,
                resets_at=resets_at,
                weekly=weekly,
                state_label=_state_label(
                    seat, cooling, entry, now, latest, disabled=seat.name in disabled
                ),
                source=source,
                age_s=age_s,
            )
        )
    return rows


def _state_label(
    seat: fleet.Seat,
    cooling: bool,
    entry: dict[str, Any],
    now: dt.datetime,
    latest: str | None,
    *,
    disabled: bool = False,
) -> str:
    if disabled:
        return "disabled"  # operator switched it off — pick_seat skips it regardless of the rest
    if seat.needs_login:
        return "needs-login"
    if cooling:
        until = _parse_dt(entry.get("cooldownUntil"))
        return f"cooling {_humanize_delta(until, now)}" if until else "cooling"
    if latest is not None and seat.name == latest:
        return "← latest run"
    return "idle"


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────


def _humanize_delta(target: dt.datetime | None, now: dt.datetime) -> str:
    if target is None:
        return "—"
    delta = (target - now).total_seconds()
    if delta <= 0:
        return "now"
    hours, rem = divmod(int(delta), 3600)
    minutes = rem // 60
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _humanize_age(age_s: float | None) -> str:
    if age_s is None:
        return "?"
    if age_s < 60:
        return "now"
    if age_s < 3600:
        return f"{int(age_s // 60)}m"
    if age_s < 86400:
        return f"{int(age_s // 3600)}h"
    return f"{int(age_s // 86400)}d"


def _bar(pct: float | None, width: int = 10) -> str:
    if pct is None:
        return " " * width
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _source_cell(row: SeatRow) -> str:
    if row.source == "live":
        return "live"
    if row.source == "stale":
        return f"stale·{_humanize_age(row.age_s)}"
    if row.source == "auth":
        return "auth?"
    return "—"


def render_seat_table(
    rows: Sequence[SeatRow],
    *,
    now: dt.datetime | None = None,
    supervisor: tuple[bool, int | None] | None = None,
) -> str:
    """Render the seat table as plain text (theme-agnostic, terminal-safe). Pure — no I/O.

    `supervisor` (when given — `supervisor_liveness()`'s return value) renders an explicit
    RUNNING/NOT RUNNING header line (B24 audit fix) so a dead supervisor cannot render
    identically to a healthy one just because the seat rows themselves still look plausible.
    """
    now = now or dt.datetime.now(dt.UTC)
    header = f"claude-relay · seats · {now.strftime('%H:%M:%SZ')}"
    lines = [header]
    if supervisor is not None:
        is_alive, pid = supervisor
        if is_alive:
            lines.append(f"supervisor: RUNNING (pid {pid})")
        elif pid is not None:
            lines.append(f"supervisor: NOT RUNNING — last known pid {pid} is dead")
        else:
            lines.append("supervisor: NOT RUNNING — no lockfile found")
    cols = f"{'SEAT':<8} {'5H%':>4} {'':10} {'RESET':>7} {'WEEK':>5} {'STATE':<14} SRC"
    lines += ["─" * len(cols), cols]
    if not rows:
        lines.append("(no seats discovered — looked for ~/.claude-* dirs)")
    for row in rows:
        pct = f"{row.percent:.0f}%" if row.percent is not None else "—"
        week = f"{row.weekly:.0f}%" if row.weekly is not None else "—"
        reset = _humanize_delta(row.resets_at, now)
        lines.append(
            f"{row.name:<8} {pct:>4} {_bar(row.percent)} {reset:>7} "
            f"{week:>5} {row.state_label:<14} {_source_cell(row)}"
        )
    lines.append("")
    lines.append(
        "observe-only · live=polled now · stale·N=last-known N ago · auth?=token expired (a run refreshes it)"
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Panel loops (each runs in its own tmux pane)
# ─────────────────────────────────────────────────────────────────────────────


def _emit(out: TextIO, text: str, *, clear: bool) -> None:
    out.write((_CLEAR if clear else "") + text + "\n")
    out.flush()


def run_seats_panel(
    config: Config,
    *,
    interval: float = DEFAULT_SEATS_INTERVAL_S,
    once: bool = False,
    out: TextIO | None = None,
) -> None:
    """The usage-table pane: re-reads state.json (the loop, a separate process, updates it) and
    re-renders every `interval` seconds. `once` prints a single snapshot (great over SSH).
    """
    out = out or sys.stdout
    cache = usage_mod.UsageCache()
    try:
        while True:
            state = cooldown.load_state(config.state_path)
            rows = build_seat_rows(config, state, cache, log_dir=config.log_dir)
            supervisor = supervisor_liveness(config)
            _emit(out, render_seat_table(rows, supervisor=supervisor), clear=not once)
            if once:
                return
            time.sleep(interval)
    except KeyboardInterrupt:  # clean Ctrl-C in the pane
        return


def run_repo_panel(
    repo: Path | None,
    *,
    interval: float = DEFAULT_REPO_INTERVAL_S,
    out: TextIO | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """The repo pane: git log + `.gad/BUILD_STATUS.md`, refreshed every `interval` seconds."""
    out = out or sys.stdout
    try:
        while True:
            _emit(out, render_repo_status(repo, runner=runner), clear=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        return


def render_repo_status(
    repo: Path | None,
    *,
    now: dt.datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    max_status_lines: int = 40,
) -> str:
    now = now or dt.datetime.now(dt.UTC)
    if repo is None:
        return "no repo configured (pass --repo or set `repo` in config.toml)"
    lines = [f"claude-relay · {repo.name} · {now.strftime('%H:%M:%SZ')}", ""]
    lines.append("── git log ──")
    try:
        proc = runner(
            ["git", "-C", str(repo), "log", "--oneline", "-n", "15"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines.append(proc.stdout.rstrip() or "(no commits yet)")
    except (OSError, subprocess.SubprocessError) as exc:
        lines.append(f"(git unavailable: {exc})")
    status = repo / ".gad" / "BUILD_STATUS.md"
    lines.append("")
    lines.append("── .gad/BUILD_STATUS.md ──")
    try:
        body = status.read_text(encoding="utf-8").splitlines()
        lines.extend(body[:max_status_lines] or ["(empty)"])
    except OSError:
        lines.append("(no .gad/BUILD_STATUS.md — repo not gad-bootstrapped, or no run yet)")
    return "\n".join(lines)


def run_log_panel(log_dir: Path, *, out: TextIO | None = None, poll_s: float = 1.0) -> None:
    """The supervisor-log pane: a `tail -F` that always follows the NEWEST `run-*.log` (each
    `claude -p` generation writes its own), switching automatically when a newer file appears.
    """
    out = out or sys.stdout
    current: Path | None = None
    pos = 0
    out.write("claude-relay · supervisor log · waiting for the first generation to start…\n")
    out.flush()
    try:
        while True:
            newest = _newest_run_log(log_dir)
            if newest is None:
                time.sleep(poll_s)
                continue
            if newest != current:
                current, pos = newest, 0
                out.write(f"\n── following {newest.name} ──\n")
                out.flush()
            try:
                with newest.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
            except OSError:
                time.sleep(poll_s)
                continue
            if chunk:
                out.write(chunk)
                out.flush()
            time.sleep(poll_s)
    except KeyboardInterrupt:
        return


# ─────────────────────────────────────────────────────────────────────────────
# tmux orchestration (observe-only: builds panes, never launches `run`)
# ─────────────────────────────────────────────────────────────────────────────


def tmux_commands(
    *,
    cli: Sequence[str],
    config_path: Path | None,
    repo: Path | None,
    session: str,
    interval: float,
) -> list[list[str]]:
    """The tmux argv lists that build a detached 3-pane session. Pure (no execution) so it is
    unit-testable. Layout: supervisor log = left column (~52%), seats table = top-right, repo
    status = bottom-right, built with *percentage* splits (not `main-vertical`, whose fixed
    absolute main-pane-width collapses the right column on narrow windows). The detached session
    is created at a generous 220×50 so panes are usable even headless; attaching resizes it to the
    real terminal and the percentage splits keep their proportions. `cli` is the invocation prefix
    for claude-relay (e.g. `[sys.executable, "/abs/bin/claude-relay"]`).
    """

    def panel(*args: str) -> str:
        argv = list(cli)
        if config_path is not None:
            argv += ["--config", str(config_path)]
        argv += list(args)
        return " ".join(shlex.quote(tok) for tok in argv)

    log_cmd = panel("_panel", "log")
    seats_cmd = panel("seats", "--watch", str(int(interval)))
    repo_args = ["_panel", "repo"] + (["--repo", str(repo)] if repo is not None else [])
    repo_cmd = panel(*repo_args)

    target = f"{session}:0"
    return [
        ["tmux", "new-session", "-d", "-s", session, "-x", "220", "-y", "50", log_cmd],
        ["tmux", "split-window", "-h", "-l", "48%", "-t", target, seats_cmd],
        ["tmux", "split-window", "-v", "-l", "50%", "-t", f"{target}.1", repo_cmd],
        ["tmux", "select-pane", "-t", f"{target}.0"],
    ]


def default_cli() -> list[str]:
    """How a tmux pane should re-invoke this CLI: the same interpreter + the resolved script."""
    script = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else "claude-relay"
    return [sys.executable, script]


def launch(
    config: Config,
    repo: Path | None,
    *,
    session: str = DEFAULT_SESSION,
    interval: float = DEFAULT_SEATS_INTERVAL_S,
    config_path: Path | None = None,
    attach: bool = True,
    cli: Sequence[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Create (if absent) and attach the monitor tmux session. Idempotent: an existing session
    of the same name is reused, not rebuilt. Raises `MonitorError` if tmux is not installed.
    """
    if shutil.which("tmux") is None:
        raise MonitorError("tmux is not on PATH — install tmux, or run `claude-relay seats --watch`")
    cli = cli or default_cli()
    exists = runner(["tmux", "has-session", "-t", session], capture_output=True, text=True).returncode == 0
    if not exists:
        for cmd in tmux_commands(
            cli=cli, config_path=config_path, repo=repo, session=session, interval=interval
        ):
            runner(cmd, check=True)
    if attach:
        verb = "switch-client" if os.environ.get("TMUX") else "attach-session"
        os.execvp("tmux", ["tmux", verb, "-t", session])  # noqa: S606 (fixed program+argv, no shell)
    return session
