"""Offline tests for relay.share: linking each seat's projects/ to a canonical store, the
already-linked/conflict/fold cases, --check reporting, plugin subpaths, and the never-touch
boundary. Pure filesystem in a temp HOME; no network, no subprocess.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from relay import fleet, share


def _seat(home: Path, name: str) -> fleet.Seat:
    seat_dir = home / f".claude-{name}"
    seat_dir.mkdir(parents=True, exist_ok=True)
    return fleet.Seat(name=name, path=seat_dir, has_creds=True, needs_login=False)


def _canon(home: Path) -> Path:
    return home / ".claude" / "projects"


class LinkTests(unittest.TestCase):
    def test_fresh_seat_gets_linked_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            seat = _seat(home, "default")
            (r1,) = share.share_seats([seat], home=home)
            self.assertEqual(r1.status, share.LINKED)
            projects = seat.path / "projects"
            self.assertTrue(projects.is_symlink())
            self.assertEqual(projects.resolve(), _canon(home).resolve())
            # second run: already correct -> no-op OK
            (r2,) = share.share_seats([seat], home=home)
            self.assertEqual(r2.status, share.OK)

    def test_symlink_pointing_elsewhere_is_a_conflict_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            seat = _seat(home, "default")
            other = home / "somewhere-else"
            other.mkdir()
            (seat.path / "projects").symlink_to(other)
            (r,) = share.share_seats([seat], home=home)
            self.assertEqual(r.status, share.CONFLICT)
            self.assertEqual((seat.path / "projects").resolve(), other.resolve())  # untouched

    def test_empty_real_dir_is_replaced_by_a_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            seat = _seat(home, "default")
            (seat.path / "projects").mkdir()
            (r,) = share.share_seats([seat], home=home)
            self.assertEqual(r.status, share.LINKED)
            self.assertTrue((seat.path / "projects").is_symlink())

    def test_real_dir_with_content_folds_and_memory_rides_along(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            seat = _seat(home, "default")
            proj = seat.path / "projects"
            (proj / "-repo-a" / "memory").mkdir(parents=True)
            (proj / "-repo-a" / "memory" / "MEMORY.md").write_text("m", encoding="utf-8")
            (proj / "-repo-a" / "session.jsonl").write_text("s", encoding="utf-8")
            (r,) = share.share_seats([seat], home=home)
            self.assertEqual(r.status, share.FOLDED)
            self.assertTrue(proj.is_symlink())
            # both the session AND its memory moved into the canonical store
            self.assertTrue((_canon(home) / "-repo-a" / "session.jsonl").is_file())
            self.assertTrue((_canon(home) / "-repo-a" / "memory" / "MEMORY.md").is_file())

    def test_fold_never_clobbers_a_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            seat = _seat(home, "default")
            # canonical already holds -repo-a with the KEEPER content
            (_canon(home) / "-repo-a").mkdir(parents=True)
            (_canon(home) / "-repo-a" / "x").write_text("keeper", encoding="utf-8")
            # seat has a colliding -repo-a AND a non-colliding -repo-b
            (seat.path / "projects" / "-repo-a").mkdir(parents=True)
            (seat.path / "projects" / "-repo-a" / "x").write_text("intruder", encoding="utf-8")
            (seat.path / "projects" / "-repo-b").mkdir()
            (seat.path / "projects" / "-repo-b" / "y").write_text("moved", encoding="utf-8")
            (r,) = share.share_seats([seat], home=home)
            self.assertEqual(r.status, share.CONFLICT)
            # keeper untouched; non-colliding repo folded in; colliding one left in the seat
            self.assertEqual((_canon(home) / "-repo-a" / "x").read_text(encoding="utf-8"), "keeper")
            self.assertTrue((_canon(home) / "-repo-b" / "y").is_file())
            self.assertTrue((seat.path / "projects" / "-repo-a").is_dir())  # left as real dir
            self.assertFalse((seat.path / "projects").is_symlink())

    def test_check_mode_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            seat = _seat(home, "default")
            (r,) = share.share_seats([seat], home=home, check=True)
            self.assertEqual(r.status, share.WOULD_LINK)
            self.assertFalse((seat.path / "projects").exists())  # nothing created
            self.assertFalse(_canon(home).exists())

    def test_include_plugins_links_cache_and_marketplaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            seat = _seat(home, "default")
            results = share.share_seats([seat], home=home, include_plugins=True)
            subpaths = {r.subpath for r in results}
            self.assertEqual(subpaths, {"projects", "plugins/cache", "plugins/marketplaces"})
            self.assertTrue((seat.path / "plugins" / "cache").is_symlink())
            self.assertTrue((seat.path / "plugins" / "marketplaces").is_symlink())

    def test_never_touches_credentials_or_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            seat = _seat(home, "default")
            (seat.path / ".credentials.json").write_text("SECRET", encoding="utf-8")
            (seat.path / "settings.json").write_text("SETTINGS", encoding="utf-8")
            share.share_seats([seat], home=home, include_plugins=True)
            self.assertFalse((seat.path / ".credentials.json").is_symlink())
            self.assertEqual((seat.path / ".credentials.json").read_text(encoding="utf-8"), "SECRET")
            self.assertFalse((seat.path / "settings.json").is_symlink())
            self.assertEqual((seat.path / "settings.json").read_text(encoding="utf-8"), "SETTINGS")


if __name__ == "__main__":
    unittest.main()
