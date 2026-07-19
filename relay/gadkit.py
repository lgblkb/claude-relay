"""The gad-kit brain: the ONLY module that knows gad-kit's on-disk shape and slash-command
surface. Every other module in this package is workload-agnostic; if/when a second workload
ever appears, this module becomes the first concrete `WorkloadAdapter` (DESIGN.md §2).

Four pure-ish functions (the only I/O is reading/writing files, `state` bookkeeping, and
shelling out to `git` — never spawning `claude` itself, that stays in runner.py):

    triage(repo, config, state) -> Plan       disk truth + artifact census decides what's next
    command(plan)         -> argv            builds the `claude` invocation for a RUN/FINISH plan
    snapshot(repo)        -> Snapshot         {HEAD, nextGen, artifact fingerprint} before/after a run
    outcome(pre, post, usage, ceiling_pct) -> str   classifies a completed run from disk + usage

Recovery-routing fix (feasibility Critical, DESIGN.md §5; reproduced by both the adversarial
reviewer and the verifier against v1): `AGENT-DEAD` writes NOTHING to disk, and `/gad-finish`
has NO Guardrails phase (see gad-kit's gad-finish.js `meta.phases`: Verify, PreMortem,
Refactor, Consolidate — no Prep/Implement/Guardrails). So routing a Prep/Implement/Guardrails-
phase death to `/gad-finish` risks committing a generation with no tests.

v1 gated FINISH-safety on a path-pattern heuristic ("does some dirty path look like a test
file"), which both reviewers broke: a Prep-phase fixture under `tests/` (test-writer hasn't
even run yet) or a coincidentally `test_`-named file anywhere in the repo satisfied the
heuristic and wrongly proved "Guardrails ran." Fixed: `triage()` now gates FINISH on ONE
genuine Verify-or-later artifact — `.gad/generation-N/reviews/verification.md` — which is
written ONLY by the Verify-phase verifier agent (gad-generation.js's `phase('Verify')` block:
"Write ${DIR}/reviews/verification.md"), and Verify can only ever start after Guardrails'
test-writer returned non-null (a null test-writer result makes gad-generation.js
`deadAgentAbort('Guardrails/test-writer')` and `return` immediately, before `phase('Verify')`
ever runs). So this file's mere presence is disk-visible proof Guardrails already completed,
regardless of what test paths happen to be dirty. If this file is absent, `triage()` ALWAYS
defaults to a full `/gad-generation` restart (never FINISH) — a redundant restart is safe; a
testless commit is not. Recovery also non-destructively parks the partial tree with
`git stash` (Invariant #6 — never `git reset --hard` unrelated work, and never even stash
unless (a) the dirtiness is scoped to a `git HEAD` claude-relay itself last saw this repo
clean at, AND (b) the dirty set shows a `.gad/`/`generation-N/` signal consistent with an
in-progress generation).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import cooldown
from . import usage as usage_mod

if TYPE_CHECKING:
    from .config import Config
    from .usage import UsageSnapshot

# Owner-decision statuses that count as "still open" for preflight gating. Per
# gad-kit's consolidator.md, a decision only gates a generation when it is BOTH still
# `status: "open"` AND carries a `blocksGen` (a decision with no `blocksGen` is ADVISORY —
# it is surfaced to the human but never blocks the autonomous crawl).
_OPEN_STATUS = "open"

_BACKLOG_HEADER_RE = re.compile(r"^##\s*G(\d+)\b", re.MULTILINE)

_REVIEW_FILENAMES = (
    "architecture-review.md",
    "testing-review.md",
    "feasibility-review.md",
    "framing-review.md",
)


class GitRecoveryError(RuntimeError):
    """Raised when a `git` operation required for safe recovery (stash) itself fails."""


# ─────────────────────────────────────────────────────────────────────────────
# Plan / Snapshot data model
# ─────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Plan:
    """The output of `triage()`. `kind` is one of DONE | AWAITING_HUMAN | BLOCKED | RUN | FINISH
    (matching DESIGN.md §3's loop pseudocode exactly). For RUN, `mode` distinguishes the two
    different slash commands a "RUN" can mean: a normal `/gad-run --max 1` crawl step, or a
    full `/gad-generation` restart after a non-destructive recovery. `command()` needs nothing
    beyond this dataclass to build the final argv.

    `blocking_decision_ids` is populated only for the AWAITING_HUMAN-via-gated-owner-decision
    case — the loop's notify key includes it so a DIFFERENT decision blocking the same repo/gen
    always produces a fresh notification instead of being silently deduped by a stale key
    (finding #2: notification dedupe must not permanently swallow a changed condition).
    """

    kind: str  # "DONE" | "AWAITING_HUMAN" | "BLOCKED" | "RUN" | "FINISH"
    repo: Path
    gen: int | None = None
    mode: str | None = None  # "gad_run" | "gad_generation" | "gad_finish" (RUN/FINISH only)
    detail: str = ""
    tier: str = "budget"
    token_target: str = "+2M"
    extra_flags: tuple[str, ...] = ()
    stashed_ref: str | None = None
    blocking_decision_ids: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class Snapshot:
    """`{HEAD, nextGen, artifact fingerprint}` per DESIGN.md §5. Two snapshots (pre/post a run)
    are diffed by `outcome()` — purely from disk-visible facts, never from stdout/model prose
    (Invariant #2).
    """

    head: str | None
    next_gen: int | None
    handoff_exists: bool
    open_decision_ids: frozenset[str]
    census: dict[str, Any]
    backlog_exhausted: bool


# ─────────────────────────────────────────────────────────────────────────────
# generations-index.json + backlog.md readers (disk truth)
# ─────────────────────────────────────────────────────────────────────────────


def index_path(repo: Path) -> Path:
    return Path(repo) / ".gad" / "generations-index.json"


def read_index(repo: Path) -> dict[str, Any] | None:
    """Parse `.gad/generations-index.json`. Returns None if the repo is not GAD-bootstrapped
    yet or the file is unreadable/corrupt — both are handled by the caller as AWAITING_HUMAN,
    never as a crash.
    """
    path = index_path(repo)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def find_owner_decisions(index: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Recursively collect every dict found under an `ownerDecisions` key anywhere in the
    index. gad-kit's consolidator.md documents the item shape
    (`{id, question, severity, default, costOfWrong, blocksGen, status:"open"}`) but is not
    fully explicit about whether the array lives at the top level, per-generation, or both —
    so this walks the whole structure rather than assuming one shape (see
    uncertainty-ledger.jsonl for this generation).
    """
    found: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            decisions = node.get("ownerDecisions")
            if isinstance(decisions, list):
                found.extend(d for d in decisions if isinstance(d, dict))
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(index or {})
    return found


def blocking_decisions(decisions: list[dict[str, Any]], next_gen: int) -> list[dict[str, Any]]:
    """Owner decisions that actually gate `next_gen`'s preflight: `status == "open"` AND a
    parseable `blocksGen <= next_gen`. A decision with no `blocksGen` at all is ADVISORY (per
    consolidator.md's own STATUS vocabulary: `ADVISORY` vs `GATED@genN`) and never blocks.
    """
    blocking: list[dict[str, Any]] = []
    for decision in decisions:
        if decision.get("status") != _OPEN_STATUS:
            continue
        raw_blocks_gen = decision.get("blocksGen")
        if raw_blocks_gen is None:
            continue  # ADVISORY — surfaced to the human elsewhere, never gates the crawl
        try:
            blocks_gen = int(raw_blocks_gen)
        except (TypeError, ValueError):
            continue
        if blocks_gen <= next_gen:
            blocking.append(decision)
    return blocking


def open_owner_decisions(repo: Path) -> list[dict[str, Any]]:
    """Every OPEN ownerDecision in the repo (gating or advisory), for the back-channel's
    self-documenting help + park messages — so the operator sees which ids are resolvable and
    what each one asks, instead of having to know them cold. Read-only; returns [] if the repo is
    not GAD-bootstrapped or unreadable.
    """
    index = read_index(repo)
    if index is None:
        return []
    return [d for d in find_owner_decisions(index) if d.get("status") == _OPEN_STATUS]


def format_decisions_for_operator(decisions: list[dict[str, Any]], *, gen: int | None = None) -> str:
    """Render owner decisions into a phone-friendly block that TELLS the operator exactly what to
    send back — question text plus the literal `resolve <id> <answer>` line — so no command syntax
    has to be memorized. Questions are truncated so a long decision can't blow up one Telegram
    message. Returns '' for an empty list (callers decide the empty-state wording).
    """
    if not decisions:
        return ""
    header = f"Decision needed (gen {gen}):" if gen is not None else "Open decisions:"
    lines = [header]
    for d in decisions:
        did = str(d.get("id", "?"))
        question = str(d.get("question", "")).strip().replace("\n", " ")
        if len(question) > 180:
            question = question[:177] + "..."
        lines.append(f"• {did}: {question}" if question else f"• {did}")
        lines.append(f"  reply:  resolve {did} <your answer>")
    return "\n".join(lines)


def backlog_generations(repo: Path) -> list[int]:
    """Generation numbers declared in `.gad/backlog.md` (headers of the form `## G<N> — ...`)."""
    backlog_path = Path(repo) / ".gad" / "backlog.md"
    if not backlog_path.exists():
        return []
    try:
        text = backlog_path.read_text(encoding="utf-8")
    except OSError:
        return []
    return sorted({int(m.group(1)) for m in _BACKLOG_HEADER_RE.finditer(text)})


def completed_generations(index: dict[str, Any] | None) -> set[int]:
    if not index:
        return set()
    generations = index.get("generations")
    if not isinstance(generations, list):
        return set()
    done: set[int] = set()
    for entry in generations:
        if isinstance(entry, dict) and "gen" in entry:
            try:
                done.add(int(entry["gen"]))
            except (TypeError, ValueError):
                continue
    return done


def pending_generations(repo: Path, index: dict[str, Any] | None) -> list[int]:
    """Generations declared in the backlog but not yet present as completed in the index —
    mirrors gad-run's own Survey phase (gad-run.js), computed from disk without spawning an
    agent. This is a lightweight heuristic reader, not gad-run's own survey logic; a
    discrepancy just means claude-relay proposes a RUN that gad-run's own (more authoritative)
    survey then confirms or corrects — never a hard failure either way.
    """
    declared = backlog_generations(repo)
    done = completed_generations(index)
    return sorted(gen for gen in declared if gen not in done)


def generation_dir(repo: Path, gen: int) -> Path:
    return Path(repo) / ".gad" / f"generation-{gen}"


def handoff_path(repo: Path, gen: int) -> Path:
    return generation_dir(repo, gen) / "handoff.md"


# ─────────────────────────────────────────────────────────────────────────────
# Artifact census (the feasibility-critical recovery-routing check)
# ─────────────────────────────────────────────────────────────────────────────

# The ONE genuine Verify-or-later artifact FINISH-safety is gated on. See the module docstring
# for the full chain of reasoning: gad-generation.js's `phase('Verify')` block writes exactly
# this file ("Write ${DIR}/reviews/verification.md"), and Verify can only run after Guardrails'
# test-writer returned non-null (a null result makes the script `deadAgentAbort` and `return`
# before Verify ever starts). Deliberately NOT any path-pattern/test-file heuristic — a prior
# version of this check ("does some dirty path look like a test file") was broken by BOTH a
# Prep-phase fixture written under `tests/` and a coincidentally `test_`-named file anywhere in
# the repo, either of which could satisfy it before Guardrails ever ran.
_VERIFY_ARTIFACT_RELATIVE = ("reviews", "verification.md")


def artifact_census(repo: Path, gen: int) -> dict[str, Any]:
    """Census `.gad/generation-<gen>/`. `verify_or_later` is the sole FINISH-safety gate (see
    the module docstring); the other fields are diagnostic only (surfaced in `Plan.detail` /
    `claude-relay status`), never used to decide FINISH vs. restart.
    """
    gen_dir = generation_dir(repo, gen)
    reviews_dir = gen_dir / "reviews"
    reviews_present = sorted(name for name in _REVIEW_FILENAMES if (reviews_dir / name).exists())
    return {
        "plan": (gen_dir / "plan.md").exists(),
        "reviews_present": reviews_present,
        "setup_notes": (gen_dir / "setup-notes.md").exists(),
        "implementation_log": (gen_dir / "implementation-log.md").exists(),
        "adversarial_review": (reviews_dir / "adversarial-review.md").exists(),
        "verify_or_later": gen_dir.joinpath(*_VERIFY_ARTIFACT_RELATIVE).exists(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# git helpers (narrow subprocess surface; never spawns `claude` — that is runner.py's job)
# ─────────────────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603, S607 - fixed `git` binary, fixed argv, not shell=True
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False
    )


def git_head(repo: Path) -> str | None:
    result = _git(repo, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def git_status_porcelain(repo: Path) -> list[str]:
    result = _git(repo, "status", "--porcelain")
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _status_line_path(line: str) -> str | None:
    """Extract the path from one `git status --porcelain` (v1) line: `XY PATH` or, for
    renames, `XY ORIG -> PATH`.
    """
    if len(line) < 4:
        return None
    rest = line[3:]
    if " -> " in rest:
        rest = rest.split(" -> ", 1)[1]
    rest = rest.strip().strip('"')
    return rest or None


def git_stash_push(repo: Path, message: str) -> str | None:
    """`git stash push -u -m <message>` — Invariant #6: prefer stash over any destructive
    discard. Returns `message` (the stash's identifying label) on success, or None if there
    was nothing to stash (an empty/clean tree, e.g. only untracked scratch files git ignores).
    """
    result = _git(repo, "stash", "push", "-u", "-m", message)
    if result.returncode != 0:
        raise GitRecoveryError(f"git stash push failed in {repo}: {result.stderr.strip()}")
    if "No local changes to save" in result.stdout:
        return None
    return message


def _has_gad_dirty_signal(repo: Path, next_gen: int, status_lines: list[str]) -> bool:
    """One of TWO conditions `triage()` requires before ever touching a dirty tree (the other
    is a matching clean-baseline HEAD, checked by the caller): does the dirty set look like an
    in-progress gad-kit generation at all? Signal: the `generation-<next_gen>/` scaffold
    directory exists (only gad-generation's own phases create it) OR some changed path is
    under `.gad/`. This is a heuristic, not a proof — flagged in uncertainty-ledger.jsonl.
    """
    if generation_dir(repo, next_gen).exists():
        return True
    for line in status_lines:
        path = _status_line_path(line)
        if path and (path == ".gad" or path.startswith(".gad/")):
            return True
    return False


def _dirty_paths(status_lines: list[str]) -> list[str]:
    return [path for line in status_lines if (path := _status_line_path(line))]


# ─────────────────────────────────────────────────────────────────────────────
# triage / command
# ─────────────────────────────────────────────────────────────────────────────


def triage(repo: Path, config: Config, state: dict[str, Any], *, dry_run: bool = False) -> Plan:
    """Decide what claude-relay should do next for `repo`, in the order DESIGN.md §5 specifies:
    1. open, GATED owner-decision blocking nextGen         -> AWAITING_HUMAN
    2. `generation-<nextGen>/handoff.md` present            -> BLOCKED
    3. dirty tree, no handoff (mid-generation interruption) -> artifact census -> FINISH or a
       safe `/gad-generation` restart (after `git stash`), or AWAITING_HUMAN if the dirtiness
       cannot be attributed to this tool's own run at all
    4. clean tree, backlog has pending generations          -> RUN `/gad-run --max 1`
    5. clean tree, backlog exhausted                        -> DONE

    `state` (the same dict `cooldown.load_state()`/`save_state()` round-trip) is consulted and
    updated for the clean-baseline-HEAD bookkeeping step 3 needs (Invariant #6/#9: never
    blanket-stash a dirty tree we cannot attribute to our own run) — the caller is responsible
    for eventually persisting it via `cooldown.save_state()`.

    `dry_run=True` (used by `claude-relay run --dry-run` and the `status`/park-loop previews)
    computes the identical decision but NEVER actually creates a `git stash` — triage must stay
    side-effect-free on the repo in that mode (it may still update the in-memory `state` dict's
    baseline bookkeeping; callers that pass `dry_run=True` are not required to persist that).
    """
    repo = Path(repo)
    index = read_index(repo)
    if index is None:
        return Plan(
            kind="AWAITING_HUMAN",
            repo=repo,
            detail=(
                f"{repo} is not GAD-bootstrapped ({index_path(repo)} missing) — run '/gad-init <repo>' first."
            ),
        )

    next_gen = int(index.get("nextGen", 0) or 0)

    blocking = blocking_decisions(find_owner_decisions(index), next_gen)
    if blocking:
        ids = tuple(str(d.get("id")) for d in blocking)
        return Plan(
            kind="AWAITING_HUMAN",
            repo=repo,
            gen=next_gen,
            detail=f"gen {next_gen} is gated on open owner decision(s): {list(ids)}",
            blocking_decision_ids=ids,
        )

    handoff = handoff_path(repo, next_gen)
    if handoff.exists():
        return Plan(
            kind="BLOCKED",
            repo=repo,
            gen=next_gen,
            detail=(
                f"consolidator handoff present at {handoff} — resolve the blocker "
                f"(claude-relay resolve <id> <answer>, or manual edit), then re-run"
            ),
        )

    status_lines = git_status_porcelain(repo)
    if status_lines:
        head = git_head(repo)
        baseline_head = cooldown.get_clean_baseline(state, repo)
        baseline_matches = baseline_head is not None and baseline_head == head
        gad_signal = _has_gad_dirty_signal(repo, next_gen, status_lines)

        if not (baseline_matches and gad_signal):
            reasons = []
            if baseline_head is None:
                reasons.append("claude-relay has never recorded this repo clean (no baseline)")
            elif not baseline_matches:
                reasons.append(f"HEAD moved since the last known-clean baseline ({baseline_head} -> {head})")
            if not gad_signal:
                reasons.append("no generation-N/ scaffold or .gad/ change in the dirty set")
            return Plan(
                kind="AWAITING_HUMAN",
                repo=repo,
                gen=next_gen,
                detail=(
                    "working tree is dirty but is not safely attributable to a claude-relay-"
                    f"initiated run ({'; '.join(reasons)}) — Invariant #6 forbids guessing "
                    "here (never blanket-stash unrelated work); inspect manually, then re-run"
                ),
            )

        census = artifact_census(repo, next_gen)
        if census["verify_or_later"]:
            return Plan(
                kind="FINISH",
                repo=repo,
                gen=next_gen,
                mode="gad_finish",
                tier=config.gadkit_tier,
                token_target=config.token_target,
                extra_flags=tuple(config.gadkit_extra_flags),
                detail=(
                    f"{generation_dir(repo, next_gen)}/reviews/verification.md present — the "
                    "Verify phase (which only ever starts after Guardrails' test-writer "
                    "succeeded) already ran at least once; safe to resume via /gad-finish"
                ),
            )
        if dry_run:
            return Plan(
                kind="RUN",
                repo=repo,
                gen=next_gen,
                mode="gad_generation",
                tier=config.gadkit_tier,
                token_target=config.token_target,
                extra_flags=tuple(config.gadkit_extra_flags),
                stashed_ref=None,
                detail=(
                    "[dry-run] mid-generation interruption with no reviews/verification.md yet "
                    "(Guardrails/Verify not proven complete); would default to a full "
                    "'/gad-generation' restart after 'git stash push -u' — NOT stashing in "
                    "dry-run mode, repo left untouched"
                ),
            )
        dirty_paths = _dirty_paths(status_lines)
        stash_ref = git_stash_push(
            repo, f"claude-relay: parking gen{next_gen} before safe restart {_now_iso()}"
        )
        if stash_ref is not None:
            cooldown.record_stash(state, repo, stash_ref, dirty_paths)
        return Plan(
            kind="RUN",
            repo=repo,
            gen=next_gen,
            mode="gad_generation",
            tier=config.gadkit_tier,
            token_target=config.token_target,
            extra_flags=tuple(config.gadkit_extra_flags),
            stashed_ref=stash_ref,
            detail=(
                "mid-generation interruption with no reviews/verification.md yet "
                "(Guardrails/Verify not proven complete); defaulting to a full "
                "'/gad-generation' restart rather than risk a testless '/gad-finish' commit "
                f"(stashed_ref={stash_ref!r}, {len(dirty_paths)} path(s) swept)"
            ),
        )

    # Clean tree: this is the one point where we know for certain claude-relay is looking at a
    # committed-green boundary — record it as the baseline any FUTURE dirty state is measured
    # against (Invariant #6/#9).
    cooldown.set_clean_baseline(state, repo, git_head(repo))

    pending = pending_generations(repo, index)
    if not pending:
        return Plan(
            kind="DONE",
            repo=repo,
            gen=next_gen,
            detail="backlog exhausted — no pending generations declared beyond what is committed",
        )

    return Plan(
        kind="RUN",
        repo=repo,
        gen=pending[0],
        mode="gad_run",
        tier=config.gadkit_tier,
        token_target=config.token_target,
        extra_flags=tuple(config.gadkit_extra_flags),
        detail=f"{len(pending)} pending generation(s) {pending}; clean tree at a committed boundary",
    )


def gadkit_plugin_root() -> Path:
    """Absolute install root of the gad-kit plugin — the newest installed version that ships the
    bundled workflow scripts (`workflows/*.js`) and agent roles (`agents/`). The headless `-p`
    prompt hands the model an ABSOLUTE `scriptPath` into this tree.

    VERIFIED LIVE (2026-07-19): a `claude` launched with `CLAUDE_CONFIG_DIR=<seat>` can read the
    canonical `~/.claude/plugins/...` path even though each seat also has its own `plugins/` dir,
    so one resolved root works for every seat. gadkit.py itself runs in the supervisor process
    (real HOME), so `Path.home()` is correct here regardless of the child's CLAUDE_CONFIG_DIR.
    """
    base = Path.home() / ".claude" / "plugins" / "cache" / "gad-kit" / "gad-kit"
    candidates = [d for d in base.glob("*") if (d / "workflows" / "gad-run.js").is_file()]
    if not candidates:
        raise FileNotFoundError(
            f"gad-kit plugin workflows not found under {base} — is the gad-kit plugin installed?"
        )

    def _ver_key(path: Path) -> tuple[int, ...]:
        key: list[int] = []
        for token in path.name.split("."):
            try:
                key.append(int(token))
            except ValueError:
                key.append(0)
        return tuple(key)

    return max(candidates, key=_ver_key)


def command(plan: Plan) -> list[str]:
    """Build the full `claude` argv for a RUN/FINISH plan.

    Headless mechanics (ALL verified live 2026-07-19 — see uncertainty-ledger.jsonl):
    - gad-kit's `/gad-*` commands are thin wrappers that call the **Workflow tool** with a
      bundled JS script; the Workflow tool is ASYNC — it returns a task id immediately and only
      *notifies* on completion.
    - A plugin slash command passed as the sole `-p` prompt is rejected ("Unknown command"), and
      paraphrasing it (the old approach) let the model GUESS the Workflow call. That is exactly
      how gen 0 died: the model called `Workflow({name:"gad-kit:gad-run", args:"<json-string>"})`
      at the wrong plugin version, then ended its turn with "I'll report when it finishes" — so
      `claude -p` exited and killed the still-running generation before it committed.
    - Fix (this function): hand the model the EXACT `Workflow({scriptPath, args:<object>})` call,
      then force it to BLOCK on `TaskOutput(block=true)` until the task is terminal before ending
      its turn. A blocked TaskOutput keeps the `-p` process alive for the whole multi-minute
      workflow — verified: a 37s background workflow was awaited and its real return value
      reported, `stop_reason:end_turn` arriving only after the task reached a terminal state.

    `--profile budget|balanced` is gad-kit's own name for what claude-relay calls a "tier"
    internally (see the package docstring); it is threaded through as the `profile` workflow arg.
    """
    if plan.kind not in ("RUN", "FINISH"):
        raise ValueError(f"command() only applies to RUN/FINISH plans, got kind={plan.kind!r}")
    if plan.gen is None:
        raise ValueError(f"command() requires plan.gen, got a {plan.kind} plan with gen=None")

    repo_abs = str(Path(plan.repo).resolve())
    root = gadkit_plugin_root()
    roles_dir = str(root / "agents")
    budget_directive = ""

    if plan.mode == "gad_finish":
        script = str(root / "workflows" / "gad-finish.js")
        wf_args: dict[str, Any] = {
            "repo": repo_abs,
            "gen": plan.gen,
            "rolesDir": roles_dir,
            "profile": plan.tier,
        }
    elif plan.mode == "gad_generation":
        script = str(root / "workflows" / "gad-generation.js")
        wf_args = {
            "repo": repo_abs,
            "gen": plan.gen,
            "rolesDir": roles_dir,
            "profile": plan.tier,
        }
    elif plan.mode == "gad_run":
        script = str(root / "workflows" / "gad-run.js")
        wf_args = {
            "repo": repo_abs,
            "rolesDir": roles_dir,
            "generationScript": str(root / "workflows" / "gad-generation.js"),
            "finishScript": str(root / "workflows" / "gad-finish.js"),
            "profile": plan.tier,
            "maxGens": 1,
        }
        # TOKEN_TARGET (e.g. "+2M"): gad-run.js self-paces to the turn's token target (its
        # `budget.total`), which the harness reads from a "+N"-style directive in the prompt.
        # Only the crawl step carries it; the recovery tails use gad-generation/finish defaults.
        budget_directive = f" You have a token budget of {plan.token_target} for this turn."
    else:
        raise ValueError(f"RUN plan with unrecognized mode={plan.mode!r}")

    args_json = json.dumps(wf_args, indent=2)
    instruction = (
        "You have exactly one job: run ONE gad-kit step in the background, WAIT for it to "
        "finish, and report its final status. This message is your explicit authorization to "
        "use the Workflow tool." + budget_directive + "\n\n"
        "Step 1 — call the Workflow tool EXACTLY once. Pass `args` as a JSON OBJECT (never as a "
        "string), using these exact values:\n"
        f"Workflow({{\n  scriptPath: \"{script}\",\n  args: {args_json}\n}})\n\n"
        "The Workflow tool returns a task id immediately and keeps running in the BACKGROUND; "
        "the work is NOT done when the tool call returns.\n\n"
        "Step 2 — you MUST NOT end your turn until that task reaches a terminal state. Block on "
        "it with the TaskOutput tool:\n"
        "TaskOutput({ task_id: \"<the id from step 1>\", block: true, timeout: 600000 })\n"
        "If TaskOutput returns while the task is still running, call it AGAIN with the same id. "
        "Keep looping until the task status is \"completed\" or \"failed\". Do not give up early.\n\n"
        "Step 3 — only AFTER the task is terminal, output exactly one line:\n"
        "RESULT: <the workflow's returned status field>\n"
        "Then stop.\n\n"
        "Do NOT say \"I'll report when it finishes\" and end your turn — if you end your turn "
        "while the task is still running, the background workflow is KILLED and the job fails. "
        "You must actually block on TaskOutput until it is done. Do no other work."
    )
    # `--output-format stream-json` in print mode REQUIRES `--verbose` (verified live 2026-07-19:
    # without it claude exits "When using --print, --output-format=stream-json requires --verbose").
    return [
        "-p",
        "--dangerously-skip-permissions",
        "--verbose",
        "--output-format",
        "stream-json",
        instruction,
    ]


# ─────────────────────────────────────────────────────────────────────────────
# snapshot / outcome
# ─────────────────────────────────────────────────────────────────────────────


def snapshot(repo: Path) -> Snapshot:
    """`{HEAD, nextGen, artifact fingerprint}` — call once before and once after a run; diffed
    by `outcome()` purely on disk-visible facts (Invariant #2).
    """
    repo = Path(repo)
    index = read_index(repo)
    next_gen = int(index.get("nextGen", 0) or 0) if index else None
    head = git_head(repo)
    handoff_exists = False
    census: dict[str, Any] = {}
    if next_gen is not None:
        handoff_exists = handoff_path(repo, next_gen).exists()
        census = artifact_census(repo, next_gen)
    decisions = find_owner_decisions(index) if index else []
    open_ids = frozenset(
        str(d.get("id"))
        for d in decisions
        if d.get("status") == _OPEN_STATUS and d.get("blocksGen") is not None
    )
    backlog_exhausted = not pending_generations(repo, index) if index is not None else False
    return Snapshot(
        head=head,
        next_gen=next_gen,
        handoff_exists=handoff_exists,
        open_decision_ids=open_ids,
        census=census,
        backlog_exhausted=backlog_exhausted,
    )


def outcome(pre: Snapshot, post: Snapshot, usage: UsageSnapshot | None, ceiling_pct: float) -> str:
    """Classify a completed run from pre/post disk snapshots + a fresh post-run usage
    reading — never from stdout/model prose (Invariant #2; stdout is a backstop signal used
    only by `detector.classify()`, one layer up). One of:
    PROGRESSED · HIT_WALL · AWAITING_HUMAN · BLOCKED · AGENT_DEAD_NONLIMIT · NO_BACKLOG.

    `ceiling_pct` is the synthetic per-seat rotation ceiling that was just in effect (the seat
    the run just used) — HIT_WALL is evaluated relative to THAT ceiling, not a fixed global
    percent, so a seat configured with a low ceiling (e.g. 70%) correctly reports HIT_WALL
    right at its own ceiling rather than being misclassified as AGENT_DEAD_NONLIMIT until some
    unrelated global near-100% threshold (the "90-99% dead zone").

    Order matters: a new commit is unambiguous progress and is checked first; a new open
    owner-decision or new handoff are the two disk-visible "needs a human" signals gad-kit
    itself produces; only once none of those disk facts explain "no commit" do we fall back to
    the live usage reading (at-ceiling -> genuinely hit the wall) and finally to the weaker
    backlog-exhaustion inference before conceding "an agent died for a non-limit reason."
    """
    if post.head is not None and post.head != pre.head:
        return "PROGRESSED"

    newly_open = post.open_decision_ids - pre.open_decision_ids
    if newly_open:
        return "AWAITING_HUMAN"

    if post.handoff_exists and not pre.handoff_exists:
        return "BLOCKED"

    if usage is not None and usage_mod.near_cap(usage, threshold=ceiling_pct):
        return "HIT_WALL"

    if post.backlog_exhausted:
        return "NO_BACKLOG"

    return "AGENT_DEAD_NONLIMIT"


# ─────────────────────────────────────────────────────────────────────────────
# resolve-in: durable owner-decision resolution (DESIGN.md §7)
# ─────────────────────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class ResolveResult:
    found: bool
    decision: dict[str, Any] | None
    index_path: Path


def resolve_owner_decision(repo: Path, decision_id: str, answer: str) -> ResolveResult:
    """Atomically mark the matching `ownerDecisions[]` entry (wherever it lives in
    generations-index.json) as resolved: `status -> "resolved"`, plus a `resolution` note and
    `resolvedAt` timestamp. This edits DURABLE disk state — not a chat reply — which is what
    actually unblocks a parked AWAITING_HUMAN repo and survives seat rotation (DESIGN.md §7).
    Both `claude-relay resolve` and the Telegram poller call this SAME function.
    """
    repo = Path(repo)
    path = index_path(repo)
    index = read_index(repo)
    if index is None:
        return ResolveResult(found=False, decision=None, index_path=path)

    found_decision: dict[str, Any] | None = None

    def _walk(node: Any) -> None:
        nonlocal found_decision
        if isinstance(node, dict):
            decisions = node.get("ownerDecisions")
            if isinstance(decisions, list):
                for d in decisions:
                    if isinstance(d, dict) and str(d.get("id")) == str(decision_id):
                        d["status"] = "resolved"
                        d["resolution"] = answer
                        d["resolvedAt"] = _now_iso()
                        found_decision = d
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(index)
    if found_decision is None:
        return ResolveResult(found=False, decision=None, index_path=path)

    _atomic_write_json(path, index)
    return ResolveResult(found=True, decision=found_decision, index_path=path)


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".generations-index.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
