"""Resolve operator-selected Claude Code plugins into absolute `--plugin-dir` arguments for
headless runs.

WHY THIS EXISTS (and why it defaults to nothing):
`CLAUDE_CONFIG_DIR` relocates a seat's ENTIRE config dir, `plugins/` included, with no shared
fallback — so a plugin installed/enabled in the bare `~/.claude` is NOT enabled for any
`~/.claude-<seat>`. claude-relay's own gad-kit workload sidesteps this entirely (it invokes the
bundled workflow by ABSOLUTE `scriptPath` and injects roles by absolute-path prompt, using only
built-in tools — see gadkit.command()), so it needs ZERO per-seat plugin provisioning.

This module is the OPT-IN escape hatch for the *other* case: an operator who wants some specific
plugin's skills / slash-commands / agents available inside every seat's headless run. `claude
--plugin-dir <root>` loads a plugin from disk for that session regardless of per-seat enablement.
It is deliberately empty by default: blanket-loading behavioural plugins (e.g. context-mode, whose
SessionStart hook rewrites how an agent works) into gad-kit's tightly-choreographed coder/reviewer
sub-agents would add token cost and erode the run-to-run determinism that is the whole point of a
generational build. So the operator names EXACTLY which plugins earn a place, via `[plugins].dirs`.

A "plugin root" is the directory that contains a `.claude-plugin/plugin.json` manifest — the same
thing `claude --plugin-dir` expects. Under the cache, plugins nest as
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` (e.g. `.../gad-kit/gad-kit/1.5.0`), so
resolving a bare NAME means finding that manifest and picking the newest version.

The cache is always read from the *real* `~/.claude` (never a seat), because that is where
plugins are actually installed and it is readable no matter which `CLAUDE_CONFIG_DIR` the child
runs under (verified live; same rationale as gadkit.gadkit_plugin_root()).
"""

from __future__ import annotations

from pathlib import Path

_ALL_TOKENS = frozenset({"*", "all"})


def _version_key(path: Path) -> tuple[int, ...]:
    """Sort key from a version-looking dir name ("1.5.0" -> (1,5,0)); non-numeric tokens sort as
    0 so a stray dir never crashes `max()`. Mirrors gadkit._ver_key so both pick versions alike.
    """
    key: list[int] = []
    for token in path.name.split("."):
        try:
            key.append(int(token))
        except ValueError:
            key.append(0)
    return tuple(key)


def _is_plugin_root(path: Path) -> bool:
    return (path / ".claude-plugin" / "plugin.json").is_file()


def plugin_cache_base(*, home: Path | None = None, source_config_dir: Path | None = None) -> Path:
    """`<config>/plugins/cache` — where installed plugins live. Defaults to the real `~/.claude`
    (NOT a seat), since that is the shared install location every seat can read.
    """
    base = Path(source_config_dir) if source_config_dir is not None else (home or Path.home()) / ".claude"
    return base / "plugins" / "cache"


def _newest_version_roots(cache_base: Path) -> list[Path]:
    """Every installed plugin's newest-version root under `cache_base` (one per <marketplace>/
    <plugin>), each verified to carry a `.claude-plugin/plugin.json`.
    """
    by_plugin: dict[Path, Path] = {}  # keyed by the <plugin> dir (root.parent) -> newest root
    for manifest in cache_base.glob("*/*/*/.claude-plugin/plugin.json"):
        root = manifest.parent.parent  # <root>/.claude-plugin/plugin.json -> <root>
        current = by_plugin.get(root.parent)
        if current is None or _version_key(root) > _version_key(current):
            by_plugin[root.parent] = root
    return sorted(by_plugin.values(), key=lambda p: str(p))


def _resolve_one(name: str, cache_base: Path) -> Path | None:
    """Resolve a single `[plugins].dirs` entry to a plugin root, or None if it can't be found.

    An entry may be an ABSOLUTE PATH to a plugin root (used as-is if it carries a manifest), or a
    bare plugin NAME resolved against the cache under both the nested
    `<marketplace>/<name>/<version>/` and the flattened `<name>/<version>/` layouts, newest wins.
    """
    candidate = Path(name).expanduser()
    if candidate.is_absolute():
        return candidate if _is_plugin_root(candidate) else None

    roots: set[Path] = set()
    for pattern in (f"*/{name}/*/.claude-plugin/plugin.json", f"{name}/*/.claude-plugin/plugin.json"):
        for manifest in cache_base.glob(pattern):
            roots.add(manifest.parent.parent)
    if not roots:
        return None
    return max(roots, key=_version_key)


def resolve_plugin_dirs(
    names: list[str] | None,
    *,
    home: Path | None = None,
    source_config_dir: Path | None = None,
) -> list[Path]:
    """Turn `[plugins].dirs` entries into deduped absolute plugin-root paths (order preserved).

    `"*"`/`"all"` expands to every installed plugin's newest version. Entries that can't be
    resolved (typo, not installed) are silently dropped rather than raising — a missing optional
    plugin must never crash the rotation loop; the caller can diff requested-vs-resolved to warn.
    """
    if not names:
        return []
    cache_base = plugin_cache_base(home=home, source_config_dir=source_config_dir)
    resolved: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        if path not in seen:
            seen.add(path)
            resolved.append(path)

    for name in names:
        if name.strip().lower() in _ALL_TOKENS:
            for root in _newest_version_roots(cache_base):
                _add(root)
        else:
            root = _resolve_one(name, cache_base)
            if root is not None:
                _add(root)
    return resolved


def plugin_flags(dirs: list[Path] | list[str]) -> list[str]:
    """Interleave resolved plugin roots into repeated `--plugin-dir <root>` claude CLI flags."""
    flags: list[str] = []
    for d in dirs:
        flags.extend(["--plugin-dir", str(d)])
    return flags
