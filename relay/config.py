"""Configuration: `config.toml` (§8 of DESIGN.md) merged with environment variables and CLI
overrides. No hardcoded paths anywhere — every filesystem location is derived from
`pathlib.Path.home()` or an explicit override.

Load order (later wins): built-in defaults -> config.toml -> environment variables
(only for the two Telegram secrets, per DESIGN.md §7) -> explicit CLI overrides passed by
the caller as keyword arguments to `load_config()`.

Requires Python >= 3.11 for the stdlib `tomllib` reader. On older interpreters we raise a
clear, actionable error rather than silently vendoring a third-party TOML parser (this
tool is stdlib-only by design — see DESIGN.md constraints).
"""

from __future__ import annotations

import dataclasses
import os
import sys
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    """Raised for unusable configuration (e.g. Python too old for tomllib, malformed TOML)."""


def _require_tomllib() -> Any:
    # Deliberately runtime-checked despite pyproject's `requires-python >= 3.11`: an operator
    # can still invoke this file directly with an older interpreter, and a clear message here
    # beats an opaque `ModuleNotFoundError: tomllib` several lines down.
    if sys.version_info < (3, 11):  # noqa: UP036 - see comment above; this guard is intentional
        raise ConfigError(
            "claude-relay requires Python >= 3.11 (stdlib 'tomllib' for config.toml parsing); "
            f"found {sys.version_info.major}.{sys.version_info.minor}. Upgrade your interpreter "
            "— this tool is intentionally stdlib-only and will not vendor a TOML parser."
        )
    import tomllib  # noqa: PLC0415 (intentionally deferred: only needed here, keeps import cheap)

    return tomllib


def default_state_dir() -> Path:
    """`~/.claude-relay` — never hardcoded, always derived from the real home directory."""
    return Path.home() / ".claude-relay"


def default_config_path() -> Path:
    return default_state_dir() / "config.toml"


def default_log_dir() -> Path:
    return default_state_dir() / "logs"


def default_state_path() -> Path:
    return default_state_dir() / "state.json"


@dataclasses.dataclass
class SeatConfig:
    """Per-seat override, `[seats.<name>]` in config.toml. `exclude`/`main` both mean "keep
    this seat OUT of the rotation pool" (a `main`/daily-driver seat an operator still uses
    interactively is exactly as pool-ineligible as an explicitly excluded one).
    """

    ceiling_pct: float | None = None  # None = fall back to [defaults].ceiling_pct
    exclude: bool = False
    main: bool = False


@dataclasses.dataclass
class Config:
    """Resolved configuration for one claude-relay invocation. All fields have sane defaults
    so a bare `config.toml` (or none at all) is usable — DESIGN.md §8 says "all optional".
    """

    repo: str | None = None
    exclude: list[str] = dataclasses.field(default_factory=lambda: ["yerasyl"])
    poll_ttl: float = 90.0
    token_target: str = "+2M"
    max_units: int = 0  # 0 = until DONE; counts completed RUN/FINISH units (loop.py enforces)
    run_timeout_s: float = 7200.0  # wall-clock cap on one `claude` invocation (runner.py)

    # [defaults] — the synthetic per-seat 5h ceiling: a rotation gate LOWER than Claude's real
    # 100%, replacing the old global high_pct/start_cap. Rotate off / don't start a seat once
    # its real usage percent reaches `ceiling_pct`; prefer seats with percent < ceiling_pct -
    # start_margin. See `resolve_seat_ceiling()` for the full per-seat precedence.
    ceiling_pct: float = 70.0
    start_margin: float = 5.0

    # [seats.<name>] — per-seat ceiling override / pool exclusion.
    seat_configs: dict[str, SeatConfig] = dataclasses.field(default_factory=dict)
    # Repeatable CLI `--ceiling <name>=<pct>` (highest precedence; run/--dry-run only).
    ceiling_overrides: dict[str, float] = dataclasses.field(default_factory=dict)

    # [gadkit]
    gadkit_tier: str = "budget"  # gad-kit calls this its --profile flag; see gadkit.py docstring
    gadkit_extra_flags: list[str] = dataclasses.field(default_factory=list)

    # [telegram]
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # [notify] — `sink` is the only field DESIGN.md §8 shows explicitly; `command`/
    # `webhook_url`/`shellular_command` are additive knobs the `command`/`webhook`/`shellular`
    # sinks need to be configurable at all (notify.py degrades gracefully to stdout if the
    # chosen sink's knob is unset — see notify.py's `dispatch()`). TRUST BOUNDARY: these three
    # are EXECUTED (a local shell command) or POSTed to (a webhook URL) verbatim — they must
    # only ever come from this operator's own local, trusted config.toml, never from an
    # untrusted or remote source (see README.md's "Trust boundary" section).
    notify_sink: str = "telegram"
    notify_command: str | None = None
    notify_webhook_url: str | None = None
    shellular_command: str | None = None

    # Filesystem locations (never hardcoded; always Path.home()-derived unless overridden).
    state_dir: Path = dataclasses.field(default_factory=default_state_dir)
    log_dir: Path = dataclasses.field(default_factory=default_log_dir)
    state_path: Path = dataclasses.field(default_factory=default_state_path)

    def resolve_seat_ceiling(self, seat_name: str) -> float:
        """The synthetic ceiling percent that applies to `seat_name`. Precedence (highest
        first): CLI `--ceiling` override > `[seats.<name>].ceiling_pct` > `[defaults].ceiling_pct`.
        """
        if seat_name in self.ceiling_overrides:
            return self.ceiling_overrides[seat_name]
        seat_cfg = self.seat_configs.get(seat_name)
        if seat_cfg is not None and seat_cfg.ceiling_pct is not None:
            return seat_cfg.ceiling_pct
        return self.ceiling_pct

    def effective_exclude(self) -> list[str]:
        """`exclude` merged with any `[seats.<name>]` marked `exclude = true` or `main = true`."""
        names = set(self.exclude)
        for name, seat_cfg in self.seat_configs.items():
            if seat_cfg.exclude or seat_cfg.main:
                names.add(name)
        return sorted(names)


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    tomllib = _require_tomllib()
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - trivial passthrough of stdlib error
        raise ConfigError(f"malformed TOML at {path}: {exc}") from exc


