"""Offline tests for relay.monitor: the live+fallback seat-row builder (live poll, needs-login
fallback, rate-limit/network fallback, and no-data), table + repo rendering, the reset/age
humanizers, latest-run-seat parsing, and the pure tmux_commands() builder. No network, no tmux:
the usage cache is a fake that returns canned snapshots or raises, and tmux is never executed.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from relay import cooldown, fleet, monitor
from relay import usage as usage_mod
from relay.config import Config

NOW = dt.datetime(2026, 7, 19, 12, 0, 0, tzinfo=dt.UTC)


def _seat(name: str, *, needs_login: bool = False) -> fleet.Seat:
    return fleet.Seat(
        name=name,
        path=Path(f"/fake/.claude-{name}"),
        has_creds=not needs_login,
        needs_login=needs_login,
    )


def _usage(
    percent: float, *, resets_in_s: float = 2 * 3600, weekly: float | None = None
) -> usage_mod.UsageSnapshot:
    when = (NOW + dt.timedelta(seconds=resets_in_s)).isoformat()
    obj: dict = {"five_hour": {"utilization": percent, "resets_at": when}}
    if weekly is not None:
        obj["limits"] = [{"kind": "weekly_all", "percent": weekly, "is_active": True}]
    return usage_mod.UsageSnapshot.from_json(obj, fetched_at=time.time())


class _FakeCache:
    """Returns a canned snapshot per seat path, or raises a canned UsageError for it."""

    def __init__(self, readings: dict[str, object]):
        self._readings = readings

    def poll(self, seat_dir, ttl: float = 90.0, **_kw):  # noqa: ANN001
        val = self._readings[str(seat_dir)]
        if isinstance(val, Exception):
            raise val
        return val


class BuildSeatRowsTests(unittest.TestCase):
    def _rows(self, seats, cache, state):
        with mock.patch.object(monitor.fleet, "discover_seats", return_value=seats):
            return monitor.build_seat_rows(Config(), state, cache, now=NOW, log_dir=Path("/no/logs"))

    def test_live_seat_uses_polled_reading(self) -> None:
        seat = _seat("almas")
        cache = _FakeCache({str(seat.path): _usage(28.0, resets_in_s=90 * 60, weekly=11.0)})
        (row,) = self._rows([seat], cache, cooldown.load_state(Path("/none")))
        self.assertEqual(row.source, "live")
        self.assertEqual(row.percent, 28.0)
        self.assertEqual(row.weekly, 11.0)
        self.assertEqual(monitor._humanize_delta(row.resets_at, NOW), "1h30m")

    def test_needs_login_seat_falls_back_to_state_json_with_age(self) -> None:
        seat = _seat("ayan", needs_login=True)
        state = cooldown.load_state(Path("/none"))
        seen = (NOW - dt.timedelta(hours=2)).isoformat()
        cooldown.update_seat(
            state, seat.path, last_percent=18.0,
            last_resets_at=(NOW + dt.timedelta(minutes=51)).isoformat(), last_seen_at=seen,
        )
        # needs-login seat is never polled, so its path need not be in the cache at all
        (row,) = self._rows([seat], _FakeCache({}), state)
        self.assertEqual(row.source, "stale")
        self.assertEqual(row.percent, 18.0)
        self.assertEqual(row.state_label, "needs-login")
        self.assertAlmostEqual(row.age_s, 2 * 3600, delta=1)
        self.assertEqual(monitor._humanize_age(row.age_s), "2h")

    def test_rate_limited_live_poll_falls_back_to_last_known(self) -> None:
        seat = _seat("sam")
        state = cooldown.load_state(Path("/none"))
        cooldown.update_seat(
            state, seat.path, last_percent=43.0,
            last_seen_at=(NOW - dt.timedelta(minutes=5)).isoformat(),
        )
        cache = _FakeCache({str(seat.path): usage_mod.RateLimited(30.0)})
        (row,) = self._rows([seat], cache, state)
        self.assertEqual(row.source, "stale")
        self.assertEqual(row.percent, 43.0)

    def test_usable_seat_with_no_data_anywhere_is_none(self) -> None:
        seat = _seat("azim")
        cache = _FakeCache({str(seat.path): usage_mod.UsageFetchError("network down")})
        (row,) = self._rows([seat], cache, cooldown.load_state(Path("/none")))
        self.assertEqual(row.source, "none")
        self.assertIsNone(row.percent)

    def test_http_401_with_no_history_is_flagged_auth(self) -> None:
        seat = _seat("almas")
        err = usage_mod.UsageFetchError("HTTP 401", status_code=401)
        (row,) = self._rows([seat], _FakeCache({str(seat.path): err}), cooldown.load_state(Path("/none")))
        self.assertEqual(row.source, "auth")  # token expired, nothing to fall back to → say why

    def test_http_401_with_history_prefers_showing_last_known(self) -> None:
        seat = _seat("sam")
        state = cooldown.load_state(Path("/none"))
        cooldown.update_seat(state, seat.path, last_percent=43.0, last_seen_at=NOW.isoformat())
        err = usage_mod.UsageFetchError("HTTP 401", status_code=401)
        (row,) = self._rows([seat], _FakeCache({str(seat.path): err}), state)
        self.assertEqual(row.source, "stale")  # a real last-known % beats a bare "auth" flag
        self.assertEqual(row.percent, 43.0)

    def test_disabled_seat_is_labeled_disabled(self) -> None:
        seat = _seat("almas")
        state = cooldown.load_state(Path("/none"))
        cooldown.set_seat_disabled(state, "almas", True)
        # even a live-pollable seat shows "disabled" (still displays its %, just out of rotation)
        cache = _FakeCache({str(seat.path): _usage(28.0)})
        (row,) = self._rows([seat], cache, state)
        self.assertEqual(row.state_label, "disabled")
        self.assertEqual(row.source, "live")

    def test_cooling_seat_is_labeled_with_remaining_time(self) -> None:
        seat = _seat("sam")
        state = cooldown.load_state(Path("/none"))
        cooldown.update_seat(
            state, seat.path, cooldown_until=(NOW + dt.timedelta(minutes=12)).isoformat(),
            last_percent=71.0, last_seen_at=NOW.isoformat(),
        )
        cache = _FakeCache({str(seat.path): usage_mod.NeedsLoginError("x")})
        (row,) = self._rows([seat], cache, state)
        self.assertEqual(row.state_label, "cooling 12m")


class HumanizeTests(unittest.TestCase):
    def test_delta(self) -> None:
        self.assertEqual(monitor._humanize_delta(None, NOW), "—")
        self.assertEqual(monitor._humanize_delta(NOW - dt.timedelta(minutes=1), NOW), "now")
        self.assertEqual(monitor._humanize_delta(NOW + dt.timedelta(minutes=45), NOW), "45m")
        self.assertEqual(monitor._humanize_delta(NOW + dt.timedelta(hours=1, minutes=2), NOW), "1h02m")

    def test_age(self) -> None:
        self.assertEqual(monitor._humanize_age(None), "?")
        self.assertEqual(monitor._humanize_age(30), "now")
        self.assertEqual(monitor._humanize_age(45 * 60), "45m")
        self.assertEqual(monitor._humanize_age(3 * 3600), "3h")
        self.assertEqual(monitor._humanize_age(2 * 86400), "2d")


class RenderTests(unittest.TestCase):
    def test_table_contains_seat_and_provenance(self) -> None:
        rows = [
            monitor.SeatRow(
                "almas", 28.0, NOW + dt.timedelta(minutes=90), 11.0, "← latest run", "live", None
            ),
            monitor.SeatRow("ayan", 18.0, None, None, "needs-login", "stale", 2 * 3600),
        ]
        text = monitor.render_seat_table(rows, now=NOW)
        self.assertIn("almas", text)
        self.assertIn("28%", text)
        self.assertIn("live", text)
        self.assertIn("stale·2h", text)
        self.assertIn("needs-login", text)
        self.assertIn("observe-only", text)

    def test_auth_row_renders_auth_marker(self) -> None:
        rows = [monitor.SeatRow("almas", None, None, None, "idle", "auth", None)]
        self.assertIn("auth?", monitor.render_seat_table(rows, now=NOW))

    def test_empty_rows_render_a_hint(self) -> None:
        self.assertIn("no seats", monitor.render_seat_table([], now=NOW))

    def test_supervisor_running_renders_pid(self) -> None:
        text = monitor.render_seat_table([], now=NOW, supervisor=(True, 4242))
        self.assertIn("RUNNING (pid 4242)", text)
        self.assertNotIn("NOT RUNNING", text)

    def test_supervisor_dead_pid_renders_not_running(self) -> None:
        text = monitor.render_seat_table([], now=NOW, supervisor=(False, 4242))
        self.assertIn("NOT RUNNING", text)
        self.assertIn("4242", text)

    def test_supervisor_no_lockfile_renders_not_running_with_no_pid(self) -> None:
        text = monitor.render_seat_table([], now=NOW, supervisor=(False, None))
        self.assertIn("NOT RUNNING", text)
        self.assertIn("no lockfile", text)

    def test_no_supervisor_arg_omits_the_header_line_entirely(self) -> None:
        """Backward compatible: omitting `supervisor=` must not print anything about it."""
        text = monitor.render_seat_table([], now=NOW)
        self.assertNotIn("supervisor:", text)

    def test_repo_status_reads_git_and_build_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".gad").mkdir()
            (repo / ".gad" / "BUILD_STATUS.md").write_text("# gen 3 GREEN\n", encoding="utf-8")
            fake = mock.Mock(return_value=mock.Mock(stdout="abc123 do a thing\n"))
            text = monitor.render_repo_status(repo, now=NOW, runner=fake)
        self.assertIn("abc123 do a thing", text)
        self.assertIn("gen 3 GREEN", text)

    def test_repo_status_without_repo(self) -> None:
        self.assertIn("no repo configured", monitor.render_repo_status(None, now=NOW))


class SupervisorLivenessTests(unittest.TestCase):
    """B24 audit fix: the monitor must be able to distinguish "supervisor running" from "died
    hours ago" by actually reading the lockfile's `pid:starttime`, not by inferring health from
    run-log mtimes (which persist forever)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = Config(state_dir=Path(self._tmp.name))

    def _lock_path(self) -> Path:
        return self.config.state_dir / "claude-relay.lock"

    def test_no_lockfile_is_not_running_with_no_pid(self) -> None:
        self.assertEqual(monitor.supervisor_liveness(self.config), (False, None))

    def test_own_live_pid_with_matching_starttime_is_running(self) -> None:
        my_pid = os.getpid()
        from relay import loop as loop_mod

        starttime = loop_mod._proc_start_time(my_pid)
        self._lock_path().write_text(f"{my_pid}:{starttime if starttime is not None else ''}")
        is_alive, pid = monitor.supervisor_liveness(self.config)
        self.assertTrue(is_alive)
        self.assertEqual(pid, my_pid)

    def test_a_dead_pid_is_not_running(self) -> None:
        for candidate in (999999, 999998, 999997, 899999):
            try:
                os.kill(candidate, 0)
            except ProcessLookupError:
                dead_pid = candidate
                break
            except PermissionError:  # pragma: no cover - exists but not ours; try the next one
                continue
        else:
            self.skipTest("could not find an unused PID to simulate a dead process")
        self._lock_path().write_text(f"{dead_pid}:12345")
        self.assertEqual(monitor.supervisor_liveness(self.config), (False, dead_pid))

    def test_a_reused_pid_with_a_mismatched_starttime_is_not_running(self) -> None:
        """The exact PID-reuse false-positive `_proc_start_time` corroboration exists to catch:
        our own PID is genuinely alive, but the recorded starttime does not match — meaning the
        lockfile's process has since exited and the OS handed this PID to someone else."""
        my_pid = os.getpid()
        self._lock_path().write_text(f"{my_pid}:1")  # implausible starttime — not really ours
        is_alive, pid = monitor.supervisor_liveness(self.config)
        self.assertFalse(is_alive)
        self.assertEqual(pid, my_pid)


