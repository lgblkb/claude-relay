"""Share session history + memory (and, opt-in, the plugin cache) across seats by symlinking
each seat's `projects/` to one canonical store under the real `~/.claude`.

This ports the essential behaviour of the `multi-profile-shared-claude` skill into claude-relay
so a fresh machine (e.g. a server with only `~/.claude-default`) gets the same shared-history
setup in one command — and it is deliberately COMPATIBLE with that skill: it adopts the SAME
canonical target (`~/.claude/projects`) and the SAME "already a symlink → leave it alone" fast
path, so running it on a laptop that already uses the skill changes nothing.

WHY symlink the WHOLE `projects/` dir (never per-repo): Claude Code's `--resume` picker
enumerates `projects/` with a directory filter that does NOT follow symlinks, so a per-repo
symlink child is silently dropped from resume. Memory needs no separate handling — it physically
lives at `projects/<repo-slug>/memory/`, so sharing `projects/` shares memory as a side effect.

BOUNDARY (never crossed): this touches ONLY `projects/` and, with `include_plugins=True`,
`plugins/cache` + `plugins/marketplaces`. It NEVER touches `.credentials.json`, `settings.json`,
`.claude.json`, or `history.jsonl` — a seat is a distinct Anthropic account and those stay
per-seat. Migration of a pre-existing REAL `projects/` is fold-NEVER-clobber: a repo not already
in the canonical store is moved in, a stale per-repo symlink is dropped, and a genuine name
collision is left untouched and reported for manual review — data is never overwritten or lost.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

from . import fleet

# subpaths a seat may share, each relative to both the seat dir and the canonical ~/.claude.
PROJECTS = "projects"
PLUGIN_SUBPATHS = ("plugins/cache", "plugins/marketplaces")

# result statuses (see LinkResult.status).
OK = "ok"  # already correctly symlinked to canonical — no-op
LINKED = "linked"  # created the symlink this run
WOULD_LINK = "would-link"  # --check: absent/empty target that WOULD be linked
FOLDED = "folded"  # migrated a real dir's contents into canonical, then symlinked
WOULD_FOLD = "would-fold"  # --check: real dir that WOULD be folded+linked
CONFLICT = "conflict"  # symlink points elsewhere, or real content collides — left untouched
CONFLICT_STATUSES = frozenset({CONFLICT})


@dataclasses.dataclass(frozen=True)
class LinkResult:
    seat: str
    subpath: str
    status: str
    detail: str = ""


def canonical_dir(subpath: str, *, home: Path | None = None) -> Path:
    """The canonical shared location for `subpath` — always under the real `~/.claude`."""
    return (home or Path.home()) / ".claude" / subpath


def _fold_real_dir(target: Path, canon: Path) -> tuple[int, list[str]]:
    """Move children of a real `target` dir into `canon` without ever clobbering: a stale per-repo
    symlink is dropped; a child absent from `canon` is moved in; a name that already exists in
    `canon` is left in place. Returns (moved_count, [collision names still in target]).
    """
    moved = 0
    collisions: list[str] = []
    for child in sorted(target.iterdir()):
        dest = canon / child.name
        if child.is_symlink():
            child.unlink()  # old per-repo link is superseded by the whole-dir symlink; safe to drop
        elif not dest.exists():
            shutil.move(str(child), str(dest))
            moved += 1
        else:
            collisions.append(child.name)
    return moved, collisions


def _link_one(
    seat_name: str, seat_dir: Path, subpath: str, *, check: bool, home: Path | None
) -> LinkResult:
    target = seat_dir / subpath
    canon = canonical_dir(subpath, home=home)

    if target.is_symlink():
        # resolve() (strict=False) fully follows the link and absolutizes even a not-yet-created
        # canonical path, so this correctly matches whether or not ~/.claude/projects exists yet.
        resolved = target.resolve()
        if resolved == canon.resolve():
            return LinkResult(seat_name, subpath, OK, f"-> {canon}")
        return LinkResult(seat_name, subpath, CONFLICT, f"symlink points elsewhere: {resolved}")

    if target.exists() and not target.is_dir():
        return LinkResult(seat_name, subpath, CONFLICT, "a non-directory file is in the way")

    def _do_link() -> None:
        canon.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(canon)

    if not target.exists():
        if check:
            return LinkResult(seat_name, subpath, WOULD_LINK, f"-> {canon}")
        _do_link()
        return LinkResult(seat_name, subpath, LINKED, f"-> {canon}")

    # target is a real, existing directory.
    has_content = any(target.iterdir())
    if not has_content:
        if check:
            return LinkResult(seat_name, subpath, WOULD_LINK, f"empty dir -> {canon}")
        target.rmdir()
        _do_link()
        return LinkResult(seat_name, subpath, LINKED, f"-> {canon}")

    if check:
        return LinkResult(seat_name, subpath, WOULD_FOLD, "real dir with content -> fold into canonical")

    canon.mkdir(parents=True, exist_ok=True)
    moved, collisions = _fold_real_dir(target, canon)
    if collisions:
        return LinkResult(
            seat_name,
            subpath,
            CONFLICT,
            f"folded {moved}; left as a real dir — {len(collisions)} name collision(s) "
            f"kept for review: {', '.join(collisions[:5])}{'…' if len(collisions) > 5 else ''}",
        )
    target.rmdir()
    _do_link()
    return LinkResult(seat_name, subpath, FOLDED, f"folded {moved} -> {canon}")


def share_seats(
    seats: list[fleet.Seat],
    *,
    home: Path | None = None,
    check: bool = False,
    include_plugins: bool = False,
) -> list[LinkResult]:
    """Link every seat's `projects/` (and, if `include_plugins`, the plugin cache/marketplaces)
    to the canonical `~/.claude` store. Idempotent: already-correct symlinks report `ok`. With
    `check=True` nothing is modified — statuses describe what WOULD happen.
    """
    subpaths = [PROJECTS, *(PLUGIN_SUBPATHS if include_plugins else ())]
    results: list[LinkResult] = []
    for seat in seats:
        for subpath in subpaths:
            results.append(_link_one(seat.name, seat.path, subpath, check=check, home=home))
    return results
