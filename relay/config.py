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
    # None = fall back to [defaults].tokens_per_percent. Per-seat because Claude accounts differ by
    # SUBSCRIPTION TIER: one percent of a Max 20x five-hour window holds several times the tokens of
    # one percent of a Pro window, so a single global constant would starve the large seats and
    # overshoot the small ones. See Config.tokens_per_percent.
    tokens_per_percent: float | None = None


@dataclasses.dataclass(frozen=True)
class LaunchBudget:
    """`Config.launch_budget_for()`'s answer: may this seat be launched, and if so under what
    output-token allowance. `reason` is operator-facing text for the pick_seat notes / run log —
    "why was this seat skipped" and "how big a budget did it get, and from what" are both questions
    that get asked at 3am, and neither is reconstructible from the numbers after the fact.
    """

    startable: bool
    # Output tokens this launch may spend. None means UNBOUNDED — not zero. Only meaningful when
    # `startable`; a non-startable budget carries no allowance at all.
    allowance: int | None = None
    reason: str = ""


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

    # ── Soft-ceiling → token-budget bridge ────────────────────────────────────────────────────
    # `ceiling_pct` above is only a PRE-LAUNCH gate: it decides which seat to start on and when to
    # rotate off afterwards, but nothing stops a long run from consuming the rest of the window
    # once it has started (observed: a seat with a 70% ceiling finishing a run at 97%). gad-kit's
    # workflow scripts cannot poll usage themselves — they have no network and no filesystem, only
    # the harness `budget` object — so the ONLY way to express "stop at N% of this seat's 5-hour
    # window" *inside* a generation is to hand the run an output-token allowance sized to the seat's
    # remaining headroom, and let gad-kit pause itself at a unit boundary once it has spent that much.
    #
    # ⚠️ SCOPE, stated honestly because it is narrower than "pauses at any phase boundary": the
    # pause can only fire at a gate gad-kit actually reaches, and every pre-Verify gate is disabled by
    # default for resume safety (see min_token_target). A generation whose Verify is clean on the
    # first iteration therefore never consults the allowance at all. What bounds THAT run is the
    # `min_token_target` LAUNCH GATE — relay simply does not start a seat that cannot afford a whole
    # generation. The in-flight pause covers the tail case the launch gate cannot predict: a verify
    # loop that keeps finding work. Both halves are needed; neither alone closes the overshoot.
    #
    # ⚠️ The allowance travels as a WORKFLOW ARG (`tokenAllowance`, see gadkit.command()), NOT as the
    # `token_target` prompt directive, and gad-kit gates on `budget.spent()`, NOT on
    # `budget.remaining()`. That is forced by the CLI, verified by disassembling the installed
    # bundle (2.1.220): the workflow sandbox builds `budget.total` from a module variable that is
    # initialized to null and whose ONLY writer is a function with zero call sites anywhere in the
    # bundle — so `budget.total` is ALWAYS null and `budget.remaining()` is ALWAYS Infinity, no
    # matter what the `+2M` directive says. `budget.spent()` is the one member that works: it sums
    # `outputTokens` across all model usage in the `claude` PROCESS, from a baseline that is likewise
    # never advanced, so it is process-cumulative. Relay launches exactly one process per run, so
    # process-cumulative and per-run coincide here — which is what makes a flat per-launch allowance
    # correct, including across gad-run's multi-generation crawl (parent and children share the one
    # counter, so the allowance is a single shared pool needing no per-generation subdivision).
    #
    # `tokens_per_percent` converts one percent of a 5h window into the OUTPUT tokens that
    # `budget.spent()` actually counts. `[seats.<name>].tokens_per_percent` overrides it per
    # account, and loop.py refines a learned per-seat value from each run's observed ratio, so
    # subscription-tier differences converge on their own without the operator declaring a tier.
    #
    # ⚠️ THE DEFAULT IS AN ESTIMATE, NOT A MEASUREMENT, and it is the least certain number in this
    # module. `budget.spent()` counts output tokens summed over every subagent, which is NOT a
    # figure the run logs expose: a full generation on this project reported ~1.48M tokens of all
    # types (dominated by cache reads) for ~100% of a five-hour window, and output is typically a
    # small single-digit-to-ten-percent share of such a mix — hence ~1.2k output tokens per
    # percent. It is calibrated for real WITHOUT the operator doing arithmetic: gad-run appends its
    # true `budget.spent()` to `.gad/perf-history.jsonl`, and `loop._learn_tokens_per_percent()`
    # divides that by the seat's observed percent delta for the same run and stores the ratio in
    # seat state, where it outranks this default (see `resolve_seat_tokens_per_percent`).
    #
    # Error directions are NOT symmetric. Too HIGH ⇒ the derived target never binds and the soft
    # pause silently never fires (inert, fails safe). Too LOW ⇒ every seat is judged unable to
    # fund even one phase; `derive_token_target_for()` returns None and the seat is skipped rather
    # than started, which is why an under-estimate degrades into "no seat is startable" instead of
    # the far worse "pause before Plan on every seat, forever, making no progress".
    tokens_per_percent: float = 1200.0
    # Subtracted from the headroom before conversion so the derived target aims BELOW the ceiling
    # instead of exactly at it. The conversion is noisy and overshoot is the harmful direction.
    headroom_safety_pct: float = 5.0
    # ⚠️ OFF BY DEFAULT, deliberately. Turning derivation on without a MEASURED
    # `tokens_per_percent` for your accounts is actively dangerous, because the same unknown scale
    # appears twice — here, and in gad-kit's per-phase `PHASE_TOKENS` estimates — and getting the
    # ratio wrong breaks the pool in one of three ways:
    #   • rate too HIGH  ⇒ the target never binds; the soft pause never fires (inert).
    #   • rate too LOW   ⇒ no seat can fund one phase; every seat is skipped; nothing ever runs.
    #   • phase estimates too HIGH relative to the rate ⇒ every generation pauses immediately.
    # With this False, no `tokenAllowance` arg is passed at all, so every gad-kit gate is inert (they
    # are all no-ops when the allowance is absent) and behaviour is bit-for-bit unchanged.
    #
    # ROLLOUT, and it is self-calibrating: run generations with this False. Each run leaves its real
    # `budget.spent()` in `.gad/perf-history.jsonl`, and loop.py pairs it with the seat's percent delta
    # to learn that account's true output-tokens-per-percent into seat state (visible in
    # `claude-relay status`). Once a seat has a learned rate that looks stable, sanity-check gad-kit's
    # `phaseTokens` against the same scale and set this True. Pin
    # `[seats.<name>].tokens_per_percent` only to OVERRIDE what was learned.
    derive_token_target: bool = False
    # Floor, and it is a LAUNCH GATE rather than a clamp: a seat whose headroom is worth less than
    # this is SKIPPED by `pick_seat()`, not started with a small allowance.
    #
    # ⚠️ THE FLOOR IS THE PRIMARY ENFORCEMENT MECHANISM, not a safety detail — because the pause
    # cannot stop a generation that never reaches a pause point. gad-kit's gates before Verify are
    # all behind `pauseBeforeVerifyPhases`, which defaults OFF for a hard resume-safety reason (a
    # pre-Verify pause leaves no `reviews/verification.md`, so relay's artifact census reads "nothing
    # happened", and `triage()` responds by `git stash push -u`-ing the whole tree and restarting
    # from Preflight — and nothing ever pops that stash). What remains enabled by default is the
    # verify-loop continuation gate and the VerifyFix gate. So for a generation whose Verify comes
    # back clean on the first iteration, NO gate is ever consulted and the run bills its full cost
    # regardless of the allowance.
    #
    # Read it as the rule it encodes: **only start a seat that can afford a WHOLE generation.** The
    # soft pause then earns its keep on the expensive tail — a verify loop that keeps finding work —
    # which is precisely the overshoot case that motivated the feature (a 70%-ceiling seat observed
    # finishing at 97%).
    #
    # This default deliberately MATCHES gad-kit's own `perGenTokens` default, because the two sides
    # must agree about what a generation costs: relay decides whether to launch, gad-kit decides
    # whether to start the next generation once launched, and if relay's figure were the larger one it
    # would hand over allowances gad-kit then refuses to begin — burning a launch to do nothing.
    #
    # It is only the FLOOR, not the estimate. `gadkit.adaptive_generation_cost()` raises it to the
    # worst of the last few real generations (from `.gad/perf-history.jsonl`) plus gad-kit's own ×1.15
    # margin, mirroring gad-kit's adaptive rule — so on any repo with history the number in force is
    # MEASURED, and this constant only matters before that history exists.
    min_token_target: int = 200_000
    # When `claude-relay init`/`adopt` runs, whether to turn a bare ~/.claude login into a named
    # seat (~/.claude-default): "always" (default) adopts it whenever ~/.claude has a login and
    # the seat doesn't exist yet; "if-empty" only when no other named seats exist; "never" skips.
    adopt_default: str = "always"

    # [seats.<name>] — per-seat ceiling override / pool exclusion.
    seat_configs: dict[str, SeatConfig] = dataclasses.field(default_factory=dict)
    # Repeatable CLI `--ceiling <name>=<pct>` (highest precedence; run/--dry-run only).
    ceiling_overrides: dict[str, float] = dataclasses.field(default_factory=dict)

    # [gadkit]
    gadkit_tier: str = "budget"  # gad-kit calls this its --profile flag; see gadkit.py docstring
    # NOTE: there is deliberately no `extra_flags` knob (removed 2026-07-26). It was dead config:
    # `Plan.extra_flags` was populated by triage() and never read by `gadkit.command()`, and it
    # could not have worked as documented anyway — command() builds a `Workflow({scriptPath, args})`
    # JSON OBJECT, so the things it advertised ("--milestone", "--skip-premortem") would have to be
    # `milestone: true` / `skipPreMortem: true` args KEYS, not CLI flag strings. Inventing that
    # replacement is a separate decision; do not re-add a flag list.

    # [plugins].dirs — OPT-IN plugin names/paths exposed to every headless run as `--plugin-dir`.
    # Default empty: gad-kit needs NO per-seat plugin (it runs by absolute path + built-in tools).
    # Name an entry only for a plugin you want available inside runs on every seat; `"*"` = all.
    # See plugins.py for why blanket-loading behavioural plugins (e.g. context-mode) is a bad idea.
    plugin_dirs: list[str] = dataclasses.field(default_factory=list)

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

    def resolve_seat_tokens_per_percent(
        self, seat_name: str, *, learned: float | None = None
    ) -> float:
        """Tokens per one percent of `seat_name`'s 5-hour window. Precedence (highest first):
        `[seats.<name>].tokens_per_percent` > the `learned` value loop.py measured from this
        seat's own past runs > `[defaults].tokens_per_percent`.

        An explicit per-seat number outranks the learned one deliberately: if an operator has
        pinned a value for an account, that is a statement of intent about a known subscription
        tier, and a noisy measurement should not silently override it.
        """
        seat_cfg = self.seat_configs.get(seat_name)
        if seat_cfg is not None and seat_cfg.tokens_per_percent is not None:
            return seat_cfg.tokens_per_percent
        if learned is not None and learned > 0:
            return float(learned)
        return self.tokens_per_percent

    def launch_budget_for(
        self,
        seat_name: str,
        current_pct: float | None,
        *,
        learned: float | None = None,
        required_tokens: int | None = None,
        required_why: str = "",
    ) -> LaunchBudget:
        """What one launch on `seat_name` may spend, given that seat's live usage percent.

        This is the bridge that lets gad-kit's in-script soft pause mean "stop at N% of THIS
        account's 5-hour window" — see Config.tokens_per_percent for the mechanism and for why the
        figure is an output-token allowance rather than the `token_target` prompt directive.

        Three genuinely different answers, which is why this returns a small record instead of an
        overloaded `int | None`:

        * `startable, allowance=None` — derivation is off, or the seat has no live reading to size
          against. The launch proceeds UNBOUNDED (no `tokenAllowance` arg, gad-kit's gates inert):
          with no reading there is no headroom to size against, and guessing would be strictly worse
          than the pre-existing behaviour.
        * `startable, allowance=N` — spend at most N output tokens.
        * **not startable** — the headroom cannot fund one generation, so THIS SEAT MUST NOT BE
          LAUNCHED AT ALL. Callers must honour that. Handing over a too-small allowance instead
          would livelock: gad-kit would pause at its first gate, relay would rotate, the next
          seat would pause at its first gate too, and the pool would spend a launch per seat
          while making no progress whatsoever.

        `required_tokens` overrides `min_token_target` with a MEASURED per-repo generation cost
        (`gadkit.adaptive_generation_cost()`, from `.gad/perf-history.jsonl`); `required_why` is its
        one-phrase provenance for the operator-facing note. Callers without a repo in hand — the
        `--dry-run` preview, the CLI — omit both and get the configured floor, which is exactly right
        for a repo-independent question.
        """
        if not self.derive_token_target:
            return LaunchBudget(startable=True, reason="token-allowance derivation disabled")
        if current_pct is None:
            return LaunchBudget(startable=True, reason="no live usage reading to size against")
        ceiling = self.resolve_seat_ceiling(seat_name)
        headroom_pct = ceiling - current_pct - self.headroom_safety_pct
        per_pct = self.resolve_seat_tokens_per_percent(seat_name, learned=learned)
        tokens = int(max(0.0, headroom_pct) * per_pct)
        needed = self.min_token_target if required_tokens is None else max(0, required_tokens)
        needed_why = required_why or "configured floor"
        if tokens < needed:
            return LaunchBudget(
                startable=False,
                reason=(
                    f"{max(0.0, headroom_pct):.1f}% of headroom below the {ceiling}% ceiling "
                    f"(at {current_pct}%, less {self.headroom_safety_pct}% safety) is worth only "
                    f"~{tokens} output tokens at {per_pct:.0f}/pct — under the {needed} a generation "
                    f"needs ({needed_why})"
                ),
            )
        # Never exceed the operator's global target: `token_target` stays a hard upper bound, so
        # enabling derivation can only ever make a launch's budget SMALLER, never larger.
        cap = parse_token_target(self.token_target)
        if cap is not None:
            tokens = min(tokens, cap)
        return LaunchBudget(
            startable=True,
            allowance=tokens,
            reason=(
                f"{max(0.0, headroom_pct):.1f}% of headroom below the {ceiling}% ceiling "
                f"at {per_pct:.0f} output tokens/pct"
            ),
        )

    def effective_exclude(self) -> list[str]:
        """`exclude` merged with any `[seats.<name>]` marked `exclude = true` or `main = true`."""
        names = set(self.exclude)
        for name, seat_cfg in self.seat_configs.items():
            if seat_cfg.exclude or seat_cfg.main:
                names.add(name)
        return sorted(names)


