"""Offline tests for relay.gadkit: the triage() decision order against fixture `.gad/` repo
states (the feasibility-critical recovery-routing logic), command() argv construction,
outcome() bucketing, and resolve_owner_decision(). Uses real local `git` (no network) against
throwaway temp repos.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay import cooldown, gadkit
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

    def test_resolved_decision_no_longer_blocks(self) -> None:
        _seed_committed_gad_state(
            self.repo,
            {
                "project": "test",
                "nextGen": 2,
                "generations": [{"gen": 0}, {"gen": 1}],
                "ownerDecisions": [
                    {"id": "D1", "question": "pick a DB", "blocksGen": 2, "status": "resolved"}
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
        """The ONE genuine Verify-or-later artifact (reviews/verification.md — written only by
        the Verify-phase verifier, which can only run after Guardrails' test-writer succeeded)
        being present is both necessary AND sufficient to route to /gad-finish.
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
        plan = gadkit.triage(self.repo, _config(), self.state)
        self.assertEqual(plan.kind, "FINISH")
        self.assertEqual(plan.mode, "gad_finish")
        self.assertEqual(plan.gen, 1)
        # FINISH must not touch the tree at all (git-finish resumes verify itself).
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(self.repo), capture_output=True, text=True
        )
        self.assertNotEqual(status.stdout.strip(), "")

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
        self.assertEqual(result.decision["status"], "resolved")
        self.assertEqual(result.decision["resolution"], "use postgres")
        reloaded = gadkit.read_index(self.repo)
        assert reloaded is not None
        self.assertEqual(reloaded["ownerDecisions"][0]["status"], "resolved")

    def test_resolve_unknown_id_reports_not_found(self) -> None:
        _write_index(self.repo, {"project": "test", "nextGen": 1, "generations": [], "ownerDecisions": []})
        result = gadkit.resolve_owner_decision(self.repo, "NOPE", "answer")
        self.assertFalse(result.found)

    def test_resolve_with_no_index_reports_not_found(self) -> None:
        result = gadkit.resolve_owner_decision(self.repo, "D1", "answer")
        self.assertFalse(result.found)


if __name__ == "__main__":
    unittest.main()