class LatestRunSeatTests(unittest.TestCase):
    def test_picks_seat_from_newest_run_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            old = log_dir / "run-20260719T060000-almas.log"
            new = log_dir / "run-20260719T070000-sam.log"
            old.write_text("", encoding="utf-8")
            new.write_text("", encoding="utf-8")
            os.utime(old, (1000, 1000))
            os.utime(new, (2000, 2000))
            self.assertEqual(monitor.latest_run_seat(log_dir), "sam")

    def test_none_when_no_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(monitor.latest_run_seat(Path(tmp)))


class TmuxCommandsTests(unittest.TestCase):
    def test_builds_three_pane_layout(self) -> None:
        cmds = monitor.tmux_commands(
            cli=["/usr/bin/python3", "/abs/bin/claude-relay"],
            config_path=Path("/etc/claude-relay/config.toml"),
            repo=Path("/home/me/proj"),
            session="claude-relay",
            interval=60,
        )
        self.assertEqual(len(cmds), 4)
        self.assertEqual(cmds[0][:4], ["tmux", "new-session", "-d", "-s"])
        self.assertIn("-x", cmds[0])  # generous detached size so panes aren't width-starved
        self.assertEqual(cmds[1][1:3], ["split-window", "-h"])  # right column
        self.assertEqual(cmds[2][1:3], ["split-window", "-v"])  # stacked under it
        joined = "\n".join(cmd[-1] for cmd in cmds)
        self.assertIn("_panel log", joined)
        self.assertIn("seats --watch 60", joined)
        self.assertIn("_panel repo --repo /home/me/proj", joined)
        self.assertIn("--config /etc/claude-relay/config.toml", joined)

    def test_omits_config_flag_when_none_and_quotes_spaces(self) -> None:
        cmds = monitor.tmux_commands(
            cli=["claude-relay"], config_path=None,
            repo=Path("/home/my repo/x"), session="s", interval=30,
        )
        repo_cmd = cmds[2][-1]
        self.assertNotIn("--config", "\n".join(c[-1] for c in cmds))
        self.assertIn("'/home/my repo/x'", repo_cmd)  # shlex-quoted path with a space


if __name__ == "__main__":
    unittest.main()
