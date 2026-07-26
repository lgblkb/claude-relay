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
heuristic and wrongly proved "Guardrails ran." Fixed: `triage()` now gates FINISH on TWO
genuine Verify-or-later artifacts — `.gad/generation-N/reviews/verification.md` (written ONLY
by the Verify-phase verifier agent, gad-generation.js's `phase('Verify')` block: "Write
${DIR}/reviews/verification.md") AND `.gad/generation-N/reviews/adversarial-review.md`.

The SAFETY PROPERTY `verification.md` alone buys (gad-kit 2.0, which no longer has a single
mandatory Guardrails agent): whichever agent Guardrails hard-requires for this generation's
`genType` MUST have returned non-null before `phase('Verify')` can run at all, because every
Guardrails branch aborts-and-returns on a null result *before* Verify:
  - `genType: 'ideation'` — the results-skeptic's ideation vet IS the guardrail (there is no
    test-writer, since ideation ships no code): `if (vet === null) return
    deadAgentAbort('Guardrails/skeptic-vet')`.
  - every other genType (`build`/`experiment`/`eda`) — the test-writer is the hard requirement:
    `if (!guardrails || guardrails[0] === null) return deadAgentAbort('Guardrails/test-writer')`.

REFUSED-status regression fix (2026-07-26, gad-kit's uncommitted 2.1.0 work): `verification.md`
alone turned out NOT to be sufficient, because the adversarial-reviewer/results-skeptic
component of Guardrails is only ADVISORY for non-ideation genTypes — if it dies after retries,
gad-generation.js logs "proceeding without an adversarial review this generation (the verifier
+ gate remain the backstop)" and still runs Verify, still writing verification.md. So
"verification.md present, adversarial-review.md absent" is an ORDINARY, gad-kit-sanctioned
committed state — but NOT a safe FINISH target for an interrupted generation, because
gad-finish.js's own Verify prompt now MECHANICALLY REFUSES exactly this case (ship-blocker 2,
same date): absent `adversarial-review.md` it sets `guardrailsArtifactMissing`, returns `status:
'REFUSED'`, and writes NOTHING — the tail considers itself the wrong route for that generation.
Routing to FINISH anyway wastes an invocation for nothing (worse: three, before HARD_ERROR, since
`outcome()` reads "nothing committed" as `AGENT_DEAD_NONLIMIT` and the next `triage()` would
propose the SAME refused FINISH again — nothing on disk changed). Requiring BOTH files can never
reject a genuinely resumable ideation generation: ideation's hard-required guardrail call IS the
one that writes adversarial-review.md, so the two files are never out of sync in that genType —
only the non-ideation advisory-death gap is excluded, which is exactly the point.

Either way the dangerous case this gate exists to prevent (a `/gad-finish` that commits or is
attempted against a generation whose mandatory guardrail evidence is incomplete) is excluded. If
either file is absent, `triage()` ALWAYS defaults to a full `/gad-generation` restart (never
FINISH) — a redundant restart is safe; a testless/reviewless commit (or a wasted REFUSED
invocation) is not. Recovery also non-destructively parks the partial tree with `git stash`
(Invariant #6 — never `git reset --hard` unrelated work, and never even stash unless (a) the
dirtiness is scoped to a `git HEAD` claude-relay itself last saw this repo clean at, AND (b) the
dirty set shows a `.gad/`/`generation-N/` signal consistent with an in-progress generation).

Which generation is "in flight" is ALSO disk evidence, not `index.nextGen` (2026-07-26 audit).
`nextGen` is only advanced by a successful consolidation (gad-kit/agents/consolidator.md:91
"...commit placeholder, set `nextGen`"), so it names *a* generation, not necessarily the one
whose partial work is on disk. In gad-kit 2.0 the crawl no longer runs pending generations in
gen order at all — gad-run.js:283 sorts them `priority DESC, then gen ASC`, and that sort is
NOT gated on research mode, so it activates on any repo the moment one backlog entry carries a
`Priority:` line. An out-of-order generation that is then interrupted leaves `nextGen` pointing
at a DIFFERENT generation: the artifact census read the wrong `generation-N/`, concluded
`verify_or_later=False`, and claude-relay stashed the in-flight generation's tree to "restart"
an unrelated one. `in_flight_generation()` therefore derives it from the dirty set first and
directory mtimes second, and falls back to `nextGen` only when disk says nothing at all — the
ambiguous case keeps the pre-2.0 behaviour rather than guessing. Note the `git stash` gate
itself (`_has_gad_dirty_signal`) no longer takes a generation number at all — it is satisfied
ONLY by an actual dirty path under `.gad/` (2026-07-26 audit finding B10: a bare
`generation_dir(repo, next_gen).exists()` clause used to satisfy it unconditionally the moment
that scaffold directory existed on disk, regardless of whether anything under `.gad/` was
actually dirty; reproduced with three unrelated operator files — two of them untracked — swept
into a stash with zero `.gad/` paths in the dirty set. Removed rather than fixed-in-place, per
the module's own docstring above, which always described the gate as "some changed path is
under `.gad/`"). Widening *which* generation we recover (see the A5-fix note on `recovery_gen`
in `triage()`) must never widen *when* we are willing to touch the tree at all (Invariant #6).
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

# The one status token gad-kit recognizes as unblocking. See `resolve_owner_decision()` for the
# full defect writeup: gad-kit's open-decision predicate is an LLM-judged prompt string that says
# 'status "open" (i.e. not "answered")' (gad-generation.js:443) / 'status "open"/not "answered"'
# (gad-run.js:220), so any third value is undefined behaviour there.
_ANSWERED_STATUS = "answered"

_BACKLOG_HEADER_RE = re.compile(r"^##\s*G(\d+)\b", re.MULTILINE)

# One backlog entry's declared generation type. Two on-disk renderings must both parse:
# the research template writes a markdown bullet with bold key (`- **Type**: experiment`, e.g.
# gad-kit/templates/backlog-research.md:39/47), while gad-run's survey prompt only ever asks the
# agent for "its \"Type:\" line" (gad-kit/workflows/gad-run.js:218) — so a hand-written bare
# `Type: experiment` is equally legitimate. Hence the optional bullet marker / `**` / backticks.
_BACKLOG_TYPE_RE = re.compile(
    r"^[ \t]*[-*]?[ \t]*\*{0,2}[ \t]*type[ \t]*\*{0,2}[ \t]*:[ \t]*`?([A-Za-z]+)",
    re.IGNORECASE | re.MULTILINE,
)

# The genType values gad-generation.js:124 actually accepts; anything else (including a literal
# "build") coerces to 'build' there, which is also the no-argument default — so claude-relay
# reports those as None and OMITS the key entirely, mirroring gad-run.js:350.
_RESEARCH_GEN_TYPES = ("experiment", "eda", "ideation")

# The load-bearing research-mode marker gad-run's survey looks for (gad-run.js:219), rendered as
# an HTML comment by the research backlog template (templates/backlog-research.md:3).
_RESEARCH_MODE_MARKER = "gad-mode: research"

# A9 audit fix: `is_research_repo()` used to be a bare substring test over the WHOLE backlog, so
# a build repo merely discussing "gad-mode: research" in prose (plausible in a research-flavoured
# build backlog's own narrative) never terminated cleanly — it kept handing an exhausted backlog
# to /gad-run's auto-ideation forever. Now the marker must appear inside an HTML comment, which is
# the only form the template actually emits it in.
_HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)

