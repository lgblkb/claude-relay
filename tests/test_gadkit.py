"""Offline tests for relay.gadkit: the triage() decision order against fixture `.gad/` repo
states (the feasibility-critical recovery-routing logic), command() argv construction,
outcome() bucketing, and resolve_owner_decision(). Uses real local `git` (no network) against
throwaway temp repos.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay import cooldown, detector, gadkit
from relay.config import Config


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("placeholder\n", encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "initial commit")


def _write_index(repo: Path, index: dict) -> None:
    gad_dir = repo / ".gad"
    gad_dir.mkdir(parents=True, exist_ok=True)
    (gad_dir / "generations-index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")


def _write_backlog(repo: Path, gens: list[tuple[int, str]]) -> None:
    lines = ["# Test Project — GAD Backlog", ""]
    for gen, title in gens:
        lines.append(f"## G{gen} — {title}")
        lines.append("- **Goal**: test.")
        lines.append("")
    (repo / ".gad").mkdir(parents=True, exist_ok=True)
    (repo / ".gad" / "backlog.md").write_text("\n".join(lines), encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", message)


def _seed_committed_gad_state(repo: Path, index: dict, gens: list[tuple[int, str]]) -> None:
    """Write generations-index.json + backlog.md and COMMIT them — realistic baseline: gad-kit
    commits this state as part of each generation's consolidation, so a genuinely "clean tree"
    fixture must have it committed too, not just present on disk.
    """
    _write_index(repo, index)
    _write_backlog(repo, gens)
    _commit_all(repo, "seed .gad state")


def _config() -> Config:
    return Config(gadkit_tier="budget", token_target="+2M")


def _fresh_state() -> dict:
    return cooldown.load_state(Path("/nonexistent-claude-relay-state.json"))


# ─────────────────────────────────────────────────────────────────────────────
# Free-form-backlog fixtures: `_write_backlog()` above can only express `## G<n>` + Goal, but
# the `Type:` / `gad-mode: research` readers parse hand-written markdown, so those tests need
# to control the file byte for byte.
# ─────────────────────────────────────────────────────────────────────────────

# gad-kit's own research template really does put a per-entry convention LEGEND above the first
# `## G` header (templates/backlog-research.md:15), and that legend contains a `- **Type**:` line.
# Every backlog fixture that tests `backlog_gen_type()` carries it, because an unbounded
# section search would report the legend's first value ("experiment") as G0's declared type.
_RESEARCH_LEGEND = (
    "# Fixture Project — GAD Research Backlog\n"
    "\n"
    "<!-- gad-mode: research -->\n"
    "\n"
    "Convention per entry:\n"
    "- **Type**: `experiment` | `eda` | `ideation` | `build`\n"
    "- **Priority**: 0-100\n"
    "\n"
    "---\n"
    "\n"
)


def _write_raw_backlog(repo: Path, text: str) -> None:
    (repo / ".gad").mkdir(parents=True, exist_ok=True)
    (repo / ".gad" / "backlog.md").write_text(text, encoding="utf-8")


def _stash_list(repo: Path) -> str:
    return subprocess.run(
        ["git", "stash", "list"], cwd=str(repo), capture_output=True, text=True
    ).stdout


def _porcelain(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True
    ).stdout


def _make_generation_dir(repo: Path, gen: int, *relative_files: str) -> Path:
    """Create `.gad/generation-<gen>/` plus the given files (relative to it), e.g.
    `_make_generation_dir(repo, 7, "plan.md", "reviews/verification.md")`.
    """
    gen_dir = gadkit.generation_dir(repo, gen)
    gen_dir.mkdir(parents=True, exist_ok=True)
    for relative in relative_files:
        path = gen_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    return gen_dir


class TriageDecisionOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.addCleanup(self._tmp.cleanup)
        self.state = _fresh_state()
        _init_repo(self.repo)

    def _prime_clean_baseline(self) -> None:
        """Establish `state`'s clean-baseline HEAD by triaging once while the tree is still
        clean — required before any dirty-tree scenario in these tests, since v1's recovery
        gate now requires (finding #5c) that the dirtiness be attributable to a HEAD
        claude-relay itself last observed clean (Invariant #6/#9).
        """
        gadkit.triage(self.repo, _config(), self.state)

    def test_not_bootstrapped_is_awaiting_human(self) -> None:
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "AWAITING_HUMAN")
        self.assertIn("gad-init", plan.detail)

    def test_gated_open_owner_decision_blocks(self) -> None:
        _seed_committed_gad_state(
            self.repo,
            {
                "project": "test",
                "nextGen": 2,
                "generations": [{"gen": 0}, {"gen": 1}],
                "ownerDecisions": [{"id": "D1", "question": "pick a DB", "blocksGen": 2, "status": "open"}],
            },
            [(0, "a"), (1, "b"), (2, "c")],
        )
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "AWAITING_HUMAN")
        self.assertEqual(plan.gen, 2)
        self.assertIn("D1", plan.detail)
        self.assertEqual(plan.blocking_decision_ids, ("D1",))

    def test_advisory_decision_without_blocks_gen_does_not_block(self) -> None:
        _seed_committed_gad_state(
            self.repo,
            {
                "project": "test",
                "nextGen": 2,
                "generations": [{"gen": 0}, {"gen": 1}],
                "ownerDecisions": [
                    {"id": "D2", "question": "advisory only", "status": "open"}  # no blocksGen
                ],
            },
            [(0, "a"), (1, "b"), (2, "c")],
        )
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "RUN")
        self.assertEqual(plan.mode, "gad_run")

    def test_answered_decision_no_longer_blocks(self) -> None:
        _seed_committed_gad_state(
            self.repo,
            {
                "project": "test",
                "nextGen": 2,
                "generations": [{"gen": 0}, {"gen": 1}],
                "ownerDecisions": [
                    # "answered" is what `resolve_owner_decision()` now writes, and the only token
                    # gad-kit's own preflight predicate accepts as unblocking.
                    {"id": "D1", "question": "pick a DB", "blocksGen": 2, "status": "answered"}
                ],
            },
            [(0, "a"), (1, "b"), (2, "c")],
        )
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "RUN")

    def test_handoff_present_is_blocked(self) -> None:
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        gen_dir = self.repo / ".gad" / "generation-1"
        gen_dir.mkdir(parents=True)
        (gen_dir / "handoff.md").write_text("BLOCKED: needs owner input\n", encoding="utf-8")
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "BLOCKED")
        self.assertEqual(plan.gen, 1)

    def test_dirty_with_no_baseline_at_all_is_awaiting_human_and_untouched(self) -> None:
        """No prior clean-triage call ever happened (fresh state.json) — even a dirty tree
        WITH a recognizable .gad/generation-N/ signal must not be auto-recovered, because
        claude-relay cannot yet prove this dirtiness is its own (finding #5c).
        """
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        # NOTE: deliberately no self._prime_clean_baseline() call here.
        gen_dir = self.repo / ".gad" / "generation-1"
        gen_dir.mkdir(parents=True)
        (gen_dir / "plan.md").write_text("# plan\n", encoding="utf-8")
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "AWAITING_HUMAN")
        self.assertIn("no baseline", plan.detail)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(self.repo), capture_output=True, text=True
        )
        self.assertNotEqual(status.stdout.strip(), "")  # untouched — nothing was stashed

    def test_dirty_unrecognized_tree_is_awaiting_human_and_untouched(self) -> None:
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        self._prime_clean_baseline()
        # Dirty the (otherwise clean, committed, baseline-matching) tree with something that
        # has NOTHING to do with gad-kit's own output.
        (self.repo / "scratch.txt").write_text("someone's unrelated WIP\n", encoding="utf-8")
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "AWAITING_HUMAN")
        self.assertIn("Invariant #6", plan.detail)
        self.assertIn("no generation-N/", plan.detail)
        # Invariant #6: must NOT have touched the working tree (no stash was created).
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(self.repo), capture_output=True, text=True
        )
        self.assertIn("scratch.txt", status.stdout)

    def test_dirty_recognized_incomplete_restarts_via_gad_generation_and_stashes(self) -> None:
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        self._prime_clean_baseline()
        gen_dir = self.repo / ".gad" / "generation-1"
        gen_dir.mkdir(parents=True)
        (gen_dir / "plan.md").write_text("# plan\n", encoding="utf-8")  # Plan done, nothing else
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "RUN")
        self.assertEqual(plan.mode, "gad_generation")
        self.assertEqual(plan.gen, 1)
        self.assertIsNotNone(plan.stashed_ref)
        # Invariant #6: stash, not reset --hard — the tree should now be CLEAN (stashed away).
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(self.repo), capture_output=True, text=True
        )
        self.assertEqual(status.stdout.strip(), "")
        stash_list = subprocess.run(
            ["git", "stash", "list"], cwd=str(self.repo), capture_output=True, text=True
        )
        self.assertIn("parking gen1", stash_list.stdout)
        # The stash ref + swept file list must be persisted into state (finding #5b).
        recorded = cooldown.get_last_stash(self.state, self.repo)
        assert recorded is not None
        self.assertEqual(recorded["ref"], plan.stashed_ref)
        # git reports a wholly-new directory as one untracked line, not per-file — assert the
        # generation-1 scaffold is represented in the swept path list either way.
        self.assertTrue(any("generation-1" in f for f in recorded["files"]), recorded["files"])

    def test_dry_run_never_stashes(self) -> None:
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        self._prime_clean_baseline()
        gen_dir = self.repo / ".gad" / "generation-1"
        gen_dir.mkdir(parents=True)
        (gen_dir / "plan.md").write_text("# plan\n", encoding="utf-8")
        plan = gadkit.triage(self.repo, _config(), self.state, dry_run=True)
        self.assertEqual(plan.kind, "RUN")
        self.assertEqual(plan.mode, "gad_generation")
        self.assertIsNone(plan.stashed_ref)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(self.repo), capture_output=True, text=True
        )
        self.assertNotEqual(status.stdout.strip(), "")  # still dirty — nothing was stashed

    def test_finish_routing_false_positive_regression(self) -> None:
        """Regression test for the feasibility-Critical FINISH-routing bug both reviewers
        reproduced: a Prep-phase fixture written under `tests/` PLUS a coincidentally
        `test_`-named file at the repo root must NOT be mistaken for proof that Guardrails
        ran. With no `reviews/verification.md`, this MUST default to a full `/gad-generation`
        restart — never FINISH — even though `implementation-log.md` is present.
        """
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        self._prime_clean_baseline()
        gen_dir = self.repo / ".gad" / "generation-1"
        gen_dir.mkdir(parents=True)
        (gen_dir / "plan.md").write_text("# plan\n", encoding="utf-8")
        (gen_dir / "implementation-log.md").write_text("did the thing\n", encoding="utf-8")
        # (a) a Prep-phase fixture under tests/, written BEFORE Guardrails ever runs.
        tests_dir = self.repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "fixtures.py").write_text("FIXTURE = 1\n", encoding="utf-8")
        # (b) a coincidentally test_-named file anywhere in the repo.
        (self.repo / "test_repro.py").write_text("def test_repro(): assert True\n", encoding="utf-8")
        # Deliberately NO reviews/verification.md — Guardrails/Verify never actually ran.
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "RUN")
        self.assertEqual(plan.mode, "gad_generation")
        self.assertIsNotNone(plan.stashed_ref)

    def test_verify_artifact_present_finishes(self) -> None:
        """The two genuine Verify-or-later artifacts (reviews/verification.md — written only by
        the Verify-phase verifier, which can only run after Guardrails' test-writer succeeded —
        AND reviews/adversarial-review.md, required since the REFUSED-status fix, 2026-07-26)
        both being present is both necessary AND sufficient to route to /gad-finish.
        """
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        self._prime_clean_baseline()
        gen_dir = self.repo / ".gad" / "generation-1"
        (gen_dir / "reviews").mkdir(parents=True)
        (gen_dir / "plan.md").write_text("# plan\n", encoding="utf-8")
        (gen_dir / "implementation-log.md").write_text("did the thing\n", encoding="utf-8")
        (gen_dir / "reviews" / "verification.md").write_text("GATE: RED (iter 1)\n", encoding="utf-8")
        (gen_dir / "reviews" / "adversarial-review.md").write_text("verdict: APPROVED\n", encoding="utf-8")
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "FINISH")
        self.assertEqual(plan.mode, "gad_finish")
        self.assertEqual(plan.gen, 1)

    def test_verification_without_adversarial_review_does_not_finish(self) -> None:
        """REFUSED-status regression fix (gad-kit's uncommitted 2.1.0 work): gad-generation.js's
        Guardrails phase treats the adversarial-reviewer/results-skeptic as ADVISORY for
        non-ideation genTypes — a generation can legitimately have verification.md with NO
        adversarial-review.md (the reviewer died after retries, Verify ran anyway). Routing that
        to /gad-finish is unsafe: gad-finish.js's own Verify prompt now MECHANICALLY REFUSES
        (status: 'REFUSED', writes nothing) exactly this case — so `triage()` must default to a
        full /gad-generation restart instead, never FINISH, for this artifact combination.
        """
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        self._prime_clean_baseline()
        gen_dir = self.repo / ".gad" / "generation-1"
        (gen_dir / "reviews").mkdir(parents=True)
        (gen_dir / "plan.md").write_text("# plan\n", encoding="utf-8")
        (gen_dir / "implementation-log.md").write_text("did the thing\n", encoding="utf-8")
        (gen_dir / "reviews" / "verification.md").write_text("GATE: RED (iter 1)\n", encoding="utf-8")
        # Deliberately NO reviews/adversarial-review.md.
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "RUN")
        self.assertEqual(plan.mode, "gad_generation")
        self.assertIsNotNone(plan.stashed_ref)

    def test_clean_tree_with_pending_generation_runs_gad_run(self) -> None:
        _seed_committed_gad_state(
            self.repo,
            {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]},
            [(0, "a"), (1, "b"), (2, "c")],
        )
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "RUN")
        self.assertEqual(plan.mode, "gad_run")
        self.assertEqual(plan.gen, 1)  # first PENDING gen, not necessarily nextGen

    def test_clean_tree_backlog_exhausted_is_done(self) -> None:
        _seed_committed_gad_state(
            self.repo,
            {"project": "test", "nextGen": 2, "generations": [{"gen": 0}, {"gen": 1}]},
            [(0, "a"), (1, "b")],
        )
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "DONE")

    def test_clean_tree_records_a_baseline_for_future_dirty_detection(self) -> None:
        _seed_committed_gad_state(
            self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]}, [(0, "a"), (1, "b")]
        )
        self.assertIsNone(cooldown.get_clean_baseline(self.state, self.repo))
        gadkit.triage(self.repo, _config(), self.state)
        self.assertIsNotNone(cooldown.get_clean_baseline(self.state, self.repo))


class CommandArgvTests(unittest.TestCase):
    # A fixed fake plugin root so these tests are hermetic — they do not require gad-kit to be
    # installed on the machine running them, and can assert the exact bundled-script paths.
    FAKE_ROOT = Path("/plugins/gad-kit/1.5.0")

    def _command(self, plan: gadkit.Plan) -> list[str]:
        with mock.patch.object(gadkit, "gadkit_plugin_root", return_value=self.FAKE_ROOT):
            return gadkit.command(plan)

    def _workflow_args(self, prompt: str) -> dict:
        """Extract and json.loads the object the prompt hands the model as Workflow `args`. Also
        the regression guard for gen 0's root cause: the model there passed `args` as a JSON
        *string* at the wrong plugin version — so this asserts the prompt presents a real OBJECT
        that round-trips through json.loads.
        """
        start = prompt.index("args: ") + len("args: ")
        end = prompt.index("\n})", start)
        return json.loads(prompt[start:end])

    def test_gad_run_includes_token_target_and_profile(self) -> None:
        plan = gadkit.Plan(
            kind="RUN",
            repo=Path("/abs/repo"),
            gen=3,
            mode="gad_run",
            tier="budget",
            token_target="+2M",
        )
        argv = self._command(plan)
        self.assertEqual(argv[0], "-p")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("--verbose", argv)  # stream-json in -p mode requires --verbose (live-verified)
        prompt = argv[-1]
        # A direct Workflow-tool call at the exact bundled script — NOT a bare/paraphrased slash
        # (a raw `/gad-run` is rejected in -p mode, and paraphrasing one made gen 0 guess wrong).
        self.assertIn("Workflow tool", prompt)
        self.assertIn(str(self.FAKE_ROOT / "workflows" / "gad-run.js"), prompt)
        self.assertNotIn("/gad-run /abs/repo", prompt)
        self.assertFalse(prompt.lstrip().startswith("/gad-run"))
        args = self._workflow_args(prompt)
        self.assertEqual(args["repo"], "/abs/repo")
        self.assertEqual(args["maxGens"], 1)
        self.assertEqual(args["profile"], "budget")
        self.assertIn("gad-generation.js", args["generationScript"])
        self.assertIn("gad-finish.js", args["finishScript"])
        self.assertIn("+2M", prompt)  # token-budget directive (gad_run crawl step only)
        # The blocking discipline that keeps `claude -p` alive until the async workflow commits.
        self.assertIn("TaskOutput", prompt)
        self.assertIn("block: true", prompt)
        self.assertIn("MUST NOT end your turn", prompt)

    def test_gad_generation_restart_has_no_token_target(self) -> None:
        plan = gadkit.Plan(
            kind="RUN",
            repo=Path("/abs/repo"),
            gen=3,
            mode="gad_generation",
            tier="balanced",
            token_target="+2M",
        )
        prompt = self._command(plan)[-1]
        self.assertIn(str(self.FAKE_ROOT / "workflows" / "gad-generation.js"), prompt)
        args = self._workflow_args(prompt)
        self.assertEqual(args["gen"], 3)
        self.assertEqual(args["profile"], "balanced")
        self.assertNotIn("maxGens", args)  # a single-gen restart, not the crawl driver
        self.assertNotIn("+2M", prompt)  # recovery tails carry no token-budget directive

    def test_gad_finish_prompt(self) -> None:
        plan = gadkit.Plan(kind="FINISH", repo=Path("/abs/repo"), gen=3, mode="gad_finish", tier="budget")
        prompt = self._command(plan)[-1]
        self.assertIn(str(self.FAKE_ROOT / "workflows" / "gad-finish.js"), prompt)
        args = self._workflow_args(prompt)
        self.assertEqual(args["gen"], 3)
        self.assertEqual(args["profile"], "budget")
        self.assertNotIn("+2M", prompt)

    def test_command_rejects_non_run_finish_plans(self) -> None:
        plan = gadkit.Plan(kind="DONE", repo=Path("/abs/repo"))
        with self.assertRaises(ValueError):
            gadkit.command(plan)

    def test_gen_type_is_threaded_into_both_recovery_modes(self) -> None:
        """Both recovery tails MUST carry the generation's declared gad-kit 2.0 genType: dropping
        it silently reruns a research generation as a `build` one (different planner/implement/
        guardrail roles, and the research verify/fix notes emptied).
        """
        for mode, kind in (("gad_finish", "FINISH"), ("gad_generation", "RUN")):
            for gen_type in ("experiment", "eda", "ideation"):
                with self.subTest(mode=mode, gen_type=gen_type):
                    plan = gadkit.Plan(
                        kind=kind, repo=Path("/abs/repo"), gen=4, mode=mode, gen_type=gen_type
                    )
                    args = self._workflow_args(self._command(plan)[-1])
                    self.assertEqual(args["genType"], gen_type)
                    # the pre-existing args must be unaffected by the new key
                    self.assertEqual(args["gen"], 4)
                    self.assertEqual(args["repo"], "/abs/repo")
                    self.assertIn("agents", args["rolesDir"])

    def test_gen_type_key_is_omitted_entirely_when_none(self) -> None:
        """None means "the workflow default, `build`" — and gad-run itself OMITS the key rather
        than passing `genType: null` (gad-run.js:350 spreads it only for a non-'build' type), so a
        recovery invocation must be byte-identical to gad-run's own.
        """
        for mode, kind in (("gad_finish", "FINISH"), ("gad_generation", "RUN")):
            with self.subTest(mode=mode):
                plan = gadkit.Plan(kind=kind, repo=Path("/abs/repo"), gen=4, mode=mode, gen_type=None)
                args = self._workflow_args(self._command(plan)[-1])
                self.assertNotIn("genType", args)

    def test_gad_run_mode_never_carries_gen_type_even_if_the_plan_sets_one(self) -> None:
        """gad-run derives genType per-generation from each backlog entry, so a single value
        threaded into the crawl driver would mislabel every OTHER generation it runs. Asserted
        against a plan that deliberately sets `gen_type`, so a future caller cannot leak it here.
        """
        plan = gadkit.Plan(
            kind="RUN", repo=Path("/abs/repo"), gen=3, mode="gad_run", gen_type="experiment"
        )
        args = self._workflow_args(self._command(plan)[-1])
        self.assertNotIn("genType", args)
        self.assertEqual(args["maxGens"], 1)  # still the crawl driver in every other respect

    def test_plan_no_longer_carries_an_extra_flags_field(self) -> None:
        """`Plan.extra_flags`/`[gadkit].extra_flags` was dead config — populated by triage() and
        never read by command(). It is gone; this pins that it does not come back by accident (it
        could not have worked as advertised anyway: command() builds a JSON args OBJECT, so
        "--milestone"-style CLI strings had nowhere to go).
        """
        field_names = {f.name for f in dataclasses.fields(gadkit.Plan)}
        self.assertNotIn("extra_flags", field_names)
        self.assertFalse(hasattr(gadkit.Plan(kind="DONE", repo=Path("/abs/repo")), "extra_flags"))


class SnapshotFunctionTests(unittest.TestCase):
    """`gadkit.snapshot()`: the real disk-reading function `run_once()` calls before/after a run
    (as opposed to `OutcomeTests` below, which builds `Snapshot` dataclasses by hand).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)

    def test_handoff_exists_is_true_for_an_out_of_order_generation_not_next_gen(self) -> None:
        """A8 audit fix: `handoff_exists` used to be keyed on `nextGen` alone, so
        `outcome()`'s BLOCKED detection would silently miss a consolidator parking an
        OUT-OF-ORDER generation (gad-kit 2.0's priority-sorted crawl routinely interrupts a
        generation whose number differs from `index.nextGen`).
        """
        _write_index(self.repo, {"project": "test", "nextGen": 3, "generations": [{"gen": 0}]})
        _make_generation_dir(self.repo, 7, "handoff.md")  # NOT next_gen (3)
        snap = gadkit.snapshot(self.repo)
        self.assertEqual(snap.next_gen, 3)
        self.assertTrue(snap.handoff_exists)

    def test_handoff_under_a_completed_generation_does_not_count(self) -> None:
        _write_index(self.repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]})
        _make_generation_dir(self.repo, 0, "handoff.md")  # gen 0 IS completed
        snap = gadkit.snapshot(self.repo)
        self.assertFalse(snap.handoff_exists)


class OutcomeTests(unittest.TestCase):
    DEFAULT_CEILING = 70.0

    def _snapshot(self, **overrides) -> gadkit.Snapshot:
        base = dict(
            head="abc123",
            next_gen=3,
            handoff_exists=False,
            open_decision_ids=frozenset(),
            census={},
            backlog_exhausted=False,
        )
        base.update(overrides)
        return gadkit.Snapshot(**base)

    def _usage_at(self, percent: float) -> object:
        from relay import usage as usage_mod

        return usage_mod.UsageSnapshot.from_json(
            {"limits": [{"kind": "session", "percent": percent, "severity": "normal", "is_active": True}]},
            fetched_at=0.0,
        )

    def test_new_commit_is_progressed(self) -> None:
        pre = self._snapshot(head="aaa")
        post = self._snapshot(head="bbb")
        self.assertEqual(gadkit.outcome(pre, post, None, self.DEFAULT_CEILING), "PROGRESSED")

    def test_new_open_decision_is_awaiting_human(self) -> None:
        pre = self._snapshot(head="aaa", open_decision_ids=frozenset())
        post = self._snapshot(head="aaa", open_decision_ids=frozenset({"D1"}))
        self.assertEqual(gadkit.outcome(pre, post, None, self.DEFAULT_CEILING), "AWAITING_HUMAN")

    def test_new_handoff_is_blocked(self) -> None:
        pre = self._snapshot(head="aaa", handoff_exists=False)
        post = self._snapshot(head="aaa", handoff_exists=True)
        self.assertEqual(gadkit.outcome(pre, post, None, self.DEFAULT_CEILING), "BLOCKED")

    def test_at_ceiling_usage_is_hit_wall(self) -> None:
        """The synthetic-ceiling fix (finding #10 / the 90-99% "dead zone"): a seat stalling
        right at ITS OWN configured ceiling (here 70%, nowhere near the old fixed 99% default)
        must classify HIT_WALL, not AGENT_DEAD_NONLIMIT.
        """
        pre = self._snapshot(head="aaa")
        post = self._snapshot(head="aaa")
        at_ceiling_usage = self._usage_at(72.0)
        self.assertEqual(gadkit.outcome(pre, post, at_ceiling_usage, ceiling_pct=70.0), "HIT_WALL")

    def test_below_ceiling_usage_is_not_hit_wall(self) -> None:
        pre = self._snapshot(head="aaa")
        post = self._snapshot(head="aaa")
        below_ceiling_usage = self._usage_at(50.0)
        self.assertEqual(
            gadkit.outcome(pre, post, below_ceiling_usage, ceiling_pct=70.0), "AGENT_DEAD_NONLIMIT"
        )

    def test_near_100_usage_is_hit_wall_regardless_of_ceiling(self) -> None:
        pre = self._snapshot(head="aaa")
        post = self._snapshot(head="aaa")
        near_cap_usage = self._usage_at(100.0)
        self.assertEqual(gadkit.outcome(pre, post, near_cap_usage, ceiling_pct=70.0), "HIT_WALL")

    def test_backlog_exhausted_without_progress_is_no_backlog(self) -> None:
        pre = self._snapshot(head="aaa", backlog_exhausted=False)
        post = self._snapshot(head="aaa", backlog_exhausted=True)
        self.assertEqual(gadkit.outcome(pre, post, None, self.DEFAULT_CEILING), "NO_BACKLOG")

    def test_no_progress_no_signal_is_agent_dead_nonlimit(self) -> None:
        pre = self._snapshot(head="aaa")
        post = self._snapshot(head="aaa")
        self.assertEqual(gadkit.outcome(pre, post, None, self.DEFAULT_CEILING), "AGENT_DEAD_NONLIMIT")


class ResolveOwnerDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.addCleanup(self._tmp.cleanup)
        _init_repo(self.repo)

    def test_resolve_marks_matching_decision(self) -> None:
        _write_index(
            self.repo,
            {
                "project": "test",
                "nextGen": 1,
                "generations": [],
                "ownerDecisions": [{"id": "D1", "question": "q", "blocksGen": 1, "status": "open"}],
            },
        )
        result = gadkit.resolve_owner_decision(self.repo, "D1", "use postgres")
        self.assertTrue(result.found)
        assert result.decision is not None
        # "answered" is gad-kit's ONLY unblocking token (gad-generation.js:443 / gad-run.js:220);
        # the old "resolved" satisfied neither branch of its LLM-judged predicate and livelocked.
        self.assertEqual(result.decision["status"], "answered")
        self.assertEqual(result.decision["resolution"], "use postgres")
        reloaded = gadkit.read_index(self.repo)
        assert reloaded is not None
        self.assertEqual(reloaded["ownerDecisions"][0]["status"], "answered")

    def test_resolve_unknown_id_reports_not_found(self) -> None:
        _write_index(self.repo, {"project": "test", "nextGen": 1, "generations": [], "ownerDecisions": []})
        result = gadkit.resolve_owner_decision(self.repo, "NOPE", "answer")
        self.assertFalse(result.found)

    def test_resolve_with_no_index_reports_not_found(self) -> None:
        result = gadkit.resolve_owner_decision(self.repo, "D1", "answer")
        self.assertFalse(result.found)

    def test_b14_resolving_an_already_answered_decision_does_not_overwrite_it(self) -> None:
        """B14 audit fix: a STALE, replayed `resolve <id> <answer>` (the exact consequence of a
        torn `state.json` resetting the Telegram update cursor to zero, per `cooldown.py`'s
        `_quarantine_corrupt_state()`) must not silently clobber an already-answered decision's
        real resolution with old content. Only an `open` decision may be (re-)answered."""
        _write_index(
            self.repo,
            {
                "project": "test",
                "nextGen": 1,
                "generations": [],
                "ownerDecisions": [{"id": "D1", "question": "q", "blocksGen": 1, "status": "open"}],
            },
        )
        first = gadkit.resolve_owner_decision(self.repo, "D1", "use postgres")
        self.assertTrue(first.found)

        replayed = gadkit.resolve_owner_decision(self.repo, "D1", "STALE: use sqlite instead")
        self.assertFalse(replayed.found)  # reuses "no OPEN ownerDecision" — now literally true

        reloaded = gadkit.read_index(self.repo)
        assert reloaded is not None
        self.assertEqual(reloaded["ownerDecisions"][0]["resolution"], "use postgres")
        self.assertEqual(reloaded["ownerDecisions"][0]["status"], "answered")

    def test_answered_status_is_not_blocking_for_the_deterministic_predicates(self) -> None:
        """The status `resolve` writes must be one claude-relay's OWN preflight readers treat as
        unblocking — otherwise the un-park it performs is not real. `blocking_decisions()` and
        `open_owner_decisions()` are the two readers the loop consults.
        """
        _write_index(
            self.repo,
            {
                "project": "test",
                "nextGen": 2,
                "generations": [
                    {
                        "gen": 1,
                        "ownerDecisions": [
                            {"id": "D9", "question": "q", "blocksGen": 2, "status": "open"}
                        ],
                    }
                ],
            },
        )
        index_before = gadkit.read_index(self.repo)
        assert index_before is not None
        self.assertEqual(
            [d["id"] for d in gadkit.blocking_decisions(gadkit.find_owner_decisions(index_before), 2)], ["D9"]
        )

        self.assertTrue(gadkit.resolve_owner_decision(self.repo, "D9", "use postgres").found)

        index_after = gadkit.read_index(self.repo)
        assert index_after is not None
        # nested under generations[] — the write must find it wherever it lives, and the reader
        # must then stop reporting it, both for the gating and the advisory listing.
        self.assertEqual(gadkit.blocking_decisions(gadkit.find_owner_decisions(index_after), 2), [])
        self.assertEqual(gadkit.open_owner_decisions(self.repo), [])
        self.assertEqual(index_after["generations"][0]["ownerDecisions"][0]["status"], "answered")

    def test_resolve_writes_valid_json_atomically_and_leaves_no_temp_files(self) -> None:
        """The write is `tmp file + os.replace` (a torn generations-index.json would brick the
        repo for gad-kit AND for claude-relay's own readers): the file must parse afterwards, no
        `.generations-index.*.tmp` may survive, and unrelated content must round-trip untouched.
        """
        _write_index(
            self.repo,
            {
                "project": "test",
                "nextGen": 3,
                "generations": [{"gen": 0, "notes": "kéep me — ünicode"}],
                "ownerDecisions": [
                    {"id": "D1", "question": "q1", "blocksGen": 3, "status": "open"},
                    {"id": "D2", "question": "q2", "blocksGen": 3, "status": "open"},
                ],
            },
        )
        answer = "answer with ünicode — and a newline\nsecond"
        result = gadkit.resolve_owner_decision(self.repo, "D1", answer)
        self.assertTrue(result.found)

        raw = (self.repo / ".gad" / "generations-index.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)  # valid JSON, not a truncated/torn write
        self.assertEqual(parsed["ownerDecisions"][0]["status"], "answered")
        self.assertEqual(parsed["ownerDecisions"][0]["resolution"], answer)
        self.assertIn("resolvedAt", parsed["ownerDecisions"][0])
        self.assertEqual(parsed["ownerDecisions"][1]["status"], "open")  # the other one untouched
        self.assertEqual(parsed["generations"][0]["notes"], "kéep me — ünicode")
        leftovers = [p.name for p in (self.repo / ".gad").iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_resolve_does_not_leave_the_decision_blocking_triage(self) -> None:
        """End-to-end criterion for the un-park: after a resolve, triage must no longer report
        THIS decision as the blocker (`blocking_decision_ids` empty), AND the tree must be clean
        again immediately (B13 audit fix) — not merely correct-until-the-next-dirty-tree-check.
        Previously `resolve_owner_decision()` left `.gad/generations-index.json` modified and
        UNCOMMITTED, so the very next `triage()` could either park the repo forever as
        AWAITING_HUMAN (if HEAD had moved since the last clean baseline) or — worse — sweep the
        resolution itself into a `git stash` as part of an unrelated recovery, reverting the
        decision back to `"open"` (2 independent confirmations, 3 reproductions in the audit).
        """
        _seed_committed_gad_state(
            self.repo,
            {
                "project": "test",
                "nextGen": 2,
                "generations": [{"gen": 0}, {"gen": 1}],
                "ownerDecisions": [{"id": "D1", "question": "pick a DB", "blocksGen": 2, "status": "open"}],
            },
            [(0, "a"), (1, "b"), (2, "c")],
        )
        state = _fresh_state()
        parked = gadkit.triage(self.repo, _config(), state, dry_run=True)
        self.assertEqual(parked.blocking_decision_ids, ("D1",))

        gadkit.resolve_owner_decision(self.repo, "D1", "use postgres")

        # The resolve's own commit must have landed — the tree is clean, not merely edited.
        self.assertEqual(gadkit.git_status_porcelain(self.repo), [])

        after = gadkit.triage(self.repo, _config(), state, dry_run=True)
        self.assertEqual(after.blocking_decision_ids, ())
        self.assertNotIn("owner decision", after.detail)
        # The B13 regression proper: triage must not have parked on a NOW-dirty tree either.
        self.assertNotEqual(after.kind, "AWAITING_HUMAN")

    def test_resolve_commits_the_index_file_immediately(self) -> None:
        """B13 audit fix, direct unit coverage: `resolve_owner_decision()` must leave the tree
        clean by committing `.gad/generations-index.json` itself, scoped so any OTHER unrelated
        dirt in the tree is left untouched (a pathspec-limited commit, not `git add -A`).
        """
        _write_index(
            self.repo,
            {
                "project": "test",
                "nextGen": 1,
                "generations": [],
                "ownerDecisions": [{"id": "D1", "question": "q", "blocksGen": 1, "status": "open"}],
            },
        )
        _commit_all(self.repo, "seed .gad state")
        (self.repo / "unrelated-scratch.txt").write_text("do not touch me\n", encoding="utf-8")

        result = gadkit.resolve_owner_decision(self.repo, "D1", "use postgres")
        self.assertTrue(result.found)
        self.assertTrue(result.committed)

        status = gadkit.git_status_porcelain(self.repo)
        # generations-index.json's own edit is committed away...
        self.assertFalse(any("generations-index.json" in line for line in status))
        # ...but the unrelated scratch file is NOT swept up into that commit.
        self.assertTrue(any("unrelated-scratch.txt" in line for line in status))

        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(self.repo),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("resolve ownerDecision D1", log.stdout)


class ResolveOwnerDecisionCommitFailureTests(unittest.TestCase):
    """B13 audit fix, SECOND round (adversarial review, 2026-07-26): `resolve_owner_decision()`'s
    OWN commit can fail — a rejecting pre-commit hook, or a repo with no git identity configured
    (both ordinary operator conditions, not exotic). The disk write itself must still succeed
    (the decision IS resolved, unblocking `blocking_decisions()` immediately) but the returned
    `ResolveResult` must make the uncommitted state impossible to mistake for an ordinary clean
    resolution — and a LATER `triage()` must never sweep it into a `git stash`.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.addCleanup(self._tmp.cleanup)
        _init_repo(self.repo)
        _write_index(
            self.repo,
            {
                "project": "test",
                "nextGen": 1,
                "generations": [],
                "ownerDecisions": [{"id": "D1", "question": "q", "blocksGen": 1, "status": "open"}],
            },
        )
        _write_backlog(self.repo, [(0, "a"), (1, "b")])
        _commit_all(self.repo, "seed .gad state")

    def _install_rejecting_hook(self) -> None:
        hooks_dir = self.repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text(
            "#!/bin/sh\n"
            'echo "pre-commit hook: rejecting commit (simulated policy check failure)" >&2\n'
            "exit 1\n",
            encoding="utf-8",
        )
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def test_a_rejecting_pre_commit_hook_leaves_the_write_applied_but_uncommitted(self) -> None:
        self._install_rejecting_hook()
        result = gadkit.resolve_owner_decision(self.repo, "D1", "use postgres")
        self.assertTrue(result.found)
        self.assertFalse(result.committed)
        # The write itself DID apply — this is what makes the repo unblocked immediately
        # regardless of the commit outcome.
        reloaded = gadkit.read_index(self.repo)
        assert reloaded is not None
        self.assertEqual(reloaded["ownerDecisions"][0]["status"], "answered")
        # And the tree really is left dirty (the commit genuinely failed, not silently no-opped).
        status = gadkit.git_status_porcelain(self.repo)
        self.assertTrue(any("generations-index.json" in line for line in status), status)

    def test_no_git_identity_leaves_the_write_applied_but_uncommitted(self) -> None:
        # `user.useConfigOnly = true` forces git to REFUSE to guess an identity from GECOS/
        # hostname fallback, so this reproduces deterministically regardless of the host's own
        # global ~/.gitconfig.
        _run_git(self.repo, "config", "--unset", "user.name")
        _run_git(self.repo, "config", "--unset", "user.email")
        _run_git(self.repo, "config", "user.useConfigOnly", "true")
        result = gadkit.resolve_owner_decision(self.repo, "D1", "use postgres")
        self.assertTrue(result.found)
        self.assertFalse(result.committed)
        reloaded = gadkit.read_index(self.repo)
        assert reloaded is not None
        self.assertEqual(reloaded["ownerDecisions"][0]["status"], "answered")

    def test_a_rejected_commit_is_not_swept_by_a_later_triage(self) -> None:
        """The B13 failure scenario, end to end: after a failed commit, the NEXT `triage()` must
        neither park forever nor sweep the resolution into a `git stash` reverting it to "open".
        """
        self._install_rejecting_hook()
        state = _fresh_state()
        # Prime the clean baseline BEFORE resolving (matches the real sequence: triage() already
        # ran at least once against this committed HEAD).
        gadkit.triage(self.repo, _config(), state)
        result = gadkit.resolve_owner_decision(self.repo, "D1", "use postgres")
        self.assertFalse(result.committed)

        plan = gadkit.triage(self.repo, _config(), state)
        # Must not be swept into a stash (which would revert the decision to "open") ...
        self.assertIsNone(plan.stashed_ref)
        reloaded = gadkit.read_index(self.repo)
        assert reloaded is not None
        self.assertEqual(reloaded["ownerDecisions"][0]["status"], "answered")
        # ... and must not park as AWAITING_HUMAN on its own uncommitted resolution either.
        self.assertNotEqual(plan.kind, "AWAITING_HUMAN")

    def test_triage_keeps_retrying_the_commit_and_it_eventually_lands(self) -> None:
        """Once the hook is removed (the transient-failure case), a LATER `triage()` cycle must
        land the commit ITSELF on its own — no operator intervention required. Deliberately
        checks the commit MESSAGE (not just "tree is clean"): a clean tree can ALSO result from
        the dirty-tree branch's `git stash` sweeping the diff away instead of committing it,
        which would revert the decision back to "open" — the exact defect this fix prevents. A
        mutation that disabled the retry-commit but left the tree-cleanliness assertion alone
        stayed GREEN here even though the resolution had been silently stashed and reverted;
        checking the commit log (and the decision's status) closes that gap.
        """
        self._install_rejecting_hook()
        state = _fresh_state()
        gadkit.triage(self.repo, _config(), state)
        gadkit.resolve_owner_decision(self.repo, "D1", "use postgres")
        self.assertNotEqual(gadkit.git_status_porcelain(self.repo), [])

        (self.repo / ".git" / "hooks" / "pre-commit").unlink()
        gadkit.triage(self.repo, _config(), state)
        self.assertEqual(gadkit.git_status_porcelain(self.repo), [])
        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(self.repo),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("ownerDecision", log.stdout)
        reloaded = gadkit.read_index(self.repo)
        assert reloaded is not None
        self.assertEqual(reloaded["ownerDecisions"][0]["status"], "answered")


class IndexOnlyOwnerDecisionDirtExemptionTests(unittest.TestCase):
    """Direct unit coverage for the two helpers `ResolveOwnerDecisionCommitFailureTests` above
    exercises indirectly through `triage()`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.addCleanup(self._tmp.cleanup)
        _init_repo(self.repo)

    def test_a_lone_modified_index_diff_is_recognized(self) -> None:
        _write_index(self.repo, {"project": "t", "nextGen": 0, "generations": []})
        _commit_all(self.repo, "seed")
        _write_index(self.repo, {"project": "t", "nextGen": 0, "generations": [], "extra": 1})
        status = gadkit.git_status_porcelain(self.repo)
        self.assertTrue(gadkit._is_index_only_owner_decision_dirt(self.repo, status))

    def test_an_untracked_new_index_file_does_not_qualify(self) -> None:
        _write_index(self.repo, {"project": "t", "nextGen": 0, "generations": []})
        status = gadkit.git_status_porcelain(self.repo)
        self.assertFalse(gadkit._is_index_only_owner_decision_dirt(self.repo, status))

    def test_an_additional_dirty_path_disqualifies_it(self) -> None:
        _write_index(self.repo, {"project": "t", "nextGen": 0, "generations": []})
        _commit_all(self.repo, "seed")
        _write_index(self.repo, {"project": "t", "nextGen": 0, "generations": [], "extra": 1})
        (self.repo / "scratch.txt").write_text("wip\n", encoding="utf-8")
        status = gadkit.git_status_porcelain(self.repo)
        self.assertFalse(gadkit._is_index_only_owner_decision_dirt(self.repo, status))

    def test_retry_commit_lands_once_nothing_is_blocking_it(self) -> None:
        _write_index(self.repo, {"project": "t", "nextGen": 0, "generations": []})
        _commit_all(self.repo, "seed")
        _write_index(self.repo, {"project": "t", "nextGen": 0, "generations": [], "extra": 1})
        landed = gadkit._retry_commit_index_only(self.repo, gadkit.index_path(self.repo))
        self.assertTrue(landed)
        self.assertEqual(gadkit.git_status_porcelain(self.repo), [])


class OpenDecisionsAndFormatTests(unittest.TestCase):
    """The back-channel self-documenting helpers: which decisions are resolvable, and rendering
    them so the operator is told the exact `resolve <id> <answer>` line."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.addCleanup(self._tmp.cleanup)
        _init_repo(self.repo)

    def test_open_owner_decisions_returns_only_open_across_nesting(self) -> None:
        _write_index(
            self.repo,
            {
                "project": "t",
                "nextGen": 1,
                "generations": [
                    {
                        "gen": 0,
                        "ownerDecisions": [
                            {"id": "D1", "question": "q1", "status": "open"},
                            {"id": "D2", "question": "q2", "status": "resolved"},
                        ],
                    }
                ],
            },
        )
        self.assertEqual([d["id"] for d in gadkit.open_owner_decisions(self.repo)], ["D1"])

    def test_open_owner_decisions_empty_when_no_index(self) -> None:
        self.assertEqual(gadkit.open_owner_decisions(self.repo), [])

    def test_format_includes_question_and_exact_reply_line(self) -> None:
        block = gadkit.format_decisions_for_operator(
            [{"id": "G0-2", "question": "Add packaging?", "status": "open"}], gen=1
        )
        self.assertIn("gen 1", block)
        self.assertIn("G0-2", block)
        self.assertIn("Add packaging?", block)
        self.assertIn("resolve G0-2 <your answer>", block)

    def test_format_empty_list_is_empty_string(self) -> None:
        self.assertEqual(gadkit.format_decisions_for_operator([]), "")

    def test_format_truncates_a_very_long_question(self) -> None:
        block = gadkit.format_decisions_for_operator([{"id": "D1", "question": "x" * 500}])
        self.assertIn("...", block)
        self.assertLess(len(block), 300)


class BacklogTypeReaderTests(unittest.TestCase):
    """`backlog_section()` / `backlog_gen_type()`: the reader that keeps a recovery invocation
    from silently downgrading a research generation to a `build` one. Pure file parsing — no git.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".gad").mkdir(parents=True)

    def test_both_on_disk_renderings_and_case_variants_parse(self) -> None:
        """Two renderings are legitimate on disk: the research template's markdown bullet with a
        bold key (`- **Type**: experiment`), and a bare `Type: experiment` — which is all
        gad-run's own survey prompt asks the agent for (gad-run.js:218).
        """
        cases = (
            ("- **Type**: eda", "eda"),
            ("- **Type**: `experiment`", "experiment"),
            ("- **Type**: `ideation`", "ideation"),
            ("Type: experiment", "experiment"),
            ("Type: `eda`", "eda"),
            ("* Type: ideation", "ideation"),
            ("- Type: eda", "eda"),
            ("- **TYPE**: EDA", "eda"),
            ("- **type**:   Experiment", "experiment"),
            ("-   Type:\tideation", "ideation"),
            ("  - **Type**: eda", "eda"),
            ("**Type**: experiment", "experiment"),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                _write_raw_backlog(
                    self.repo,
                    _RESEARCH_LEGEND + f"## G1 — an entry\n- **Goal**: x.\n{line}\n- **Priority**: 50\n",
                )
                self.assertEqual(gadkit.backlog_gen_type(self.repo, 1), expected)

    def test_build_unrecognized_and_absent_types_all_report_none(self) -> None:
        """None means "omit the key", which is exactly right for all three: gad-generation.js:124
        and gad-finish.js:65 coerce an absent/unknown genType to 'build' with no error path.
        """
        _write_raw_backlog(
            self.repo,
            _RESEARCH_LEGEND
            + "## G0 — foundation\n- **Type**: build\n\n"
            + "## G1 — bogus type\n- **Type**: sideways\n\n"
            + "## G2 — nothing declared\n- **Goal**: ship it.\n\n"
            + "## G3 — empty value\n- **Type**:\n",
        )
        for gen in (0, 1, 2, 3):
            with self.subTest(gen=gen):
                self.assertIsNone(gadkit.backlog_gen_type(self.repo, gen))

    def test_preamble_legend_is_not_read_as_the_first_entrys_type(self) -> None:
        """The load-bearing reason `backlog_section()` bounds its search: the legend above `## G0`
        contains `- **Type**: \\`experiment\\` | ...`, so an unbounded search would report G0 as an
        experiment generation when it declares no type at all.
        """
        _write_raw_backlog(self.repo, _RESEARCH_LEGEND + "## G0 — foundation\n- **Goal**: scaffold.\n")
        self.assertIsNone(gadkit.backlog_gen_type(self.repo, 0))

    def test_section_boundary_g1_does_not_absorb_g10s_type(self) -> None:
        _write_raw_backlog(
            self.repo,
            "# B\n\n## G1 — untyped\n- **Goal**: x.\n\n## G10 — typed\n- **Type**: experiment\n",
        )
        self.assertIsNone(gadkit.backlog_gen_type(self.repo, 1))
        self.assertEqual(gadkit.backlog_gen_type(self.repo, 10), "experiment")

    def test_section_boundary_holds_when_g10_is_declared_before_g1(self) -> None:
        """Backlog order is not gen order in gad-kit 2.0 (the crawl sorts `priority DESC, gen
        ASC`), so the reverse arrangement must be just as safe: `## G10`'s untyped section must
        not reach forward into `## G1`'s declaration either.
        """
        _write_raw_backlog(
            self.repo,
            "# B\n\n## G10 — untyped\n- **Goal**: x.\n\n## G1 — typed\n- **Type**: eda\n",
        )
        self.assertIsNone(gadkit.backlog_gen_type(self.repo, 10))
        self.assertEqual(gadkit.backlog_gen_type(self.repo, 1), "eda")

    def test_backlog_section_is_bounded_by_the_next_header(self) -> None:
        _write_raw_backlog(
            self.repo,
            "# B\n\n## G1 — first\n- **Goal**: alpha.\n\n## G2 — second\n- **Goal**: beta.\n",
        )
        section = gadkit.backlog_section(self.repo, 1)
        assert section is not None
        self.assertIn("alpha", section)
        self.assertNotIn("beta", section)
        self.assertNotIn("## G2", section)

    def test_last_entrys_section_runs_to_end_of_file(self) -> None:
        _write_raw_backlog(
            self.repo,
            "# B\n\n## G1 — a\n- **Goal**: x.\n\n## G2 — last\n\nsome prose\n\n- **Type**: ideation\n",
        )
        self.assertEqual(gadkit.backlog_gen_type(self.repo, 2), "ideation")

    def test_generation_not_declared_in_the_backlog_reports_none(self) -> None:
        _write_raw_backlog(self.repo, _RESEARCH_LEGEND + "## G0 — a\n- **Type**: eda\n")
        self.assertIsNone(gadkit.backlog_section(self.repo, 9))
        self.assertIsNone(gadkit.backlog_gen_type(self.repo, 9))

    def test_missing_backlog_file_reports_none(self) -> None:
        empty_repo = Path(self._tmp.name) / "no-gad"
        empty_repo.mkdir()
        self.assertIsNone(gadkit.backlog_section(empty_repo, 0))
        self.assertIsNone(gadkit.backlog_gen_type(empty_repo, 0))
        self.assertEqual(gadkit.backlog_generations(empty_repo), [])

    def test_unreadable_backlog_reports_none_rather_than_raising(self) -> None:
        """`backlog.md` exists but cannot be read as a file (here: it IS a directory, which is
        chmod- and root-independent). Every backlog reader must degrade, never raise — triage()
        calls them on repos it does not control.
        """
        (self.repo / ".gad" / "backlog.md").mkdir()
        self.assertIsNone(gadkit.backlog_gen_type(self.repo, 0))
        self.assertIsNone(gadkit.backlog_section(self.repo, 0))
        self.assertEqual(gadkit.backlog_generations(self.repo), [])
        self.assertFalse(gadkit.is_research_repo(self.repo))

    def test_crlf_and_bom_encodings_still_parse(self) -> None:
        """Fixture-monoculture guard: every other fixture here is LF + no BOM, but a backlog
        hand-edited on Windows (or written by an editor that emits a BOM) is a real input.
        """
        text = _RESEARCH_LEGEND + "## G2 — windows-edited\n- **Type**: experiment\n- **Priority**: 10\n"
        crlf = text.replace("\n", "\r\n")
        (self.repo / ".gad" / "backlog.md").write_bytes(crlf.encode("utf-8"))
        self.assertEqual(gadkit.backlog_gen_type(self.repo, 2), "experiment")
        self.assertEqual(gadkit.backlog_generations(self.repo), [2])
        self.assertTrue(gadkit.is_research_repo(self.repo))

        (self.repo / ".gad" / "backlog.md").write_bytes(("﻿" + crlf).encode("utf-8"))
        self.assertEqual(gadkit.backlog_gen_type(self.repo, 2), "experiment")
        self.assertTrue(gadkit.is_research_repo(self.repo))

    def test_multi_digit_and_zero_generations_are_distinguished(self) -> None:
        _write_raw_backlog(
            self.repo,
            _RESEARCH_LEGEND
            + "## G0 — zero\n- **Type**: eda\n\n"
            + "## G7 — seven\n- **Type**: experiment\n\n"
            + "## G70 — seventy\n- **Type**: ideation\n",
        )
        self.assertEqual(gadkit.backlog_generations(self.repo), [0, 7, 70])
        self.assertEqual(gadkit.backlog_gen_type(self.repo, 0), "eda")
        self.assertEqual(gadkit.backlog_gen_type(self.repo, 7), "experiment")
        self.assertEqual(gadkit.backlog_gen_type(self.repo, 70), "ideation")


class ResearchModeMarkerTests(unittest.TestCase):
    """`is_research_repo()`: the `gad-mode: research` predicate that decides whether an exhausted
    backlog means DONE or one more `/gad-run` for auto-ideation.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".gad").mkdir(parents=True)

    def test_marker_in_an_html_comment_is_research(self) -> None:
        _write_raw_backlog(self.repo, _RESEARCH_LEGEND + "## G0 — a\n- **Type**: eda\n")
        self.assertTrue(gadkit.is_research_repo(self.repo))

    def test_backlog_without_the_marker_is_not_research(self) -> None:
        _write_raw_backlog(self.repo, "# Build Backlog\n\n## G0 — a\n- **Goal**: x.\n")
        self.assertFalse(gadkit.is_research_repo(self.repo))

    def test_a_backlog_that_merely_mentions_research_is_not_research(self) -> None:
        """Guard against a looser predicate: the marker is the literal `gad-mode: research`
        token, so prose about research (very common in a research-flavoured build backlog) must
        not flip a repo into the auto-ideation branch.
        """
        _write_raw_backlog(
            self.repo,
            "# Backlog\n\n## G0 — literature research and a research spike\n- **Goal**: research it.\n",
        )
        self.assertFalse(gadkit.is_research_repo(self.repo))

    def test_missing_backlog_is_not_research(self) -> None:
        self.assertFalse(gadkit.is_research_repo(Path(self._tmp.name) / "absent"))

    def test_marker_as_bare_prose_outside_a_comment_is_not_research(self) -> None:
        """A9 audit fix: the marker must be rendered as an HTML comment (the only form the
        research backlog template actually emits) — a backlog that merely writes the literal
        phrase as plain prose (e.g. quoting the convention in its own narrative) must not flip
        the repo into the auto-ideation branch.
        """
        _write_raw_backlog(
            self.repo,
            "# Backlog\n\nThis repo sets gad-mode: research in its config.\n\n"
            "## G0 — a\n- **Goal**: x.\n",
        )
        self.assertFalse(gadkit.is_research_repo(self.repo))

    def test_marker_in_a_comment_after_the_first_heading_is_not_research(self) -> None:
        """The template always places the marker in the PREAMBLE, before any `## G` heading —
        a comment appearing later (e.g. inside one entry's own notes) must not count.
        """
        _write_raw_backlog(
            self.repo,
            "# Backlog\n\n## G0 — a\n- **Goal**: x.\n\n<!-- gad-mode: research -->\n",
        )
        self.assertFalse(gadkit.is_research_repo(self.repo))


class GenerationDirDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".gad").mkdir(parents=True)

    def test_numeric_generation_dirs_are_found_and_keyed_by_number(self) -> None:
        _make_generation_dir(self.repo, 0)
        _make_generation_dir(self.repo, 12)
        found = gadkit.existing_generation_dirs(self.repo)
        self.assertEqual(sorted(found), [0, 12])
        self.assertEqual(found[12].name, "generation-12")

    def test_non_numeric_and_non_directory_entries_are_ignored(self) -> None:
        (self.repo / ".gad" / "generation-old").mkdir()
        (self.repo / ".gad" / "generation-2.bak").mkdir()
        (self.repo / ".gad" / "generation-9").write_text("a FILE, not a dir\n", encoding="utf-8")
        _make_generation_dir(self.repo, 3)
        self.assertEqual(sorted(gadkit.existing_generation_dirs(self.repo)), [3])

    def test_repo_without_a_gad_dir_yields_nothing(self) -> None:
        self.assertEqual(gadkit.existing_generation_dirs(Path(self._tmp.name) / "absent"), {})


class InFlightGenerationTests(unittest.TestCase):
    """`in_flight_generation()`: the documented evidence precedence (dirty path > directory mtime
    > None). Status lines are passed in, so most cases need no git — the shapes used here are the
    ones real `git status --porcelain` produces (see `PorcelainShapeTests`).
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".gad").mkdir(parents=True)

    def _index(self, next_gen: int, completed: tuple[int, ...]) -> dict:
        return {"project": "t", "nextGen": next_gen, "generations": [{"gen": g} for g in completed]}

    def _touch(self, gen: int, when: float) -> None:
        os.utime(gadkit.generation_dir(self.repo, gen), (when, when))

    def test_dirty_path_beats_the_mtime_fallback(self) -> None:
        """Precedence #1 over #2, asserted where they DISAGREE: gen 3's directory is the most
        recently modified, but gen 7 is the one with uncommitted artifacts.
        """
        _make_generation_dir(self.repo, 3, "plan.md")
        _make_generation_dir(self.repo, 7, "plan.md")
        self._touch(7, 1_000_000.0)
        self._touch(3, 2_000_000.0)  # newest by mtime — must lose to the dirty-path evidence
        index = self._index(3, (0, 1, 2))
        self.assertEqual(
            gadkit.in_flight_generation(self.repo, index, ["?? .gad/generation-7/plan.md"]), 7
        )
        # ...and with no dirty evidence at all, #2 does pick gen 3.
        self.assertEqual(gadkit.in_flight_generation(self.repo, index, []), 3)

    def test_multiple_dirty_uncommitted_generations_tie_break_by_mtime_not_gen_number(self) -> None:
        """A3 audit fix regression: gad-kit 2.0 runs generations `priority DESC, gen ASC`
        (gad-run.js:283), not creation order, so a stale abandoned HIGH-numbered scaffold must
        not automatically outrank a genuinely in-progress LOWER-numbered one. When both are
        dirty, the more RECENTLY MODIFIED directory wins, regardless of which gen number is
        larger.
        """
        _make_generation_dir(self.repo, 3, "plan.md")
        _make_generation_dir(self.repo, 9, "plan.md")
        self._touch(9, 1_000_000.0)  # abandoned long ago
        self._touch(3, 2_000_000.0)  # the one actually being worked on right now
        index = self._index(0, (0,))
        lines = ["?? .gad/generation-3/plan.md", "?? .gad/generation-9/plan.md"]
        self.assertEqual(gadkit.in_flight_generation(self.repo, index, lines), 3)
        # ...and flipping which one is fresher flips the answer, proving it's mtime-driven.
        self._touch(3, 1_000_000.0)
        self._touch(9, 2_000_000.0)
        self.assertEqual(gadkit.in_flight_generation(self.repo, index, lines), 9)

    def test_largest_dirty_uncommitted_generation_wins(self) -> None:
        index = self._index(3, (0, 1, 2))
        lines = [
            "?? .gad/generation-3/",
            " M .gad/generation-5/plan.md",
            "?? .gad/generation-7/reviews/verification.md",
        ]
        self.assertEqual(gadkit.in_flight_generation(self.repo, index, lines), 7)

    def test_completed_generations_are_excluded_from_the_dirty_set(self) -> None:
        """A committed generation whose files are merely edited (e.g. a doc touch-up) must not be
        mistaken for the in-flight one — otherwise triage would route recovery at an already
        consolidated generation.
        """
        index = self._index(5, (0, 1, 2, 3))
        lines = [" M .gad/generation-1/plan.md", "?? .gad/generation-4/plan.md"]
        self.assertEqual(gadkit.in_flight_generation(self.repo, index, lines), 4)
        # ...and when the ONLY dirty generation is a completed one, there is no evidence at all.
        self.assertIsNone(gadkit.in_flight_generation(self.repo, index, [" M .gad/generation-1/plan.md"]))

    def test_recognizes_the_bare_directory_entry_git_reports_for_a_new_scaffold(self) -> None:
        # git collapses a wholly-untracked directory into ONE porcelain line ending in "/".
        self.assertEqual(gadkit.in_flight_generation(self.repo, None, ["?? .gad/generation-7/"]), 7)

    def test_rename_destination_and_quoted_paths_are_parsed(self) -> None:
        self.assertEqual(
            gadkit.in_flight_generation(self.repo, None, ["R  old/plan.md -> .gad/generation-4/plan.md"]), 4
        )
        # porcelain quotes paths containing unusual bytes — the quotes must not defeat the match
        self.assertEqual(
            gadkit.in_flight_generation(self.repo, None, ['?? ".gad/generation-5/wéird note.md"']), 5
        )

    def test_dirty_paths_outside_gad_are_not_generation_evidence(self) -> None:
        lines = [" M src/generation-4/thing.py", "?? notes.md", " M README.md"]
        self.assertIsNone(gadkit.in_flight_generation(self.repo, None, lines))

    def test_mtime_fallback_picks_the_most_recently_modified_uncommitted_dir(self) -> None:
        _make_generation_dir(self.repo, 3, "plan.md")
        _make_generation_dir(self.repo, 7, "plan.md")
        index = self._index(3, (0, 1, 2))
        self._touch(3, 1_000_000.0)
        self._touch(7, 2_000_000.0)
        self.assertEqual(gadkit.in_flight_generation(self.repo, index, []), 7)
        self._touch(7, 1_000_000.0)
        self._touch(3, 2_000_000.0)  # flip the order — the answer must follow the mtimes
        self.assertEqual(gadkit.in_flight_generation(self.repo, index, []), 3)

    def test_mtime_fallback_is_deterministic_on_an_exact_tie(self) -> None:
        """Coarse-granularity filesystems make equal mtimes plausible; `max()` must not depend on
        dict iteration order, so the tie resolves to the larger (more recently created) gen.
        """
        _make_generation_dir(self.repo, 4, "plan.md")
        _make_generation_dir(self.repo, 9, "plan.md")
        self._touch(4, 1_500_000.0)
        self._touch(9, 1_500_000.0)
        index = self._index(4, (0,))
        for _ in range(5):
            self.assertEqual(gadkit.in_flight_generation(self.repo, index, []), 9)

    def test_mtime_fallback_skips_completed_generations(self) -> None:
        _make_generation_dir(self.repo, 1, "plan.md")
        _make_generation_dir(self.repo, 2, "plan.md")
        self._touch(1, 2_000_000.0)  # newest, but already consolidated
        self._touch(2, 1_000_000.0)
        self.assertEqual(gadkit.in_flight_generation(self.repo, self._index(3, (0, 1)), []), 2)

    def test_no_evidence_at_all_is_none(self) -> None:
        self.assertIsNone(gadkit.in_flight_generation(self.repo, self._index(2, (0, 1)), []))
        self.assertIsNone(gadkit.in_flight_generation(Path(self._tmp.name) / "absent", None, []))

    def test_every_generation_dir_completed_is_none(self) -> None:
        _make_generation_dir(self.repo, 0, "plan.md")
        _make_generation_dir(self.repo, 1, "plan.md")
        self.assertIsNone(gadkit.in_flight_generation(self.repo, self._index(2, (0, 1)), []))

    def test_a_missing_index_treats_nothing_as_completed(self) -> None:
        _make_generation_dir(self.repo, 6, "plan.md")
        self.assertEqual(gadkit.in_flight_generation(self.repo, None, []), 6)


class UncommittedHandoffScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / ".gad").mkdir(parents=True)

    def _index(self, completed: tuple[int, ...]) -> dict:
        return {
            "project": "t",
            "nextGen": max(completed, default=-1) + 1,
            "generations": [{"gen": g} for g in completed],
        }

    def test_finds_a_handoff_under_a_generation_that_is_not_next_gen(self) -> None:
        _make_generation_dir(self.repo, 7, "handoff.md")
        self.assertEqual(gadkit.find_uncommitted_handoff(self.repo, self._index((0, 1, 2)), None), 7)

    def test_handoff_under_a_committed_generation_is_ignored(self) -> None:
        """`handoff.md` is never deleted once a later `/gad-finish` succeeds, so counting a
        committed generation's stale one would park the repo forever.
        """
        _make_generation_dir(self.repo, 1, "handoff.md")
        self.assertIsNone(gadkit.find_uncommitted_handoff(self.repo, self._index((0, 1)), None))

    def test_the_in_flight_generation_wins_when_it_also_has_a_handoff(self) -> None:
        _make_generation_dir(self.repo, 4, "handoff.md")
        _make_generation_dir(self.repo, 7, "handoff.md")
        self.assertEqual(gadkit.find_uncommitted_handoff(self.repo, self._index((0,)), 7), 7)

    def test_smallest_blocked_generation_is_reported_when_the_in_flight_one_has_none(self) -> None:
        _make_generation_dir(self.repo, 4, "handoff.md")
        _make_generation_dir(self.repo, 7, "plan.md")  # in flight, but not blocked
        self.assertEqual(gadkit.find_uncommitted_handoff(self.repo, self._index((0,)), 7), 4)

    def test_no_handoff_anywhere_is_none(self) -> None:
        _make_generation_dir(self.repo, 3, "plan.md", "reviews/verification.md")
        self.assertIsNone(gadkit.find_uncommitted_handoff(self.repo, self._index((0,)), 3))


class HandoffIsNotExclusivelyABlockerSignalTests(unittest.TestCase):
    """2026-07-26 fix (live-verified against a real gad-kit run, empirically discovered mid-
    Track-B): `handoff.md`'s mere existence was never a BLOCKED signal — `agents/consolidator.md`
    step 2 writes it as an ordinary per-generation artifact (open questions, the ranked
    "DECISIONS THE OWNER MUST MAKE" section, live-seam operator actions) on EVERY generation,
    committed status included; only the precondition-failure path makes it a genuine blocker.
    Uses a REAL git repo (unlike `UncommittedHandoffScanTests` above, whose fixtures have no git
    history at all) so the git-log fallback — the exact "commit landed, index.json wasn't
    updated yet" crash gap `consolidator.md`'s own IDEMPOTENT COMMIT guard exists for — can be
    exercised for real.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_repo(self.repo)

    def _stale_index(self) -> dict:
        # Deliberately NOT updated to list gen 0 as completed — simulating the crash gap between
        # the artifact commit landing and a later, separate index-only reconciliation.
        return {"project": "t", "nextGen": 0, "generations": []}

    def test_git_log_has_gen_commit_finds_a_real_gen_commit(self) -> None:
        _make_generation_dir(self.repo, 0, "handoff.md")
        _commit_all(self.repo, "gen-0: initial feature")
        self.assertTrue(gadkit.git_log_has_gen_commit(self.repo, 0))
        self.assertFalse(gadkit.git_log_has_gen_commit(self.repo, 1))

    def test_an_unrelated_commit_merely_mentioning_the_prefix_does_not_count(self) -> None:
        _make_generation_dir(self.repo, 0, "handoff.md")
        # Contains the substring "gen-0" but NOT as the anchored `^gen-0:` message prefix.
        _commit_all(self.repo, "chore: something about gen-0 in prose, not the real prefix")
        self.assertFalse(gadkit.git_log_has_gen_commit(self.repo, 0))

    def test_a_committed_generations_handoff_does_not_block_even_though_index_json_lags(self) -> None:
        """The exact empirical reproduction: a `gen-N:` commit landed (handoff.md included, as
        consolidator.md writes on every generation) but `generations-index.json` was never
        updated to reflect it — `find_uncommitted_handoff()` must trust git history, not just
        the (here, stale) index."""
        _make_generation_dir(self.repo, 0, "handoff.md", "summary.md")
        _commit_all(self.repo, "gen-0: initial feature")
        self.assertIsNone(gadkit.find_uncommitted_handoff(self.repo, self._stale_index(), None))

    def test_a_genuinely_uncommitted_generations_handoff_still_blocks(self) -> None:
        """The complementary case the audit demanded: no `gen-N:` commit anywhere AND the index
        doesn't list it either — this IS the real consolidator-refusal signal and must still
        block, so the fix doesn't just make everything permissive."""
        _make_generation_dir(self.repo, 0, "handoff.md")
        # No commit at all for generation 0's directory — genuinely uncommitted.
        self.assertEqual(gadkit.find_uncommitted_handoff(self.repo, self._stale_index(), None), 0)

    def test_triage_does_not_report_blocked_for_a_committed_generation_with_a_lagging_index(self) -> None:
        """End-to-end: `triage()` step 2 (which IS `find_uncommitted_handoff()`) must not park a
        repo whose generation actually committed successfully."""
        _write_backlog(self.repo, [(0, "first"), (1, "second")])
        _commit_all(self.repo, "seed backlog")
        _make_generation_dir(self.repo, 0, "handoff.md", "summary.md")
        _commit_all(self.repo, "gen-0: initial feature")
        _write_index(self.repo, self._stale_index())  # NOT committed — mirrors an in-progress .gad/ write
        plan = gadkit.triage(self.repo, _config(), _fresh_state())
        self.assertNotEqual(plan.kind, "BLOCKED")


class RecoveryGenerationRoutingTests(unittest.TestCase):
    """triage() end-to-end: the generation it recovers comes from DISK evidence, not
    `index.nextGen` — the gad-kit 2.0 `priority DESC, gen ASC` crawl runs generations out of gen
    order, so `nextGen` routinely names a generation whose directory is empty.
    """

    # nextGen lags at 3 while the crawl actually ran the high-priority gen 7 and died in it.
    OUT_OF_ORDER_BACKLOG = (
        "# Fixture — GAD Research Backlog\n\n"
        "<!-- gad-mode: research -->\n\n"
        "- **Type**: `experiment` | `eda` | `ideation` | `build`\n\n---\n\n"
        "## G0 — done\n- **Type**: build\n\n"
        "## G1 — done\n- **Type**: build\n\n"
        "## G2 — done\n- **Type**: build\n\n"
        "## G3 — low priority\n- **Type**: build\n- **Priority**: 1\n\n"
        "## G7 — high priority\n- **Type**: experiment\n- **Priority**: 99\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.state = _fresh_state()
        _init_repo(self.repo)

    def _seed(self, *, next_gen: int, completed: tuple[int, ...], backlog: str) -> None:
        _write_index(
            self.repo,
            {"project": "test", "nextGen": next_gen, "generations": [{"gen": g} for g in completed]},
        )
        _write_raw_backlog(self.repo, backlog)
        _commit_all(self.repo, "seed .gad state")

    def _seed_out_of_order(self) -> None:
        self._seed(next_gen=3, completed=(0, 1, 2), backlog=self.OUT_OF_ORDER_BACKLOG)
        gadkit.triage(self.repo, _config(), self.state)  # prime the clean baseline

    def test_new_scaffold_is_one_untracked_porcelain_entry_and_is_still_recognized(self) -> None:
        """Pins the real `git status --porcelain` shape the disk-evidence regex has to handle: a
        wholly-new `generation-7/` scaffold is reported as the SINGLE entry `?? .gad/generation-7/`,
        not one line per file.
        """
        self._seed_out_of_order()
        _make_generation_dir(self.repo, 7, "plan.md", "reviews/verification.md")
        lines = gadkit.git_status_porcelain(self.repo)
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].endswith(".gad/generation-7/"), lines[0])
        self.assertEqual(gadkit.in_flight_generation(self.repo, gadkit.read_index(self.repo), lines), 7)

    def test_finish_routes_to_the_in_flight_generation_not_next_gen(self) -> None:
        self._seed_out_of_order()
        _make_generation_dir(
            self.repo,
            7,
            "plan.md",
            "implementation-log.md",
            "reviews/verification.md",
            "reviews/adversarial-review.md",
        )
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "FINISH")
        self.assertEqual(plan.mode, "gad_finish")
        self.assertEqual(plan.gen, 7)  # NOT nextGen (3), whose directory does not even exist
        self.assertEqual(plan.gen_type, "experiment")  # threaded from `## G7`'s Type line
        self.assertIn("generation-7", plan.detail)
        self.assertEqual(_stash_list(self.repo), "")  # FINISH never touches the tree

    def test_restart_routes_to_the_in_flight_generation_and_stashes_under_its_number(self) -> None:
        self._seed_out_of_order()
        _make_generation_dir(self.repo, 7, "plan.md")  # Plan done, no verification.md
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual((plan.kind, plan.mode, plan.gen), ("RUN", "gad_generation", 7))
        self.assertEqual(plan.gen_type, "experiment")
        self.assertIsNotNone(plan.stashed_ref)
        self.assertIn("parking gen7", _stash_list(self.repo))
        self.assertEqual(_porcelain(self.repo).strip(), "")  # stashed, not discarded
        recorded = cooldown.get_last_stash(self.state, self.repo)
        assert recorded is not None
        self.assertTrue(any("generation-7" in f for f in recorded["files"]), recorded["files"])

    def test_falls_back_to_index_next_gen_when_disk_shows_no_generation_at_all(self) -> None:
        """The documented ambiguous case: dirt inside `.gad/` but no generation directory
        anywhere. `in_flight_generation()` must say None and triage must keep the pre-2.0
        `nextGen` behaviour rather than guess.
        """
        self._seed_out_of_order()
        backlog = self.repo / ".gad" / "backlog.md"
        backlog.write_text(backlog.read_text(encoding="utf-8") + "\n<!-- hand-edited -->\n", encoding="utf-8")
        self.assertIsNone(
            gadkit.in_flight_generation(
                self.repo, gadkit.read_index(self.repo), gadkit.git_status_porcelain(self.repo)
            )
        )
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual((plan.kind, plan.mode, plan.gen), ("RUN", "gad_generation", 3))
        self.assertIsNone(plan.gen_type)  # `## G3` declares build
        self.assertIn("parking gen3", _stash_list(self.repo))

    def test_handoff_under_an_uncommitted_non_next_gen_generation_blocks_and_names_it(self) -> None:
        self._seed_out_of_order()
        _make_generation_dir(self.repo, 7, "plan.md", "handoff.md")
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "BLOCKED")
        self.assertEqual(plan.gen, 7)
        self.assertIn("generation 7", plan.detail)
        self.assertIn(str(gadkit.handoff_path(self.repo, 7)), plan.detail)
        self.assertEqual(_stash_list(self.repo), "")  # BLOCKED parks, never stashes

    def test_stale_handoff_under_a_committed_generation_does_not_park_forever(self) -> None:
        self._seed(next_gen=3, completed=(0, 1, 2), backlog=self.OUT_OF_ORDER_BACKLOG)
        _make_generation_dir(self.repo, 1, "handoff.md")  # gen 1 IS in the index -> consolidated
        _commit_all(self.repo, "gen 1 artifacts (handoff.md never deleted)")
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual((plan.kind, plan.mode), ("RUN", "gad_run"))

    def test_unrelated_dirt_beside_a_stale_generation_dir_still_awaits_human(self) -> None:
        """The regression guard for the deliberate decision to keep the stash gate keyed on
        `nextGen`: `in_flight_generation()` only ever returns an N whose directory exists, so
        feeding it to `_has_gad_dirty_signal()` would make the "a scaffold exists" clause
        self-fulfilling and a tree dirtied by unrelated work next to a stale, uncommitted
        generation directory would become stashable (Invariant #6 forbids widening that).
        """
        self._seed(next_gen=1, completed=(0,), backlog="# B\n\n## G0 — a\n\n## G1 — b\n")
        _make_generation_dir(self.repo, 5, "plan.md")  # on disk, not in the index, and COMMITTED
        _commit_all(self.repo, "an abandoned generation-5 someone committed")
        gadkit.triage(self.repo, _config(), self.state)  # prime the clean baseline at this HEAD
        (self.repo / "scratch.txt").write_text("someone's unrelated WIP\n", encoding="utf-8")

        # The recovery ANSWER did change (disk evidence now points at 5)...
        self.assertEqual(
            gadkit.in_flight_generation(
                self.repo, gadkit.read_index(self.repo), gadkit.git_status_porcelain(self.repo)
            ),
            5,
        )
        # ...but the may-we-touch-the-tree GATE did not.
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "AWAITING_HUMAN")
        self.assertEqual(plan.gen, 1)  # reported against nextGen, matching the signal it explains
        self.assertIn("no generation-N/", plan.detail)
        self.assertEqual(_stash_list(self.repo), "")
        self.assertIn("scratch.txt", _porcelain(self.repo))

    def test_owner_decision_gate_uses_the_smallest_pending_generation(self) -> None:
        """gad-run.js:222 defines its own `nextGen` as "the smallest pending gen number (or the
        index's nextGen if the backlog has none left)" — the gate must match that, or a decision
        gating the generation about to be attempted is missed after an out-of-order crawl.
        """
        _write_index(
            self.repo,
            {
                "project": "test",
                "nextGen": 3,
                "generations": [{"gen": 0}, {"gen": 1}, {"gen": 2}],
                "ownerDecisions": [{"id": "D5", "question": "q", "blocksGen": 3, "status": "open"}],
            },
        )
        _write_raw_backlog(self.repo, self.OUT_OF_ORDER_BACKLOG)
        _commit_all(self.repo, "seed .gad state")
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "AWAITING_HUMAN")
        self.assertEqual(plan.gen, 3)
        self.assertEqual(plan.blocking_decision_ids, ("D5",))

    def test_owner_decision_gating_the_out_of_order_recovery_target_is_not_missed(self) -> None:
        """A4 audit fix: the owner-decision gate must use `recovery_gen` (not `pending[0]`) once
        the tree is dirty and disk names an out-of-order generation as in-flight — a decision
        gating THAT generation (blocksGen=7) is not necessarily <= pending[0] (3) at all. Gating
        on `pending[0]` unconditionally let such a decision slip through; gad-kit's own preflight
        then halted anyway, writing nothing, reproducing the livelock the recovery-routing fix
        exists to remove.
        """
        _write_index(
            self.repo,
            {
                "project": "test",
                "nextGen": 3,
                "generations": [{"gen": 0}, {"gen": 1}, {"gen": 2}],
                "ownerDecisions": [{"id": "D9", "question": "q", "blocksGen": 7, "status": "open"}],
            },
        )
        _write_raw_backlog(self.repo, self.OUT_OF_ORDER_BACKLOG)
        _commit_all(self.repo, "seed .gad state")
        gadkit.triage(self.repo, _config(), self.state)  # prime the clean baseline
        _make_generation_dir(self.repo, 7, "plan.md")  # the out-of-order generation is in flight
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "AWAITING_HUMAN")
        self.assertEqual(plan.gen, 7)
        self.assertEqual(plan.blocking_decision_ids, ("D9",))
        self.assertEqual(_stash_list(self.repo), "")  # never touches the tree

    def test_in_flight_generation_absent_from_the_backlog_falls_back_to_next_gen(self) -> None:
        """A5 audit fix: `in_flight_generation()` is pure disk evidence and can name a generation
        the backlog never declared at all (the concrete case: gad-run's own auto-ideation
        sub-generation, numbered `maxBacklogGen + 1`, which never gets a `## G<n>` heading).
        Recovering that number is unsafe — `backlog_gen_type()` has nothing to read for it and
        silently coerces to 'build' — so triage() must fall back to `nextGen` instead of trusting
        it, exactly like the "disk says nothing at all" case.
        """
        self._seed(next_gen=3, completed=(0, 1, 2), backlog=self.OUT_OF_ORDER_BACKLOG)
        gadkit.triage(self.repo, _config(), self.state)  # prime the clean baseline
        # gen 9 is on disk and dirty, but never declared anywhere in OUT_OF_ORDER_BACKLOG.
        _make_generation_dir(self.repo, 9, "plan.md")
        self.assertEqual(
            gadkit.in_flight_generation(
                self.repo, gadkit.read_index(self.repo), gadkit.git_status_porcelain(self.repo)
            ),
            9,
        )
        plan = gadkit.triage(self.repo, _config(), self.state)
        # Falls back to nextGen (3, a real declared-and-pending generation with an experiment
        # type), NOT gen 9 (which would have silently coerced to 'build').
        self.assertEqual((plan.kind, plan.mode, plan.gen), ("RUN", "gad_generation", 3))
        self.assertIsNone(plan.gen_type)  # `## G3` declares build
        self.assertIn("parking gen3", _stash_list(self.repo))

    def test_stale_handoff_for_a_generation_absent_from_the_backlog_does_not_block_forever(
        self,
    ) -> None:
        """A7 audit fix: a `handoff.md` under a generation neither completed NOR ever declared in
        the backlog (same ideation-number gap as A5, or a removed backlog entry) has no path
        back — nothing will ever consolidate a number the backlog doesn't declare — and must not
        park the repo on it forever. It also must not stand in the way of a legitimate pending
        generation elsewhere.
        """
        self._seed(next_gen=3, completed=(0, 1, 2), backlog=self.OUT_OF_ORDER_BACKLOG)
        gadkit.triage(self.repo, _config(), self.state)  # prime the clean baseline
        _make_generation_dir(self.repo, 42, "handoff.md")  # orphaned: not pending, not next_gen
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertNotEqual(plan.kind, "BLOCKED")


