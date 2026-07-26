"""Offline tests for relay.config: TOML + env + CLI-override merge order, no hardcoded paths."""

from __future__ import annotations

import dataclasses
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

    def test_stale_gadkit_extra_flags_key_loads_without_error_and_is_not_exposed(self) -> None:
        """`[gadkit].extra_flags` was removed (it was dead config: populated onto `Plan` and never
        read by `gadkit.command()`, and it could not have worked as documented anyway — command()
        builds a JSON args OBJECT, not a CLI flag list). An operator's stale config.toml must
        still load: crashing on a leftover key would take the supervisor down, not the key.
        """
        path = self.tmp_dir / "config.toml"
        path.write_text(
            "[gadkit]\n"
            'tier = "balanced"\n'
            'extra_flags = ["--milestone", "--skip-premortem"]\n',
            encoding="utf-8",
        )
        cfg = config_mod.load_config(path)
        self.assertEqual(cfg.gadkit_tier, "balanced")  # the surviving key still parses
        self.assertFalse(hasattr(cfg, "gadkit_extra_flags"))
        self.assertNotIn(
            "extra_flags", {f.name for f in dataclasses.fields(config_mod.Config)}
        )
        self.assertNotIn(
            "gadkit_extra_flags", {f.name for f in dataclasses.fields(config_mod.Config)}
        )

    def test_stale_extra_flags_of_any_shape_is_ignored_rather_than_validated(self) -> None:
        # Not a list, and alongside other unknown keys — none of it may raise.
        path = self.tmp_dir / "config.toml"
        path.write_text(
            '[gadkit]\nextra_flags = "--milestone"\nsome_future_key = 3\n', encoding="utf-8"
        )
        cfg = config_mod.load_config(path)
        self.assertEqual(cfg.gadkit_tier, "budget")

    def test_default_paths_are_derived_from_home_not_hardcoded(self) -> None:
        state_dir = config_mod.default_state_dir()
        self.assertEqual(state_dir, Path.home() / ".claude-relay")


class NumericValidationTests(unittest.TestCase):
    """B30 audit fix: numeric config values are now sanity-checked at load time — a bad value
    must fail loudly as a `ConfigError`, not silently wreck unattended operation later.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def _load(self, toml_body: str) -> config_mod.Config:
        path = self.tmp_dir / "config.toml"
        path.write_text(toml_body, encoding="utf-8")
        return config_mod.load_config(path)

    def test_zero_run_timeout_s_is_rejected(self) -> None:
        """The exact B30 reproduction: `run_timeout_s = 0` would otherwise make every run
        instantly "time out" and burn the fleet into repeated cooldowns doing zero work."""
        with self.assertRaises(config_mod.ConfigError):
            self._load("run_timeout_s = 0\n")

    def test_negative_run_timeout_s_is_rejected(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            self._load("run_timeout_s = -5\n")

    def test_zero_poll_ttl_is_rejected(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            self._load("poll_ttl = 0\n")

    def test_negative_max_units_is_rejected(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            self._load("max_units = -1\n")

    def test_start_margin_exceeding_ceiling_pct_is_rejected(self) -> None:
        """The exact B30 reproduction: `start_margin > ceiling_pct` makes `start_cap` negative,
        so `pick_seat()` could never select ANY seat — a silent, permanent B2-style stall."""
        with self.assertRaises(config_mod.ConfigError):
            self._load("[defaults]\nceiling_pct = 70\nstart_margin = 80\n")

    def test_start_margin_equal_to_ceiling_pct_is_also_rejected(self) -> None:
        """The boundary case: start_cap would be exactly zero, still unsatisfiable by any real
        (non-negative) percent."""
        with self.assertRaises(config_mod.ConfigError):
            self._load("[defaults]\nceiling_pct = 70\nstart_margin = 70\n")

    def test_ceiling_pct_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            self._load("[defaults]\nceiling_pct = 150\n")

    def test_negative_start_margin_is_rejected(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            self._load("[defaults]\nstart_margin = -5\n")

    def test_per_seat_ceiling_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            self._load("[seats.sam]\nceiling_pct = 0\n")

    def test_non_numeric_run_timeout_s_raises_config_error_not_a_bare_value_error(self) -> None:
        """The OTHER half of B30: a malformed (wrong-type) numeric TOML value used to raise a
        bare `ValueError` straight out of `load_config()` instead of the `ConfigError` every
        other config problem in this module raises."""
        with self.assertRaises(config_mod.ConfigError):
            self._load('run_timeout_s = "not a number"\n')

    def test_cli_ceiling_override_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load_config(self.tmp_dir / "missing.toml", ceiling_overrides={"sam": 0.0})

    def test_a_valid_config_still_loads_cleanly(self) -> None:
        cfg = self._load("run_timeout_s = 3600\n[defaults]\nceiling_pct = 70\nstart_margin = 5\n")
        self.assertEqual(cfg.run_timeout_s, 3600.0)


if __name__ == "__main__":
    unittest.main()
