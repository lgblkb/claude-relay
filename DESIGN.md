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
   (Refined: disk-truth requires an *artifact census*, not a dirty-tree boolean — §5.) A
   narrower carve-out (2026-07-26, Blocker 1): `--output-format stream-json` is real NDJSON —
   `detector.py` decodes it (never bare-substring-matches the raw lines, which is not what
   production ever produces) and treats exactly two DECODED signals as authoritative even when
   the usage poll succeeded, because both are structured platform data, not model prose: a
   `rate_limit_event` envelope's `rate_limit_info`, and gad-run's own `RESULT:` line content
   (a diagnosis gad-run itself reached via a second agent call). Everything else stays
   prose-backstop-only, gated on the usage poll having failed.
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
  **Confirmed present (2026-07-26 live probe, RECORDED not built — Blocker 1 item 5):** the
  terminal `result` NDJSON envelope (`{"type":"result","subtype":"success",...}`) carries
  `modelUsage` and `api_error_status` alongside `is_error`/`num_turns`/`duration_ms` — these are
  exactly the structured token-cost fields this calibration needs, already decodable via
  `detector.py`'s `_iter_envelopes()` seam (built for the NDJSON tail-parsing rewrite below).
  Not implemented: Phase 2 still needs to read `result.modelUsage` per invocation, correlate it
  against the generation actually committed, and derive the per-unit cost — this note only
  records where the raw data lives so that work has a starting point.
  **Calibrated (2026-07-27, §4b):** the first real supervised generation measured **$14.73 / 90
  points of `five_hour` / 9 points of `seven_day` for one committed unit** — i.e. ~4 generations per
  seat per week against the 95-point weekly ceiling — with a burn rate linear at ~1.45 points/min and
  an exchange rate of ~10 `five_hour` points per `seven_day` point that holds across two very
  different workloads. Phase 2 batching now has the per-unit cost it was waiting for. The one caveat
  that matters for the model's *shape*: cache reads are 98.6% of tokens but output tokens are the
  majority of the bill, so a model keyed on token count rather than token kind is wrong by an order
  of magnitude.
  **Collection now exists (2026-07-27):** `relay/capture.py` is an opt-in passive tap
  (`CLAUDE_RELAY_CAPTURE_DIR`) that records every terminal `result` envelope — `modelUsage`
  included — from normal runs, at ~zero marginal cost, because `runner.py` already reads every
  NDJSON line. Phase 2 no longer has to build its own collection path; it needs only the
  correlation and the cost model. The same tap collects `rate_limit_event` envelopes for the
  rate-limit calibration below, which is why one mechanism serves both.

### 4a. Rate-limit signal calibration (`bin/rate-limit-probe`)

`detector._KNOWN_SAFE_RATE_LIMIT_STATUSES` is a **one-element** frozenset and
`_RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD` is a hand-picked `0.9`. Both encode a guess about an
enum whose only ever-observed value is `allowed_warning` at `utilization: 0.76`. Rotation and
forced multi-hour cooldowns ride on that guess, so it is the largest remaining uncalibrated
constant in the design.

It cannot be calibrated by a conventional test: `utilization` is a property of the account's
window, not of the test fixture — it can only be *spent*, never *set*. Hence a cost-tiered harness
(Tier 0 free / Tier 0.5 prices the spend / Tier 1 passive / Tier 2 spends), documented in full at
the top of `relay/ratelimit_probe.py` together with six pre-registered questions.

Two schema facts worth recording here, both from the 2026-07-27 Tier-0 run:

- The endpoint sends ~**55** leaf fields; `UsageSnapshot.from_json()` keeps **three**
  (`five_hour`, `seven_day`, `limits`). Everything else — `extra_usage.*` (overage state),
  `spend.*` (with its own `percent`/`severity`), and per-model weekly gauges
  `seven_day_opus` / `seven_day_sonnet` — is invisible to every rotation decision. The per-model
  gauges are present-but-**null** on the observed account, so this is a future-proofing note, not
  a live bug: if they ever populate, a seat whose *Opus* weekly is exhausted while its aggregate
  weekly is not would look healthy to `session_percent()`.
- The endpoint 429s readily — `Retry-After: 300` after a few dozen reads within minutes. The
  harness therefore polls at the same 90s `poll_ttl` §4 already specifies, rather than inventing a
  faster cadence for the tool most likely to run beside a live supervisor.