def parse_token_target(raw_value: str | None) -> int | None:
    """Parse a token-target directive (`"+2M"`, `"500k"`, `"+350000"`) into an integer count.

    Returns None for anything unparseable, including None itself — callers treat that as "no
    stated bound" rather than zero, because a misread directive must never silently become a
    budget of nothing. Deliberately tolerant of a missing `+` and of either case for the k/M
    suffix, since this string is hand-written in config.toml.
    """
    if raw_value is None:
        return None
    text = str(raw_value).strip().lstrip("+").replace("_", "")
    if not text:
        return None
    multiplier = 1
    if text[-1] in ("k", "K"):
        multiplier, text = 1_000, text[:-1]
    elif text[-1] in ("m", "M"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    if value <= 0:
        return None
    return int(value * multiplier)


def _to_float(raw_value: Any, field: str) -> float:
    """B30 audit fix: a malformed numeric TOML value (wrong type, non-numeric string) used to
    raise a bare `ValueError` straight out of `load_config()` — unhelpful and inconsistent with
    every OTHER config problem in this module, which raises `ConfigError`. Wraps the coercion
    with a message naming the actual offending field and value.
    """
    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} must be a number, got {raw_value!r}") from exc


def _to_int(raw_value: Any, field: str) -> int:
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} must be an integer, got {raw_value!r}") from exc