class DryRunTriageIsSideEffectFreeTests(unittest.TestCase):
    """Invariant #6 regression guard covering EVERY triage decision path: `dry_run=True` must
    never create a `git stash` and must leave the working tree byte-identical. Both
    `claude-relay run --dry-run` and the park loop's periodic re-triage call it on a live repo.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._counter = 0

    def _repo(self, *, research: bool, exhausted: bool) -> tuple[Path, dict]:
        self._counter += 1
        repo = Path(self._tmp.name) / f"repo{self._counter}"
        _init_repo(repo)
        marker = "<!-- gad-mode: research -->\n\n" if research else ""
        entries = "## G0 — a\n- **Type**: eda\n" if exhausted else "## G0 — a\n\n## G1 — b\n- **Type**: eda\n"
        _write_index(repo, {"project": "test", "nextGen": 1, "generations": [{"gen": 0}]})
        _write_raw_backlog(repo, f"# B\n\n{marker}{entries}")
        _commit_all(repo, "seed .gad state")
        state = _fresh_state()
        gadkit.triage(repo, _config(), state)  # prime the clean baseline at this HEAD
        return repo, state

    def test_no_decision_path_stashes_or_mutates_the_tree(self) -> None:
        def restart(repo: Path) -> None:
            _make_generation_dir(repo, 1, "plan.md")

        def finish(repo: Path) -> None:
            _make_generation_dir(
                repo, 1, "plan.md", "reviews/verification.md", "reviews/adversarial-review.md"
            )

        def out_of_order_finish(repo: Path) -> None:
            _make_generation_dir(
                repo, 9, "plan.md", "reviews/verification.md", "reviews/adversarial-review.md"
            )

        def unrelated_dirt(repo: Path) -> None:
            (repo / "scratch.txt").write_text("unrelated WIP\n", encoding="utf-8")

        def handoff(repo: Path) -> None:
            _make_generation_dir(repo, 1, "handoff.md")

        def clean(repo: Path) -> None:
            return None

        scenarios = (
            ("mid-generation restart", restart, False),
            ("verify artifact present", finish, False),
            ("out-of-order verify artifact", out_of_order_finish, False),
            ("unattributable dirt", unrelated_dirt, False),
            ("consolidator handoff", handoff, False),
            ("clean tree, pending backlog", clean, False),
            ("clean tree, exhausted research backlog", clean, True),
        )
        for label, setup, exhausted in scenarios:
            with self.subTest(scenario=label):
                repo, state = self._repo(research=exhausted, exhausted=exhausted)
                setup(repo)
                before = _porcelain(repo)
                plan = gadkit.triage(repo, _config(), state, dry_run=True)
                self.assertEqual(_stash_list(repo), "", f"{label} -> {plan.kind} created a stash")
                self.assertEqual(_porcelain(repo), before, f"{label} -> {plan.kind} mutated the tree")
                self.assertIsNone(plan.stashed_ref)


class ExhaustedResearchBacklogTests(unittest.TestCase):
    """Fix 4: an exhausted backlog in a RESEARCH repo hands one `/gad-run` to gad-kit so its
    auto-ideation can refill the hypothesis queue, instead of terminating the crawl — bounded to
    one attempt per git HEAD so it can never livelock.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.state = _fresh_state()
        _init_repo(self.repo)

    def _seed(self, *, research: bool, pending: bool = False) -> None:
        marker = "<!-- gad-mode: research -->\n\n" if research else ""
        entries = "## G0 — a\n- **Type**: eda\n\n## G1 — b\n- **Type**: experiment\n"
        if pending:
            entries += "\n## G2 — fresh hypothesis\n- **Type**: experiment\n"
        _write_index(
            self.repo, {"project": "test", "nextGen": 2, "generations": [{"gen": 0}, {"gen": 1}]}
        )
        _write_raw_backlog(self.repo, f"# B\n\n{marker}{entries}")
        _commit_all(self.repo, "seed .gad state")

    def test_exhausted_research_repo_runs_gad_run_instead_of_terminating(self) -> None:
        self._seed(research=True)
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual((plan.kind, plan.mode), ("RUN", "gad_run"))
        self.assertIn("RESEARCH", plan.detail)
        self.assertIn("auto-ideation", plan.detail)
        # gad-run derives the ideation generation itself, so the plan must not pin a genType.
        self.assertIsNone(plan.gen_type)
        self.assertIsNotNone(plan.gen)  # command() requires one
        self.assertEqual(
            cooldown.get_ideation_attempt_head(self.state, self.repo), gadkit.git_head(self.repo)
        )

    def test_second_attempt_at_the_same_head_is_done(self) -> None:
        self._seed(research=True)
        self.assertEqual(gadkit.triage(self.repo, _config(), self.state).kind, "RUN")
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "DONE")
        self.assertIn("already", plan.detail)
        self.assertEqual(_stash_list(self.repo), "")

    def test_non_research_exhausted_repo_still_terminates_immediately(self) -> None:
        self._seed(research=False)
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "DONE")
        self.assertIn("backlog exhausted", plan.detail)
        self.assertIsNone(plan.mode)
        # a build repo must not even record the research-only bookkeeping
        self.assertIsNone(cooldown.get_ideation_attempt_head(self.state, self.repo))

    def test_dry_run_previews_the_refill_without_consuming_it(self) -> None:
        """`loop._park_and_wait()` re-triages with `dry_run=True` but the REAL state object, so a
        booking there would let a mere re-check spend the repo's single refill and the next real
        triage would return DONE having never invoked gad-run.
        """
        self._seed(research=True)
        for attempt in range(3):
            with self.subTest(attempt=attempt):
                preview = gadkit.triage(self.repo, _config(), self.state, dry_run=True)
                self.assertEqual((preview.kind, preview.mode), ("RUN", "gad_run"))
                self.assertIsNone(cooldown.get_ideation_attempt_head(self.state, self.repo))
        real = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(real.kind, "RUN")
        self.assertIsNotNone(cooldown.get_ideation_attempt_head(self.state, self.repo))

    def test_a_new_head_re_opens_the_refill(self) -> None:
        self._seed(research=True)
        first = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(first.kind, "RUN")
        booked_at = cooldown.get_ideation_attempt_head(self.state, self.repo)
        (self.repo / "result.md").write_text("the ideation landed\n", encoding="utf-8")
        _commit_all(self.repo, "gen 2: ideation")
        second = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual((second.kind, second.mode), ("RUN", "gad_run"))
        new_head = cooldown.get_ideation_attempt_head(self.state, self.repo)
        self.assertEqual(new_head, gadkit.git_head(self.repo))
        self.assertNotEqual(new_head, booked_at)

    def test_handing_the_attempt_back_re_opens_it_at_the_same_head(self) -> None:
        """What `loop.run_once()` does when no seat was available: the attempt was booked at
        decision time but never spent, so clearing it must restore the RUN decision.
        """
        self._seed(research=True)
        self.assertEqual(gadkit.triage(self.repo, _config(), self.state).kind, "RUN")
        self.assertEqual(gadkit.triage(self.repo, _config(), self.state).kind, "DONE")
        cooldown.clear_ideation_attempt_head(self.state, self.repo)
        self.assertEqual(gadkit.triage(self.repo, _config(), self.state).kind, "RUN")

    def test_a_repo_with_no_resolvable_head_degrades_to_done(self) -> None:
        """`head is None` cannot be bounded by HEAD identity, so it must degrade to DONE rather
        than hand out an unbounded refill every iteration.
        """
        self._seed(research=True)
        with mock.patch.object(gadkit, "git_head", return_value=None):
            plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "DONE")
        self.assertIsNone(cooldown.get_ideation_attempt_head(self.state, self.repo))

    def test_a_research_repo_with_pending_work_books_nothing(self) -> None:
        self._seed(research=True, pending=True)
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual((plan.kind, plan.mode, plan.gen), ("RUN", "gad_run", 2))
        self.assertIsNone(cooldown.get_ideation_attempt_head(self.state, self.repo))

    def test_the_refill_plan_builds_a_valid_gad_run_command(self) -> None:
        """The refill plan is handed straight to `command()`, which requires `plan.gen` — so this
        proves the new branch produces an INVOCABLE plan, not just the right kind.
        """
        self._seed(research=True)
        plan = gadkit.triage(self.repo, _config(), self.state)
        with mock.patch.object(gadkit, "gadkit_plugin_root", return_value=Path("/plugins/gad-kit/1.5.0")):
            argv = gadkit.command(plan)
        prompt = argv[-1]
        self.assertIn("gad-run.js", prompt)
        start = prompt.index("args: ") + len("args: ")
        args = json.loads(prompt[start : prompt.index("\n})", start)])
        self.assertEqual(args["maxGens"], 1)
        self.assertNotIn("genType", args)
        self.assertNotIn("gen", args)  # gad-run computes IDEATION_GEN itself