**Standing prediction, recorded before the data exists** (so it can be refuted rather than
rationalized): `_rate_limit_event_action()` ignores `rate_limit_type` and feeds the event's
`resetsAt` straight into `_force_cooldown()`. A `seven_day` event at utilization ≥ 0.9 would
therefore cool a seat until the **weekly** reset — days — discarding the remaining ~10% of weekly
*and* a possibly-fresh five-hour window. Both the threshold and the cooldown horizon likely need
to become window-aware.

#### Results (2026-07-27 Tier-2 run)

The first real capture produced this envelope, and it changed three things:

```json
{"status":"allowed","resetsAt":1785105000,"rateLimitType":"five_hour",
 "overageStatus":"rejected","overageDisabledReason":"org_level_disabled",
 "isUsingOverage":false}
```

- **Q3 — answered YES.** `rateLimitType: "five_hour"` exists, so the in-run signal genuinely
  covers the window the rotation logic keys on. Previously only `seven_day` had ever been seen,
  which had left open the possibility that the authoritative signal covered a window the
  supervisor did not rotate on.
- **Q6 — answered YES, richly.** The terminal `result` carries `modelUsage` with per-model
  `inputTokens` / `outputTokens` / `cacheReadInputTokens` / `costUSD` / `contextWindow`, plus a
  top-level `total_cost_usd`, `api_error_status`, `stop_reason` and `terminal_reason`. Phase 2 has
  everything it needs; only the correlation and the cost model remain.
- **A live bug, now fixed.** `status: "allowed"` — the ordinary healthy value — was absent from
  `_KNOWN_SAFE_RATE_LIMIT_STATUSES`, so it was classified UNRECOGNIZED and rotated the seat off
  with a forced cooldown until `resetsAt`.

  **Scope, stated precisely** (the first write-up of this overstated it): `classify()` reaches
  `_rate_limit_event_action()` only in the `AGENT_DEAD_NONLIMIT` branch — `PROGRESSED`, `HIT_WALL`,
  `AWAITING_HUMAN`, `BLOCKED` and `NO_BACKLOG` all return earlier. Ordinary successful runs were
  never affected, and the fleet did not stall on the happy path. What it *did* do is turn every
  non-limit agent death (crash, hang, timeout, refusal) into a multi-hour seat outage: the seat was
  cooled until its window reset instead of retried. Two unrelated crashes park a two-seat fleet for
  hours. **Crash amplification, not immediate stall.**

  It is nonetheless a true signal inversion. That branch exists to distinguish *"died because of a
  limit"* from *"died for some other reason"* — and an `allowed` event is positive evidence of
  **not**-limit. Reading it as a limit flipped the signal's meaning rather than merely failing to
  help. The original reasoning, *"unknown → assume limited is the conservative direction,"* was
  backwards for this signal: Invariant #2 makes disk and the endpoint primary, so a false positive
  here breaks recovery while a false negative defers to a mechanism that was already authoritative.

Two schema notes from the same envelope:

- `overageStatus` / `overageDisabledReason` are undocumented additions. `overageStatus:
  "rejected"` means the **org disabled overage spending**, not that the request was denied — a
  substring scan for `"rejected"` would read a healthy event as a wall.
- the event carried **no `utilization` and no `surpassedThreshold` at all**. Those appear only
  once a warning threshold is crossed, so `_RATE_LIMIT_UTILIZATION_ROTATE_THRESHOLD` cannot fire
  on a plain `allowed` event regardless of its value.

#### Q1 answered — the event channel cannot report a denial

A second Tier-2 burn drove one five-hour window from **84% → 100%**, capturing **42 events across
42 calls** for **$1.49** and **+2 points** of weekly quota. Every call **succeeded**: `is_error`
false, `subtype: success`, `api_error_status: null`, `stop_reason: end_turn`, throughout.

| status | rateLimitType | utilization | count |
| --- | --- | --- | --- |
| `allowed` | `five_hour` | *absent* | 13 |
| `allowed_warning` | `five_hour` | 0.90 → 0.99 | 29 |

The event stream warned continuously and **never emitted a blocking status**; `utilization` peaked
at 0.99 and never reached 1.0. The wall then materialized in a *different* session minutes later.

So Q1's premise was wrong. There is no "walled run" whose envelopes can be inspected, because **the
run that gets refused never starts**. `rate_limit_event` is a *warning* channel, full stop.

The authoritative wall signal is the **usage endpoint**, verified against the genuinely walled seat:

