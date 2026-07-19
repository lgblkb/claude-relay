# claude-relay — design plan (v2, revised against framing/architecture/feasibility reviews)

> Working name: **claude-relay** ("relay" = passing the baton between accounts across
> rate-limit boundaries). Terminology: an Anthropic account/`CLAUDE_CONFIG_DIR` is a
> **seat**; gad-kit's `--profile budget|balanced` is a **tier** (avoids the "profile"
> collision the framing review flagged).

A standalone, portable, external supervisor that keeps a long **headless** Claude Code
workload alive across each account's real **5-hour usage limit** by rotating between
authenticated **seats**, detecting exhaustion from the **real** Anthropic usage signal,
resuming the workload on a fresh seat, and **notifying the operator** when a human is
actually needed. First (and, for v1, only) workload = **gad-kit** generational runs.

Changelog v1→v2: dropped premature adapter/provider Protocols (build concrete, extract
later); replaced the naive dirty-tree recovery with artifact-aware, phase-safe routing
(feasibility Critical); removed poisoned %-delta calibration from the critical path
(→ `--max 1` for v1, batching deferred); moved the notifier into Phase 1; added the
required token-target directive; recorded confirmed smoke tests + the canonical-seat quirk.

---

## 0. Established & validated context

- **Baseline this improves on (framing #1):** gad-kit ALREADY does single-account
  multi-day continuity — `/gad-run` is budget-aware, self-pacing, and "pairs with
  ScheduleWakeup for days-and-nights runs." The *specific* value claude-relay adds:
  without it, each account burns up to ~5h of **dead wall-clock** waiting out its own
  quota reset before it can continue; rotation collapses that to ~zero by handing the
  run to a seat that already has headroom. That delta — not "continuity" per se — is
  the reason this tool exists.
- **Seats are distinct Anthropic accounts** → independent 5-hour quotas (operator-
  confirmed). Pool discovered from `~/.claude*` dirs; yerasyl excluded.
- **Detection endpoint (live-confirmed):** `GET https://api.anthropic.com/api/oauth/usage`,
  `Authorization: Bearer <accessToken from {seat}/.credentials.json → claudeAiOauth.accessToken>`,
  `anthropic-beta: oauth-2025-04-20`, `User-Agent: claude-code/2.1`. Returns a
  normalized `limits[]` (each `{kind:"session"|"weekly_all"|"weekly_scoped", percent,
  severity, resets_at, scope:{model}, is_active}`) plus `five_hour`/`seven_day`. Real,
  per-account. Self-rate-limits (~5-min) → cache reads ≥60–90s.
- **Smoke tests run & CONFIRMED (2026-07-18):**
  - Headless `claude -p --dangerously-skip-permissions "/gad-generation … --dry-run"` →
    slash command + Workflow tool + `${CLAUDE_PLUGIN_ROOT}` all resolve; dryRun honored.
  - `--dangerously-skip-permissions` suppresses ALL approval prompts during REAL Bash +
    Write tool use headless (test: files created, exit 0, no stall).
  - **Token refresh on launch:** `claude -p` on the expired `dias` seat re-authenticated
    (returned OK, exit 0) and its `.credentials.json` `expiresAt` advanced to a fresh
    future time + usage endpoint went 401→200. ⇒ the core loop needs **no** standalone
    OAuth refresh to rotate onto an idle/expired seat.
  - **Canonical-seat quirk:** bare `~/.claude`'s `.claude.json` lives at `~/.claude.json`
    (home default), so `CLAUDE_CONFIG_DIR=~/.claude` prints "config not found" warnings /
    a degraded session. Named `~/.claude-*` seats don't. → v1 pool = **named seats only**;
    bare-canonical/dias handled later (symlink its `.claude.json`, or re-home the account).
- **gad-kit is disk-stateful** (`.gad/` + one git commit per consolidated generation, no
  session memory) — BUT see §5: not every end-state is on disk.
- **Still to verify empirically (on the first real run, not a separate test):** that a
  FULL generation (~15–20 chained agent calls) completes headlessly without hitting an
  undocumented per-turn ceiling — the invocation must carry an explicit token-target
  directive (feasibility #4).

## 1. Invariants
1. **Two-plane separation.** External process; wraps/spawns/outlives `claude`; never runs
   in-session.
2. **Disk + usage endpoint decide "what next"; model prose is never parsed** for outcome
   classification. stdout/stream-json is for the live monitor and a backstop signal only.
   (Refined: disk-truth requires an *artifact census*, not a dirty-tree boolean — §5.)
3. **Graceful degradation is normal.** Usable pool = discovered ∩ has-usable-creds ∩
   not-in-cooldown, recomputed each iteration. Runs on whatever's available (3/1/0 seats);
   re-checks so a re-logged-in seat rejoins automatically. Logged-out seats are skipped +
   flagged, never fatal.
4. **Fully portable.** No hardcoded home/paths/seat list. stdlib-only Python 3.
5. **Never leak secrets.** Tokens used in-memory only; never logged/persisted.
6. **Never destroy unrelated work (new).** Recovery that discards a repo's uncommitted
   state must only touch changes the supervisor's own run produced (baseline-tracked),
   prefer `git stash`/safety-branch over `reset --hard`, and require a clean repo at start.
7. **Idempotent / crash-safe.** Restart re-triages from disk and continues. Single
   instance via lockfile.

## 2. Architecture (v1 = concrete, NOT prematurely generic)

Per the framing review: only one workload (gad-kit) and one provider (Anthropic) exist,
so v1 builds them **concretely** in cohesive modules — no `WorkloadAdapter`/
`ProviderAdapter` Protocol yet. gad-kit specifics live in ONE module so the future seam
is obvious; Anthropic/CLI specifics (endpoint shape, `.credentials.json`, `claude`
binary) stay isolated in their own modules. Extract interfaces when a real 2nd
workload/provider arrives — not before.

```
claude-relay/
  relay/
    fleet.py     # seat discovery (~/.claude* minus excludes), per-seat credential state
    usage.py     # /api/oauth/usage client, limits[] parsing, thresholds, read-cache
    cooldown.py  # state.json (atomic tmp+rename); cooldown math from resets_at
    runner.py    # spawn `claude -p …` (env CLAUDE_CONFIG_DIR), stream capture, exit code
    gadkit.py    # THE gad-kit brain: triage(repo)->Plan, command(plan)->argv,
                 #   snapshot(repo)->State, outcome(pre,post,usage)->Outcome  (plain fns)
    detector.py  # single classify() called by the loop: (outcome, usage, tail) -> Action
    loop.py      # the rotation loop
    notify.py    # notifier sinks: shellular (default) | command | webhook | stdout
    config.py    # config.toml + CLI merge; no hardcoded paths
  bin/claude-relay
  install.sh  verify.sh  config.example.toml  README.md  DESIGN.md
```
Future-seam note (not built now): if/when a 2nd workload or provider appears, `gadkit.py`
becomes the first `WorkloadAdapter` and `fleet/usage/runner` split behind a
`SeatProvider` — the module boundaries above are drawn so that extraction is mechanical.

## 3. The rotation loop
```
acquire single-instance lock; load config; resolve repo
loop:
  plan = gadkit.triage(repo)                 # disk truth + artifact census (§5)
  match plan:
    DONE           -> notify(DONE); break
    AWAITING_HUMAN -> notify(detail); park; break        # owner-decision / needs-login
    BLOCKED        -> notify(handoff summary); park; break # consolidator refusal
    RUN(argv) | FINISH(argv):
      seat = pick_seat()                     # §4
      if seat is None: wait_until(earliest_reset()); notify_if_long(); continue
      pre  = gadkit.snapshot(repo)           # HEAD, nextGen, artifact fingerprint
      rc, tail = runner.run(seat, repo, gadkit.command(plan))   # blocks til claude exits
      usage = usage.poll(seat)               # fresh post-run reading
      cooldown.update(seat, usage)           # cooldownUntil = session.resets_at if high
      post = gadkit.snapshot(repo)
      action = detector.classify(gadkit.outcome(pre, post, usage), usage, tail)
      handle(action)   # PROGRESSED->continue | HIT_WALL->continue(rotates) |
                       # AWAITING_HUMAN/BLOCKED->notify+park | AGENT_DEAD_NONLIMIT->retry/rotate | DONE->break
```
**Granularity = `--max 1` for v1** (one committed-green boundary per invocation → the
supervisor regains control to poll usage and rotate). Batching (`--max N`) is deferred to
Phase 2 because it needs sound per-unit cost calibration, which v1 does not have (§4/§6).

## 4. Seat selection & thresholds (`fleet.py` + `usage.py`)
- **Discovery:** glob `~/.claude-*` seats (bare `~/.claude` excluded in v1 — §0 quirk),
  minus operator excludes (yerasyl). Usable = `.credentials.json` parses with a
  `claudeAiOauth.accessToken` AND not in cooldown. Logged-out seats (no token) → skipped,
  flagged `needs-login` (notify once).
- **Synthetic per-seat 5h ceiling (operator feature):** each seat has a configurable
  synthetic ceiling `ceiling_pct` LOWER than Claude's real 100% — so headroom always
  remains for one-off web/interactive use. **Default 70%** for all pool (non-main) seats.
  The real endpoint `percent` is still the measurement; the *ceiling* is what the
  supervisor treats as "full." Doubles as a cheap TEST lever (set ~20–30% to trigger
  rotation fast without +2M / real limits). Per-seat + runtime overridable
  (`--ceiling <seat>=<pct>`).
- **When to rotate off / not start:** read the seat's live `limits[]`; the `is_active`
  session `percent`/`severity` are the measurement. Rotate-off / don't-start when
  `percent >= ceiling_pct(seat)` or `severity != normal`. Also gate on `weekly_all` + any
  per-model `weekly_scoped`. `resets_at` (the REAL window) still governs cooldown recovery
  — the synthetic ceiling only lowers the stop threshold, not the real reset clock.
- **pick_seat():** among usable seats, prefer lowest active-session `percent` with
  `percent < ceiling_pct(seat) - start_margin` (default margin ~5); none qualifies → wait
  for the soonest `resets_at`.
- **main vs worker:** a seat may be marked `main`/`exclude` in config to stay OUT of
  automation (reserved for the operator's own web/interactive use). v1 pool = named worker
  seats (almas/ayan/azim/sam); canonical/dias (the likely 'main') is already excluded
  (its `.claude.json` quirk), so all v1 seats take the 70% default.
- **Polling & tokens (confirmed simplifications):** only the seat about to be used / just
  used needs polling; launching `claude -p` refreshes its token, so no standalone OAuth
  refresh in v1. Exhausted seats are known-unavailable until their captured `resets_at`
  (no polling). Cache usage reads for `POLL_TTL` (default 90s); honor 429 `Retry-After`.
- **Calibration: NOT on the v1 path.** (Feasibility #3 CONFIRMED the naive plan poisoned:
  session-`percent` delta is corrupted whenever `resets_at` rolls between pre/post polls.)
  v1 paces purely by the between-invocation `percent` check above — no per-unit cost model
  needed. Phase 2 batching will derive per-unit cost from the **structured token-usage
  fields** Claude Code emits in `--output-format stream-json` (immune to window
  semantics; not "prose"), guarded to discard any sample where `resets_at` changed.

## 5. gad-kit brain: triage, outcome & the recovery-routing fix (`gadkit.py`)

**Feasibility Critical finding:** `AGENT-DEAD` writes NOTHING to disk (the failed
`phase` is only in the workflow return value), and `/gad-finish` has NO Guardrails phase
— so routing a Prep/Implement/Guardrails death to `/gad-finish` is wrong, and a
test-writer death routed to `/gad-finish` can commit a generation **missing mandatory
tests**. And mid-phase usage-wall exhaustion (the very thing we handle) triggers exactly
this. So triage must be artifact-aware and default to the safe path.

**triage(repo) decision order:**
1. Open `ownerDecisions[]` (in `generations-index.json`) with `blocksGen <= nextGen`,
   status `open` → `AWAITING_HUMAN` (checked pre-spend in gad-kit Preflight; disk-visible).
2. `<repo>/.gad/generation-<nextGen>/handoff.md` present → consolidator BLOCKED →
   `BLOCKED` (notify + park; a real blocker usually needs a human; do not auto-loop).
3. Working tree dirty / mid-generation, no handoff → **AGENT-DEAD-style interruption**:
   census `generation-<nextGen>/` artifacts (plan.md, reviews/*, setup-notes.md,
   implementation-log.md, guardrail test files):
   - Artifacts prove Implement **and** Guardrails/tests completed (died at/after Verify)
     → `FINISH(/gad-finish <repo> <nextGen>)` — the one case `/gad-finish` is safe.
   - Otherwise (uncertain, or Plan/Prep/Implement/Guardrails incomplete) → **safe
     default: full `RUN(/gad-generation <repo> <nextGen>)` restart**, after non-
     destructively parking the partial tree (Invariant #6: `git stash`/safety-branch,
     scoped to the supervisor's own baseline; never nuke unrelated work). Never risk a
     testless commit.
4. Clean tree at a committed boundary, backlog has pending gens → `RUN(/gad-run <repo>
   --max 1 --tier budget <TOKEN_TARGET>)`.
5. Clean tree, backlog exhausted → `DONE`.

**command(plan):** builds the argv:
`["-p","--dangerously-skip-permissions","--output-format","stream-json", <slash+flags+token-target>]`
The slash text MUST include an explicit **token-target directive** (feasibility #4 — e.g.
the `+2M`-style directive gad-run's own docs require for long turns) so a full generation
doesn't die on an undocumented per-turn ceiling. Exact directive syntax to confirm on the
first real run.

**snapshot / outcome:** snapshot = `{HEAD, nextGen, artifact fingerprint}`. outcome()
classifies from pre/post disk + post-run usage into a **richer set** (feasibility #5):
`PROGRESSED` (new `gen-N` commit) · `HIT_WALL` (no new commit AND active `percent`≈100 /
limit signature in tail) · `AWAITING_HUMAN` (new open ownerDecision) · `BLOCKED` (new
handoff.md) · `AGENT_DEAD_NONLIMIT` (no progress, seat NOT near cap → transient, not a
limit) · `NO_BACKLOG`. detector.classify() maps outcome+usage+tail → the loop Action, as
the single source of that decision (arch #4).

## 6. State — `~/.claude-relay/state.json` (atomic)
```json
{ "schemaVersion": 1,
  "seats": { "<seatDir>": {
      "hasCreds": true, "cooldownUntil": "<resets_at|null>",
      "lastPercent": 49, "lastResetsAt": "…",
      "consecutiveFailures": 0, "note": "needs-login|null" } },
  "lastNotified": { "all-exhausted": "…", "needs-login:<dir>": "…" } }
```
Cooldowns derived from real `resets_at`, never a fixed "+5h". (No per-unit cost samples in
v1 — calibration deferred to Phase 2 per §4.)

## 7. Notifier (Phase 1) & Monitor (Phase 2)
- **Back-channel = notify-out + resolve-in, decoupled from chat sessions** (shellular
  investigation 2026-07-19): shellular's wired notify hook is one-way/local/undrained,
  and its full-app reply path (ACP `session/new|resume|fork`) lands replies under an
  ambiguous session-id and needs a non-persistent daemon → UNSAFE as a dependency. So:
  - **notify-out (Phase 1):** reliable push straight from the supervisor (external proc,
    full network) to the phone. `notify.py` pluggable sinks — chosen channel (Telegram
    bot recommended / ntfy.sh / webhook / command / stdout); shellular kept only as an
    OPTIONAL sink, never depended on. Events: `AWAITING_HUMAN` (owner-decision /
    needs-login), `BLOCKED`, `DONE`, `ALL_SEATS_EXHAUSTED (waiting Nm)`, `HARD_ERROR`;
    deduped via `lastNotified`. This is the operator's Android view for v1.
  - **resolve-in (Phase 1):** the operator's decision is applied to DURABLE on-disk state
    (gad-kit: mark the `ownerDecision` resolved in `.gad/generations-index.json`) — that,
    not a chat reply, is what unblocks the run and survives rotation. Exposed as an
    idempotent `claude-relay resolve <decisionId> <answer>` command (SSH-from-phone
    reachable), and, if the Telegram sink is chosen, as a bot reply the supervisor polls
    and maps to the same disk edit. Supervisor parks the repo, keeps serving other work,
    and auto-resumes when it sees the resolution on disk.
- **Monitor (Phase 2, implemented — observe-only):** `claude-relay monitor` builds a 3-pane
  tmux cockpit (supervisor log = newest `run-*.log` followed live · all-seats usage table ·
  `git log`/`.gad/BUILD_STATUS.md`); `claude-relay seats [--watch]` is the table on its own
  (no tmux; ideal over SSH). The table is **live + fallback**: each seat polled live (shared
  `poll_ttl`/~5-min discipline), falling back to `state.json`'s last-known reading (labelled
  `stale·N` by age, or `auth?` for an expired token) when a live poll fails. It is a *dashboard*,
  never an input to selection — `pick_seat()` polls live at decision time regardless. Deliberately
  observe-only: it never launches a run. STILL deferred: a standalone OAuth refresh so *idle*
  seats poll truly-live between runs (today they show last-known); no monitor needs it, and even
  `claude-hud` doesn't do it.

## 8. Config (`config.toml`, all optional)
```toml
repo       = "/abs/path"          # or CLI arg
exclude    = ["yerasyl"]          # profile-name fragments never used by automation
poll_ttl   = 90
token_target = "+2M"             # directive appended to run prompt (verify on 1st run)
max_units  = 0                    # 0 = until DONE
[defaults] ceiling_pct = 70; start_margin = 5    # synthetic 5h ceiling for all pool seats
[seats.almas] ceiling_pct = 70                    # per-seat override; --ceiling to override at runtime
[seats.dias]  main = true                         # reserved: excluded from automation
[gadkit] tier = "budget"; extra_flags = []
[telegram] bot_token = "env:CLAUDE_RELAY_TELEGRAM_BOT_TOKEN"; chat_id = "…"  # or inline (chmod 600)
[notify]  sink = "telegram"
```
CLI: `claude-relay run [repo] [--once] [--dry-run]`, `status`, `login-check`, `resolve`,
`seats [--watch]`, `monitor [repo]`.

## 9. Failure modes / edge cases
All-cooling → sleep to earliest resets_at (+notify if long). 0 usable seats → notify
needs-login, wait+re-check. Mid-gen wall → §5.3 artifact-safe recovery. AWAITING-OWNER →
notify+park. Our-own endpoint 429 → honor Retry-After, use cached. Two instances →
lockfile (stale reclaimed by age). Clock skew / past resets_at → clamp + re-poll. Network
down → assume current seat usable, run, observe. `claude`/plugin missing → verify.sh +
loop notify. Repo not gad-bootstrapped → AWAITING_HUMAN("run /gad-init"). Concurrency: one
session at a time (no parallel seats) → no shared-quota or worktree races.

## 10. Security
`--dangerously-skip-permissions` = operator's approved deliberate choice, scoped to their
own repos. Creds read from `.credentials.json`, never logged. state.json+logs `chmod 600`.
Recovery never destroys unrelated user work (Invariant #6).

## 11. Phased delivery (reordered)
- **Phase 1:** loop + fleet/usage/cooldown/runner + gadkit brain (artifact-safe recovery)
  + detector + config + install/verify + **minimal shellular notifier**. Runs on live
  named seats. `--dry-run`/`--once` for testing. First real run also validates the
  token-target on a full generation.
- **Phase 2:** tmux monitor ✅ (observe-only: `monitor`/`seats`, live+fallback table) +
  standalone OAuth refresh (→ *idle*-seat truly-live table; still deferred) + calibrated
  `--max N` batching (stream-json token cost; still deferred).
- **Phase 3:** more notifier sinks + dedupe polish.
- **Later / v2 boundaries:** mobile web dashboard; multi-repo concurrent off one pool
  (today: single-repo, global lock); provider/workload interface extraction; multi-
  provider (opencode/OpenRouter) grunt subagents.

## 12. Resolved review items (traceability)
- framing #1 baseline → §0. #2 batching → deferred (§3/§4). #3 premature generality →
  §2 concrete-first. #4 notifier-first → §7/§11. #5 multi-repo → §11 v2. #6 terminology →
  seat/tier throughout.
- arch #1 snapshot method → dissolved (plain fns, §2). #2 provider seam → §2 future-seam.
  #3 vocab leak → no cost samples in v1 (§6). #4 detector single call → §5. #5 headroom
  in command → token_target param (§5/§8). #6 verify preflight → §11 (deferred).
- feasibility #1 recovery routing (Critical) → §5. #2/#3 calibration poison → §4 deferred.
  #3 token refresh → CONFIRMED (§0). #4 token target → §5/§8. #5 outcome buckets → §5.
