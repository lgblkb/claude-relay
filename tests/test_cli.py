"""Offline tests for relay.cli: the argument parser wiring, `claude-relay init` (config seeding
for pipx/uv installs), and `claude-relay resolve` (the B13 commit-failure surfacing). HOME is
redirected to a temp dir so nothing touches the real ~/.claude-relay. No network. `ResolveCommand
Tests` spawns real local `git` subprocesses (no network) against throwaway temp repos, mirroring
`tests/test_gadkit.py`'s convention.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from relay import cli, cooldown
from relay import config as config_mod


class ParserTests(unittest.TestCase):
    def test_every_subcommand_binds_a_func(self) -> None:
        parser = cli.build_parser()
        for argv in (
            ["run", "/x", "--dry-run"],
            ["status"],
            ["login-check"],
            ["init", "--force", "--no-adopt"],
            ["adopt", "--name", "default", "--force"],
            ["disable", "sam"],
            ["enable", "sam"],
            ["share", "--check", "--plugins"],
            ["resolve", "d1", "yes"],
            ["seats", "--watch", "30"],
            ["monitor", "/x"],
            ["_panel", "log"],
        ):
            args = parser.parse_args(argv)
            self.assertTrue(callable(getattr(args, "func", None)), f"no func for {argv!r}")


def _fake_home_with_main_login(tmp: str) -> Path:
    """A temp HOME whose bare ~/.claude carries a .credentials.json (the 'main account')."""
    home = Path(tmp)
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text('{"claudeAiOauth": {}}', encoding="utf-8")
    return home


class AdoptTests(unittest.TestCase):
    def test_adopt_copies_bare_claude_into_named_seat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _fake_home_with_main_login(tmp)
            res = cli._adopt_default_seat("default", home=home)
            self.assertEqual(res.status, "adopted")
            seat_creds = home / ".claude-default" / ".credentials.json"
            self.assertTrue(seat_creds.is_file())
            self.assertEqual(stat.S_IMODE(seat_creds.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((home / ".claude-default").stat().st_mode), 0o700)
            # source is never modified
            self.assertTrue((home / ".claude" / ".credentials.json").is_file())

    def test_adopt_is_idempotent_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _fake_home_with_main_login(tmp)
            cli._adopt_default_seat("default", home=home)
            (home / ".claude-default" / ".credentials.json").write_text("REFRESHED", encoding="utf-8")
            res = cli._adopt_default_seat("default", home=home)  # no force
            self.assertEqual(res.status, "exists")
            # must NOT clobber the seat's own (refreshed) token
            self.assertEqual(
                (home / ".claude-default" / ".credentials.json").read_text(encoding="utf-8"), "REFRESHED"
            )

    def test_adopt_no_source_when_no_bare_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res = cli._adopt_default_seat("default", home=Path(tmp))
            self.assertEqual(res.status, "no-source")
            self.assertFalse((Path(tmp) / ".claude-default").exists())

    def test_maybe_adopt_if_empty_skips_when_a_seat_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _fake_home_with_main_login(tmp)
            # a pre-existing usable named seat
            (home / ".claude-almas").mkdir()
            (home / ".claude-almas" / ".credentials.json").write_text(
                '{"claudeAiOauth": {"accessToken": "tok"}}', encoding="utf-8"
            )
            cfg = config_mod.Config(adopt_default="if-empty")
            res = cli._maybe_adopt(cfg, name="default", no_adopt=False, home=home)
            self.assertEqual(res.status, "skipped")
            self.assertFalse((home / ".claude-default").exists())

    def test_maybe_adopt_always_adopts_even_with_other_seats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _fake_home_with_main_login(tmp)
            (home / ".claude-almas").mkdir()
            (home / ".claude-almas" / ".credentials.json").write_text(
                '{"claudeAiOauth": {"accessToken": "tok"}}', encoding="utf-8"
            )
            cfg = config_mod.Config(adopt_default="always")
            res = cli._maybe_adopt(cfg, name="default", no_adopt=False, home=home)
            self.assertEqual(res.status, "adopted")
            self.assertTrue((home / ".claude-default" / ".credentials.json").is_file())

    def test_maybe_adopt_respects_no_adopt_and_never(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _fake_home_with_main_login(tmp)
            self.assertEqual(
                cli._maybe_adopt(config_mod.Config(), name="default", no_adopt=True, home=home).status,
                "skipped",
            )
            self.assertEqual(
                cli._maybe_adopt(
                    config_mod.Config(adopt_default="never"), name="default", no_adopt=False, home=home
                ).status,
                "skipped",
            )
            self.assertFalse((home / ".claude-default").exists())


class DisableEnableTests(unittest.TestCase):
    def test_disable_then_enable_roundtrips_in_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = cooldown.load_state(state_path)
            self.assertTrue(cooldown.set_seat_disabled(state, "sam", True))
            self.assertFalse(cooldown.set_seat_disabled(state, "sam", True))  # already disabled
            self.assertIn("sam", cooldown.disabled_seats(state))
            cooldown.save_state(state_path, state)
            reloaded = cooldown.load_state(state_path)  # persists across load
            self.assertIn("sam", cooldown.disabled_seats(reloaded))
            self.assertTrue(cooldown.set_seat_disabled(reloaded, "sam", False))
            self.assertNotIn("sam", cooldown.disabled_seats(reloaded))


class InitTests(unittest.TestCase):
    def _run_init(self, home: Path, *, force: bool = False) -> int:
        args = cli.build_parser().parse_args(["init", *(["--force"] if force else [])])
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            return cli.cmd_init(args)

    def test_init_seeds_a_valid_config_and_logs_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rc = self._run_init(home)
            self.assertEqual(rc, 0)
            cfg_path = home / ".claude-relay" / "config.toml"
            self.assertTrue(cfg_path.exists())
            self.assertTrue((home / ".claude-relay" / "logs").is_dir())
            # 0600 — the file may later hold a Telegram token, so it must not be world-readable.
            self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
            # The seeded template must be valid TOML that the real loader accepts.
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                cfg = config_mod.load_config(cfg_path)
            self.assertIn("yerasyl", cfg.effective_exclude())

    def test_init_is_idempotent_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._run_init(home)
            cfg_path = home / ".claude-relay" / "config.toml"
            cfg_path.write_text('repo = "/edited/by/user"\n', encoding="utf-8")
            self._run_init(home)  # second run must NOT clobber the user's edit
            self.assertIn("/edited/by/user", cfg_path.read_text(encoding="utf-8"))

    def test_init_force_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self._run_init(home)
            cfg_path = home / ".claude-relay" / "config.toml"
            cfg_path.write_text('repo = "/edited/by/user"\n', encoding="utf-8")
            self._run_init(home, force=True)
            self.assertNotIn("/edited/by/user", cfg_path.read_text(encoding="utf-8"))


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _init_gad_repo(repo: Path, decision_id: str = "D1") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    gad = repo / ".gad"
    gad.mkdir()
    index = {
        "project": "t",
        "nextGen": 1,
        "generations": [],
        "ownerDecisions": [{"id": decision_id, "question": "pick a DB", "blocksGen": 1, "status": "open"}],
    }
    (gad / "generations-index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "seed .gad state")


class ResolveCommandTests(unittest.TestCase):
    """B13 audit fix, second round: `claude-relay resolve` must never report an uncommitted
    resolution (a rejecting pre-commit hook, an unset git identity) as an ordinary clean
    success — this drives the REAL `cmd_resolve` against a real repo, not a mock of
    `resolve_owner_decision`.
    """

    def _resolve(self, home: Path, repo: Path) -> tuple[int, str]:
        args = cli.build_parser().parse_args(["resolve", "D1", "use postgres"])
        args.repo = str(repo)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False), redirect_stderr(buf):
            rc = cli.cmd_resolve(args)
        return rc, buf.getvalue()

    def test_a_clean_resolution_prints_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"
            _init_gad_repo(repo)
            rc, stderr = self._resolve(home, repo)
        self.assertEqual(rc, 0)
        self.assertNotIn("WARNING", stderr)

    def test_a_failed_commit_prints_a_loud_warning_and_still_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = Path(tmp) / "repo"
            _init_gad_repo(repo)
            hooks_dir = repo / ".git" / "hooks"
            hook = hooks_dir / "pre-commit"
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            rc, stderr = self._resolve(home, repo)
        # The resolution DID apply on disk — this is not a "not found"/failure return code.
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", stderr)
        self.assertIn("FAILED", stderr)


class EnableDisableTruthfulnessTests(unittest.TestCase):
    """`enable`/`disable` must not claim an effect they cannot deliver.

    MEASURED (2026-07-27, DESIGN.md §4c). `claude-relay enable azim` printed:

        seat 'azim' was already enabled.
          note: no seat named 'azim' discovered yet — setting saved, applies if it appears.

    for a seat that existed on disk and was config-excluded. BOTH lines were false, and the operator
    went on believing azim was pooled. Two distinct causes:

      * the "discovered" probe passed `cfg.effective_exclude()` into `discover_seats()`, so every
        config-`exclude`d seat reads as nonexistent — and an excluded seat is exactly what someone
        runs `enable` for;
      * `disabledSeats` (what this command writes) and config.toml's `[seats.<name>] exclude`/`main`
        (which filters at discovery) are INDEPENDENT gates. Clearing the first while the second still
        holds leaves the seat out of rotation, and nothing said so.
    """

    def _seat_dir(self, home: Path, name: str) -> None:
        seat = home / f".claude-{name}"
        seat.mkdir(parents=True)
        (seat / ".claude.json").write_text("{}", encoding="utf-8")

    def _toggle(self, home: Path, cfg_path: Path, verb: str, seat: str) -> str:
        # `--config` lives on the TOP-LEVEL parser, so it must precede the subcommand.
        args = cli.build_parser().parse_args(["--config", str(cfg_path), verb, seat])
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False), redirect_stdout(buf):
            rc = cli.cmd_enable(args) if verb == "enable" else cli.cmd_disable(args)
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def _setup(self, tmp: str, *, exclude_azim: bool) -> tuple[Path, Path]:
        home = Path(tmp) / "home"
        home.mkdir()
        self._seat_dir(home, "azim")
        cfg_path = Path(tmp) / "config.toml"
        # No `state_path` here: it is NOT a TOML key, it defaults from `Path.home()` — which the
        # patched HOME already redirects into this tmpdir. Writing it here would be a silent no-op,
        # and the real state file is what would get mutated if HOME were not patched.
        body = f'repo = "{tmp}"\n'
        if exclude_azim:
            body += "\n[seats.azim]\nexclude = true\n"
        cfg_path.write_text(body, encoding="utf-8")
        return home, cfg_path

    def test_enabling_a_config_excluded_seat_warns_that_config_still_excludes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, cfg_path = self._setup(tmp, exclude_azim=True)
            out = self._toggle(home, cfg_path, "enable", "azim")
        self.assertIn("WARNING", out)
        self.assertIn("config.toml", out)
        self.assertIn("[seats.azim]", out)

    def test_a_config_excluded_seat_is_not_reported_as_undiscovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, cfg_path = self._setup(tmp, exclude_azim=True)
            out = self._toggle(home, cfg_path, "enable", "azim")
        self.assertNotIn("discovered yet", out)

    def test_enabling_a_pooled_seat_warns_about_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, cfg_path = self._setup(tmp, exclude_azim=False)
            out = self._toggle(home, cfg_path, "enable", "azim")
        self.assertNotIn("WARNING", out)
        self.assertNotIn("discovered yet", out)

    def test_a_genuinely_absent_seat_still_reports_undiscovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, cfg_path = self._setup(tmp, exclude_azim=False)
            out = self._toggle(home, cfg_path, "enable", "nosuchseat")
        self.assertIn("discovered yet", out)

    def test_disable_does_not_emit_the_enable_only_warning(self) -> None:
        # `disable` on a config-excluded seat is redundant, not misleading: the seat ends up out of
        # rotation either way, which is what the operator asked for.
        with tempfile.TemporaryDirectory() as tmp:
            home, cfg_path = self._setup(tmp, exclude_azim=True)
            out = self._toggle(home, cfg_path, "disable", "azim")
        self.assertNotIn("WARNING", out)


if __name__ == "__main__":
    unittest.main()
