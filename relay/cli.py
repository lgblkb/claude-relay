"""claude-relay CLI. Subcommands: run, status, login-check, init, adopt, disable, enable,
resolve, seats, monitor.

This module is the console entry point declared in pyproject.toml
(`claude-relay = "relay.cli:main"`), so a `pip`/`pipx`/`uv tool` install exposes it as the
`claude-relay` command directly. The `bin/claude-relay` script is a thin shim around
`main()` for the clone-and-symlink dev flow (`install.sh`); both share this one code path.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
from pathlib import Path

from relay import config as config_mod
from relay import cooldown, fleet, gadkit, loop, monitor, share

DEFAULT_ADOPT_NAME = "default"

# Minimal starter config written by `claude-relay init` (for pipx/uv installs that never run
# install.sh). The fully-documented reference lives in config.example.toml in the repo; this is
# deliberately short — every key is optional and claude-relay runs on defaults with an empty file.
_DEFAULT_CONFIG = """\
# claude-relay config. Full documented reference:
#   https://github.com/lgblkb/claude-relay/blob/main/config.example.toml
# Every key is optional; claude-relay runs on built-in defaults with an empty file.

# Absolute path to your gad-bootstrapped repo (or pass it to `claude-relay run <repo>`).
repo = ""

# Seat names (suffix after "~/.claude-") to never pool. Bare "~/.claude" is always excluded.
exclude = ["yerasyl"]

[defaults]
# Synthetic per-seat rotation ceiling — deliberately LOWER than Claude's real 100% usage cap.
ceiling_pct = 70
# On `init`/`adopt`, turn a bare ~/.claude login into a named seat (~/.claude-default):
#   "always" (default) | "if-empty" (only when no other seats) | "never".
# Switch any seat off/on at runtime with `claude-relay disable <name>` / `enable <name>`.
adopt_default = "always"

