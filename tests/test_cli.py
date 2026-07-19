"""Offline tests for relay.cli: the argument parser wiring and `claude-relay init` (config
seeding for pipx/uv installs). HOME is redirected to a temp dir so nothing touches the real
~/.claude-relay. No network, no subprocesses.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay import cli
from relay import config as config_mod


class ParserTests(unittest.TestCase):
    def test_every_subcommand_binds_a_func(self) -> None:
        parser = cli.build_parser()
        for argv in (
            ["run", "/x", "--dry-run"],
            ["status"],
            ["login-check"],
            ["init", "--force"],
            ["resolve", "d1", "yes"],
            ["seats", "--watch", "30"],
            ["monitor", "/x"],
            ["_panel", "log"],
        ):
            args = parser.parse_args(argv)
            self.assertTrue(callable(getattr(args, "func", None)), f"no func for {argv!r}")


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


if __name__ == "__main__":
    unittest.main()