class InstalledGadKitArgContractTests(unittest.TestCase):
    """The one thing the offline fixtures above CANNOT prove: that the arg keys `command()` emits
    are the keys the bundled workflow scripts actually read. Everything else here asserts
    claude-relay's own JSON against claude-relay's own expectations — a renamed `genType` key (or a
    fourth accepted value) would leave every other test green while every recovery invocation
    silently ran as a `build` generation.

    Read-only, offline (it only reads local plugin files — no `claude`, no network) and SKIPPED
    when gad-kit is not installed, or when the newest installed copy predates genType entirely.
    """

    def setUp(self) -> None:
        try:
            self.root = gadkit.gadkit_plugin_root()
        except FileNotFoundError as exc:
            self.skipTest(f"gad-kit plugin not installed on this machine: {exc}")

    def test_every_script_path_command_emits_exists(self) -> None:
        for name in ("gad-run.js", "gad-generation.js", "gad-finish.js"):
            with self.subTest(script=name):
                self.assertTrue((self.root / "workflows" / name).is_file())

    def test_the_recovery_scripts_read_the_gen_type_key_claude_relay_sends(self) -> None:
        for name in ("gad-generation.js", "gad-finish.js"):
            source = (self.root / "workflows" / name).read_text(encoding="utf-8", errors="replace")
            if "genType" not in source:
                self.skipTest(f"installed {self.root.name}/{name} predates genType — nothing to check")
            with self.subTest(script=name):
                # the exact expression claude-relay's args have to satisfy: `cfg.genType`
                self.assertIn("genType", source)
                for gen_type in gadkit._RESEARCH_GEN_TYPES:
                    self.assertIn(
                        f"'{gen_type}'",
                        source,
                        f"{name} does not accept genType {gen_type!r} that claude-relay may send",
                    )


