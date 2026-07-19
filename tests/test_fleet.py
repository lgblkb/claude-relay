"""Offline tests for relay.fleet: seat discovery, the bare-`.claude` exclusion quirk, and
needs-login classification. All filesystem-only — no network, no real seat directories.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from relay import fleet


class DiscoverSeatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _make_seat(self, name: str, creds: dict | None) -> Path:
        seat_dir = self.home / f".claude-{name}"
        seat_dir.mkdir(parents=True, exist_ok=True)
        (seat_dir / ".claude.json").write_text("{}", encoding="utf-8")  # profile-identity marker
        if creds is not None:
            (seat_dir / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")
        return seat_dir

    def test_usable_seat_has_creds_and_access_token(self) -> None:
        self._make_seat(
            "alice",
            {"claudeAiOauth": {"accessToken": "tok-alice", "subscriptionType": "team"}},
        )
        seats = fleet.discover_seats(exclude=[], home=self.home)
        self.assertEqual(len(seats), 1)
        self.assertEqual(seats[0].name, "alice")
        self.assertTrue(seats[0].usable)
        self.assertFalse(seats[0].needs_login)
        self.assertEqual(seats[0].access_token, "tok-alice")

    def test_missing_credentials_file_is_needs_login(self) -> None:
        self._make_seat("bob", creds=None)
        seats = fleet.discover_seats(exclude=[], home=self.home)
        self.assertEqual(len(seats), 1)
        self.assertTrue(seats[0].needs_login)
        self.assertFalse(seats[0].usable)

    def test_malformed_credentials_json_is_needs_login_not_a_crash(self) -> None:
        seat_dir = self.home / ".claude-charlie"
        seat_dir.mkdir()
        (seat_dir / ".credentials.json").write_text("{not valid json", encoding="utf-8")
        seats = fleet.discover_seats(exclude=[], home=self.home)
        self.assertEqual(len(seats), 1)
        self.assertTrue(seats[0].needs_login)

    def test_credentials_without_access_token_is_needs_login(self) -> None:
        self._make_seat("dana", {"claudeAiOauth": {"subscriptionType": "team"}})
        seats = fleet.discover_seats(exclude=[], home=self.home)
        self.assertTrue(seats[0].needs_login)

    def test_exclude_list_filters_by_name(self) -> None:
        self._make_seat("alice", {"claudeAiOauth": {"accessToken": "tok"}})
        self._make_seat("yerasyl", {"claudeAiOauth": {"accessToken": "tok"}})
        seats = fleet.discover_seats(exclude=["yerasyl"], home=self.home)
        names = {s.name for s in seats}
        self.assertEqual(names, {"alice"})

    def test_bare_dot_claude_is_never_included(self) -> None:
        # Bare ~/.claude can never match the ".claude-*" glob (no trailing "-"), but this test
        # pins that invariant explicitly (DESIGN.md §0 canonical-seat quirk).
        bare = self.home / ".claude"
        bare.mkdir()
        (bare / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "tok"}}), encoding="utf-8"
        )
        self._make_seat("alice", {"claudeAiOauth": {"accessToken": "tok"}})
        seats = fleet.discover_seats(exclude=[], home=self.home)
        names = {s.name for s in seats}
        self.assertEqual(names, {"alice"})

    def test_non_directory_matching_the_glob_is_skipped(self) -> None:
        # A stray backup file like ".claude-almas-projects-symlinks-....txt" must not be
        # mistaken for a seat directory.
        (self.home / ".claude-alice-backup-notes.txt").write_text("not a seat", encoding="utf-8")
        self._make_seat("alice", {"claudeAiOauth": {"accessToken": "tok"}})
        seats = fleet.discover_seats(exclude=[], home=self.home)
        names = {s.name for s in seats}
        self.assertEqual(names, {"alice"})

    def test_dir_without_profile_markers_is_skipped(self) -> None:
        # This tool's own ~/.claude-relay state dir matches the ".claude-*" glob but has
        # neither .claude.json nor .credentials.json, so it must NOT be treated as a seat.
        state_dir = self.home / ".claude-relay"
        state_dir.mkdir()
        (state_dir / "config.toml").write_text("x = 1", encoding="utf-8")
        self._make_seat("alice", {"claudeAiOauth": {"accessToken": "tok"}})
        seats = fleet.discover_seats(exclude=[], home=self.home)
        self.assertEqual({s.name for s in seats}, {"alice"})

    def test_find_seat_by_name_or_path(self) -> None:
        seat_dir = self._make_seat("alice", {"claudeAiOauth": {"accessToken": "tok"}})
        seats = fleet.discover_seats(exclude=[], home=self.home)
        self.assertIsNotNone(fleet.find_seat(seats, "alice"))
        self.assertIsNotNone(fleet.find_seat(seats, str(seat_dir)))
        self.assertIsNone(fleet.find_seat(seats, "nonexistent"))


if __name__ == "__main__":
    unittest.main()