[telegram]
# notify-out + resolve-in channel. Secrets may instead come from the
# CLAUDE_RELAY_TELEGRAM_BOT_TOKEN / CLAUDE_RELAY_TELEGRAM_CHAT_ID env vars (those win). NEVER logged.
bot_token = ""
chat_id = ""
"""


@dataclasses.dataclass(frozen=True)
class AdoptResult:
    status: str  # "adopted" | "exists" | "no-source" | "skipped"
    name: str
    seat_dir: Path


def _adopt_default_seat(name: str, *, home: Path | None = None, force: bool = False) -> AdoptResult:
    """Turn the bare ~/.claude login into a named seat ~/.claude-<name> by copying its
    credentials into a fresh, private (0700/0600) config dir. Never touches ~/.claude itself.
    Idempotent: returns "exists" (no write) if the seat already has credentials, unless `force`.
    Copying just `.credentials.json` is enough to authenticate; Claude Code initializes the rest
    of that config dir on first run. (The file is copied, never read/echoed.)
    """
    home = home or Path.home()
    src = home / ".claude" / ".credentials.json"
    seat_dir = home / f".claude-{name}"
    dst = seat_dir / ".credentials.json"
    if not src.is_file():
        return AdoptResult("no-source", name, seat_dir)
    if dst.exists() and not force:
        return AdoptResult("exists", name, seat_dir)
    seat_dir.mkdir(parents=True, exist_ok=True)
    try:
        seat_dir.chmod(0o700)
    except OSError:  # pragma: no cover - best-effort on exotic filesystems
        pass
    shutil.copy2(src, dst)
    try:
        dst.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    return AdoptResult("adopted", name, seat_dir)


def _maybe_adopt(
    cfg: config_mod.Config, *, name: str, no_adopt: bool, force: bool = False, home: Path | None = None
) -> AdoptResult:
    """Run adoption according to `[defaults].adopt_default` (unless `--no-adopt`): "never" skips;
    "if-empty" adopts only when no usable named seat exists yet; "always" adopts whenever
    ~/.claude has a login and the target seat isn't there already.
    """
    if no_adopt or cfg.adopt_default == "never":
        return AdoptResult("skipped", name, (home or Path.home()) / f".claude-{name}")
    home = home or Path.home()
    if cfg.adopt_default == "if-empty":
        usable = [s for s in fleet.discover_seats(cfg.effective_exclude(), home=home) if s.usable]
        if usable:
            return AdoptResult("skipped", name, home / f".claude-{name}")
    return _adopt_default_seat(name, home=home, force=force)


def _report_adopt(res: AdoptResult, *, verbose: bool) -> None:
    """Print the outcome of an adoption. `init` stays quiet on no-op/skip (verbose=False); the
    explicit `adopt` command reports every outcome (verbose=True).
    """
    if res.status == "adopted":
        print(
            f"adopted your ~/.claude login as seat {res.name!r} -> {res.seat_dir}\n"
            f"  (shares one account/quota with ~/.claude; "
            f"turn it off with `claude-relay disable {res.name}`, undo with `rm -rf {res.seat_dir}`)"
        )
    elif not verbose:
        return
    elif res.status == "exists":
        print(f"seat {res.name!r} already exists at {res.seat_dir} (use --force to re-copy credentials)")
    elif res.status == "no-source":
        print("nothing to adopt: no ~/.claude/.credentials.json (log in with `claude` first)")
    elif res.status == "skipped":
        print(f"adoption skipped (adopt_default policy or --no-adopt); no changes to {res.seat_dir}")


def _parse_ceiling_overrides(raw: list[str] | None) -> dict[str, float]:
    """Parse repeatable `--ceiling <seatname>=<pct>` tokens into a dict. Raises SystemExit(2)
    with a clear message on a malformed token rather than silently ignoring it.
    """
    overrides: dict[str, float] = {}
    for token in raw or []:
        name, sep, pct_str = token.partition("=")
        if not sep or not name or not pct_str:
            print(f"error: --ceiling expects <seatname>=<pct>, got {token!r}", file=sys.stderr)
            raise SystemExit(2)
        try:
            overrides[name] = float(pct_str)
        except ValueError:
            print(f"error: --ceiling percent must be a number, got {token!r}", file=sys.stderr)
            raise SystemExit(2) from None
    return overrides


def _load_config(args: argparse.Namespace) -> config_mod.Config:
    config_path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    overrides: dict[str, object] = {}
    if getattr(args, "profile", None):
        overrides["profile"] = args.profile
    ceiling_tokens = getattr(args, "ceiling", None)
    if ceiling_tokens:
        overrides["ceiling_overrides"] = _parse_ceiling_overrides(ceiling_tokens)
    try:
        return config_mod.load_config(config_path, repo=getattr(args, "repo", None), **overrides)
    except config_mod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    repo_str = cfg.repo
    if not repo_str:
        print("error: no repo given (pass a path, or set `repo` in config.toml)", file=sys.stderr)
        return 2
    repo = Path(repo_str).expanduser().resolve()
    if not repo.is_dir():
        print(f"error: repo path does not exist or is not a directory: {repo}", file=sys.stderr)
        return 2

    if args.dry_run:
        state = cooldown.load_state(cfg.state_path)
        preview = loop.dry_run_preview(repo, cfg, state)
        print(json.dumps(preview, indent=2, default=str))
        return 0

    try:
        return loop.run(repo, cfg, once=args.once)
    except loop.LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    state = cooldown.load_state(cfg.state_path)
    report = loop.status_report(cfg, state)
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_login_check(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    seats = fleet.discover_seats(cfg.effective_exclude())
    if not seats:
        print("no seats discovered (looked for ~/.claude-* directories with .credentials.json)")
        return 1
    disabled = cooldown.disabled_seats(cooldown.load_state(cfg.state_path))
    exit_code = 0
    for seat in seats:
        if seat.name in disabled:
            status = "disabled"  # off by operator; still a real login, just out of rotation
        elif seat.usable:
            status = "usable"
        else:
            status = "needs-login"
        if seat.needs_login:
            exit_code = 1
        print(f"{status:12s} {seat.name:20s} {seat.path}")
    return exit_code


def cmd_resolve(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    repo_str = args.repo or cfg.repo
    if not repo_str:
        print("error: no repo given (pass --repo, or set `repo` in config.toml)", file=sys.stderr)
        return 2
    repo = Path(repo_str).expanduser().resolve()
    result = gadkit.resolve_owner_decision(repo, args.decision_id, args.answer)
    if not result.found:
        print(
            f"no open ownerDecision with id={args.decision_id!r} found in {result.index_path}",
            file=sys.stderr,
        )
        return 1
    print(f"resolved {args.decision_id!r} in {result.index_path}: {result.decision}")
    if not result.committed:
        # B13 audit fix, second round: the resolution itself DID apply (the JSON write already
        # succeeded and is what `blocking_decisions()` reads — the repo IS unblocked right now),
        # but its own commit failed (a rejecting pre-commit hook, or no git identity configured
        # in {repo}). Loud, not silent: a failure here must never read as an ordinary clean
        # resolution. `triage()` will not sweep this specific uncommitted change into a stash
        # (it is exempted), and will retry committing it on every future cycle — but committing
        # it by hand now removes any doubt.
        print(
            f"WARNING: the resolution was applied to disk but its own git commit FAILED "
            f"(check for a rejecting pre-commit hook, or missing git user.name/user.email, in "
            f"{repo}). The repo is unblocked already, and claude-relay will keep retrying the "
            f"commit automatically — but you should commit {result.index_path} by hand to be "
            "safe.",
            file=sys.stderr,
        )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Create ~/.claude-relay/ (config + logs) for a fresh install AND adopt a bare ~/.claude
    login into a named seat (per [defaults].adopt_default). Safe to re-run: never overwrites an
    existing config.toml unless --force, and adoption is idempotent. Mainly for pipx/uv installs,
    which don't run install.sh; the clone flow's install.sh seeds the richer config.example.toml.
    """
    config_path = _config_path_arg(args) or config_mod.default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_mod.default_log_dir().mkdir(parents=True, exist_ok=True)

    if config_path.exists() and not args.force:
        print(f"config already exists: {config_path} (left as-is; --force to overwrite)")
    else:
        config_path.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        try:
            config_path.chmod(0o600)
        except OSError:  # pragma: no cover - best-effort on exotic filesystems
            pass
        print(f"wrote {config_path}")

    cfg = _load_config(args)
    _report_adopt(_maybe_adopt(cfg, name=args.adopt_name, no_adopt=args.no_adopt), verbose=False)

    print(
        "edit `repo` and [telegram] (or set CLAUDE_RELAY_TELEGRAM_* env vars), "
        "then run `claude-relay login-check`."
    )
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    """Explicitly turn the bare ~/.claude login into a named seat (ignores adopt_default policy —
    if you asked, you get it). Idempotent unless --force."""
    res = _adopt_default_seat(args.name, force=args.force)
    _report_adopt(res, verbose=True)
    return 0 if res.status in ("adopted", "exists") else 1


