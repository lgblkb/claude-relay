# claude-relay

A standalone, stdlib-only Python 3 supervisor that keeps a long, headless `claude -p`
workload (currently: [gad-kit](https://github.com/lgblkb/gad-kit) generational runs) alive
across each account's real 5-hour usage limit, by rotating between authenticated **seats**
(distinct Anthropic accounts / `CLAUDE_CONFIG_DIR`s), detecting exhaustion from the real
Anthropic usage endpoint, resuming the workload on a fresh seat, and notifying the operator
(Telegram by default) when a human is actually needed.

See `DESIGN.md` for the full design (architecture, the rotation loop, seat-selection
thresholds, the gad-kit recovery-routing logic, state schema, notifier, config, and failure
modes). This is a **Phase 1** implementation: the rotation loop, fleet/usage/cooldown/runner,
the gad-kit triage/recovery brain, the detector, config, install/verify, and a minimal
Telegram-first notifier. Phase 2 (tmux monitor, standalone OAuth refresh, calibrated `--max N`
batching) is out of scope here — `claude-relay monitor` is a stub that says so.

## Requirements

- Python >= 3.11 (stdlib `tomllib` for `config.toml`; no third-party dependencies at all).
- The `claude` CLI on `PATH`, logged in on at least one named seat (`~/.claude-<name>`).
- A repo already bootstrapped with gad-kit's `/gad-init` (`.gad/generations-index.json` and
  `.gad/backlog.md` present).

## Install

```bash
./install.sh
```

This symlinks `bin/claude-relay` into `~/.local/bin/claude-relay`, writes
`~/.claude-relay/config.toml` from `config.example.toml` if one doesn't exist yet, and runs
`verify.sh`. Edit `~/.claude-relay/config.toml` — at minimum set `repo` and, if you want
Telegram notifications/resolve-in, `[telegram].bot_token` / `chat_id` (or the
`CLAUDE_RELAY_TELEGRAM_BOT_TOKEN` / `CLAUDE_RELAY_TELEGRAM_CHAT_ID` environment variables).

## Seats

A "seat" is a directory `~/.claude-<name>` holding its own `.credentials.json` — a distinct
Anthropic OAuth login with its own independent 5-hour usage window. claude-relay discovers
every `~/.claude-*` directory, minus:

- bare `~/.claude` (always excluded in v1 — its `.claude.json` lives at `~/.claude.json`, the
  historical default location, so `CLAUDE_CONFIG_DIR=~/.claude` produces a degraded session);
- any name in `exclude` (default `["yerasyl"]`).

`claude-relay login-check` lists every discovered seat and whether it currently has a usable
access token.

### Synthetic per-seat rotation ceiling

Rotation is gated on a **synthetic ceiling** — a percent deliberately LOWER than Claude's real
100% usage cap — not on real exhaustion. Default `[defaults].ceiling_pct = 70` for every pool
seat; override per-seat with `[seats.<name>].ceiling_pct`, or per-run with a repeatable CLI
flag: `claude-relay run --ceiling sam=85 --ceiling almas=60`. A `[seats.<name>]` table with
`main = true` or `exclude = true` keeps that seat out of the pool entirely (e.g. a daily-driver
account you still use interactively). See `config.example.toml` for the full `[defaults]` /
`[seats.*]` shape. This also fixes an early "dead zone": a seat idling between its ceiling and
literal exhaustion is correctly classified as having hit its wall (rotate), not as a dead agent.

## Usage

```bash
claude-relay run <repo> --dry-run   # prints the triage Plan + chosen seat + exact argv; spawns nothing
claude-relay run <repo> --once      # one triage/run/classify iteration, then exit
claude-relay run <repo>             # the full rotation loop (blocks; Ctrl-C to stop)
claude-relay status                 # offline seat + triage snapshot as JSON
claude-relay login-check            # list seats + login state
claude-relay resolve <id> <answer>  # durably resolve an ownerDecision (unblocks AWAITING_HUMAN)
claude-relay monitor                # Phase 2 stub
```

`repo` may also be set once in `config.toml` and omitted from every CLI invocation.

## How it decides what to run

`relay/gadkit.py` reads `.gad/generations-index.json` + `.gad/backlog.md` (disk truth only —
never model stdout) to decide, in order: is a `GATED` owner-decision blocking the next
generation (`AWAITING_HUMAN`)? Is there a consolidator `handoff.md` (`BLOCKED`)? Is the tree
dirty from a mid-generation interruption — and if so:

- is the dirtiness safely attributable to a claude-relay-initiated run at all (the current
  `git HEAD` matches the last HEAD claude-relay itself observed this repo clean at, AND the
  dirty set shows a `.gad/`/`generation-N/` signal)? If not, `AWAITING_HUMAN` — the tree is
  left completely untouched (never guess-stash unrelated work);
- does `.gad/generation-N/reviews/verification.md` exist? That file is written ONLY by the
  Verify-phase verifier, which can only ever run after Guardrails' test-writer succeeded — so
  its mere presence is disk-visible proof Implement **and** Guardrails/tests both completed,
  the ONE case it's safe to resume with `/gad-finish`;
- otherwise, a full, non-destructive `/gad-generation` restart (`git stash push -u` — never
  `git reset --hard` — with the stash ref + swept file list recorded in `state.json` and a
  low-priority notification).

Otherwise: is there a pending generation (`/gad-run --max 1 --profile <tier> <token_target>`),
or is the backlog exhausted (`DONE`)?

After each run, `relay/detector.py` is the single place that turns the result (disk diff + a
fresh usage reading + the run's stdout tail as a backstop only) into the loop's next action.
`max_units` (config.toml, 0 = unlimited) caps how many completed RUN/FINISH units one `run`
invocation performs before stopping cleanly; `run_timeout_s` (default 7200) caps how long any
single `claude` invocation may run before claude-relay kills its whole process group and
rotates off that seat.

## Trust boundary

`config.toml` is trusted, operator-authored local configuration — `[notify].command`,
`shellular_command`, and `webhook_url` are executed (a local shell command) or POSTed to
verbatim, with no sandboxing. Only ever populate these from your own local file; never point
claude-relay at a config.toml written by an untrusted or remote source.

## Notifications & resolving owner decisions

By default, events (`AWAITING_HUMAN`, `BLOCKED`, `DONE`, all-seats-exhausted, hard errors) are
pushed to Telegram. Reply `resolve <id> <answer>` or `status` in that chat, or run
`claude-relay resolve <id> <answer>` over SSH — both go through the exact same durable disk
edit (`.gad/generations-index.json`'s matching `ownerDecisions[]` entry marked `"resolved"`),
which is what actually unblocks the parked repo (not a chat reply).

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Offline unit tests for the pure logic: usage `limits[]` parsing/thresholds, gad-kit triage's
decision order against fixture `.gad/` states (including the FINISH-vs-restart regression
tests), `detector.classify()`, the notify sinks/dedupe/regexes, and `pick_seat()`/
`SingleInstanceLock`. No network calls.

`tests_live/` holds real (non-mocked) live-verification tests for the three network/process
seams this tool touches (the usage endpoint, Telegram, and spawning `claude` itself) — see
`.gad/live-seams.json`. These are NOT part of the offline suite above; run them explicitly
(most require opt-in env vars, since two of the three cost real quota / send a real message).