def load_config(
    config_path: Path | None = None,
    *,
    repo: str | None = None,
    dry_run: bool = False,  # accepted for CLI convenience; not stored (loop.py owns run-mode flags)
    **cli_overrides: Any,
) -> Config:
    """Build a `Config` by merging (in increasing precedence): defaults, config.toml, the two
    Telegram env vars, then any explicit `cli_overrides` keyword (only overrides fields whose
    value is not None, so callers can pass argparse's `Namespace.__dict__` wholesale).
    """
    del dry_run  # not part of Config; the `run` subcommand handles it directly
    path = config_path or default_config_path()
    raw = _read_toml(path)

    cfg = Config()
    if repo is not None:
        cfg.repo = repo
    elif "repo" in raw:
        cfg.repo = str(raw["repo"])

    if "exclude" in raw and isinstance(raw["exclude"], list):
        cfg.exclude = [str(x) for x in raw["exclude"]]
    if "poll_ttl" in raw:
        cfg.poll_ttl = float(raw["poll_ttl"])
    if "token_target" in raw:
        cfg.token_target = str(raw["token_target"])
    if "max_units" in raw:
        cfg.max_units = int(raw["max_units"])
    if "run_timeout_s" in raw:
        cfg.run_timeout_s = float(raw["run_timeout_s"])

    defaults = raw.get("defaults") or {}
    if isinstance(defaults, dict):
        if "ceiling_pct" in defaults:
            cfg.ceiling_pct = float(defaults["ceiling_pct"])
        if "start_margin" in defaults:
            cfg.start_margin = float(defaults["start_margin"])

    seats_raw = raw.get("seats") or {}
    if isinstance(seats_raw, dict):
        for seat_name, seat_table in seats_raw.items():
            if not isinstance(seat_table, dict):
                continue
            cfg.seat_configs[str(seat_name)] = SeatConfig(
                ceiling_pct=float(seat_table["ceiling_pct"]) if "ceiling_pct" in seat_table else None,
                exclude=bool(seat_table.get("exclude", False)),
                main=bool(seat_table.get("main", False)),
            )

    gadkit = raw.get("gadkit") or {}
    if isinstance(gadkit, dict):
        if "tier" in gadkit:
            cfg.gadkit_tier = str(gadkit["tier"])
        if "extra_flags" in gadkit and isinstance(gadkit["extra_flags"], list):
            cfg.gadkit_extra_flags = [str(x) for x in gadkit["extra_flags"]]

    telegram = raw.get("telegram") or {}
    if isinstance(telegram, dict):
        cfg.telegram_bot_token = telegram.get("bot_token") or cfg.telegram_bot_token
        cfg.telegram_chat_id = telegram.get("chat_id") or cfg.telegram_chat_id

    notify = raw.get("notify") or {}
    if isinstance(notify, dict):
        if "sink" in notify:
            cfg.notify_sink = str(notify["sink"])
        if "command" in notify:
            cfg.notify_command = str(notify["command"])
        if "webhook_url" in notify:
            cfg.notify_webhook_url = str(notify["webhook_url"])
        if "shellular_command" in notify:
            cfg.shellular_command = str(notify["shellular_command"])

    # Environment variables override config.toml for the two secrets only (never logged).
    env_token = os.environ.get("CLAUDE_RELAY_TELEGRAM_BOT_TOKEN")
    env_chat = os.environ.get("CLAUDE_RELAY_TELEGRAM_CHAT_ID")
    if env_token:
        cfg.telegram_bot_token = env_token
    if env_chat:
        cfg.telegram_chat_id = env_chat

    # Explicit CLI overrides win last (only non-None values participate).
    for key, value in cli_overrides.items():
        if value is None:
            continue
        if key == "profile":  # CLI convenience alias for [gadkit].tier
            cfg.gadkit_tier = str(value)
            continue
        if key == "ceiling_overrides":
            # Repeatable `--ceiling <seatname>=<pct>` (highest precedence). `value` is a dict
            # here (bin/claude-relay parses each "name=pct" token before calling us) — merge
            # rather than replace so CLI overrides never wipe an already-resolved dict.
            if isinstance(value, dict):
                cfg.ceiling_overrides.update({str(k): float(v) for k, v in value.items()})
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    if cfg.gadkit_tier not in ("budget", "balanced"):
        raise ConfigError(
            f"[gadkit].tier must be 'budget' or 'balanced', got {cfg.gadkit_tier!r} "
            "(gad-kit's slash command flag is --profile; claude-relay calls this a 'tier' "
            "internally to avoid the name collision — see DESIGN.md header)."
        )

    return cfg