```json
five_hour {"utilization": 100.0, "resets_at": "2026-07-26T22:30:00Z"}
limits[]  {"kind":"session","percent":100,"severity":"critical","is_active":true}
```

`severity: "critical"` is a third observed value (after `normal` and `warning` at 82%).
`rotate_off()` gates on `severity != "normal"`, so it routes correctly with no enum change —
`session_utilization()`, `session_percent()`, `near_cap()` and `rotate_off(high=90)` all reported
the wall correctly. **Invariant #2's primary path works as designed.**

This is also the strongest justification for the `_KNOWN_SAFE_RATE_LIMIT_STATUSES` fix above: if the
event channel *cannot* report a denial, then treating an unfamiliar status **as** a denial can only
ever produce false positives — which is precisely the force-cooling bug.

#### Q4 answered — the threshold constant does less than it appears

`0.9` is a **real platform threshold**: every event carrying `utilization` also carried
`surpassedThreshold: 0.9`, so the hand-picked value happened to match. But `utilization` is
**absent below 0.9 entirely**, so `>= 0.9` fires on the first event that carries the field at all —
the constant is operationally a *presence check*, and lowering it would change nothing. Also,
`surpassedThreshold` can be **absent even when `utilization` is present** (2 events at exactly
0.90), so it must never be used as a presence proxy.

#### The rotation/cooldown split (implemented 2026-07-27)

Q1's answer has a direct consequence. `loop.py`'s `AGENT_DEAD_NONLIMIT` branch is the *only*
consumer of `detector.Action.resets_at`, and it passes that value to `_force_cooldown()` as the
cooldown **boundary**. Deriving that boundary from a high-utilization event marks the seat unusable
until its window resets — indefensible once you know the channel only ever warns.

The weekly case shows the harm plainly: a `seven_day` event at 0.90 cooled the seat for **days**,
discarding ~10% of still-spendable weekly quota, on the strength of a warning about a seat that was
demonstrably still serving requests.

So the two concerns are now separated:

| signal | drives rotation | sets cooldown horizon |
| --- | --- | --- |
| `allowed*` at ≥ threshold (a **warning**) | yes | **no** — short `_TIMEOUT_COOLDOWN_S` fallback |
| unrecognized status (possible **denial**) | yes | yes — keeps the event's `resetsAt` |
| usage endpoint `severity: critical` / `percent: 100` | yes | yes — via `_record_usage()` |

Rotation is unaffected: `_force_cooldown()` still applies its short default, which is all that branch
needs to stop `pick_seat()` re-selecting the seat next iteration. Real exhaustion stays the usage
endpoint's call — Invariant #2's primary path, verified against a genuinely walled seat.

This was the standing prediction below. Q1 turned it from a suspicion into a straightforward
consequence, so it was fixed on the reasoning rather than waiting for a `seven_day`-at-0.90
observation. Two tests that had pinned the old behaviour were **inverted, not deleted**, each quoting
its previous assertion so the change of contract stays legible.

#### The wall, as the CLI presents it

Operator observation (2026-07-27): on hitting the five-hour limit *interactively*, Claude Code states
the limit and offers two choices — wait for the reset, or ask an admin to raise the quota. So the CLI
has its own definitive wall handling, which is consistent with Q1: the refusal happens **before** a
run produces envelopes, which is why no blocking `rate_limit_event` is observable.

It also corroborates the Tier-0 schema findings. "Ask an admin to raise the quota" is the
human-facing form of the overage fields the usage endpoint already exposes and `UsageSnapshot`
discards — `extra_usage.user_disabled: true`, and on the event side
`overageStatus: "rejected"` with `overageDisabledReason: "org_level_disabled"`. Those are the
machine-readable version of the same condition.

Not yet observed: what that wall looks like **headlessly**, where there is no prompt to answer. The
likely locus is the terminal `result` envelope (`is_error`, `api_error_status`, `subtype`), of which
42 *successful* samples now exist and zero walled ones. The Tier-1 tap records exactly that envelope,
so the comparison will arrive on its own.

#### Two mispredictions, left on the record

The standing prediction above is **still untested**, and I was wrong twice about why. First I wrote
that "Q3 and Q4 settle it" — they do not; Q3 says nothing about the cooldown horizon. Then I wrote
that Q4 might be unreachable because five-hour events omit `utilization` — also wrong; they carry it
above 0.9, and I had generalized from the single pre-burn `allowed` event.