def _toggle_seat(args: argparse.Namespace, *, disable: bool) -> int:
    cfg = _load_config(args)
    state = cooldown.load_state(cfg.state_path)
    changed = cooldown.set_seat_disabled(state, args.seat, disable)
    cooldown.save_state(cfg.state_path, state)
    verb = "disabled" if disable else "enabled"
    print(f"seat {args.seat!r} {verb}." if changed else f"seat {args.seat!r} was already {verb}.")
    known = {s.name for s in fleet.discover_seats(cfg.effective_exclude())}
    if args.seat not in known:
        print(f"  note: no seat named {args.seat!r} discovered yet — setting saved, applies if it appears.")
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    """Keep a seat out of rotation (pick_seat skips it); it still shows in `seats`/`login-check`."""
    return _toggle_seat(args, disable=True)


def cmd_enable(args: argparse.Namespace) -> int:
    """Put a previously-disabled seat back into rotation."""
    return _toggle_seat(args, disable=False)


def cmd_share(args: argparse.Namespace) -> int:
    """Symlink every seat's `projects/` to one canonical `~/.claude/projects` so all seats share
    session history + memory (mirrors the multi-profile-shared-claude skill; compatible with it).
    `--plugins` also shares the plugin cache/marketplaces; `--check` reports without changing
    anything. Returns non-zero only if a real conflict was found (a symlink pointing elsewhere,
    or a name collision left for manual review).
    """
    home = Path.home()
    # ALL real seats — NOT the rotation-filtered set: you still want an `exclude`d/disabled seat's
    # sessions shared. discover_seats already skips ~/.claude-relay and the bare ~/.claude.
    seats = fleet.discover_seats(exclude=None, home=home)
    if not seats:
        print("No seats discovered (nothing under ~/.claude-* looks like a profile) — nothing to share.")
        return 0

    results = share.share_seats(
        seats, home=home, check=bool(args.check), include_plugins=bool(args.plugins)
    )
    canon = share.canonical_dir(share.PROJECTS, home=home)
    verb = "would change" if args.check else "state"
    print(f"Sharing seats -> canonical {canon}  ({verb}):\n")
    width = max((len(r.seat) for r in results), default=6)
    for r in results:
        print(f"  {r.seat:<{width}}  {r.subpath:<20}  {r.status:<11}  {r.detail}")

    conflicts = [r for r in results if r.status in share.CONFLICT_STATUSES]
    changed = [r for r in results if r.status in (share.LINKED, share.FOLDED)]
    print()
    if args.check:
        pending = [r for r in results if r.status in (share.WOULD_LINK, share.WOULD_FOLD)]
        print(
            f"{len(pending)} link(s) pending, {len(conflicts)} conflict(s). "
            "Re-run without --check to apply."
        )
    else:
        print(f"{len(changed)} link(s) created/folded, {len(conflicts)} conflict(s) left for review.")
    if conflicts:
        print("Conflicts were left untouched (nothing was overwritten) — resolve them by hand.")
        return 1
    return 0


