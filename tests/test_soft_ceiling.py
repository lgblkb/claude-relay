"""The soft usage ceiling (2026-07-30): the claude-relay half of gad-kit's unit-granular pause.

The mechanism, end to end, because no single test shows all of it:

1. `Config.launch_budget_for()` converts a seat's headroom below its synthetic ceiling into an
   OUTPUT-TOKEN allowance — or refuses to start the seat at all when that headroom is too small to
   fund one phase. `ceiling_pct` alone cannot do this: it only decides which seat to START on, and
   nothing stops a long run from consuming the rest of the window once it has begun (observed in
   the field: a seat with a 70% ceiling finishing a run at 97%).
2. `pick_seat()` skips seats that are not startable; `run_once()` stamps the allowance onto the
   plan, which `gadkit.command()` passes to the workflow as the `tokenAllowance` arg.
3. gad-kit compares `budget.spent()` against it at unit boundaries and returns `RESULT: PAUSED`.
4. `detector.classify()` recognises that as HEALTHY — rotate, never retry the spent seat — and
   flags it so `run_once()` cools the seat and `loop.run()` spares the HARD_ERROR breaker.
5. `_learn_tokens_per_percent()` closes the loop, pairing gad-kit's disk-written `budget.spent()`
   with relay's own observed percent delta so the conversion rate in (1) is measured, not guessed.

⚠️ Why the allowance travels as a workflow ARG and gad-kit gates on `budget.spent()` rather than
`budget.remaining()`: `budget.total` is hardcoded null in the shipped CLI. Verified by
disassembling the installed bundle (2.1.220) — the sandbox reads `total` from a module variable
initialized to null whose only writer has zero call sites in the entire binary, so
`budget.remaining()` is always Infinity no matter what the `+2M` directive says. `budget.spent()`
is the one member that works. `test_the_dead_harness_budget_is_not_relied_on` pins the consequence.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from relay import config as config_mod
from relay import cooldown, detector, fleet, gadkit, loop
from relay import usage as usage_mod
from relay.config import Config, SeatConfig


def _usage_at(percent: float) -> usage_mod.UsageSnapshot:
    return usage_mod.UsageSnapshot.from_json(
        {"limits": [{"kind": "session", "percent": percent, "severity": "normal", "is_active": True}]},
        fetched_at=time.time(),
    )


def _seat(name: str) -> fleet.Seat:
    return fleet.Seat(
        name=name, path=pathlib.Path(f"/fake/.claude-{name}"), has_creds=True, needs_login=False
    )


def _state() -> dict:
    return cooldown.load_state(pathlib.Path("/nonexistent-claude-relay-state.json"))


def _assistant_line(text: str) -> str:
    """A realistic `assistant` NDJSON envelope. A BARE "RESULT: X" string must not be used here:
    production tails are JSON envelopes and the detector deliberately refuses to read a raw line as
    a RESULT line — see tests/test_detector.py's own warning about that exact false premise, which
    this file originally tripped over.
    """
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
            "session_id": "sess-1",
        }
    )


def _snapshot(head: str = "abc", handoff: bool = False) -> gadkit.Snapshot:
    """A pre/post disk snapshot that says "nothing notable on disk" — so `outcome()` is decided by
    whatever the test varies (the usage reading, the tail) rather than by an incidental fixture."""
    return gadkit.Snapshot(
        head=head,
        next_gen=7,
        handoff_exists=handoff,
        open_decision_ids=frozenset(),
        census=None,
        backlog_exhausted=False,
    )


def _run_result(*texts: str) -> object:
    return loop.runner.RunResult(
        returncode=0,
        tail=[_assistant_line(t) for t in texts],
        log_path=pathlib.Path("/dev/null"),
        duration_s=1.0,
        timed_out=False,
    )


class _FakeCache:
    def __init__(self, readings: dict[str, usage_mod.UsageSnapshot]):
        self._readings = readings

    def poll(self, seat_dir, ttl: float = 90.0, force: bool = False):  # noqa: ANN001, ANN201
        key = str(seat_dir)
        if key not in self._readings:
            raise usage_mod.NeedsLoginError(f"no fixture reading for {key}")
        return self._readings[key]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Sizing the allowance
# ─────────────────────────────────────────────────────────────────────────────


class ParseTokenTargetTests(unittest.TestCase):
    def test_the_suffix_forms_operators_actually_write_all_parse(self) -> None:
        self.assertEqual(config_mod.parse_token_target("+2M"), 2_000_000)
        self.assertEqual(config_mod.parse_token_target("2m"), 2_000_000)
        self.assertEqual(config_mod.parse_token_target("+500k"), 500_000)
        self.assertEqual(config_mod.parse_token_target("500K"), 500_000)
        self.assertEqual(config_mod.parse_token_target("+350000"), 350_000)
        self.assertEqual(config_mod.parse_token_target("1_000_000"), 1_000_000)

    def test_unparseable_input_is_none_and_never_zero(self) -> None:
        """None means "no stated bound". Returning 0 would silently become a budget of nothing,
        which would make every generation pause before its first phase."""
        for bad in (None, "", "   ", "junk", "-5", "0", "+0"):
            self.assertIsNone(config_mod.parse_token_target(bad), f"{bad!r} should be None")


class LaunchBudgetTests(unittest.TestCase):
    def _cfg(self, **kw: object) -> Config:
        """Fixtures pin `min_token_target` explicitly wherever the floor is load-bearing. The
        SHIPPED default is 860_000 — a whole clean generation, see Config.min_token_target — which is
        deliberately far above these illustrative arithmetic cases; `ShippedDefaultsTests` covers the
        real defaults separately, since a fixture that quietly replaced them is exactly how the
        "no seat is ever startable" trap went unnoticed.
        """
        kw.setdefault("min_token_target", 80_000)
        cfg = Config(**kw)  # type: ignore[arg-type]
        cfg.seat_configs = {
            "sam": SeatConfig(ceiling_pct=50.0),
            "almas": SeatConfig(ceiling_pct=70.0),
        }
        return cfg

    def test_derivation_is_off_by_default_so_the_feature_ships_dormant(self) -> None:
        """Until an operator has a MEASURED rate for their accounts the whole feature must be a
        no-op — an unmeasured rate breaks the pool in one of three documented ways. Off means
        `startable` with NO allowance, i.e. unbounded, i.e. every gad-kit gate inert."""
        cfg = self._cfg()
        self.assertFalse(cfg.derive_token_target)
        for pct in (0.0, 97.0):
            budget = cfg.launch_budget_for("sam", pct)
            self.assertTrue(budget.startable)
            self.assertIsNone(budget.allowance)

    def test_headroom_below_the_ceiling_is_converted_to_tokens(self) -> None:
        cfg = self._cfg(
            derive_token_target=True, tokens_per_percent=10_000.0, headroom_safety_pct=5.0
        )
        # sam: ceiling 50 - current 10 - safety 5 = 35 percent of headroom * 10k = 350k.
        self.assertEqual(cfg.launch_budget_for("sam", 10.0).allowance, 350_000)
        # almas carries a different ceiling, so the same current percent yields a bigger budget.
        self.assertEqual(cfg.launch_budget_for("almas", 10.0).allowance, 550_000)

    def test_a_seat_too_close_to_its_ceiling_is_not_startable(self) -> None:
        """The livelock guard. Handing over an allowance too small to fund one phase would make
        gad-kit pause before Plan, relay rotate, the next seat pause before Plan too — a launch
        burned per seat with zero progress. `startable=False` tells the caller to skip it."""
        cfg = self._cfg(
            derive_token_target=True,
            tokens_per_percent=10_000.0,
            headroom_safety_pct=5.0,
            min_token_target=80_000,
        )
        # 50 - 46 - 5 is negative => 0 tokens => below the floor.
        self.assertFalse(cfg.launch_budget_for("sam", 46.0).startable)
        # 50 - 40 - 5 = 5 percent => 50k, still under the 80k floor.
        self.assertFalse(cfg.launch_budget_for("sam", 40.0).startable)
        # 50 - 37 - 5 = 8 percent => 80k, exactly at the floor, so it is allowed.
        self.assertEqual(cfg.launch_budget_for("sam", 37.0).allowance, 80_000)

    def test_a_non_startable_budget_explains_itself(self) -> None:
        """The reason string is the only thing an operator has at 3am to tell "the pool is idle
        because every seat is spent" apart from "the pool is idle because of a bad rate"."""
        cfg = self._cfg(derive_token_target=True, tokens_per_percent=10.0)
        reason = cfg.launch_budget_for("sam", 10.0).reason
        for expected in ("headroom", "ceiling", "floor"):
            self.assertIn(expected, reason)

    def test_an_unknown_current_percent_falls_back_to_unbounded_rather_than_guessing(self) -> None:
        cfg = self._cfg(derive_token_target=True)
        budget = cfg.launch_budget_for("sam", None)
        self.assertTrue(budget.startable)
        self.assertIsNone(budget.allowance)

    def test_the_configured_token_target_stays_a_hard_upper_bound(self) -> None:
        """Enabling derivation must only ever shrink a launch's budget, never grow it past what
        the operator globally allowed."""
        cfg = self._cfg(
            derive_token_target=True, tokens_per_percent=1_000_000.0, token_target="+500k"
        )
        self.assertEqual(cfg.launch_budget_for("sam", 0.0).allowance, 500_000)

    def test_per_seat_tokens_per_percent_handles_differing_subscription_tiers(self) -> None:
        """Accounts differ by plan: one percent of a Max 20x window holds several times the tokens
        of one percent of a Pro window, so this rate cannot be a single global constant."""
        cfg = self._cfg(derive_token_target=True, tokens_per_percent=1_000.0)
        cfg.seat_configs["sam"] = SeatConfig(ceiling_pct=50.0, tokens_per_percent=20_000.0)
        self.assertEqual(cfg.resolve_seat_tokens_per_percent("sam"), 20_000.0)
        self.assertEqual(cfg.resolve_seat_tokens_per_percent("almas"), 1_000.0)
        # sam: 45 percent of headroom * its OWN 20k rate.
        self.assertEqual(cfg.launch_budget_for("sam", 0.0).allowance, 900_000)

    def test_an_explicit_per_seat_rate_outranks_a_learned_one(self) -> None:
        """A pinned value is a statement of intent about a known tier; a noisy measurement must not
        silently override it."""
        cfg = self._cfg(derive_token_target=True, tokens_per_percent=1_000.0)
        cfg.seat_configs["sam"] = SeatConfig(ceiling_pct=50.0, tokens_per_percent=20_000.0)
        self.assertEqual(cfg.resolve_seat_tokens_per_percent("sam", learned=999_999.0), 20_000.0)
        # With no explicit per-seat rate, the learned value DOES win over [defaults].
        self.assertEqual(cfg.resolve_seat_tokens_per_percent("almas", learned=7_000.0), 7_000.0)
        # A nonsensical learned value is ignored rather than trusted.
        self.assertEqual(cfg.resolve_seat_tokens_per_percent("almas", learned=0.0), 1_000.0)
        self.assertEqual(cfg.resolve_seat_tokens_per_percent("almas", learned=-3.0), 1_000.0)

