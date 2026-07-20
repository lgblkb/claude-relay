"""Offline tests for relay.plugins (opt-in --plugin-dir resolution) and runner.build_claude_argv.
A fake HOME holds a synthetic plugin cache; no network, no real ~/.claude, no subprocess.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay import plugins, runner


def _install_plugin(home: Path, marketplace: str, plugin: str, version: str) -> Path:
    root = home / ".claude" / "plugins" / "cache" / marketplace / plugin / version
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
    return root


class ResolveTests(unittest.TestCase):
    def test_empty_and_none_resolve_to_nothing(self) -> None:
        self.assertEqual(plugins.resolve_plugin_dirs([]), [])
        self.assertEqual(plugins.resolve_plugin_dirs(None), [])

    def test_name_resolves_to_newest_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _install_plugin(home, "gad-kit", "gad-kit", "1.4.0")
            newest = _install_plugin(home, "gad-kit", "gad-kit", "1.5.0")
            _install_plugin(home, "gad-kit", "gad-kit", "1.10.0")  # 1.10 > 1.5 numerically
            got = plugins.resolve_plugin_dirs(["gad-kit"], home=home)
            self.assertEqual(got, [newest.parent / "1.10.0"])

    def test_absolute_path_used_iff_it_carries_a_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = _install_plugin(home, "mkt", "thing", "1.0.0")
            self.assertEqual(plugins.resolve_plugin_dirs([str(root)], home=home), [root])
            bare = home / "not-a-plugin"
            bare.mkdir()
            self.assertEqual(plugins.resolve_plugin_dirs([str(bare)], home=home), [])

    def test_star_expands_to_every_plugin_newest_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _install_plugin(home, "gad-kit", "gad-kit", "1.0.0")
            g = _install_plugin(home, "gad-kit", "gad-kit", "1.5.0")
            c = _install_plugin(home, "context-mode", "context-mode", "1.0.169")
            got = set(plugins.resolve_plugin_dirs(["*"], home=home))
            self.assertEqual(got, {g, c})

    def test_unknown_name_is_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _install_plugin(home, "gad-kit", "gad-kit", "1.5.0")
            got = plugins.resolve_plugin_dirs(["gad-kit", "no-such-plugin"], home=home)
            self.assertEqual([p.name for p in got], ["1.5.0"])

    def test_duplicates_are_deduped_order_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            g = _install_plugin(home, "gad-kit", "gad-kit", "1.5.0")
            c = _install_plugin(home, "context-mode", "context-mode", "1.0.0")
            got = plugins.resolve_plugin_dirs(["gad-kit", "context-mode", "gad-kit"], home=home)
            self.assertEqual(got, [g, c])


class FlagsAndArgvTests(unittest.TestCase):
    def test_plugin_flags_interleaves_flag_per_dir(self) -> None:
        self.assertEqual(plugins.plugin_flags([]), [])
        self.assertEqual(
            plugins.plugin_flags([Path("/a"), "/b"]), ["--plugin-dir", "/a", "--plugin-dir", "/b"]
        )

    def test_build_claude_argv_prepends_flags_before_the_workload_argv(self) -> None:
        self.assertEqual(runner.build_claude_argv(["-p", "hi"]), ["claude", "-p", "hi"])
        self.assertEqual(
            runner.build_claude_argv(["-p", "hi"], ["/plug"]),
            ["claude", "--plugin-dir", "/plug", "-p", "hi"],
        )


if __name__ == "__main__":
    unittest.main()