# ─────────────────────────────────────────────────────────────────────────────
# Status-vocabulary contract test: gad-kit's RESULT-line status vocabulary is a contract between
# the two repos, and it has drifted THREE times without any gate ever noticing — the v1.5->v2.0
# upgrade added IDEATED/IDEATION-FAILED, 2.1.0's orchestration work added NO-PROGRESS, 2.1.0's
# release work added REFUSED. Every one of those was found only by a human reading a diff. This
# extends the `InstalledGadKitArgContractTests` pattern above (read-only, offline, skip-gated) to
# that vocabulary instead of the arg keys.
# ─────────────────────────────────────────────────────────────────────────────

# Matches any quoted, screaming-kebab-ish token of 3+ characters: `'DIRTY-TREE'`, `"AGENT-DEAD"`,
# `'COMMITTED'`, etc. Deliberately NOT anchored to a specific JS construct (a first attempt that
# only matched lines containing the literal word "status"/"enum" MISSED real hits in practice —
# e.g. `const st = LIMIT_SUSPECTED ? 'LIMIT-SUSPECTED' : 'SURVEY-FAILED'` carries no "status"
# keyword on the same line as the literal it feeds into `return { status: st, ... }` two lines
# later). Deliberately OVER-inclusive instead (per the task's own instruction: a false positive
# here costs one line in `_STATUS_VOCABULARY_ALLOWLIST` below; a false negative is the whole bug
# this test exists to prevent): it also matches unrelated all-caps quoted tokens (nested verdict/
# gate enum values, a code-comment's hypothetical example, a script's own internal bookkeeping
# label, gad-init.js's scaffold statuses) — every one of those is individually accounted for in
# the allowlist below, never silently dropped by a narrower regex.
_STATUS_TOKEN_RE = re.compile(r"""['"]([A-Z][A-Z0-9]*(?:[_-][A-Z0-9]+)*)['"]""")