Testing it needs a `seven_day` event at ≥ 0.9, i.e. a nearly-exhausted **weekly** window, which the
Tier-1 passive tap will eventually observe and a five-hour burn never can. Both errors share one
shape — inferring a general rule from a single observation — which is the exact failure the harness
exists to prevent.

### 4b. First end-to-end supervised run (2026-07-27, `live-run-01`)

Everything in §3, §6, §7 and §8 — the loop, cooldown state, the notifier, config loading — had until
this point been exercised **only by unit tests**. `~/.claude-relay/` did not exist on the box: the
supervisor had never actually run. This section records the first real run, because the results move
several assumptions in this document from "assumed" to "measured", and because the failures found are
of a class that three prior adversarial reading passes did not produce.

**Setup.** Throwaway target repo (`~/projects/gad-live-target`), a python-uv project whose
`scripts/gate.sh` was verified GREEN *before* the run, so a gate failure during the generation would
mean something real rather than a broken fixture. `.gad/` was scaffolded by hand from gad-kit's own
templates rather than via `/gad-init` — that command is itself an async Workflow and cannot be driven
by a bare `-p` prompt (see `command()`'s docstring), and the subject under test was claude-relay, not
gad-kit's bootstrapper. `azim` was excluded from the pool (81% weekly, and it carries the operator's
interactive session); `ayan` was the spend seat at a fresh `five_hour: 0`. `max_units = 1`, notify
sink `stdout`, run launched detached via `setsid` so it would survive the operator's session.

**Feasibility #4 — ANSWERED YES.** A full generation completes headlessly under `claude -p`
supervision: **17 distinct agents, 63.8 min, `RESULT: COMPLETED-BATCH`**, `is_error=False`,
`stop_reason=end_turn`, `api_error_status=None`, 10 turns. Committed, `gate: GREEN`, **19/19
acceptance criteria met**, `nextGen` advanced 0 → 1. This was the longest-standing open question in
the design and the claim everything else rests on. The blocking-`TaskOutput` mechanism held for the
whole 64 minutes across two `verify → fix` rounds — the failure mode that killed gen 0 (ending the
turn while the workflow still runs) did not recur.

The supervisor's own contract also held: `max_units` counted the unit and stopped cleanly, the
notification was recorded in `state.json`'s `lastNotified`, and the seat's cooldown was set from the
**real** `resets_at` (21:40Z) rather than a fixed +5h, exactly as §6 requires.

#### Cost, finally measured (this closes the §4 Phase-2 data gap)

One generation, from the terminal `result` envelope's `modelUsage`:

| model | input | output | cache read | cost | share |
|---|---|---|---|---|---|
| `claude-sonnet-5` | 666 | 276,864 | 20,155,836 | **$13.52** | 92% |
| `claude-haiku-4-5` | 846 | 44,322 | 3,377,360 | $0.76 | 5% |
| `claude-opus-5[1m]` | 19 | 1,191 | 278,335 | $0.45 | 3% |

**One generation = $14.73, 90 points of `five_hour`, 9 points of `seven_day`.** Against a 95-point
weekly ceiling that is **~4 generations per seat per week** — the planning number §4 said Phase 2
needed, now available from one run.

Corollaries worth keeping:

- **`opus` is only the outer wrapper (3%).** The `-p` turn claude-relay spawns is opus, but gad-kit's
  `budget` profile puts the actual work on sonnet + haiku. A pre-run worry that "the generation runs
  on opus and will be ruinous" was wrong, and the argv's model is not the cost lever it looks like.
- **Output tokens are 1.4% of tokens and the majority of the bill.** Cache reads are 98.6% of tokens
  but cost ~10× less per token. Any cost model keyed on token *count* rather than token *kind* will
  be wrong by an order of magnitude.
- **Burn rate is linear enough to plan with**: 1.54 → 1.49 → 1.42 points/min measured at 11.7, 28.8
  and 63.5 minutes. Concurrent phases (premortem ‖ refactor ‖ consolidate) did **not** spike it. A
  five-hour window is therefore ~65-70 minutes of real generation work, which makes rotation
  load-bearing rather than a convenience — "5-hour limit" badly undersells the consumption rate.
- **The exchange rate is workload-insensitive.** ~10 points of `five_hour` per point of `seven_day`,
  now measured three ways: the Tier-2 burn of uniform trivial prompts (16:2), and this generation at
  two checkpoints (18:2, 90:9). Two workloads with completely different token mixes agree, which is
  what makes the constant usable rather than anecdotal.

#### Findings