class ShippedDefaultsTests(unittest.TestCase):
    """Exercises `Config()`'s LITERAL defaults, with no fixture substitution.

    This class exists because its absence hid a critical bug: every other test here injects a
    `tokens_per_percent` and a `min_token_target`, so nobody ever asked what the shipped numbers do.
    They could not fund a single seat — `(70 ceiling - 5 safety) * 1200 = 78,000` against what was
    then an 80,000 floor — so flipping `derive_token_target = true` would have made EVERY seat
    unstartable at EVERY usage percent, idling the pool permanently and with no way out: a seat that
    never launches never writes a calibration record, so it can never learn the rate that would have
    rescued it.
    """

    def test_the_defaults_cannot_silently_idle_the_pool(self) -> None:
        """The unmeasured default rate must FAIL LOUDLY when derivation is switched on, not look
        healthy and quietly stop working."""
        cfg = Config()
        cfg.derive_token_target = True
        with self.assertRaises(config_mod.ConfigError) as caught:
            config_mod._validate(cfg)
        message = str(caught.exception)
        # The error has to name the fix, not just the symptom.
        self.assertIn("tokens_per_percent", message)
        self.assertIn("calibration.jsonl", message)

    def test_a_measured_rate_makes_seats_startable_again(self) -> None:
        cfg = Config()
        cfg.derive_token_target = True
        cfg.tokens_per_percent = 30_000.0
        config_mod._validate(cfg)  # must not raise
        budget = cfg.launch_budget_for("any", 0.0)
        self.assertTrue(budget.startable)
        assert budget.allowance is not None
        self.assertGreaterEqual(budget.allowance, cfg.min_token_target)

    def test_a_pinned_per_seat_rate_can_satisfy_the_check_alone(self) -> None:
        """An operator who knows one account's tier should be able to enable the feature for it
        without first raising the global default."""
        cfg = Config()
        cfg.derive_token_target = True
        cfg.seat_configs = {"big": SeatConfig(tokens_per_percent=30_000.0)}
        config_mod._validate(cfg)  # must not raise

    def test_the_defaults_are_inert_while_derivation_is_off(self) -> None:
        """The same numbers that are rejected above must be perfectly legal with the switch off —
        otherwise every existing installation would fail to load."""
        config_mod._validate(Config())

    def test_the_floor_covers_a_whole_clean_generation(self) -> None:
        """The floor is the PRIMARY enforcement mechanism, because gad-kit's pre-Verify gates are
        disabled for resume safety and a generation whose Verify is clean on the first iteration
        never consults the allowance at all. So the floor must cover gad-kit's full PHASE_TOKENS for
        a clean run — Plan 60k + PlanReview 90k + Prep 60k + Implement 300k + Guardrails 120k +
        Verify 150k + Consolidate 80k = 860k. Drop it materially below that and the run sails past
        every disabled gate while the allowance bounds nothing, which is the exact overshoot this
        feature exists to close.
        """
        self.assertGreaterEqual(Config().min_token_target, 860_000)


