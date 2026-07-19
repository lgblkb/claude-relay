"""Seat discovery: find candidate `CLAUDE_CONFIG_DIR` directories ("seats") on this machine
and classify each as usable / needs-login. Pure filesystem + JSON parsing — no network I/O
(that lives in usage.py) and no subprocess spawning (that lives in runner.py).

A "seat" is a directory `~/.claude-<name>` holding its own `.credentials.json` (and
`.claude.json`), i.e. a distinct Anthropic OAuth login. Bare `~/.claude` is deliberately
EXCLUDED in v1: its `.claude.json` lives at `~/.claude.json` (the historical default), so
`CLAUDE_CONFIG_DIR=~/.claude` produces a degraded/"config not found" session (confirmed by
smoke test, DESIGN.md §0). Only named `~/.claude-*` seats are pooled.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Seat:
    """One discovered seat directory and its credential state.

    `name` is the suffix after `.claude-` (e.g. `~/.claude-almas` -> `almas`) — this is what
    operator `exclude` lists (config.toml `exclude = ["yerasyl"]`) are matched against, and
    what state.json uses as `seats` keys are actually the full `path` string (see cooldown.py).
    """

    name: str
    path: Path
    has_creds: bool
    needs_login: bool
    access_token: str | None = dataclasses.field(default=None, repr=False, compare=False)
    subscription_type: str | None = None

    @property
    def usable(self) -> bool:
        return self.has_creds and not self.needs_login


def read_credentials(seat_dir: Path) -> dict | None:
    """Parse `{seat_dir}/.credentials.json`. Returns None (never raises) if the file is
    missing, unreadable, or not valid JSON — all of those are just "needs-login" to the
    caller, not a hard error (Invariant #3: graceful degradation is normal).
    """
    cred_path = seat_dir / ".credentials.json"
    try:
        with cred_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _seat_from_dir(seat_dir: Path, name: str) -> Seat:
    creds = read_credentials(seat_dir)
    oauth = (creds or {}).get("claudeAiOauth")
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    has_creds = bool(creds) and bool(token)
    return Seat(
        name=name,
        path=seat_dir,
        has_creds=has_creds,
        needs_login=not has_creds,
        access_token=token if has_creds else None,
        subscription_type=(oauth or {}).get("subscriptionType") if isinstance(oauth, dict) else None,
    )


def discover_seats(exclude: list[str] | None = None, home: Path | None = None) -> list[Seat]:
    """Glob `{home}/.claude-*` for seat directories, excluding:
      - anything that is not a directory (e.g. stray backup files matching the glob),
      - bare `.claude` itself (defensive; it can never match this glob since it has no
        trailing `-`, but the exclusion is stated explicitly to keep the invariant visible),
      - any seat whose `name` (the suffix after `.claude-`) is in `exclude`.

    Never raises for a bad/missing seat — those are surfaced as `needs_login=True` seats so
    the caller can notify once and keep going (Invariant #3).
    """
    home = home or Path.home()
    exclude_set = set(exclude or [])
    prefix = ".claude-"
    seats: list[Seat] = []
    for entry in sorted(home.glob(f"{prefix}*")):
        if not entry.is_dir():
            continue
        if entry.name == ".claude":  # can't actually match the glob, kept for clarity/safety
            continue
        # A real Claude profile dir carries a `.claude.json` (profile identity) and/or a
        # `.credentials.json`. Skip dirs that have NEITHER — most importantly this tool's own
        # `~/.claude-relay` state dir, which matches the `.claude-*` glob but is not a seat.
        # (A logged-out profile like ayan/azim still has `.claude.json`, so it is kept and
        # correctly surfaced as needs-login.)
        if not ((entry / ".claude.json").exists() or (entry / ".credentials.json").exists()):
            continue
        name = entry.name[len(prefix) :]
        if name in exclude_set:
            continue
        seats.append(_seat_from_dir(entry, name))
    return seats


def find_seat(seats: list[Seat], name_or_path: str) -> Seat | None:
    """Look up a seat by its short name or its full directory path (string)."""
    for seat in seats:
        if seat.name == name_or_path or str(seat.path) == name_or_path:
            return seat
    return None