def _scrape_status_tokens(source: str) -> set[str]:
    """Every quoted, screaming-case-ish token (>=3 chars) anywhere in `source`."""
    return {m.group(1) for m in _STATUS_TOKEN_RE.finditer(source) if len(m.group(1)) >= 3}


class InstalledGadKitStatusVocabularyContractTests(unittest.TestCase):
    """Scrapes the INSTALLED gad-kit plugin's `workflows/*.js` for every quoted status-shaped
    token — covering both shapes the task calls out: the consolidator/child `status:` values (the
    `CONSOLIDATE_SCHEMA` enum, `deadAgentAbort()`'s `abortStatus`, gad-generation.js's own
    `AWAITING-OWNER` preflight abort, gad-finish.js's `REFUSED`) and gad-run.js's own returned
    `stopReason` values — and asserts `relay.detector` handles every one of them. "Handles" is
    defined precisely as: the token is a member of `detector._GAD_RUN_RESULT_STATUSES` (the actual
    registry `classify()`'s RESULT-line disambiguation consults — see that module's docstring,
    "Handled EXHAUSTIVELY") OR it is named in this test's own explicit, commented
    `_STATUS_VOCABULARY_ALLOWLIST` for a token that is genuinely not the model's own top-level
    RESULT-line status (a different schema field, a script claude-relay never invokes, a
    hypothetical example inside a code comment) — never a silent gap either way.

    Why `detector._GAD_RUN_RESULT_STATUSES` membership specifically, rather than a bare substring
    search over detector.py's source text: `detector.py` also contains the UNRELATED literal
    string `"BLOCKED"` at `if outcome == "BLOCKED":` (gadkit's disk-truth OUTCOME BUCKET name,
    lexically identical to gad-kit's own consolidator status of the same spelling, but a
    completely different vocabulary/namespace) — a plain text search would have been fooled into
    crediting that as "handling" the RESULT-line status `BLOCKED`, which is exactly the kind of
    false-negative-shaped bug this test exists to prevent. Importing the real Python frozenset and
    checking membership is precise where a source-grep would be ambiguous.

    CRITICAL ROBUSTNESS NOTE — direction of drift that matters (read this before treating a red
    run here as "the installed plugin is just newer/older, ignore it"): the INSTALLED plugin and
    the WORKING CHECKOUT can be, and right now ARE, different versions — the cache holds gad-kit
    2.0.0 while /home/dias/projects/gad-kit carries uncommitted 2.1.0 work (REFUSED, NO-PROGRESS)
    that is NOT installed yet. This test asserts against whatever `gadkit.gadkit_plugin_root()`
    resolves — the INSTALLED copy — because that is what production actually invokes; asserting
    against the checkout instead would test a version claude-relay never runs against.
      - BENIGN direction, must never fail this test: `detector._GAD_RUN_RESULT_STATUSES`
        containing MORE entries than the installed copy currently emits. This is the ordinary,
        expected state whenever the checkout is ahead of the cache (exactly NO-PROGRESS/REFUSED
        right now) — nothing here demands the reverse inclusion; see
        `_scrape_status_tokens()`/this test's own body, which only ever checks "does everything
        the installed copy CAN say get recognized," never "does detector.py's set exactly match
        what this one installed copy happens to use."
      - REAL-DEFECT direction, this test's whole reason to exist: a status the INSTALLED copy CAN
        emit that `detector.py` neither recognizes nor this test's allowlist excuses. That is
        exactly what a future gad-kit RENAMING or REMOVING a status also reduces to: a rename
        drops the old quoted token from the scrape (nothing asserts on a name nothing emits
        anymore — silently and correctly inert) and introduces a new quoted token, which then has
        to earn its own recognition or allowlist entry here exactly like any other addition; a
        removal is likewise just "the scrape no longer contains it," never a failure by itself.

    Read-only, offline (only reads local plugin files off disk — no `claude` process, no network)
    and SKIPPED (never failed) when gad-kit is not installed on this machine at all, reusing
    `gadkit.gadkit_plugin_root()` (the SAME resolution production uses) rather than reimplementing
    the glob.
    """

    # Tokens the deliberately over-inclusive regex above WILL find in workflows/*.js that are
    # genuinely NOT the model's own top-level RESULT-line status — each is an intentional
    # non-handling decision, not an oversight, with its specific reason recorded here.
    _STATUS_VOCABULARY_ALLOWLIST: dict[str, str] = {
        # gad-generation.js/gad-finish.js's PlanReview verdict enum (`verdict: {enum: ['APPROVED',
        # 'NEEDS REVISION', 'NEEDS MAJOR REVISION']}`) — a DIFFERENT structured-output field than
        # the top-level `status` gadkit.command() Step 3 tells the model to print; never itself
        # the RESULT line's content.
        "APPROVED": "a PlanReview verdict field value, not the top-level status field",
        # gad-generation.js/gad-finish.js's Verify-phase gate enum (`gate: {enum: ['GREEN',
        # 'RED']}`) — again a nested field, not the top-level status.
        "GREEN": "a Verify-phase gate field value, not the top-level status field",
        "RED": "a Verify-phase gate field value, not the top-level status field",
        # gad-generation.js (source comment, live-verified 2026-07-26): "a free string here let a
        # creative consolidator status (e.g. \"FAILED\") flow ..." — a hypothetical example INSIDE
        # a code comment illustrating the bug CONSOLIDATE_SCHEMA's hard enum fixes; gad-kit never
        # actually returns this value.
        "FAILED": "appears only inside a code comment as a hypothetical example, never returned",
        # gad-run.js's OWN internal bookkeeping variable when a CHILD (gad-generation.js/
        # gad-finish.js) invocation returns a malformed/null result: `const status = r && r.status
        # ? r.status : 'UNKNOWN'`. This is gad-run.js's LOCAL label for a child's status while it
        # decides whether to park/continue the crawl — never gad-run.js's own returned top-level
        # `status` (that is always `stopReason`, whose value set never includes 'UNKNOWN'), and
        # gad-generation.js/gad-finish.js never return 'UNKNOWN' as their own status either.
        "UNKNOWN": "gad-run.js's internal label for a malformed CHILD result, never a top-level RESULT",
        # gad-run.js's own internal `parked[].reason` label ("child returned some non-COMMITTED
        # status the schema does not otherwise recognize") — a diagnostic annotation on ONE parked
        # entry inside gad-run.js's own return value, never itself the top-level `status` field.
        "UNRECOGNIZED-STATUS": "gad-run.js's internal parked[].reason label, never a top-level RESULT status",
        # gad-run.js's own crawl-level stop reason for "a child hit a non-COMMITTED status and
        # stopOnBlocked is configured true." Structurally UNREACHABLE for claude-relay
        # specifically: `gadkit.command()`'s gad_run-mode wf_args never set `stopOnBlocked` at all
        # (see gadkit.py's `command()`), and gad-run.js's own default is `cfg.stopOnBlocked ===
        # true` (false unless explicitly set) — so claude-relay's own `/gad-run --max 1` crawl step
        # can never produce this RESULT line. If `command()` is ever changed to pass
        # `stopOnBlocked: true`, THIS ALLOWLIST ENTRY MUST BE REMOVED and 'STOPPED-ON-BLOCKED'
        # added to `detector._GAD_RUN_RESULT_STATUSES` instead.
        "STOPPED-ON-BLOCKED": "unreachable — claude-relay's own gad_run invocation never sets stopOnBlocked",
        # gad-init.js's scaffold-status enum (`SCAFFOLDED`/`PARTIAL`/`ABORTED`). claude-relay never
        # invokes gad-init.js at all — the only mention anywhere in relay/ is a user-facing
        # suggestion string ("run '/gad-init <repo>' first" in gadkit.triage()'s AWAITING_HUMAN
        # detail), never an actual Workflow call — so gad-init.js's RESULT-line vocabulary is out
        # of scope for this contract by construction.
        "SCAFFOLDED": "gad-init.js only — claude-relay never invokes gad-init.js",
        "PARTIAL": "gad-init.js only — claude-relay never invokes gad-init.js",
        "ABORTED": "gad-init.js only — claude-relay never invokes gad-init.js",
    }

    def setUp(self) -> None:
        try:
            self.root = gadkit.gadkit_plugin_root()
        except FileNotFoundError as exc:
            self.skipTest(f"gad-kit plugin not installed on this machine: {exc}")

    def test_every_status_the_installed_plugin_can_report_is_recognized_or_allowlisted(self) -> None:
        scripts = sorted(self.root.glob("workflows/*.js"))
        if not scripts:
            self.skipTest(f"installed plugin root {self.root} ships no workflows/*.js to scan")

        seen_in: dict[str, set[str]] = {}
        for script_path in scripts:
            source = script_path.read_text(encoding="utf-8", errors="replace")
            for token in _scrape_status_tokens(source):
                seen_in.setdefault(token, set()).add(script_path.name)

        recognized = detector._GAD_RUN_RESULT_STATUSES
        unhandled = {
            token: files
            for token, files in seen_in.items()
            if token not in recognized and token not in self._STATUS_VOCABULARY_ALLOWLIST
        }
        if not unhandled:
            return

        lines = [
            f"  {token!r} — seen in {', '.join(sorted(files))}" for token, files in sorted(unhandled.items())
        ]
        self.fail(
            "The installed gad-kit plugin (at "
            f"{self.root}) can report a status token that relay.detector neither recognizes "
            "(relay.detector._GAD_RUN_RESULT_STATUSES) nor this test explicitly allowlists as "
            "non-actionable:\n" + "\n".join(lines) + "\n\n"
            "This is the exact class of contract drift that has bitten claude-relay silently "
            "before (IDEATED/IDEATION-FAILED, NO-PROGRESS, REFUSED — each found only by a human "
            "reading a diff, never by a test). Fix: for each name above, either (a) add it to "
            "relay.detector._GAD_RUN_RESULT_STATUSES if classify()'s RESULT-line handling should "
            "recognize it as a real, reachable top-level status, or (b) add it to this test's "
            "InstalledGadKitStatusVocabularyContractTests._STATUS_VOCABULARY_ALLOWLIST with a "
            "comment explaining why claude-relay never needs to act on it (e.g. it belongs to a "
            "different schema field, or to a script claude-relay never invokes)."
        )


if __name__ == "__main__":
    unittest.main()