class SoftCeilingConfigValidationTests(unittest.TestCase):
    def _load(self, body: str) -> Config:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "config.toml"
            path.write_text(body, encoding="utf-8")
            return config_mod.load_config(path)

    def test_the_new_keys_round_trip_from_toml(self) -> None:
        cfg = self._load(
            "[defaults]\n"
            "tokens_per_percent = 2500\n"
            "headroom_safety_pct = 3\n"
            "min_token_target = 90000\n"
            "derive_token_target = true\n"
            "[seats.sam]\n"
            "ceiling_pct = 50\n"
            "tokens_per_percent = 30000\n"
        )
        self.assertEqual(cfg.tokens_per_percent, 2500.0)
        self.assertEqual(cfg.headroom_safety_pct, 3.0)
        self.assertEqual(cfg.min_token_target, 90_000)
        self.assertTrue(cfg.derive_token_target)
        self.assertEqual(cfg.resolve_seat_tokens_per_percent("sam"), 30_000.0)

    def test_a_nonpositive_rate_is_rejected_rather_than_silently_halting_the_pool(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            self._load("[defaults]\ntokens_per_percent = 0\n")
        with self.assertRaises(config_mod.ConfigError):
            self._load("[defaults]\ntokens_per_percent = -1\n")
        with self.assertRaises(config_mod.ConfigError):
            self._load("[seats.sam]\ntokens_per_percent = 0\n")

    def test_other_new_bounds_are_validated(self) -> None:
        with self.assertRaises(config_mod.ConfigError):
            self._load("[defaults]\nheadroom_safety_pct = -1\n")
        with self.assertRaises(config_mod.ConfigError):
            self._load("[defaults]\nmin_token_target = 0\n")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Getting the allowance to the workflow
# ─────────────────────────────────────────────────────────────────────────────


class TokenAllowanceArgTests(unittest.TestCase):
    """`tokenAllowance` in the workflow args is the ONLY channel by which the number reaches the
    decision — a script has no network and no filesystem — so its presence and its absence are
    both load-bearing."""

    def _args_for(self, mode: str, allowance: int | None) -> dict:
        plan = gadkit.Plan(
            kind="RUN", repo=pathlib.Path("/fake/repo"), gen=7, mode=mode, token_allowance=allowance
        )
        argv = gadkit.command(plan)
        blob = "\n".join(argv)
        # The prompt embeds a JS-style object literal — `args: { "repo": ... }` — not a JSON
        # key, so the brace scan starts from the unquoted `args:`.
        start = blob.index("args: {")
        depth, end = 0, None
        for i in range(start + len("args: "), len(blob)):
            if blob[i] == "{":
                depth += 1
            elif blob[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        assert end is not None, blob
        return json.loads(blob[start + len("args: ") : end])

    def test_every_mode_carries_the_allowance(self) -> None:
        """An allowance is a property of the LAUNCH, not of a generation, so all three scripts a
        launch can start must respect the same number."""
        for mode in ("gad_run", "gad_generation", "gad_finish"):
            self.assertEqual(self._args_for(mode, 350_000).get("tokenAllowance"), 350_000, mode)

    def test_no_allowance_means_the_key_is_absent_not_null(self) -> None:
        """gad-kit reads "unbounded" from the key being MISSING. A literal null would flow into its
        `Number.isFinite()` check as a falsy value that happens to work, but a 0 would not — and
        omitting the key keeps the args byte-identical to what gad-run itself passes, which is the
        invariant `command()` is written around."""
        for mode in ("gad_run", "gad_generation", "gad_finish"):
            self.assertNotIn("tokenAllowance", self._args_for(mode, None), mode)

    def test_the_dead_harness_budget_is_not_relied_on(self) -> None:
        """Pins the reason this arg exists at all (see the module docstring): the launch prompt's
        `+2M` token-target sentence is inert prose, so it must never be the enforcement path. If
        someone ever deletes `tokenAllowance` and leans on the directive again, this fails."""
        args = self._args_for("gad_run", 350_000)
        self.assertIn("tokenAllowance", args)


# ─────────────────────────────────────────────────────────────────────────────
# 3./4. Recognising a pause
# ─────────────────────────────────────────────────────────────────────────────


class PausedResultStatusTests(unittest.TestCase):
    def _classify(self, status: str, outcome: str = "AGENT_DEAD_NONLIMIT") -> detector.Action:
        return detector.classify(outcome, None, [_assistant_line(f"RESULT: {status}")])

    def test_a_pause_rotates_instead_of_retrying_the_spent_seat(self) -> None:
        for status in ("PAUSED", "PAUSED-ON-BUDGET"):
            action = self._classify(status)
            self.assertEqual(
                action.kind,
                detector.CONTINUE_ROTATE,
                f"{status} must rotate; retrying re-hits the same gate on the same seat",
            )
            self.assertFalse(action.no_retry)
            self.assertTrue(action.paused)

    def test_a_pause_after_a_commit_is_still_flagged_as_a_pause(self) -> None:
        """The case `kind` alone cannot express, and the one that bites: gad-run commits generation
        N and pauses on N+1, so `outcome()` says PROGRESSED and the AGENT_DEAD branch never runs.
        The commit is real progress (kind stays CONTINUE) but the SEAT is just as spent."""
        action = self._classify("PAUSED-ON-BUDGET", outcome="PROGRESSED")
        self.assertEqual(action.kind, detector.CONTINUE)
        self.assertTrue(action.paused)

    def test_an_ordinary_commit_is_not_flagged_as_a_pause(self) -> None:
        self.assertFalse(detector.classify("PROGRESSED", None, []).paused)
        self.assertFalse(
            detector.classify("PROGRESSED", None, [_assistant_line("RESULT: COMPLETED")]).paused
        )

    def test_a_pause_is_in_the_known_vocabulary_so_it_is_never_unrecognized(self) -> None:
        """The vocabulary set is documented as exhaustive, and drift in it has silently bitten
        this project before — an unknown status degrades into a generic retry."""
        for status in ("PAUSED", "PAUSED-ON-BUDGET", "GENERATION-THREW"):
            self.assertIn(status, detector._GAD_RUN_RESULT_STATUSES)
            self.assertNotIn(
                "UNRECOGNIZED", self._classify(status).reason.upper().replace("-", "")
            )

    def test_gad_runs_cannot_afford_to_start_exit_is_a_pause_not_a_failure(self) -> None:
        """BUDGET-EXHAUSTED regression (2026-07-30). It is gad-run's "I cannot afford to START the
        next generation" exit, and it was unreachable dead code until the soft ceiling landed —
        its gate read `budget.total && budget.remaining() < perGenTokens`, and `budget.total` is
        permanently null. Rebuilding that gate on `budget.spent()` made it live, at which point
        being a plain member of `_GAD_RUN_RESULT_STATUSES` classified it as an ordinary RETRY: no
        cooldown (run_once's `_force_cooldown` is keyed on CONTINUE_ROTATE), so pick_seat re-selects
        the same seat — whose percent has barely moved, because the run exited before doing any
        work — it exhausts again at once, and three iterations later the HARD_ERROR breaker parks
        the entire repo. A healthy budget stop must never look like a crash loop.
        """
        action = self._classify("BUDGET-EXHAUSTED")
        self.assertEqual(action.kind, detector.CONTINUE_ROTATE)
        self.assertTrue(action.paused)
        self.assertFalse(action.no_retry)

    def test_a_child_generation_that_threw_is_still_retryable(self) -> None:
        """GENERATION-THREW is a real failure, unlike a pause — the crawl merely reported it
        cleanly. It must NOT be treated as a healthy rotation."""
        action = self._classify("GENERATION-THREW")
        self.assertEqual(action.kind, detector.RETRY)
        self.assertFalse(action.paused)

    def test_a_genuinely_unknown_status_still_falls_back_to_a_plain_retry(self) -> None:
        action = self._classify("SOMETHING-NEW-NOBODY-TAUGHT-US")
        self.assertEqual(action.kind, detector.RETRY)
        self.assertIn("UNRECOGNIZED", action.reason.upper())


class PausedBreakerTests(unittest.TestCase):
    """A pause must not count toward the HARD_ERROR breaker. Several seats pausing in a row is the
    EXPECTED steady state once a fleet sits near its ceilings, so counting them would park the repo
    on healthy operation — precisely the outcome the soft ceiling exists to prevent."""

    def test_the_breaker_reset_condition_admits_a_pause(self) -> None:
        paused_rotate = detector.Action(detector.CONTINUE_ROTATE, "paused", paused=True)
        plain_rotate = detector.Action(detector.CONTINUE_ROTATE, "dead agent")
        # The wall-hit predicate alone does NOT cover a pause (outcome is AGENT_DEAD_NONLIMIT, not
        # HIT_WALL), which is why loop.run() has to consult `action.paused` as well.
        self.assertFalse(
            loop._is_genuine_wall_hit_rotation(paused_rotate.kind, "AGENT_DEAD_NONLIMIT")
        )
        self.assertTrue(paused_rotate.paused)
        self.assertFalse(plain_rotate.paused)


# ─────────────────────────────────────────────────────────────────────────────
# 2b. The launch gate in pick_seat / run_once
# ─────────────────────────────────────────────────────────────────────────────


class LaunchGateTests(unittest.TestCase):
    def _cfg(self) -> Config:
        """'spent' carries a LOW ceiling, which is what lets it be simultaneously the lowest-percent
        seat (so every pre-existing preference picks it) and short of headroom (so only the new
        token floor rejects it). Two seats with the same ceiling cannot express that: lowest percent
        and most headroom would be the same seat, and the gate would be untestable against the
        selection it is meant to override.
        """
        cfg = Config()
        cfg.derive_token_target = True
        cfg.tokens_per_percent = 30_000.0
        cfg.seat_configs = {
            "rich": SeatConfig(ceiling_pct=70.0),
            "spent": SeatConfig(ceiling_pct=25.0),
        }
        return cfg

    # 'spent' at 18%: under its 20% start cap (25 - 5 margin), so every pre-existing check passes
    # it, but only 2% of headroom is left below its ceiling (25 - 18 - 5 safety) = 60k tokens,
    # far under the 860k floor. 'rich' at 30%: 35% of headroom * 30k = 1.05M, comfortably startable.
    _SPENT_PCT = 18.0
    _RICH_PCT = 30.0

    def test_a_seat_below_the_token_floor_is_not_selected(self) -> None:
        rich, spent = _seat("rich"), _seat("spent")
        cache = _FakeCache(
            {str(rich.path): _usage_at(self._RICH_PCT), str(spent.path): _usage_at(self._SPENT_PCT)}
        )
        seat, _usage, notes = loop.pick_seat([spent, rich], _state(), cache, self._cfg())
        self.assertIsNotNone(seat)
        assert seat is not None
        self.assertEqual(seat.name, "rich", f"notes={notes}")
        self.assertTrue(
            any("below-token-floor" in n and "spent" in n for n in notes),
            f"expected a below-token-floor note for 'spent', got {notes}",
        )

    def test_with_derivation_off_the_same_seat_is_selected_as_before(self) -> None:
        """The gate must be bit-for-bit inert until an operator opts in — so with derivation off the
        SAME fixture picks the seat the old lowest-percent rule always picked."""
        rich, spent = _seat("rich"), _seat("spent")
        cfg = self._cfg()
        cfg.derive_token_target = False
        cache = _FakeCache(
            {str(rich.path): _usage_at(self._RICH_PCT), str(spent.path): _usage_at(self._SPENT_PCT)}
        )
        seat, _usage, notes = loop.pick_seat([spent, rich], _state(), cache, cfg)
        assert seat is not None
        self.assertEqual(seat.name, "spent")
        self.assertFalse(any("below-token-floor" in n for n in notes))

    def test_no_startable_seat_leaves_the_pool_waiting_rather_than_launching(self) -> None:
        """The whole pool short of headroom must WAIT, not launch something it cannot fund."""
        spent = _seat("spent")
        cache = _FakeCache({str(spent.path): _usage_at(self._SPENT_PCT)})
        seat, usage, notes = loop.pick_seat([spent], _state(), cache, self._cfg())
        self.assertIsNone(seat)
        self.assertIsNone(usage)
        self.assertTrue(any("below-token-floor" in n for n in notes), f"notes={notes}")


class RunOnceAllowanceTests(unittest.TestCase):
    """`run_once()` stamping the allowance onto the plan is the one line that makes the whole
    feature real — without it the config plumbing is unused and gad-kit never sees a number."""

    def _run(self, cfg: Config, seat_percent: float):  # noqa: ANN202
        repo = pathlib.Path("/fake/repo")
        seat = _seat("rich")
        plan = gadkit.Plan(kind="RUN", repo=repo, gen=7, mode="gad_run")
        captured: dict[str, object] = {}

        def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
            captured["argv"] = argv
            return _run_result("RESULT: PAUSED-ON-BUDGET")

        snap = _snapshot()
        with (
            mock.patch.object(loop.gadkit, "triage", return_value=plan),
            mock.patch.object(loop.fleet, "discover_seats", return_value=[seat]),
            mock.patch.object(loop, "sync_seat_login_state"),
            mock.patch.object(loop.gadkit, "snapshot", return_value=snap),
            mock.patch.object(loop.runner, "run", side_effect=_fake_run),
            mock.patch.object(loop.plugins, "resolve_plugin_dirs", return_value=[]),
        ):
            cache = _FakeCache({str(seat.path): _usage_at(seat_percent)})
            result = loop.run_once(repo, cfg, _state(), cache)  # type: ignore[arg-type]
        return result, "\n".join(captured.get("argv") or [])  # type: ignore[arg-type]

    def _cfg(self, derive: bool) -> Config:
        cfg = Config()
        cfg.derive_token_target = derive
        cfg.tokens_per_percent = 30_000.0
        cfg.seat_configs = {"rich": SeatConfig(ceiling_pct=70.0)}
        return cfg

    def test_the_allowance_reaches_the_workflow_args(self) -> None:
        _result, argv_blob = self._run(self._cfg(derive=True), 20.0)
        # 70 - 20 - 5 = 45 percent of headroom * 30k.
        self.assertIn('"tokenAllowance": 1350000', argv_blob)

    def test_with_derivation_off_no_allowance_is_passed(self) -> None:
        _result, argv_blob = self._run(self._cfg(derive=False), 20.0)
        self.assertNotIn("tokenAllowance", argv_blob)

    def test_a_pause_cools_the_seat_off(self) -> None:
        """Without this the next iteration re-picks the same seat, whose fresh allowance is now
        near zero, and it pauses again at the same gate — one launch burned per iteration."""
        repo = pathlib.Path("/fake/repo")
        seat = _seat("rich")
        plan = gadkit.Plan(kind="RUN", repo=repo, gen=7, mode="gad_run")
        state = _state()
        snap = _snapshot()

        def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN202
            return _run_result("RESULT: PAUSED")

        with (
            mock.patch.object(loop.gadkit, "triage", return_value=plan),
            mock.patch.object(loop.fleet, "discover_seats", return_value=[seat]),
            mock.patch.object(loop, "sync_seat_login_state"),
            mock.patch.object(loop.gadkit, "snapshot", return_value=snap),
            mock.patch.object(loop.runner, "run", side_effect=_fake_run),
            mock.patch.object(loop.plugins, "resolve_plugin_dirs", return_value=[]),
        ):
            cache = _FakeCache({str(seat.path): _usage_at(20.0)})
            result = loop.run_once(repo, self._cfg(derive=True), state, cache)  # type: ignore[arg-type]

        assert result.action is not None
        self.assertTrue(result.action.paused)
        self.assertTrue(
            cooldown.is_in_cooldown(state, seat.path),
            "a paused seat must be cooled off; its synthetic ceiling is nowhere near 100% so "
            "nothing else in the pipeline would ever cool it",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Closing the loop: learning the rate
# ─────────────────────────────────────────────────────────────────────────────


class CalibrationReadTests(unittest.TestCase):
    def _repo(self, tmp: str, lines: list[str]) -> pathlib.Path:
        repo = pathlib.Path(tmp)
        (repo / ".gad").mkdir(parents=True, exist_ok=True)
        gadkit.calibration_path(repo).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return repo

    def test_the_last_good_record_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                tmp,
                [
                    json.dumps({"spentOutputTokens": 111, "status": "OLD", "gensCommitted": 1}),
                    json.dumps({"spentOutputTokens": 222, "status": "PAUSED-ON-BUDGET", "gensCommitted": 2}),
                ],
            )
            cal = gadkit.read_calibration(repo)
            assert cal is not None
            self.assertEqual(cal.spent_output_tokens, 222)
            self.assertEqual(cal.status, "PAUSED-ON-BUDGET")
            self.assertEqual(cal.gens_committed, 2)
            self.assertEqual(cal.records, 2)
            self.assertEqual(gadkit.calibration_record_count(repo), 2)

    def test_one_bad_append_does_not_discard_a_good_earlier_record(self) -> None:
        """The file is written by an agent appending a line, so a truncated or garbage line is a
        realistic failure mode — and it must not cost us the calibration we already had."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                tmp,
                [
                    json.dumps({"spentOutputTokens": 333, "status": "COMPLETED"}),
                    '{"spentOutputTokens": 44',  # truncated mid-write
                    "not json at all",
                    json.dumps({"status": "no tokens key"}),
                    json.dumps({"spentOutputTokens": -5}),
                    json.dumps({"spentOutputTokens": "banana"}),
                ],
            )
            cal = gadkit.read_calibration(repo)
            assert cal is not None
            self.assertEqual(cal.spent_output_tokens, 333)

    def test_a_missing_file_is_simply_no_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            self.assertIsNone(gadkit.read_calibration(repo))
            self.assertEqual(gadkit.calibration_record_count(repo), 0)


class LearnTokensPerPercentTests(unittest.TestCase):
    def _learn(
        self,
        *,
        spent: int = 400_000,
        pre_pct: float = 10.0,
        post_pct: float = 50.0,
        before: int = 0,
        write_record: bool = True,
        previous: float | None = None,
    ) -> tuple[dict, float | None]:
        state = _state()
        seat = _seat("rich")
        if previous is not None:
            cooldown.update_seat(state, seat.path, tokens_per_percent=previous)
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            if write_record:
                (repo / ".gad").mkdir(parents=True, exist_ok=True)
                gadkit.calibration_path(repo).write_text(
                    json.dumps({"spentOutputTokens": spent, "status": "PAUSED-ON-BUDGET", "gensCommitted": 1})
                    + "\n",
                    encoding="utf-8",
                )
            loop._learn_tokens_per_percent(
                state, seat, repo, before, _usage_at(pre_pct), _usage_at(post_pct)
            )
        return state, cooldown.learned_tokens_per_percent(state, seat.path)

    def test_a_clean_observation_is_learned(self) -> None:
        """400k output tokens over 40 percent of the window = 10k per percent."""
        _state_after, learned = self._learn(spent=400_000, pre_pct=10.0, post_pct=50.0)
        self.assertEqual(learned, 10_000.0)

    def test_a_second_observation_is_blended_not_replaced(self) -> None:
        """Individual runs are noisy and this number decides whether seats are startable at all, so
        it must drift toward the truth rather than lurch to the latest sample."""
        _state_after, learned = self._learn(
            spent=400_000, pre_pct=10.0, post_pct=50.0, previous=20_000.0
        )
        assert learned is not None
        self.assertEqual(round(learned), round(0.7 * 20_000 + 0.3 * 10_000))
        self.assertLess(learned, 20_000.0)
        self.assertGreater(learned, 10_000.0)

    def test_a_stale_record_from_an_earlier_launch_is_not_learned_from(self) -> None:
        """Pairing THIS run's percent delta with a PREVIOUS run's token count invents a rate from
        two unrelated measurements. The record count is what distinguishes them."""
        _state_after, learned = self._learn(before=1)
        self.assertIsNone(learned)

    def test_no_record_at_all_teaches_nothing_and_is_not_an_error(self) -> None:
        _state_after, learned = self._learn(write_record=False)
        self.assertIsNone(learned)

    def test_a_percent_delta_too_small_to_divide_by_is_rejected(self) -> None:
        """The usage gauge is coarse; dividing real spend by a 1% delta manufactures a rate from
        pure quantization noise."""
        _state_after, learned = self._learn(pre_pct=10.0, post_pct=11.0)
        self.assertIsNone(learned)

    def test_a_window_that_reset_mid_run_is_rejected(self) -> None:
        """post < pre means the five-hour window rolled over, so the delta is meaningless rather
        than merely small — and a negative divisor would produce a negative rate."""
        _state_after, learned = self._learn(pre_pct=60.0, post_pct=5.0)
        self.assertIsNone(learned)

    def test_an_implausible_rate_is_discarded_rather_than_learned(self) -> None:
        """Too LOW a learned rate is the dangerous direction: it makes the seat unstartable, so the
        pool silently goes idle. Better to keep the previous value than to accept a bad sample."""
        _state_after, low = self._learn(spent=100, pre_pct=10.0, post_pct=90.0)
        self.assertIsNone(low)
        _state_after, high = self._learn(spent=500_000_000, pre_pct=10.0, post_pct=90.0)
        self.assertIsNone(high)

    def test_a_learned_rate_survives_a_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "state.json"
            state = _state()
            seat = _seat("rich")
            cooldown.update_seat(state, seat.path, tokens_per_percent=12_345.6)
            cooldown.save_state(path, state)
            reloaded = cooldown.load_state(path)
            self.assertAlmostEqual(
                cooldown.learned_tokens_per_percent(reloaded, seat.path) or 0.0, 12_345.6, places=1
            )

    def test_a_garbage_learned_value_in_state_is_ignored(self) -> None:
        """State is a hand-editable JSON file, and a garbage rate here would either idle the whole
        pool or hand out budgets that blow through the ceiling this feature exists to respect."""
        state = _state()
        seat = _seat("rich")
        cooldown.update_seat(state, seat.path, has_creds=True)
        entry = cooldown.get_seat_state(state, seat.path)
        for bad in ("banana", None, 0, -1, float("inf"), float("nan")):
            entry["tokensPerPercent"] = bad
            self.assertIsNone(
                cooldown.learned_tokens_per_percent(state, seat.path), f"{bad!r} should be ignored"
            )


if __name__ == "__main__":
    unittest.main()