# A `.gad/generation-<N>/...` path as `git status --porcelain` reports it (repo-relative, and a
# wholly-new scaffold shows up as the single untracked entry `.gad/generation-7/`).
_GEN_PATH_RE = re.compile(r"(?:^|/)\.gad/generation-(\d+)(?:/|$)")

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

    `gen_type` carries the generation's declared gad-kit 2.0 `genType` (`experiment`/`eda`/
    `ideation`, or None for the default `build`) on the two RECOVERY modes only. It is read from
    the backlog by `backlog_gen_type()`; `command()` threads it into the workflow args exactly
    like gad-run.js:350 does, omitting it whenever it is None.

    `ideation_refill` is True ONLY for the one RUN plan `_exhausted_backlog_plan()` produces when
    handing an exhausted RESEARCH backlog to `/gad-run` for auto-ideation — the plan that ALSO
    books `cooldown.set_ideation_attempt_head()` at decision time. A6 audit fix: `run_once()`'s
    no-seat early return must give that booking back (it was recorded but never actually spent),
    but it must do so ONLY when THIS plan is the one that booked it — gating on "no seat was
    found for a RUN/FINISH plan" alone (any plan) wiped a still-binding marker from an entirely
    unrelated iteration (e.g. a mid-generation recovery restart), letting the same HEAD be
    re-attempted for auto-ideation indefinitely once every no-seat iteration cleared it again.
    """

    kind: str  # "DONE" | "AWAITING_HUMAN" | "BLOCKED" | "RUN" | "FINISH"
    repo: Path
    gen: int | None = None
    mode: str | None = None  # "gad_run" | "gad_generation" | "gad_finish" (RUN/FINISH only)
    detail: str = ""
    tier: str = "budget"
    token_target: str = "+2M"
    gen_type: str | None = None  # gad-kit 2.0 genType; None == 'build' (the workflow default)
    stashed_ref: str | None = None
    blocking_decision_ids: tuple[str, ...] = ()
    ideation_refill: bool = False


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


def backlog_path(repo: Path) -> Path:
    return Path(repo) / ".gad" / "backlog.md"


def _read_backlog(repo: Path) -> str | None:
    """The raw backlog text, or None if it is absent/unreadable. Every backlog reader below is
    read-only and must tolerate a repo that is not GAD-bootstrapped at all.
    """
    path = backlog_path(repo)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def backlog_generations(repo: Path) -> list[int]:
    """Generation numbers declared in `.gad/backlog.md` (headers of the form `## G<N> — ...`)."""
    text = _read_backlog(repo)
    if text is None:
        return []
    return sorted({int(m.group(1)) for m in _BACKLOG_HEADER_RE.finditer(text)})


def backlog_section(repo: Path, gen: int) -> str | None:
    """The body of `.gad/backlog.md`'s `## G<gen>` entry — from just after its heading to the
    start of the NEXT `## G<n>` heading (or EOF). Bounding matters: the research template puts a
    per-entry *convention legend* (`- **Type**: \\`experiment\\` | \\`eda\\` | ...`,
    templates/backlog-research.md:15) in the preamble ABOVE `## G0`, and an unbounded search
    would read that legend as if it were G0's declared type. Returns None when the backlog is
    missing/unreadable or declares no such generation.
    """
    text = _read_backlog(repo)
    if text is None:
        return None
    headers = list(_BACKLOG_HEADER_RE.finditer(text))
    for i, match in enumerate(headers):
        if int(match.group(1)) != gen:
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        return text[match.end() : end]
    return None


def backlog_gen_type(repo: Path, gen: int) -> str | None:
    """The gad-kit 2.0 `genType` generation `gen` declares in the backlog, or None.

    None means "pass no genType" — which is exactly right for `build`, for a missing/unrecognized
    `Type:` line, and for an unreadable backlog, because gad-generation.js:124 and gad-finish.js:65
    both coerce an absent/unknown `cfg.genType` to `'build'` with no error path. Returning the
    value only for the three real research types mirrors gad-run.js:350's own
    `g.genType && g.genType !== 'build'` omit-for-build behaviour.

    Why claude-relay must read this at all: `RESEARCH = GEN_TYPE !== 'build'` switches the
    planner/prep/implement/guardrail ROLES and, when false, empties `RESEARCH_VERIFY_NOTE` /
    `RESEARCH_FIX_NOTE` — the two instructions that tell the verifier a rigorously refuted
    hypothesis is CLEAN and forbid the fixer from re-rolling pre-registered seeds or relaxing a
    ratchet floor. gad-run threads it per-generation from the backlog; claude-relay's two
    RECOVERY paths (`/gad-generation` restart, `/gad-finish` tail) are the only callers that
    would otherwise silently downgrade a research generation to a build generation.
    """
    section = backlog_section(repo, gen)
    if section is None:
        return None
    match = _BACKLOG_TYPE_RE.search(section)
    if match is None:
        return None
    value = match.group(1).strip().lower()
    return value if value in _RESEARCH_GEN_TYPES else None


def is_research_repo(repo: Path) -> bool:
    """True iff `.gad/backlog.md` carries the `gad-mode: research` marker — the same disk fact
    gad-run's survey sets `researchMode` from (gad-run.js:219), written by the research backlog
    template as an HTML comment (templates/backlog-research.md:3). Read-only and tolerant of a
    missing/unreadable backlog (a non-research repo, by definition).

    A9 audit fix: this used to be a bare substring test over the ENTIRE backlog, so a build repo
    whose narrative merely discusses "gad-mode: research" (or any other prose containing that
    exact phrase) was wrongly flipped into the auto-ideation branch and never terminated cleanly
    on an exhausted backlog. Now requires the marker to appear (a) inside an HTML comment — the
    only rendering the template actually produces — and (b) in the PREAMBLE, before the first
    `## G<n>` backlog heading, mirroring where the template places it and keeping a per-entry
    discussion of research methodology from accidentally counting.
    """
    text = _read_backlog(repo)
    if text is None:
        return False
    first_heading = _BACKLOG_HEADER_RE.search(text)
    preamble = text[: first_heading.start()] if first_heading is not None else text
    return any(_RESEARCH_MODE_MARKER in comment for comment in _HTML_COMMENT_RE.findall(preamble))


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

# The genuine Verify-or-later artifacts FINISH-safety is gated on. See the module docstring for
# the full chain of reasoning: gad-generation.js's `phase('Verify')` block writes exactly this
# file ("Write ${DIR}/reviews/verification.md"), and Verify can only run after Guardrails'
# test-writer (or, for `genType: 'ideation'`, the skeptic-vet) returned non-null (a null result
# makes the script `deadAgentAbort` and `return` before Verify ever starts). Deliberately NOT any
# path-pattern/test-file heuristic — a prior version of this check ("does some dirty path look
# like a test file") was broken by BOTH a Prep-phase fixture written under `tests/` and a
# coincidentally `test_`-named file anywhere in the repo, either of which could satisfy it before
# Guardrails ever ran.
_VERIFY_ARTIFACT_RELATIVE = ("reviews", "verification.md")

# REFUSED-status regression fix (2026-07-26, gad-kit uncommitted 2.1.0 work): `verification.md`
# alone is NO LONGER sufficient proof that Guardrails fully completed. gad-generation.js's
# Guardrails phase treats the adversarial-reviewer/results-skeptic component as ADVISORY for
# non-ideation genTypes — if it dies after retries the workflow logs "proceeding without an
# adversarial review this generation (the verifier + gate remain the backstop)" and continues
# straight to Verify anyway, which still writes verification.md. So "verification.md present,
# adversarial-review.md absent" is an ORDINARY, gad-kit-sanctioned state for a committed
# generation — but it is NOT a safe FINISH target for a generation that died mid-tail, because
# gad-finish.js's own Verify prompt now MECHANICALLY REFUSES exactly this case (ship-blocker 2,
# same date): if `reviews/adversarial-review.md` is absent it sets `guardrailsArtifactMissing`
# and returns `status: 'REFUSED'` with NO commit, NO fix-loop spend — the tail considers itself
# the wrong route for that generation. Before this fix, `triage()` would route such a
# generation to FINISH anyway (verification.md alone was "proof enough"), gad-finish would
# refuse and write nothing, `outcome()` would read that as AGENT_DEAD_NONLIMIT, and the identical
# refused FINISH would be retried 2 more times before HARD_ERROR — a livelock on a generation
# that a full `/gad-generation` restart handles perfectly well (claude-relay's own existing safe
# default whenever FINISH-safety is unproven). For `genType: 'ideation'` this can never diverge:
# the SAME agent call that is the hard-required guardrail (`vet === null` aborts) is the one that
# writes adversarial-review.md, so requiring both files uniformly never rejects a genuinely
# resumable ideation generation — it only excludes the non-ideation advisory-death gap.
_ADVERSARIAL_REVIEW_RELATIVE = ("reviews", "adversarial-review.md")


def artifact_census(repo: Path, gen: int) -> dict[str, Any]:
    """Census `.gad/generation-<gen>/`. `verify_or_later` is the sole FINISH-safety gate (see
    the module docstring); the other fields are diagnostic only (surfaced in `Plan.detail` /
    `claude-relay status`), never used to decide FINISH vs. restart.
    """
    gen_dir = generation_dir(repo, gen)
    reviews_dir = gen_dir / "reviews"
    reviews_present = sorted(name for name in _REVIEW_FILENAMES if (reviews_dir / name).exists())
    adversarial_review = (reviews_dir / "adversarial-review.md").exists()
    return {
        "plan": (gen_dir / "plan.md").exists(),
        "reviews_present": reviews_present,
        "setup_notes": (gen_dir / "setup-notes.md").exists(),
        "implementation_log": (gen_dir / "implementation-log.md").exists(),
        "adversarial_review": adversarial_review,
        # Both files required (REFUSED-status fix, see `_ADVERSARIAL_REVIEW_RELATIVE` above):
        # verification.md alone no longer proves Guardrails fully completed.
        "verify_or_later": (
            gen_dir.joinpath(*_VERIFY_ARTIFACT_RELATIVE).exists()
            and gen_dir.joinpath(*_ADVERSARIAL_REVIEW_RELATIVE).exists()
        ),
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


def git_log_has_gen_commit(repo: Path, gen: int) -> bool:
    """True if the current branch's history already has a `gen-<N>:` commit for `gen` — the
    literal message prefix `consolidator.md` step 8 / gad-kit's own IDEMPOTENT COMMIT guard use
    (`git commit -m "gen-N: <title>" ...`, `git log --oneline -15 --grep='^gen-N:'`). This is
    git's own durable record of "this generation's work was committed," independent of
    `generations-index.json`'s bookkeeping — which can legitimately lag behind git if a process
    dies between the artifact commit and a LATER, separate index-only reconciliation (exactly the
    gap that guard exists for). Unbounded (unlike gad-kit's own `-15`-commit window, C14): a
    `git log --grep` stops at the first match by default, so this is cheap regardless of history
    length, and there is no reason to accept the same staleness gad-kit's own bound risks.
    """
    result = _git(repo, "log", "--grep", f"^gen-{gen}:", "--format=%H", "-1")
    return result.returncode == 0 and bool(result.stdout.strip())


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


def _has_gad_dirty_signal(status_lines: list[str]) -> bool:
    """One of TWO conditions `triage()` requires before ever touching a dirty tree (the other
    is a matching clean-baseline HEAD, checked by the caller): does the dirty set look like an
    in-progress gad-kit generation at all? Signal: at least one changed path in `git status
    --porcelain` is under `.gad/`. This is a heuristic, not a proof — flagged in
    uncertainty-ledger.jsonl.

    B10 audit fix: this used to ALSO return True whenever `generation_dir(repo, next_gen)`
    merely EXISTED on disk, with no reference to `status_lines` at all. Once that scaffold
    directory was created — by any earlier gad-kit phase, possibly one that already committed —
    this clause was satisfied permanently and gated nothing: reproduced with three unrelated
    operator files (two of them untracked) getting swept into a `git stash` with ZERO `.gad/`
    paths anywhere in the dirty set. The docstring above always described the signal as "some
    changed path is under `.gad/`" — the directory-existence clause never actually matched that
    description, so it is removed here rather than special-cased. (No longer takes `repo`/
    `next_gen` at all: with the bare-existence clause gone, this function needs nothing but the
    porcelain lines themselves.)
    """
    for line in status_lines:
        path = _status_line_path(line)
        if path and (path == ".gad" or path.startswith(".gad/")):
            return True
    return False


def _dirty_paths(status_lines: list[str]) -> list[str]:
    return [path for line in status_lines if (path := _status_line_path(line))]


# B13 audit fix, second round: `resolve_owner_decision()`'s own immediate commit can fail (a
# rejecting pre-commit hook, an unset git identity — both ordinary operator conditions, not
# exotic). Left unhandled, the NEXT `triage()` either parks the repo forever (HEAD moved since
# the last clean baseline) or, worse, sweeps the resolution into a `git stash` as part of an
# unrelated dirty-tree recovery, reverting the decision back to `"open"` — silently destroying
# the operator's answer. These two helpers give `triage()` a defense-in-depth exemption: a LONE
# uncommitted `ownerDecisions[].status` diff on `generations-index.json` is never treated as
# stash-worthy "mid-generation interruption" dirt, no matter how many times committing it fails.
def _is_index_only_owner_decision_dirt(repo: Path, status_lines: list[str]) -> bool:
    """True iff the ENTIRE dirty set is exactly one path — a MODIFIED (not added/deleted/
    untracked) `generations-index.json` — the exact shape `resolve_owner_decision()`'s own write
    produces when its immediate commit attempt fails. Deliberately narrow: any OTHER dirty path
    alongside it (a genuinely in-progress generation touching `.gad/` too) does not qualify, so
    this can never mask real in-flight work — only the specific "nothing dirty except a pending
    resolution" shape.
    """
    if len(status_lines) != 1:
        return False
    path = _status_line_path(status_lines[0])
    if path is None:
        return False
    try:
        index_relative = index_path(repo).relative_to(Path(repo)).as_posix()
    except ValueError:  # pragma: no cover - defensive; index_path() is always repo/.gad/...
        return False
    if path != index_relative:
        return False
    code = status_lines[0][:2]
    # Only a MODIFICATION of an already-tracked file counts. "??" (untracked — e.g. a repo whose
    # `.gad/` was never committed at all) or a staged add/delete is a different situation this
    # exemption does not apply to.
    return "?" not in code and "M" in code


def _retry_commit_index_only(repo: Path, path: Path) -> bool:
    """Best-effort: (re-)attempt to commit JUST `generations-index.json`, mirroring
    `resolve_owner_decision()`'s own immediate attempt. Called from `triage()` on every cycle
    while the dirty set is EXACTLY this file — a transient failure (a flaky hook) self-heals
    without operator intervention; a permanent one (an unset git identity) costs one cheap
    `git add` + `git commit` attempt per triage cycle and is otherwise harmless (the JSON content
    is already the source of truth `blocking_decisions()` reads regardless of commit status).
    Returns whether the commit landed.
    """
    add_result = _git(repo, "add", "--", str(path))
    if add_result.returncode != 0:
        return False
    commit_result = _git(
        repo,
        "commit",
        "-m",
        "claude-relay: land a previously-uncommitted ownerDecision resolution",
        "--",
        str(path),
    )
    return commit_result.returncode == 0


# ─────────────────────────────────────────────────────────────────────────────
# Which generation is actually in flight (disk evidence, NOT index.nextGen)
# ─────────────────────────────────────────────────────────────────────────────


def existing_generation_dirs(repo: Path) -> dict[int, Path]:
    """Every `.gad/generation-<N>/` directory currently on disk, keyed by N. Non-numeric suffixes
    are ignored (nothing in gad-kit creates them, but a stray `generation-old/` must not raise).
    """
    gad_dir = Path(repo) / ".gad"
    found: dict[int, Path] = {}
    if not gad_dir.is_dir():
        return found
    for child in gad_dir.glob("generation-*"):
        if not child.is_dir():
            continue
        try:
            found[int(child.name.split("-", 1)[1])] = child
        except (IndexError, ValueError):
            continue
    return found


def in_flight_generation(repo: Path, index: dict[str, Any] | None, status_lines: list[str]) -> int | None:
    """The generation whose work the DISK shows in progress, or None when disk says nothing.

    `index.nextGen` cannot answer this: it only advances on a successful consolidation
    (agents/consolidator.md:91), and gad-kit 2.0's crawl runs pending generations in
    `priority DESC, gen ASC` order (gad-run.js:283) rather than gen order — so after an
    out-of-order generation is interrupted, `nextGen` names a generation whose directory is empty
    while the real partial work sits under a different one. Keying the artifact census off
    `nextGen` there made `verify_or_later` read as False and sent claude-relay to stash the
    in-flight tree and "restart" an unrelated generation (2026-07-26 audit).

    Evidence precedence, strongest first:
    1. A DIRTY path under `.gad/generation-<N>/` whose N is not already committed. Uncommitted
       artifacts are the strongest possible signal that N is the generation being worked on.
       Ties (two uncommitted generations both dirty — e.g. a resumable one plus a fresh one) are
       not resolvable from gen-number order alone (A3 audit fix: a prior version took `max(N)`
       here, reasoning "generation numbers are creation order" — but gad-kit 2.0's crawl runs
       generations `priority DESC, gen ASC` (gad-run.js:283), NOT creation order, so a stale
       abandoned high-numbered scaffold could outrank a genuinely in-progress lower-numbered one).
       Tie-broken the SAME way tier 2 already is: the most recently MODIFIED directory wins,
       falling back to the larger N only if mtimes are unreadable or exactly tied.
    2. Otherwise the most recently MODIFIED not-yet-committed `.gad/generation-<N>/` directory.
       Weaker (an mtime survives a `git stash`, and any read tool can bump nothing but still),
       but it is the only evidence left once the tree is clean of that generation's files.
    3. Otherwise None — the caller keeps its pre-existing `nextGen` behaviour rather than guess.
    """
    completed = completed_generations(index)

    def _mtime(gen: int) -> float:
        try:
            return generation_dir(repo, gen).stat().st_mtime
        except OSError:  # pragma: no cover - directory vanished between evidence collection and stat
            return -1.0

    dirty_gens = {
        int(match.group(1))
        for path in _dirty_paths(status_lines)
        if (match := _GEN_PATH_RE.search(path)) is not None
    }
    uncommitted_dirty = sorted(gen for gen in dirty_gens if gen not in completed)
    if uncommitted_dirty:
        if len(uncommitted_dirty) == 1:
            return uncommitted_dirty[0]
        return max(uncommitted_dirty, key=lambda gen: (_mtime(gen), gen))

    candidates = {gen: path for gen, path in existing_generation_dirs(repo).items() if gen not in completed}
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda gen: (candidates[gen].stat().st_mtime, gen))
    except OSError:  # pragma: no cover - the directory vanished between glob and stat
        return None


def find_uncommitted_handoff(repo: Path, index: dict[str, Any] | None, preferred: int | None) -> int | None:
    """The generation number of an on-disk `handoff.md` belonging to a generation that has NOT
    been committed, or None.

    v1 only ever looked at `generation-<nextGen>/handoff.md`, which misses the consolidator
    refusal it exists to catch whenever the interrupted generation is not `nextGen` (see
    `in_flight_generation()`): claude-relay then un-parked, spawned a run, and gad-kit's own
    dirty-tree/blocked handling stopped it again with nothing new on disk. Committed generations
    are excluded because a `handoff.md` file is never deleted once a later `/gad-finish` succeeds,
    so an old one must not park the repo forever. `preferred` (the in-flight generation) wins when
    it has a handoff; otherwise the smallest blocked generation is reported, so the operator is
    pointed at the earliest real blocker deterministically.

    2026-07-26 fix (live-verified against a real gad-kit run): `handoff.md`'s mere EXISTENCE was
    never a BLOCKED signal — `agents/consolidator.md` step 2 writes it as an ordinary per-
    generation artifact (open questions, the ranked "DECISIONS THE OWNER MUST MAKE" section,
    live-seam operator actions, the opportunity scout) on EVERY generation, committed status
    included; only the precondition-failure path (step "do NOT commit ... instead write the
    blocker to handoff.md") makes it a genuine blocker, and nothing on disk distinguishes the two
    cases by the file's mere presence. Per Invariant #2, the `## Status:` line inside the file is
    model-written prose and is deliberately never parsed here — only disk-visible facts decide.
    A generation is trusted as committed (hence its handoff, if any, is non-blocking) if EITHER
    `generations-index.json` says so (the fast path) OR git history already has a `gen-<N>:`
    commit for it (`git_log_has_gen_commit()` — the durable fallback for the exact gap
    consolidator.md's own IDEMPOTENT COMMIT guard exists for: a process dying between the
    artifact commit and a later, separate index-only reconciliation).
    """
    completed = completed_generations(index)

    def _is_committed(gen: int) -> bool:
        return gen in completed or git_log_has_gen_commit(repo, gen)

    blocked = sorted(
        gen
        for gen in existing_generation_dirs(repo)
        if not _is_committed(gen) and handoff_path(repo, gen).exists()
    )
    if not blocked:
        return None
    if preferred is not None and preferred in blocked:
        return preferred
    return blocked[0]


# ─────────────────────────────────────────────────────────────────────────────
# triage / command
# ─────────────────────────────────────────────────────────────────────────────


def triage(repo: Path, config: Config, state: dict[str, Any], *, dry_run: bool = False) -> Plan:
    """Decide what claude-relay should do next for `repo`, in the order DESIGN.md §5 specifies:
    1. open, GATED owner-decision blocking the earliest pending gen -> AWAITING_HUMAN
    2. any UNCOMMITTED `generation-<N>/handoff.md` present   -> BLOCKED
    3. dirty tree, no handoff (mid-generation interruption) -> artifact census of the generation
       DISK says is in flight -> FINISH or a safe `/gad-generation` restart (after `git stash`),
       or AWAITING_HUMAN if the dirtiness cannot be attributed to this tool's own run at all
    4. clean tree, backlog has pending generations          -> RUN `/gad-run --max 1`
    5. clean tree, backlog exhausted                        -> DONE, except that a RESEARCH repo
       gets one `/gad-run` per HEAD so gad-run's own auto-ideation can refill the backlog

    Steps 1-3 no longer key off `index.nextGen`: see `in_flight_generation()` and the module
    docstring for why that field does not name the generation whose work is on disk in gad-kit 2.0.

    `state` (the same dict `cooldown.load_state()`/`save_state()` round-trip) is consulted and
    updated for the clean-baseline-HEAD bookkeeping step 3 needs (Invariant #6/#9: never
    blanket-stash a dirty tree we cannot attribute to our own run) and for the once-per-HEAD
    ideation bookkeeping step 5 needs — the caller is responsible for eventually persisting it via
    `cooldown.save_state()`.

    `dry_run=True` (used by `claude-relay run --dry-run` and the `status`/park-loop previews)
    computes the identical decision but NEVER actually creates a `git stash` — triage must stay
    side-effect-free on the repo in that mode (it may still update the in-memory `state` dict's
    baseline/ideation bookkeeping; callers that pass `dry_run=True` are not required to persist
    that). A10 audit fix: this docstring used to also claim "every current one hands triage a
    `copy.deepcopy(state)` anyway" — false: `loop._park_and_wait()` deliberately passes the REAL
    `state` object (with `dry_run=True`) so the clean-baseline/ideation bookkeeping legitimately
    persists while the repo sits parked; `dry_run=True` there suppresses only the `git stash`
    side effect, not the state mutation. The false claim is corrected here rather than left to
    mislead the next reader into re-introducing the bug it warns about.
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
    pending = pending_generations(repo, index)
    status_lines = git_status_porcelain(repo)
    # B13 defense-in-depth (second-round blocker fix): if the ENTIRE dirty set is nothing but a
    # lone, uncommitted ownerDecisions[].status resolution (`resolve_owner_decision()`'s own
    # immediate commit attempt having failed — a rejecting pre-commit hook, an unset git
    # identity), retry landing that one commit HERE, on every triage cycle, at near-zero cost —
    # this is what lets a transient hook failure self-heal without operator intervention. Never
    # attempted in `dry_run=True` mode (triage must stay side-effect-free on the repo then).
    if not dry_run and _is_index_only_owner_decision_dirt(repo, status_lines):
        _retry_commit_index_only(repo, index_path(repo))
        status_lines = git_status_porcelain(repo)
    # The generation the DISK shows in flight — the census/FINISH/restart target. Falls back to
    # `nextGen` only when disk is silent, so an ambiguous repo keeps the pre-2.0 behaviour.
    in_flight = in_flight_generation(repo, index, status_lines)
    # A5 audit fix: `in_flight` is PURE disk evidence (git status + directory mtimes) — it can
    # name a generation number the backlog never declared at all. The concrete case: gad-run's
    # own auto-ideation sub-generation numbers itself `maxBacklogGen + 1` (gad-run.js), and that
    # number never gets a `## G<n>` heading, so `pending_generations()` never lists it. Recovering
    # such a number is unsafe: `backlog_gen_type()` has no backlog section to read for it and
    # silently returns None -> gad-generation.js/gad-finish.js coerce that to 'build' -> an
    # interrupted ideation generation gets FINISHed or restarted as an ordinary build generation,
    # and the consolidator never receives the instruction to append the vetted candidates (the
    # refill then "succeeds" — commits, advances HEAD — having produced nothing). Trust
    # `in_flight` as the recovery target only when it names a generation we can attribute to a
    # real, still-pending backlog entry; otherwise fall back to `nextGen`, exactly like the "disk
    # says nothing at all" case. (`in_flight_generation()` itself stays backlog-agnostic on
    # purpose — its own unit tests pin that contract — so this filter lives here, at its one call
    # site, where `pending` is already in scope.)
    in_flight_pending = in_flight if in_flight is not None and in_flight in pending else None
    recovery_gen = in_flight_pending if in_flight_pending is not None else next_gen

    # The owner-decision gate is `blocksGen <= X`, so X must be the generation about to be
    # attempted NEXT — which is `recovery_gen` for a dirty-tree recovery (FINISH or a
    # `/gad-generation` restart) and the smallest PENDING generation for a fresh `/gad-run` on a
    # clean tree (gad-run.js:222 computes its own `nextGen` the same way: "the smallest pending
    # gen number (or the index's nextGen if the backlog has none left)"). A4 audit fix: this used
    # to be `pending[0]` unconditionally, so an owner decision gating an out-of-order
    # `recovery_gen` (which can be LARGER than `pending[0]` — that is the whole point of the
    # priority-sorted crawl) silently slipped through this check; gad-kit's own preflight then
    # halted on it anyway, writing nothing to disk, and claude-relay retried three times into
    # HARD_ERROR — re-entering the exact livelock the recovery-routing fix exists to prevent.
    gate_gen = recovery_gen if status_lines else (pending[0] if pending else next_gen)
    blocking = blocking_decisions(find_owner_decisions(index), gate_gen)
    if blocking:
        ids = tuple(str(d.get("id")) for d in blocking)
        return Plan(
            kind="AWAITING_HUMAN",
            repo=repo,
            gen=gate_gen,
            detail=f"gen {gate_gen} is gated on open owner decision(s): {list(ids)}",
            blocking_decision_ids=ids,
        )

    # A7 audit fix: `find_uncommitted_handoff()` only excludes ALREADY-COMPLETED generations, and
    # nothing requires the tree to be dirty at all — a `handoff.md` file COMMITTED long ago under
    # a generation that was never declared in the current backlog (a hand-edited/experimental
    # directory, or a backlog entry later removed) is neither completed nor pending, has no path
    # back (nothing will ever consolidate a number the backlog does not declare), and would
    # otherwise park an entirely CLEAN tree on it forever (one notification, then silence, since
    # the same stale handoff satisfies the check on every future triage too). Only trust the
    # result when it names either a currently PENDING generation, or `next_gen` itself — the
    # latter matters because the auto-ideation refill's own recovery target IS `next_gen` (see
    # `_exhausted_backlog_plan()`) even though, per the A5 note above, that generation can never
    # appear in `pending` at all; an interrupted ideation attempt's handoff is a real, current
    # signal a human should see, not a stale orphan. `preferred` is fed the already-filtered
    # `in_flight_pending` (not the raw `in_flight`) so an untrustworthy in-flight number can't
    # even steer which blocked generation gets reported when more than one is blocked.
    handoff_gen = find_uncommitted_handoff(repo, index, in_flight_pending)
    if handoff_gen is not None and (handoff_gen in pending or handoff_gen == next_gen):
        return Plan(
            kind="BLOCKED",
            repo=repo,
            gen=handoff_gen,
            detail=(
                f"consolidator handoff present at {handoff_path(repo, handoff_gen)} (generation "
                f"{handoff_gen}) — resolve the blocker "
                f"(claude-relay resolve <id> <answer>, or manual edit), then re-run"
            ),
        )

    # B13 defense-in-depth: even after the retry-commit attempt above, a LONE uncommitted
    # ownerDecisions[].status diff (a permanently unset git identity never lands, however many
    # cycles retry) must NEVER be treated as "mid-generation interruption" dirt — that path can
    # `git stash` it, reverting a resolved decision back to `"open"` (the exact B13 failure mode,
    # reached this time via a commit that keeps failing rather than one that was never
    # attempted). Bypassing the whole branch below leaves the file exactly as it is: unstashed,
    # uncommitted, but already unblocking (`blocking_decisions()` reads the JSON content
    # directly, not git-committed state) — a future gad-kit commit will fold it in naturally.
    index_only_dirt = _is_index_only_owner_decision_dirt(repo, status_lines)
    if status_lines and not index_only_dirt:
        head = git_head(repo)
        baseline_head = cooldown.get_clean_baseline(state, repo)
        baseline_matches = baseline_head is not None and baseline_head == head
        # No longer takes a generation number at all (B10 fix removed the last clause that did) —
        # this is the gate that decides whether claude-relay may touch the tree AT ALL, and it is
        # satisfied only by an actual dirty path under `.gad/`, full stop. That keeps *whether* we
        # may `git stash` (this gate) independent of *which* generation we then recover
        # (`recovery_gen`, A5) — Invariant #6 requires the former never be broadened by the latter.
        gad_signal = _has_gad_dirty_signal(status_lines)

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

        # Both recovery tails MUST carry the generation's declared genType — dropping it silently
        # downgrades a research generation to `build` inside gad-generation.js/gad-finish.js
        # (see `backlog_gen_type()`), which swaps the planner/implement/guardrail roles and drops
        # the research verify/fix notes. gad-run threads it per-generation; we are the only other
        # caller of those two scripts.
        gen_type = backlog_gen_type(repo, recovery_gen)
        census = artifact_census(repo, recovery_gen)
        if census["verify_or_later"]:
            return Plan(
                kind="FINISH",
                repo=repo,
                gen=recovery_gen,
                mode="gad_finish",
                tier=config.gadkit_tier,
                token_target=config.token_target,
                gen_type=gen_type,
                detail=(
                    f"{generation_dir(repo, recovery_gen)}/reviews/verification.md AND "
                    "reviews/adversarial-review.md both present — Guardrails' hard-required "
                    "agent succeeded AND the adversarial-review/skeptic-vet artifact gad-finish "
                    "itself now mechanically requires (else it returns REFUSED) is on disk; "
                    "safe to resume via /gad-finish"
                ),
            )
        if dry_run:
            return Plan(
                kind="RUN",
                repo=repo,
                gen=recovery_gen,
                mode="gad_generation",
                tier=config.gadkit_tier,
                token_target=config.token_target,
                gen_type=gen_type,
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
            repo, f"claude-relay: parking gen{recovery_gen} before safe restart {_now_iso()}"
        )
        if stash_ref is not None:
            cooldown.record_stash(state, repo, stash_ref, dirty_paths)
        return Plan(
            kind="RUN",
            repo=repo,
            gen=recovery_gen,
            mode="gad_generation",
            tier=config.gadkit_tier,
            token_target=config.token_target,
            gen_type=gen_type,
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
    head = git_head(repo)
    cooldown.set_clean_baseline(state, repo, head)

    if not pending:
        return _exhausted_backlog_plan(
            repo, config, state, next_gen=next_gen, head=head, dry_run=dry_run
        )

    return Plan(
        kind="RUN",
        repo=repo,
        gen=pending[0],
        mode="gad_run",
        tier=config.gadkit_tier,
        token_target=config.token_target,
        detail=f"{len(pending)} pending generation(s) {pending}; clean tree at a committed boundary",
    )


def _exhausted_backlog_plan(
    repo: Path,
    config: Config,
    state: dict[str, Any],
    *,
    next_gen: int,
    head: str | None,
    dry_run: bool,
) -> Plan:
    """Step 5: what an exhausted backlog on a clean tree means.

    For a classic build repo it means DONE, as it always has. For a RESEARCH repo it does NOT:
    gad-run's auto-ideation (`AUTO_IDEATE` defaults ON, gad-run.js:98) fires precisely in the
    backlog-exhausted branch (gad-run.js:263-276) — it runs one `genType: 'ideation'` generation
    whose consolidator appends freshly-vetted hypotheses to `.gad/backlog.md`, and the next crawl
    picks them up. That is gad-kit's "a multi-day research crawl never starves" guarantee, and
    claude-relay used to make it unreachable by returning DONE (which loop.run() treats as
    terminal, exit 0) in the one state where the refill would have fired. So hand the decision to
    gad-run instead of terminating.

    Bounded so it can never livelock: the attempt is recorded against the CURRENT git HEAD. A
    successful ideation commits, which moves HEAD, so the next exhaustion legitimately earns a
    fresh attempt; a failed one leaves HEAD where it was and the next triage returns DONE with a
    detail saying the refill was tried and did not land. (`head is None` — a repo with no commits
    at all — cannot be bounded this way, so it degrades to plain DONE rather than loop.)

    A `dry_run=True` triage deliberately does NOT book the attempt, even in memory. `loop.py`'s
    `_park_and_wait()` re-triages a parked repo with `dry_run=True` but the REAL `state` object,
    so booking there would consume the repo's only refill during a mere re-check and the
    `dry_run=False` triage that follows would immediately return DONE — the refill would never
    run. Only the triage that can actually lead to a spend books the attempt (and `loop.run_once()`
    hands it back via `cooldown.clear_ideation_attempt_head()` if no seat was available to spend
    it on).
    """
    if not is_research_repo(repo):
        return Plan(
            kind="DONE",
            repo=repo,
            gen=next_gen,
            detail="backlog exhausted — no pending generations declared beyond what is committed",
        )
    attempted_at = cooldown.get_ideation_attempt_head(state, repo)
    if head is None or attempted_at == head:
        return Plan(
            kind="DONE",
            repo=repo,
            gen=next_gen,
            detail=(
                "backlog exhausted in a research repo, and an auto-ideation refill was already "
                f"attempted at this HEAD ({head}) without landing a commit — stopping rather than "
                "re-spending on a refill that just failed; triage the last run's log, then re-run"
            ),
        )
    if not dry_run:
        cooldown.set_ideation_attempt_head(state, repo, head)
    return Plan(
        kind="RUN",
        repo=repo,
        gen=next_gen,
        mode="gad_run",
        tier=config.gadkit_tier,
        token_target=config.token_target,
        # A6 audit fix: mark this as THE ideation-refill plan (see the `ideation_refill` field
        # docstring) so `loop.run_once()`'s no-seat early return can gate
        # `cooldown.clear_ideation_attempt_head()` on it — giving the booking back only when
        # THIS plan is the one that made it, never for an unrelated RUN/FINISH plan.
        ideation_refill=True,
        detail=(
            "backlog exhausted in a RESEARCH repo ('gad-mode: research' in .gad/backlog.md) — "
            "handing this to /gad-run so its auto-ideation can refill the hypothesis queue "
            "instead of terminating; one attempt per HEAD, so a refill that fails to commit "
            "ends the crawl on the next triage"
        ),
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

    # gad-kit 2.0's `genType`, omitted entirely when None. Both recovery scripts coerce an
    # absent/unknown value to 'build' with no error path (gad-generation.js:124,
    # gad-finish.js:65), so passing `genType: null` would be indistinguishable from omitting it —
    # but omitting it is what gad-run itself does (gad-run.js:350 spreads the key only when the
    # entry declares a non-'build' type), and keeping the args byte-identical to gad-run's own is
    # the point. NOT threaded into `gad_run` mode: gad-run derives genType per-generation from each
    # backlog entry, so a single value there would mislabel every other generation in the crawl.
    gen_type_arg: dict[str, Any] = {} if plan.gen_type is None else {"genType": plan.gen_type}

    if plan.mode == "gad_finish":
        script = str(root / "workflows" / "gad-finish.js")
        wf_args: dict[str, Any] = {
            "repo": repo_abs,
            "gen": plan.gen,
            "rolesDir": roles_dir,
            "profile": plan.tier,
            **gen_type_arg,
        }
    elif plan.mode == "gad_generation":
        script = str(root / "workflows" / "gad-generation.js")
        wf_args = {
            "repo": repo_abs,
            "gen": plan.gen,
            "rolesDir": roles_dir,
            "profile": plan.tier,
            **gen_type_arg,
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
        # TOKEN_TARGET (e.g. "+2M"): DOCS-ONLY B22 audit fix (2026-07-26) — this used to be
        # documented (here and in README.md/DESIGN.md/config.example.toml) as "gad-run.js self-
        # paces to this turn's token target (its `budget.total`)". Verified LIVE against the
        # installed `claude` 2.1.220 bundle: `budget.total`'s only writer
        # (`snapshotOutputTokensForTurn`) has NO call site anywhere in the bundle — `budget.total`
        # is always `null` regardless of what this prompt text says. The directive below is still
        # appended (harmless prompt text a model may or may not act on, and removing it is a
        # separate decision this fix does not make), but it is NOT a working pacing mechanism for
        # claude-relay's own `--max 1` crawl step, and must not be documented as one. Consequence
        # is limited for claude-relay specifically because `maxGens=1` (above) already supplies
        # the real turn boundary; it would matter more for a caller using `maxGensPerTick > 1`.
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
    census: dict[str, Any] = {}
    if next_gen is not None:
        census = artifact_census(repo, next_gen)
    # A8 audit fix: `handoff_exists` used to be keyed on `next_gen` alone
    # (`handoff_path(repo, next_gen).exists()`), so `outcome()`'s BLOCKED detection (a handoff
    # newly appearing between the pre/post snapshot) silently missed a consolidator that parked
    # an OUT-OF-ORDER generation — gad-kit 2.0's priority-sorted crawl (gad-run.js:283) routinely
    # interrupts a generation whose number differs from `index.nextGen` (see
    # `in_flight_generation()`'s docstring). `find_uncommitted_handoff()` already scans every
    # not-yet-committed generation directory for a handoff.md, independent of which number
    # `nextGen` happens to name right now — exactly "did a consolidator park ANY in-progress
    # generation." (`census` above stays keyed on `next_gen` deliberately: `artifact_census()`'s
    # own docstring says it is diagnostic-only, and `outcome()` never reads it.)
    handoff_exists = find_uncommitted_handoff(repo, index, None) is not None
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
    # B13 audit fix, second round (blocker: the commit's return code was never checked, so a
    # rejecting pre-commit hook or an unset git identity silently left the write uncommitted
    # while `found=True` was returned unconditionally — indistinguishable from genuine success).
    # `True` only when the resolution's own commit is confirmed to have landed; `False` means
    # the JSON write on disk succeeded (the decision IS resolved, and `blocking_decisions()`
    # already sees it) but the commit did not — callers MUST surface this distinctly, never
    # report it as an ordinary clean resolution.
    committed: bool = True


def resolve_owner_decision(repo: Path, decision_id: str, answer: str) -> ResolveResult:
    """Atomically mark the matching `ownerDecisions[]` entry (wherever it lives in
    generations-index.json) as answered: `status -> "answered"`, plus a `resolution` note and
    `resolvedAt` timestamp. This edits DURABLE disk state — not a chat reply — which is what
    actually unblocks a parked AWAITING_HUMAN repo and survives seat rotation (DESIGN.md §7).
    Both `claude-relay resolve` and the Telegram poller call this SAME function.

    `"answered"` is the ONLY unblocking token, and writing anything else livelocked the
    supervisor (2026-07-26 audit; this used to write `"resolved"`). gad-kit's open-decision
    predicate is not code — it is a prompt string an agent judges, at two sites:
    gad-generation.js:443 ('any decision has status "open" (i.e. not "answered")') and
    gad-run.js:220 ('any decision has status "open"/not "answered"'). A status of `"resolved"`
    is neither `"open"` nor `"answered"`, so gad-kit's preflight could non-deterministically
    still park with AWAITING-OWNER while claude-relay's own deterministic check
    (`blocking_decisions()`, which only treats a literal `"open"` as blocking) considered the
    repo unblocked: claude-relay un-parked and spawned a run, gad-kit halted writing nothing to
    disk, `outcome()` returned AGENT_DEAD_NONLIMIT -> RETRY, three times -> HARD_ERROR exit.
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
                    # B14 audit fix: only an OPEN decision may be (re-)answered. Without this,
                    # a STALE `resolve <id> <answer>` replayed from Telegram's retained history
                    # (the exact consequence of `load_state()` silently reinterpreting a torn
                    # `state.json` as fresh-empty, resetting `telegramUpdateOffset` to 0) could
                    # silently overwrite an ALREADY-answered decision's real resolution with old,
                    # wrong content. Matching on id alone had no such guard. `found=False` here
                    # reuses the existing "no open ownerDecision with id=... found" message,
                    # which is now literally accurate rather than merely a fallback string.
                    if (
                        isinstance(d, dict)
                        and str(d.get("id")) == str(decision_id)
                        and d.get("status") == _OPEN_STATUS
                    ):
                        d["status"] = _ANSWERED_STATUS
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

    # B13 audit fix (2 independent confirmations, 3 reproductions): leaving this write
    # uncommitted meant the NEXT `triage()` saw a dirty `generations-index.json` and — depending
    # on whether HEAD had moved since the last recorded clean baseline — either permanently
    # parked the repo as AWAITING_HUMAN (HEAD moved: `baseline_matches` false forever) or, WORSE,
    # swept the resolution itself into a `git stash` as part of an unrelated dirty-tree recovery
    # (HEAD unmoved: `baseline_matches` true), reverting the decision back to `"open"` — defeating
    # the entire resolve-in mechanism one layer up. Fix: commit JUST this one file immediately.
    # `-- <path>` scopes the commit to ONLY the changes already staged for this path, so any
    # other unrelated dirt already sitting in the tree (e.g. a genuinely in-progress generation)
    # is left untouched — resolve-in must be durable the instant it returns, not merely "durable
    # until the next triage." Best-effort: `repo` not being a git repository at all (or `git`
    # itself being unavailable) must not make `resolve` itself fail — the JSON write already
    # succeeded and is the actual source of truth `blocking_decisions()` reads.
    #
    # Second-round blocker fix (adversarial review, 2026-07-26): the ORIGINAL fix above checked
    # `add_result.returncode` but never the COMMIT's own return code — so a rejecting pre-commit
    # hook, or a repo with no git identity configured (`user.name`/`user.email` unset — an
    # ordinary operator condition, not exotic), left the write uncommitted while this function
    # still returned `found=True` unconditionally, EXACTLY the outcome the docstring above says
    # this mechanism prevents, just reached via a commit-time failure instead of an absent
    # commit. Reproduced live (adversarial review): (1) a rejecting hook, (2) no git identity —
    # both leave `generations-index.json` staged-but-uncommitted; the next `triage()` then either
    # parks forever (HEAD moved) or, if HEAD is unchanged, sweeps the resolution into a `git
    # stash` and reverts the decision to `"open"` — silently destroying the operator's answer.
    # Fix, defense in depth:
    #   (a) `committed` on the returned result distinguishes this outcome so callers can never
    #       mistake it for an ordinary clean resolution (see cli.py/notify.py's Telegram reply).
    #   (b) the JSON write itself already succeeded — `blocking_decisions()` is unblocked
    #       immediately regardless of the commit outcome, so a stalled commit never re-blocks a
    #       resolved decision.
    #   (c) `triage()`'s own dirty-tree stash gate independently exempts a lone, uncommitted
    #       `ownerDecisions[].status` diff on `generations-index.json` (see
    #       `_is_index_only_owner_decision_dirt()`) — so even if THIS commit attempt fails here
    #       AND every later automatic retry (a permanently unset identity) also fails, the
    #       resolution can never be swept into a stash. That exemption is what makes (a)/(b)
    #       actually sufficient rather than merely best-effort.
    add_result = _git(repo, "add", "--", str(path))
    committed = False
    if add_result.returncode == 0:
        commit_result = _git(
            repo,
            "commit",
            "-m",
            f"claude-relay: resolve ownerDecision {decision_id}",
            "--",
            str(path),
        )
        committed = commit_result.returncode == 0

    return ResolveResult(found=True, decision=found_decision, index_path=path, committed=committed)


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
