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
Telegram-first notifier — plus the **observe-only tmux monitor** (`monitor` / `seats`). The rest
of Phase 2 (standalone OAuth refresh so *idle* seats poll truly-live, calibrated `--max N`
batching) is still out of scope here — see "Monitoring" below for what the monitor does and
doesn't show.

## Requirements

- Python >= 3.11 (stdlib `tomllib` for `config.toml`; no third-party dependencies at all).
- The `claude` CLI on `PATH`, logged in on at least one named seat (`~/.claude-<name>`).
- A repo already bootstrapped with gad-kit's `/gad-init` (`.gad/generations-index.json` and
  `.gad/backlog.md` present).

## Install

claude-relay is stdlib-only (no third-party dependencies), so any of these works on a fresh
machine. Pick one:

**With [uv](https://docs.astral.sh/uv/) (recommended):**

```bash
uv tool install git+https://github.com/lgblkb/claude-relay
claude-relay init          # seed ~/.claude-relay/config.toml (+ logs dir)
```

**With [pipx](https://pipx.pypa.io/):**

```bash
pipx install git+https://github.com/lgblkb/claude-relay
claude-relay init
```

**One-liner** (auto-detects uv → pipx → git clone; seeds the config for you):

```bash
curl -fsSL https://raw.githubusercontent.com/lgblkb/claude-relay/main/install.sh | bash
```

**From a clone** (for hacking on it — runs in place, no build step):

```bash
git clone https://github.com/lgblkb/claude-relay && cd claude-relay
./install.sh               # symlinks bin/claude-relay into ~/.local/bin, seeds config, runs verify.sh
```

After installing, edit `~/.claude-relay/config.toml` — at minimum set `repo` and, if you want
Telegram notifications/resolve-in, `[telegram].bot_token` / `chat_id` (or the
`CLAUDE_RELAY_TELEGRAM_BOT_TOKEN` / `CLAUDE_RELAY_TELEGRAM_CHAT_ID` environment variables) —
then sanity-check the fleet with `claude-relay login-check`. Make sure `~/.local/bin` is on your
`PATH`.

**Update / uninstall:** `uv tool upgrade claude-relay` / `pipx upgrade claude-relay` (or re-run
the one-liner); `uv tool uninstall claude-relay` / `pipx uninstall claude-relay`.

## Seats

A "seat" is a directory `~/.claude-<name>` holding its own `.credentials.json` — a distinct
Anthropic OAuth login with its own independent 5-hour usage window. claude-relay discovers
every `~/.claude-*` directory, minus:

- bare `~/.claude` (always excluded in v1 — its `.claude.json` lives at `~/.claude.json`, the
  historical default location, so `CLAUDE_CONFIG_DIR=~/.claude` produces a degraded session);
- any name in `exclude` (default `["yerasyl"]`).

`claude-relay login-check` lists every discovered seat and whether it currently has a usable
access token (or is `disabled`, see below).

### Adopting your main account

The bare `~/.claude` is excluded for a *layout* reason, not because that account is off-limits:
a normal seat keeps its `.credentials.json` **and** `.claude.json` in one dir, but the default
profile's `.claude.json` sits at `~/.claude.json` (one level up), so `CLAUDE_CONFIG_DIR=~/.claude`
gives a degraded session. So on a fresh machine with only the main login, `init`/`adopt`
**adopts** it — copies `~/.claude/.credentials.json` into a proper named seat `~/.claude-default`
(0700 dir / 0600 file; `~/.claude` is never touched). After that your main account is a
first-class, rotatable seat. This runs automatically on install; tune it with
`[defaults].adopt_default = "always" | "if-empty" | "never"` or `init --no-adopt`, and re-run by
hand any time with `claude-relay adopt`. The adopted seat shares one account/quota with
`~/.claude`, and with a single account there's no failover — bump `ceiling_pct` toward ~95 and
add more accounts for real rotation.

### Enabling / disabling seats

`claude-relay disable <name>` keeps a seat **out of rotation** without deleting anything — it
still appears in `seats`/`login-check` (marked `disabled`), but `pick_seat` skips it; re-enable
with `claude-relay enable <name>`. This is a one-command runtime toggle stored in `state.json`,
distinct from the static config `exclude` (which hides a dir entirely) and `main = true`.

### Sharing session history & memory across seats

Because `CLAUDE_CONFIG_DIR` relocates a seat's *entire* config dir, each `~/.claude-<name>` gets
its own `projects/` — so by default seats do **not** share session history or memory. Rotation
doesn't need them to (the workload resumes from the repo's `.gad/` on disk, never a `--resume`
transcript), but if you want every seat to see the same history/memory — the way a laptop set up
with the `multi-profile-shared-claude` skill does — run:

```bash
claude-relay share            # symlink every seat's projects/ -> canonical ~/.claude/projects
claude-relay share --check    # report what would change, modify nothing
claude-relay share --plugins  # ALSO share plugins/cache + plugins/marketplaces (full mirror)
```

It symlinks each seat's **whole** `projects/` dir (the `--resume` picker doesn't follow per-repo
symlinks) to one canonical `~/.claude/projects`; memory rides along automatically because it lives
at `projects/<repo-slug>/memory/`. It is **idempotent and compatible** with that skill — a seat
already correctly linked is reported `ok` and left alone — and **safe**: it never touches
`.credentials.json` / `settings.json` / `.claude.json` / `history.jsonl` (a seat is a distinct
account), and a pre-existing real `projects/` is folded in *never-clobber* — a name collision is
left in place and reported for you to resolve by hand, never overwritten.