def _validate(cfg: Config) -> None:
    """B30 audit fix: numeric config values were never sanity-checked at all, so a typo could
    silently wreck unattended operation rather than fail loudly at startup:
      - `run_timeout_s = 0` (or negative) makes `runner.run()`'s very first deadline check see
        `remaining <= 0` immediately — EVERY run "times out" having never given the child a
        chance, burning the whole fleet into repeated timeout-cooldowns doing zero real work.
      - `start_margin > ceiling_pct` makes `start_cap = ceiling_pct - start_margin` negative,
        so `percent < start_cap` (a real percent is never negative) can never be satisfied —
        `pick_seat()` would never select ANY seat, ever, silently, and B2's all-exhausted-forever
        stall follows even though every seat is individually healthy.
      - `poll_ttl <= 0` would make every single seat check re-poll the live endpoint on every
        call — the endpoint's own self-rate-limit then turns nearly every read into a 429.
    Applies to every per-seat `ceiling_pct` override too (`[seats.<name>]` and CLI `--ceiling`),
    since `resolve_seat_ceiling()` can return any of them.
    """
    if cfg.run_timeout_s <= 0:
        raise ConfigError(f"run_timeout_s must be > 0, got {cfg.run_timeout_s!r}")
    if cfg.poll_ttl <= 0:
        raise ConfigError(f"poll_ttl must be > 0, got {cfg.poll_ttl!r}")
    if cfg.max_units < 0:
        raise ConfigError(f"max_units must be >= 0 (0 = unlimited), got {cfg.max_units!r}")
    if not (0.0 < cfg.ceiling_pct <= 100.0):
        raise ConfigError(f"[defaults].ceiling_pct must be in (0, 100], got {cfg.ceiling_pct!r}")
    if cfg.start_margin < 0.0:
        raise ConfigError(f"[defaults].start_margin must be >= 0, got {cfg.start_margin!r}")
    if cfg.ceiling_pct - cfg.start_margin <= 0.0:
        raise ConfigError(
            f"[defaults].start_margin ({cfg.start_margin!r}) must be < ceiling_pct "
            f"({cfg.ceiling_pct!r}) — otherwise no seat's percent can ever be low enough to "
            "start a fresh unit on, and the pool silently never runs anything"
        )
    if cfg.tokens_per_percent <= 0.0:
        raise ConfigError(
            f"[defaults].tokens_per_percent must be > 0, got {cfg.tokens_per_percent!r} — a "
            "zero or negative rate would derive a token target of nothing for every seat and "
            "make gad-kit pause before its very first phase, forever"
        )
    if cfg.headroom_safety_pct < 0.0:
        raise ConfigError(
            f"[defaults].headroom_safety_pct must be >= 0, got {cfg.headroom_safety_pct!r}"
        )
    if cfg.min_token_target <= 0:
        raise ConfigError(
            f"[defaults].min_token_target must be > 0, got {cfg.min_token_target!r}"
        )
    for name, seat_cfg in cfg.seat_configs.items():
        if seat_cfg.ceiling_pct is not None and not (0.0 < seat_cfg.ceiling_pct <= 100.0):
            raise ConfigError(
                f"[seats.{name}].ceiling_pct must be in (0, 100], got {seat_cfg.ceiling_pct!r}"
            )
        if seat_cfg.tokens_per_percent is not None and seat_cfg.tokens_per_percent <= 0.0:
            raise ConfigError(
                f"[seats.{name}].tokens_per_percent must be > 0, got "
                f"{seat_cfg.tokens_per_percent!r}"
            )
    for name, pct in cfg.ceiling_overrides.items():
        if not (0.0 < pct <= 100.0):
            raise ConfigError(f"--ceiling {name}={pct!r} must be in (0, 100]")

    if cfg.derive_token_target:
        # Refuse an ARITHMETICALLY UNSATISFIABLE combination rather than accept it and idle.
        #
        # Every individual value can be perfectly legal while the set of them makes EVERY seat
        # unstartable — and the failure is silent and self-sealing, which is what makes it worth a
        # hard error. The best case any seat can ever present is a completely fresh window at 0%
        # usage, worth `(ceiling - safety) * rate` tokens; if even that is under `min_token_target`
        # then `launch_budget_for()` returns not-startable for every seat at every percent forever,
        # `pick_seat()` finds no candidate, and the pool waits on cooldowns that will never help.
        # It cannot recover on its own either: a seat that never launches never writes a calibration
        # record, so it can never learn the better rate that would have made it startable.
        #
        # The shipped defaults are exactly this shape on purpose — (70 - 5) * 1200 = 78k against a
        # 200k floor — because `tokens_per_percent`'s default is an ESTIMATE, and the honest response
        # to "you enabled derivation without measuring your accounts" is to say so at startup rather
        # than to look healthy and quietly stop working.
        best_ceiling = max(
            [cfg.ceiling_pct]
            + [sc.ceiling_pct for sc in cfg.seat_configs.values() if sc.ceiling_pct is not None]
            + list(cfg.ceiling_overrides.values())
        )
        best_rate = max(
            [cfg.tokens_per_percent]
            + [
                sc.tokens_per_percent
                for sc in cfg.seat_configs.values()
                if sc.tokens_per_percent is not None
            ]
        )
        best_case = (best_ceiling - cfg.headroom_safety_pct) * best_rate
        if best_case < cfg.min_token_target:
            raise ConfigError(
                "[defaults].derive_token_target is true, but no seat can ever clear the launch "
                f"floor: the most generous case possible — a fresh window at 0% usage under the "
                f"highest configured ceiling ({best_ceiling}%), less {cfg.headroom_safety_pct}% "
                f"safety, at the highest configured rate ({best_rate:.0f} tokens/pct) — is worth "
                f"{best_case:.0f} output tokens, under the {cfg.min_token_target} "
                "min_token_target. Every seat would be skipped and the pool would never run "
                "anything. Fix by MEASURING your accounts: leave derive_token_target = false for a "
                "few runs and let relay learn each seat's tokens_per_percent from "
                ".gad/perf-history.jsonl (see `claude-relay status`), then raise "
                "[defaults].tokens_per_percent — or pin [seats.<name>].tokens_per_percent — to the "
                "observed value before enabling this. Lowering min_token_target instead re-opens "
                "the overshoot this feature exists to close; see Config.min_token_target."
            )


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
        cfg.poll_ttl = _to_float(raw["poll_ttl"], "poll_ttl")
    if "token_target" in raw:
        cfg.token_target = str(raw["token_target"])
    if "max_units" in raw:
        cfg.max_units = _to_int(raw["max_units"], "max_units")
    if "run_timeout_s" in raw:
        cfg.run_timeout_s = _to_float(raw["run_timeout_s"], "run_timeout_s")

    defaults = raw.get("defaults") or {}
    if isinstance(defaults, dict):
        if "ceiling_pct" in defaults:
            cfg.ceiling_pct = _to_float(defaults["ceiling_pct"], "[defaults].ceiling_pct")
        if "start_margin" in defaults:
            cfg.start_margin = _to_float(defaults["start_margin"], "[defaults].start_margin")
        if "adopt_default" in defaults:
            mode = str(defaults["adopt_default"]).strip().lower()
            cfg.adopt_default = mode if mode in ("always", "if-empty", "never") else "always"
        if "tokens_per_percent" in defaults:
            cfg.tokens_per_percent = _to_float(
                defaults["tokens_per_percent"], "[defaults].tokens_per_percent"
            )
        if "headroom_safety_pct" in defaults:
            cfg.headroom_safety_pct = _to_float(
                defaults["headroom_safety_pct"], "[defaults].headroom_safety_pct"
            )
        if "min_token_target" in defaults:
            cfg.min_token_target = _to_int(
                defaults["min_token_target"], "[defaults].min_token_target"
            )
        if "derive_token_target" in defaults:
            cfg.derive_token_target = bool(defaults["derive_token_target"])

    seats_raw = raw.get("seats") or {}
    if isinstance(seats_raw, dict):
        for seat_name, seat_table in seats_raw.items():
            if not isinstance(seat_table, dict):
                continue
            cfg.seat_configs[str(seat_name)] = SeatConfig(
                ceiling_pct=(
                    _to_float(seat_table["ceiling_pct"], f"[seats.{seat_name}].ceiling_pct")
                    if "ceiling_pct" in seat_table
                    else None
                ),
                exclude=bool(seat_table.get("exclude", False)),
                main=bool(seat_table.get("main", False)),
                tokens_per_percent=(
                    _to_float(
                        seat_table["tokens_per_percent"],
                        f"[seats.{seat_name}].tokens_per_percent",
                    )
                    if "tokens_per_percent" in seat_table
                    else None
                ),
            )

    gadkit = raw.get("gadkit") or {}
    if isinstance(gadkit, dict):
        if "tier" in gadkit:
            cfg.gadkit_tier = str(gadkit["tier"])

    plugins_raw = raw.get("plugins") or {}
    if isinstance(plugins_raw, dict) and isinstance(plugins_raw.get("dirs"), list):
        cfg.plugin_dirs = [str(x) for x in plugins_raw["dirs"]]

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
                cfg.ceiling_overrides.update(
                    {str(k): _to_float(v, f"--ceiling {k}") for k, v in value.items()}
                )
            continue
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    if cfg.gadkit_tier not in ("budget", "balanced"):
        raise ConfigError(
            f"[gadkit].tier must be 'budget' or 'balanced', got {cfg.gadkit_tier!r} "
            "(gad-kit's slash command flag is --profile; claude-relay calls this a 'tier' "
            "internally to avoid the name collision — see DESIGN.md header)."
        )

    _validate(cfg)

    return cfg