def _config_path_arg(args: argparse.Namespace) -> Path | None:
    return Path(args.config).expanduser() if getattr(args, "config", None) else None


def cmd_seats(args: argparse.Namespace) -> int:
    """Print the live+fallback all-seats usage table. `--watch [SECS]` refreshes in place
    (default 60s); without it, prints a single snapshot and exits (handy over SSH).
    """
    cfg = _load_config(args)
    watch = getattr(args, "watch", None)
    if watch is None:
        monitor.run_seats_panel(cfg, once=True)
    else:
        interval = float(watch) if watch else monitor.DEFAULT_SEATS_INTERVAL_S
        monitor.run_seats_panel(cfg, interval=interval, once=False)
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    """Observe-only tmux cockpit: supervisor log · all-seats usage table · git/gad status.
    NEVER launches `run` — start the loop yourself (another pane / SSH).
    """
    cfg = _load_config(args)
    repo_str = args.repo or cfg.repo
    repo = Path(repo_str).expanduser().resolve() if repo_str else None
    try:
        monitor.launch(
            cfg,
            repo,
            session=args.session,
            interval=float(args.interval),
            config_path=_config_path_arg(args),
            attach=not args.no_attach,
        )
    except monitor.MonitorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"monitor session {args.session!r} is up (tmux attach -t {args.session})")
    return 0