**1. The Tier-1 capture tap was silently off for every unattended launch.** §4a's note (and the
README) claimed that putting `CLAUDE_RELAY_CAPTURE_DIR` in **both** `~/.bashrc` and `~/.profile`
covered cron/systemd/`nohup`. Measured, it does not: `.bashrc` returns early when non-interactive and
`.profile` is read only by *login* shells, so a non-login non-interactive shell — precisely how an
unattended `claude-relay run` is launched — reads neither.

| launch | reads | tap |
|---|---|---|
| interactive tab | `~/.bashrc` | on |
| login shell (`bash -lc`, ssh) | `~/.profile` | on |
| `bash -c`, `nohup`, cron, systemd | **neither** | **off** |

This is the **second** instance of this bug class (the first: the Tier-2 recorder set the env var for
the *child* while `record_line()` ran in the *parent*). Both share a shape worth naming: the tap is a
deliberate no-op when unset, so a mis-plumbed environment produces **zero records and zero errors**.
Silence is indistinguishable from "nothing to record". Any future activation path must be verified by
asserting records appear, never by reading the config that should have produced them.

**2. `severity` is absent at 90% utilization.** §4 says to rotate off when
`percent >= ceiling_pct(seat)` **or** `severity != normal`. At `five_hour: 90` the live payload
carried `severity: None` — so `percent` carried the entire decision and the `severity` clause
contributed nothing. `rotate_off(u, 70.0)` returned True correctly, via percent alone. The gap is
latent rather than active (the primary path works), but a future threshold written to key on
`severity` would not fire on a seat this deep into its window.

**3. Invariant #6 validated by accident — and it held.** The scaffold tracked `.coverage` by mistake
(no `.gitignore`), so the tree was dirty after every gate run. The next iteration's triage refused:

> `AWAITING_HUMAN` — working tree is dirty but is not safely attributable to a claude-relay-initiated
> run (HEAD moved since the last known-clean baseline; no `generation-N/` scaffold or `.gad/` change
> in the dirty set) — Invariant #6 forbids guessing here (never blanket-stash unrelated work)

The dirty set was `.coverage` + `__pycache__` with zero `.gad/` paths, which is exactly the B10 audit
fix's predicate ("satisfied only by an actual dirty path under `.gad/`, full stop"). An accidental
fixture flaw produced the precise scenario that fix was written for, unprompted, and it behaved
correctly rather than stashing unrelated work.

**4. A gad-kit defect: `generations-index.json` records an unreachable commit.** The index reported
`commit: "d2094fa"`, but the branch head was `077278e`. The reflog explains it:

```
077278e HEAD@{0}: commit (amend)
d2094fa HEAD@{1}: commit (amend)   <-- the sha the index recorded
8cde13d HEAD@{2}: commit
```

The consolidator commits, amends twice, and writes the index **between** amends, so the recorded sha
is a dangling object — `git merge-base --is-ancestor d2094fa HEAD` fails. It resolves today only
because unreachable objects survive until `gc`; after one, a generation's audit trail back to its
code is broken. This is gad-kit's bug, not claude-relay's, but claude-relay must not start trusting
`index.commit` to identify a generation's commit (`gadkit.py` already prefers git history and disk
evidence over index bookkeeping, which is the right instinct for an independent reason).

**5. The blast radius of a supervised generation is the machine, not the repo.** Children run with
`--dangerously-skip-permissions`, and the agents demonstrably launched `local_bash` tasks running
`find / -maxdepth 6 -iname "*.jsonl"` and `find /home/dias -iname "envelopes-*.jsonl"` — reaching
well outside the target repo to locate real capture files. Nothing harmful happened (finding the real
producer is what made the run correct, see finding 6), but this is a property to hold deliberately
rather than discover. It also gave Invariant #5 an unplanned test: an unrelated agent read the
capture files, and because `capture.py` never records `assistant` envelopes there was no tool output
in them to surface.

**6. The spec was wrong, and only execution against the real producer caught it.** The backlog entry
for this generation (written by the same author as this document) specified capture records as flat
envelopes with a top-level `type`. `capture.py` in fact writes a *wrapper* —
`{captured_at, captured_at_unix, pid, seat, envelope: {...}}` — with the discriminator at
`envelope.type`, and `modelUsage` keyed by model name rather than flat fields. gad-kit's framing
review returned `NEEDS MAJOR REVISION (FRAME BROKEN)`, agents read `relay/capture.py`, `DESIGN.md`
and `tests/test_capture.py`, re-authored the contract, and parked an open `ownerDecision` asking
whether the autonomous redirect was intended.