### Plugins in headless runs

claude-relay runs gad-kit on **every** seat with **zero** per-seat plugin setup: it invokes the
bundled workflow by absolute `scriptPath` and injects each role by absolute-path prompt, using only
built-in tools — so installing gad-kit once in your main `~/.claude` covers every seat. If you *also*
want some other plugin's skills / slash-commands / agents available inside runs on every seat, list
it under `[plugins].dirs` (a name resolved from `~/.claude/plugins/cache`, an absolute plugin root,
or `"*"` for all); claude-relay then passes `claude --plugin-dir <root>` on every run. Default is
`[]` — deliberately. Blanket-loading a *behavioural* plugin (e.g. `context-mode`, whose SessionStart
hook rewrites how an agent works) into gad-kit's tightly-choreographed coder/reviewer sub-agents adds
token cost and erodes the run-to-run determinism a generational build depends on. Name only the
plugins that genuinely earn a place.

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
claude-relay init                   # seed ~/.claude-relay/ + adopt ~/.claude into a seat
claude-relay adopt [--name default] # (re)adopt the bare ~/.claude login as a named seat
claude-relay disable <seat>         # keep a seat OUT of rotation (still shown in the fleet)
claude-relay enable  <seat>         # put a disabled seat back into rotation
claude-relay share [--check]        # share session history + memory across all seats (symlinks)
claude-relay status                 # offline seat + triage snapshot as JSON
claude-relay login-check            # list seats + login state (marks disabled seats)
claude-relay resolve <id> <answer>  # durably resolve an ownerDecision (unblocks AWAITING_HUMAN)
claude-relay seats                  # print the live+fallback all-seats usage table once (great over SSH)
claude-relay seats --watch [SECS]   # ...refreshing in place every SECS (default 60)
claude-relay monitor [repo]         # observe-only tmux cockpit (log · seats · git/gad); never launches a run
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
edit (`.gad/generations-index.json`'s matching `ownerDecisions[]` entry marked
`status: "answered"` — gad-kit's only unblocking token), which is what actually unblocks the
parked repo (not a chat reply).

## Monitoring

`claude-relay monitor [repo]` opens an **observe-only** tmux cockpit — three panes:

- **supervisor log** (left): a `tail -F` that always follows the newest `run-*.log` (each `claude
  -p` generation writes its own), switching automatically as generations turn over;
- **all-seats usage table** (top-right): auto-refreshing, **live + fallback** — each seat is polled
  live against the OAuth usage endpoint (same `poll_ttl`/~5-min self-rate-limit discipline the loop
  uses); a seat whose token has expired (`auth?`) or is momentarily unreachable falls back to the
  last-known reading in `state.json`, labelled `stale·N` with its age;
- **git log + `.gad/BUILD_STATUS.md`** (bottom-right) for the watched repo.

It is deliberately **observe-only**: `monitor` NEVER starts a run — you launch `claude-relay run`
yourself (another pane, or SSH). The monitor is not an input to any decision; seat *selection*
correctness lives entirely in the loop's `pick_seat()`, which polls live at the moment it decides.
Requires `tmux`; if it's absent, use `claude-relay seats --watch` (the same table, no tmux) — which
is also the best thing to run over SSH from a phone. Idle seats can't be polled truly-live without a
standalone OAuth token refresh (still deferred), so between runs an idle seat shows its last-known
`state.json` reading (or `auth?` if its token has since expired — a run refreshes it).

For watching a *single* generation closely, `./bin/run-progress` projects the raw NDJSON stream down
to one line per event worth acting on — each agent as it starts, rate-limit events, tool failures,
and the terminal `result` with cost and models:

```sh
./bin/run-progress                    # newest log under ~/.claude-relay/logs
./bin/run-progress <path-to-run.log>  # a specific run
```

```
AGENT[9] ▸ gad-generation: implement tokens=324728 tools=169 plan=Survey>Generations>▸ gad-generation
RATE_LIMIT five_hour status=allowed_warning utilization=0.9
RESULT subtype=success is_error=False stop_reason=end_turn turns=10 cost=$14.73 dur=3829s
```

`tail -f` on the raw log is not a substitute: one `assistant` envelope can be tens of KB. Two
projection rules in there are load-bearing and non-obvious — concurrent agents interleave their
progress events (so reporting on "changed since the last event" looks exactly like a stuck loop), and
not every `task_started` is a workflow. Both are pinned by `tests/test_progress.py`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Offline unit tests for the pure logic: usage `limits[]` parsing/thresholds, gad-kit triage's
decision order against fixture `.gad/` states (including the FINISH-vs-restart regression
tests), `detector.classify()`, the notify sinks/dedupe/regexes, and `pick_seat()`/
`SingleInstanceLock`. No network calls.

`tests_live/` holds real (non-mocked) live-verification tests for the network/process seams this
tool touches: the usage endpoint, Telegram, spawning `claude` itself, and (added 2026-07-27) the
rate-limit envelope capture. These are NOT part of the offline suite above; run them explicitly —
most require opt-in env vars, since they cost real quota or send a real message. Each seam's
declaration lives in its own test module's docstring; this repo is the GAD *supervisor* and is not
itself GAD-managed, so there is no `.gad/live-seams.json` here despite older references to one.

| module | seam | gate |
| --- | --- | --- |
| `test_usage_endpoint_live.py` | usage endpoint | auto (probe-skips without a seat) |
| `test_telegram_live.py` | Telegram send | `CLAUDE_RELAY_LIVE_TELEGRAM=1` |
| `test_claude_runner_live.py` | spawning `claude` | `CLAUDE_RELAY_LIVE_CLAUDE_RUN=1` |
| `test_rate_limit_capture_live.py` | Tier 0 usage schema | auto (probe-skips without a seat) |
| `test_rate_limit_capture_live.py` | Tier 2 real child + tap | `CLAUDE_RELAY_LIVE_CLAUDE_RUN=1` |

## Rate-limit calibration

Two constants in `relay/detector.py` are educated guesses rather than measurements:
`_KNOWN_SAFE_RATE_LIMIT_STATUSES` is a **one-element** set, and
`_RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD` is a hand-picked `0.9`. Only one
`rate_limit_event.status` value has ever been observed live — `allowed_warning`, at
`utilization: 0.76` — yet rotation decisions and multi-hour forced cooldowns ride on that guess.

The obstacle is that `utilization` is a property of the **account's window**, not of anything a
test can set up: you cannot manufacture 90% utilization, only spend it. `bin/rate-limit-probe`
is therefore tiered by cost, and only the last tier spends:

```bash
./bin/rate-limit-probe baseline                 # TIER 0 — zero tokens
./bin/rate-limit-probe exchange-rate --seat X   # TIER 0.5 — a few calls, prices a burn
./bin/rate-limit-probe burn --seat X            # TIER 2 — spends; walls one 5-hour window
./bin/rate-limit-probe report                   # answers the pre-registered questions
```

- **Tier 0** dumps the usage endpoint's *verbatim* payload per seat via `usage.fetch_usage_raw()`.
  `UsageSnapshot` keeps 3 of the ~55 fields the endpoint actually sends, so unknown fields and
  unseen enum values are invisible to every other code path. It also ranks seats by burn cost.
- **Tier 0.5** spends a deliberately tiny amount and reports how many points of **weekly** quota
  one point of **five-hour** costs — turning Tier 2's price into a number you approve in advance.
- **Tier 1** is passive and has no subcommand: set `CLAUDE_RELAY_CAPTURE_DIR` and
  `relay/capture.py` records every `rate_limit_event` and terminal `result` envelope from normal
  runs at ~zero marginal cost. This is what eventually catches a real `seven_day` wall, which is
  far too expensive to provoke on purpose. It never records `assistant` envelopes — those carry
  tool output, which can contain repository contents or secrets (Invariant #5).

  Where the variable has to live, **measured 2026-07-27 rather than assumed** (an earlier version
  of this paragraph claimed `.profile` covered the unattended case; it does not):

  | launch | reads | tap |
  |---|---|---|
  | interactive terminal tab | `~/.bashrc` | on |
  | login shell (`bash -lc`, ssh, tty login) | `~/.profile` | on |
  | `bash -c`, `sh -c`, `nohup`, cron, systemd | **neither** | **off** |

  `.bashrc` returns early when non-interactive and `.profile` is read only by *login* shells, so a
  non-login non-interactive shell reads no rc file at all — and that is exactly how an unattended
  multi-day `claude-relay run` gets launched. For those, set the variable in the launch itself
  (`CLAUDE_RELAY_CAPTURE_DIR=... nohup claude-relay run ...`), the crontab line, or the systemd
  unit's `Environment=`. Putting it in an rc file is not enough and fails silently, because the tap
  is deliberately a no-op when the variable is unset — there is no error to notice.

  Verify with `./bin/rate-limit-probe report` after a run — if it reports zero records while runs
  have happened, the tap is not reaching the supervisor's environment.
- **Tier 2** drives one seat's five-hour window to its wall. Cheap in the only sense that matters:
  unused five-hour capacity is not banked, so burning the *tail* of a window on a seat you were
  not going to use costs only the weekly quota those tokens also debit. It bypasses the
  supervisor's own `ceiling_pct = 70` by invoking `claude -p` directly, caps spend in weekly
  points, grants the burn no tools, and **fails closed** — if the usage endpoint becomes
  unreadable it stops, because the spend cap cannot be enforced without it.

The pre-registered questions (fixed before any data was collected) are documented at the top of
`relay/ratelimit_probe.py`, along with a standing prediction about a suspected window-awareness
bug in `_force_cooldown()`, recorded so the data can refute it rather than be rationalized after.

## What one generation actually costs

Measured on two real supervised runs (2026-07-27; full writeups in DESIGN.md §4b and §4c), both
ending in a committed generation with a green gate:

| | greenfield build | bugfix |
|---|---|---|
| agents | 17 | 12 |
| wall clock | 63.8 min | 34.1 min |
| dollars | **$14.73** | **$7.84** |
| `five_hour` points | **90** | — (window rolled mid-run) |
| `seven_day` points | **9** | **4** |
| per seat per week | **~4 generations** | **~8 generations** |

**Scope dominates.** A bugfix generation costs about half a greenfield one, so "cost per generation"
is a range, not a constant — budget by what the generation actually asks for.

Three things follow that are easy to get wrong:

- **`seven_day` is the budget; `five_hour` is only a throttle.** Every token debits both, but the
  five-hour window refills in hours while the weekly one takes a week — and *rotation cannot
  manufacture weekly capacity*, since each seat carries its own. Exhausting a seat's five-hour costs
  patience; exhausting its weekly costs up to seven days.
- **A "5-hour window" is ~65-70 minutes of real generation work.** Burn measured a steady
  ~1.45 points/minute, so a single seat cannot sustain much more than one generation per window. This
  is why rotation is load-bearing rather than a convenience.
- **The expensive model is not the one in the argv.** claude-relay spawns `opus` for the outer `-p`
  turn, but that was 3% of the bill; gad-kit's `budget` profile put the work on `sonnet` (92%) and
  `haiku` (5%). Cache reads were 98.6% of tokens and output tokens the majority of the cost, so cost
  tracks token *kind*, not token count.

Per-model figures for any captured run come from the tap:

```sh
CLAUDE_RELAY_CAPTURE_DIR=... claude-relay run     # collect (see the Tier-1 note above)
./bin/rate-limit-probe report                     # rate-limit + result envelope summary
```