def cmd_panel(args: argparse.Namespace) -> int:
    """Internal: the process each tmux pane runs. Not meant to be called directly."""
    cfg = _load_config(args)
    if args.which == "log":
        monitor.run_log_panel(cfg.log_dir)
    elif args.which == "repo":
        repo_str = getattr(args, "repo", None) or cfg.repo
        repo = Path(repo_str).expanduser().resolve() if repo_str else None
        monitor.run_repo_panel(repo)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-relay", description=__doc__)
    parser.add_argument("--config", help="path to config.toml (default: ~/.claude-relay/config.toml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the rotation loop")
    p_run.add_argument("repo", nargs="?", help="absolute path to the target repo (else config.toml's `repo`)")
    p_run.add_argument("--once", action="store_true", help="perform a single iteration and exit")
    p_run.add_argument("--dry-run", action="store_true", help="print the plan/seat/argv; spawn nothing")
    p_run.add_argument(
        "--profile", choices=("budget", "balanced"), help="override [gadkit].tier for this run"
    )
    p_run.add_argument(
        "--ceiling",
        action="append",
        metavar="SEATNAME=PCT",
        help="override a seat's synthetic rotation ceiling for this run (repeatable, e.g. --ceiling sam=80)",
    )
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="print seat + triage status as JSON")
    p_status.set_defaults(func=cmd_status)

    p_login = sub.add_parser("login-check", help="list discovered seats and their login state")
    p_login.set_defaults(func=cmd_login_check)

    p_init = sub.add_parser("init", help="create ~/.claude-relay/ + adopt ~/.claude into a seat")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config.toml")
    p_init.add_argument("--no-adopt", action="store_true", help="skip adopting ~/.claude into a named seat")
    p_init.add_argument(
        "--adopt-name", default=DEFAULT_ADOPT_NAME,
        help=f"name for the adopted seat (default: {DEFAULT_ADOPT_NAME})",
    )
    p_init.set_defaults(func=cmd_init)

    p_adopt = sub.add_parser("adopt", help="turn the bare ~/.claude login into a named seat")
    p_adopt.add_argument(
        "--name", default=DEFAULT_ADOPT_NAME, help=f"seat name (default: {DEFAULT_ADOPT_NAME})"
    )
    p_adopt.add_argument("--force", action="store_true", help="re-copy credentials even if the seat exists")
    p_adopt.set_defaults(func=cmd_adopt)

    p_disable = sub.add_parser("disable", help="keep a seat out of rotation (still shown in the fleet)")
    p_disable.add_argument("seat", help="seat name (the ~/.claude-<name> suffix)")
    p_disable.set_defaults(func=cmd_disable)

    p_enable = sub.add_parser("enable", help="put a previously-disabled seat back into rotation")
    p_enable.add_argument("seat", help="seat name (the ~/.claude-<name> suffix)")
    p_enable.set_defaults(func=cmd_enable)

    p_share = sub.add_parser(
        "share", help="share session history + memory across seats (symlink projects/ to a canonical store)"
    )
    p_share.add_argument(
        "--check", action="store_true", help="report what would change without modifying anything"
    )
    p_share.add_argument(
        "--plugins", action="store_true",
        help="also share plugins/cache + plugins/marketplaces (full mirror, like the skill)",
    )
    p_share.set_defaults(func=cmd_share)

    p_resolve = sub.add_parser("resolve", help="mark an ownerDecision resolved in generations-index.json")
    p_resolve.add_argument("decision_id")
    p_resolve.add_argument("answer")
    p_resolve.add_argument("--repo", help="absolute path to the target repo (else config.toml's `repo`)")
    p_resolve.set_defaults(func=cmd_resolve)

    p_seats = sub.add_parser("seats", help="print the live+fallback all-seats usage table")
    p_seats.add_argument(
        "--watch",
        nargs="?",
        const="",
        metavar="SECS",
        help="refresh in place every SECS seconds (default 60); omit for a single snapshot",
    )
    p_seats.set_defaults(func=cmd_seats)

    p_monitor = sub.add_parser("monitor", help="observe-only tmux cockpit (log · seats · git/gad)")
    p_monitor.add_argument(
        "repo", nargs="?", help="repo to watch in the git/gad pane (else config.toml's `repo`)"
    )
    p_monitor.add_argument("--session", default=monitor.DEFAULT_SESSION, help="tmux session name")
    p_monitor.add_argument(
        "--interval", type=float, default=monitor.DEFAULT_SEATS_INTERVAL_S,
        help="seats-table refresh interval in seconds (default 60)",
    )
    p_monitor.add_argument(
        "--no-attach", action="store_true",
        help="build the session but don't attach (then `tmux attach -t <session>`)",
    )
    p_monitor.set_defaults(func=cmd_monitor)

    # Internal: the per-pane processes `monitor` spawns via tmux. No `help=` -> stays out of the
    # command-description list (it still appears in the {choices} metavar; that's an argparse limit).
    p_panel = sub.add_parser("_panel")
    p_panel.add_argument("which", choices=("log", "repo"))
    p_panel.add_argument("--repo", help="repo path for the repo panel (else config.toml's `repo`)")
    p_panel.set_defaults(func=cmd_panel)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