The aggravating detail: the author already knew the real shape — every ad-hoc analysis script written
that same session contains `env = rec.get("envelope") or rec`, an unwrap added *because* the wrapper
had been discovered. The spec was then written from memory and got it wrong anyway. The lesson
generalizes past "static review misses bugs in existing code": **specs authored without executing
against the real producer encode false assumptions**, and the cheapest place to catch that is an
agent whose job is to go read the producer.

**7. Green + 19/19 + adversarial review still shipped a misleading statistic.** The deliverable's
`cache_read_share` computes `cache_read / (input + cache_read)`, excluding output tokens, and so
prints `100.0%` where the meaningful figure is `98.6%`. The docstring documents the formula honestly,
so this is a spec-intent mismatch rather than a hidden bug — the criterion "report the cache-read
share of billed tokens" was met literally while the number stopped being useful, since output is both
billed and the dominant cost. It survived a gate, 19/19 criteria, an adversarial reviewer and two
verify rounds; it took about 30 seconds of running the tool on real data to notice. Acceptance
criteria bound what gets checked, not whether the checked thing means what the reader will assume.

#### Still untested after this run

Rotation and cooldown *recovery* — `--once` performs a single invocation and never rotates. Exercising
them needs a second seat in the pool, and the only candidate (`azim`) holds ~13 weekly points, about
one generation's worth. That is a real spend for a real answer and should be a deliberate decision,
not a side effect.

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

**REFUSED-status fix (2026-07-26, gad-kit's uncommitted 2.1.0 work):** step 3's "Guardrails/
tests completed" proof now requires TWO artifacts, not one — `reviews/verification.md` AND
`reviews/adversarial-review.md`. gad-generation.js's Guardrails phase treats the adversarial-
reviewer/results-skeptic as ADVISORY for non-ideation genTypes (a dead reviewer after retries
still lets Verify run, writing verification.md alone), but gad-finish.js now MECHANICALLY
REFUSES (`status: 'REFUSED'`, writes nothing) to resume exactly that generation. Requiring both
files closes the resulting livelock (FINISH → REFUSED → retry identically → HARD_ERROR) without
ever narrowing what a genuinely resumable ideation generation looks like (its one hard-required
guardrail call IS the one that writes both files together). See `gadkit.py`'s
`_ADVERSARIAL_REVIEW_RELATIVE` for the full reasoning; `detector.py`'s `classify()` also treats a
`REFUSED` RESULT status as non-retryable (defense in depth, for whenever this gate is somehow
still wrong).

**command(plan):** builds the argv:
`["-p","--dangerously-skip-permissions","--output-format","stream-json", <slash+flags+token-target>]`
The slash text includes a **token-target directive** (e.g. `+2M`) appended as free prompt
text. **B22 audit fix (2026-07-26), docs-only:** this was previously documented as a working
pacing mechanism ("so a full generation doesn't die on an undocumented per-turn ceiling").
VERIFIED LIVE against the installed `claude` 2.1.220 bundle: `budget.total` (the field
gad-run.js reads to self-pace) has no writer anywhere in the bundle and is always `null`
regardless of this directive's phrasing — it is currently inert prompt text, not a real
per-turn budget. It is kept (harmless either way, and a future CLI version may wire it up)
but must not be relied on; `--max 1` is what actually bounds each `claude-relay`-launched
invocation.

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
token_target = "+2M"             # directive appended to run prompt — VERIFIED INERT (§5, B22): the
                                  # installed CLI never populates budget.total; kept, not relied on
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
  **Executed 2026-07-27 (§4b): feasibility #4 ANSWERED YES** — 17 agents, 63.8 min, committed,
  gate GREEN, 19/19 criteria, on one `--once` invocation. Loop, config, cooldown-from-real-
  `resets_at`, `max_units` enforcement and the notifier are no longer unit-test-only. Rotation
  and cooldown *recovery* remain untested (a single invocation never rotates).
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
- feasibility #1 recovery routing (Critical) → §5. #2/#3 calibration poison → §4 deferred, then
  **CALIBRATED 2026-07-27 (§4b)** from a real generation's `modelUsage`, sidestepping the
  window-roll poison entirely. #3 token refresh → CONFIRMED (§0). #4 headless full generation →
  **ANSWERED YES 2026-07-27 (§4b/§11)**. #4 token target → §5/§8. #5 outcome buckets → §5.
