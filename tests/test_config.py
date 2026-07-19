"""Offline tests for relay.config: TOML + env + CLI-override merge order, no hardcoded paths."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay import config as config_mod


class LoadConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def test_missing_config_file_uses_defaults(self) -> None:
        cfg = config_mod.load_config(self.tmp_dir / "does-not-exist.toml")
        self.assertEqual(cfg.ceiling_pct, 70.0)
        self.assertEqual(cfg.start_margin, 5.0)
        self.assertEqual(cfg.gadkit_tier, "budget")
        self.assertEqual(cfg.notify_sink, "telegram")
        self.assertEqual(cfg.exclude, ["yerasyl"])

    def test_toml_values_override_defaults(self) -> None:
        path = self.tmp_dir / "config.toml"
        path.write_text(
            'repo = "/abs/repo"\n'
            'exclude = ["someone"]\n'
            "[defaults]\n"
            "ceiling_pct = 80\n"
            "start_margin = 10\n"
            "[gadkit]\n"
            'tier = "balanced"\n'
            "[telegram]\n"
            'bot_token = "tok"\n'
            'chat_id = "123"\n',
            encoding="utf-8",
        )
        cfg = config_mod.load_config(path)
        self.assertEqual(cfg.repo, "/abs/repo")
        self.assertEqual(cfg.ceiling_pct, 80.0)
        self.assertEqual(cfg.start_margin, 10.0)
        self.assertEqual(cfg.exclude, ["someone"])
        self.assertEqual(cfg.gadkit_tier, "balanced")
        self.assertEqual(cfg.telegram_bot_token, "tok")
        self.assertEqual(cfg.telegram_chat_id, "123")

    def test_per_seat_ceiling_and_pool_exclusion(self) -> None:
        path = self.tmp_dir / "config.toml"
        path.write_text(
            "[defaults]\n"
            "ceiling_pct = 70\n"
            "[seats.sam]\n"
            "ceiling_pct = 85\n"
            "[seats.dias]\n"
            "main = true\n"
            "[seats.retired]\n"
            "exclude = true\n",
            encoding="utf-8",
        )
        cfg = config_mod.load_config(path)
        self.assertEqual(cfg.resolve_seat_ceiling("sam"), 85.0)
        self.assertEqual(cfg.resolve_seat_ceiling("unconfigured"), 70.0)
        exclude = cfg.effective_exclude()
        self.assertIn("dias", exclude)
        self.assertIn("retired", exclude)
        self.assertNotIn("sam", exclude)

    def test_ceiling_override_wins_over_seat_and_default(self) -> None:
        path = self.tmp_dir / "config.toml"
        path.write_text("[defaults]\nceiling_pct = 70\n[seats.sam]\nceiling_pct = 85\n", encoding="utf-8")
        cfg = config_mod.load_config(path, ceiling_overrides={"sam": 60.0})
        self.assertEqual(cfg.resolve_seat_ceiling("sam"), 60.0)
        self.assertEqual(cfg.resolve_seat_ceiling("almas"), 70.0)

    def test_env_vars_override_toml_secrets_only(self) -> None:
        path = self.tmp_dir / "config.toml"
        path.write_text('[telegram]\nbot_token = "from-file"\nchat_id = "from-file"\n', encoding="utf-8")
        import os

        old_token = os.environ.get("CLAUDE_RELAY_TELEGRAM_BOT_TOKEN")
        old_chat = os.environ.get("CLAUDE_RELAY_TELEGRAM_CHAT_ID")
        try:
            os.environ["CLAUDE_RELAY_TELEGRAM_BOT_TOKEN"] = "from-env"
            os.environ["CLAUDE_RELAY_TELEGRAM_CHAT_ID"] = "999"
            cfg = config_mod.load_config(path)
        finally:
            for key, val in (
                ("CLAUDE_RELAY_TELEGRAM_BOT_TOKEN", old_token),
                ("CLAUDE_RELAY_TELEGRAM_CHAT_ID", old_chat),
            ):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
        self.assertEqual(cfg.telegram_bot_token, "from-env")
        self.assertEqual(cfg.telegram_chat_id, "999")

    def test_cli_override_repo_wins_over_toml(self) -> None:
        path = self.tmp_dir / "config.toml"
        path.write_text('repo = "/from/toml"\n', encoding="utf-8")
        cfg = config_mod.load_config(path, repo="/from/cli")
        self.assertEqual(cfg.repo, "/from/cli")

    def test_profile_cli_override_maps_to_gadkit_tier(self) -> None:
        cfg = config_mod.load_config(self.tmp_dir / "missing.toml", profile="balanced")
        self.assertEqual(cfg.gadkit_tier, "balanced")

    def test_invalid_tier_raises_config_error(self) -> None:
        path = self.tmp_dir / "config.toml"
        path.write_text('[gadkit]\ntier = "nonsense"\n', encoding="utf-8")
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_config(path)

    def test_default_paths_are_derived_from_home_not_hardcoded(self) -> None:
        state_dir = config_mod.default_state_dir()
        self.assertEqual(state_dir, Path.home() / ".claude-relay")


if __name__ == "__main__":
    unittest.main()
