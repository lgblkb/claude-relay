"""Offline tests for relay.notify: token redaction, per-sink graceful degradation, the
resolve/status message regexes, and notify() dedupe/force/clear semantics. No real network
calls — any path that would hit Telegram is either the "missing config -> stdout fallback"
branch (genuinely offline) or `send_telegram`/`get_updates` themselves mocked out.
"""

from __future__ import annotations

import http.client
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from relay import cooldown, notify
from relay.config import Config


def _state() -> dict:
    return cooldown.load_state(Path("/nonexistent-claude-relay-state.json"))


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _init_gad_repo(repo: Path, decision_id: str = "D1") -> None:
    """A minimal committed `.gad`-bootstrapped repo with one OPEN ownerDecision — enough for
    `resolve_owner_decision()` (called transitively via `poll_telegram_updates`'s resolve
    branch) to find and answer it."""
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    gad = repo / ".gad"
    gad.mkdir()
    index = {
        "project": "t",
        "nextGen": 1,
        "generations": [],
        "ownerDecisions": [{"id": decision_id, "question": "pick a DB", "blocksGen": 1, "status": "open"}],
    }
    (gad / "generations-index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "seed .gad state")


def _update(update_id: int, text: str, chat_id: str = "123") -> dict:
    return {"update_id": update_id, "message": {"text": text, "chat": {"id": chat_id}}}


class RedactTests(unittest.TestCase):
    def test_redact_scrubs_bot_token_from_url(self) -> None:
        url = "https://api.telegram.org/bot123456:ABC-DEF_secret/sendMessage"
        redacted = notify._redact(url)
        self.assertNotIn("123456:ABC-DEF_secret", redacted)
        self.assertIn("/bot***/sendMessage", redacted)

    def test_redact_is_idempotent_on_urls_without_a_token(self) -> None:
        url = "https://example.com/hooks/claude-relay"
        self.assertEqual(notify._redact(url), url)


class DispatchGracefulDegradeTests(unittest.TestCase):
    def test_stdout_sink_always_succeeds(self) -> None:
        cfg = Config(notify_sink="stdout")
        self.assertTrue(notify.dispatch(cfg, "hello"))

    def test_telegram_sink_without_credentials_falls_back_to_stdout(self) -> None:
        cfg = Config(notify_sink="telegram", telegram_bot_token=None, telegram_chat_id=None)
        # Must not attempt any network call — no mock needed, this must stay fully offline.
        self.assertTrue(notify.dispatch(cfg, "hello"))

    def test_telegram_sink_with_credentials_calls_send_telegram(self) -> None:
        cfg = Config(notify_sink="telegram", telegram_bot_token="tok", telegram_chat_id="123")
        with mock.patch.object(notify, "send_telegram", return_value=True) as sent:
            self.assertTrue(notify.dispatch(cfg, "hello"))
        sent.assert_called_once_with("tok", "123", "hello")

    def test_command_sink_without_command_configured_falls_back(self) -> None:
        cfg = Config(notify_sink="command", notify_command=None)
        self.assertTrue(notify.dispatch(cfg, "hello"))

    def test_command_sink_runs_configured_command(self) -> None:
        cfg = Config(notify_sink="command", notify_command="cat >/dev/null")
        self.assertTrue(notify.dispatch(cfg, "hello"))

    def test_webhook_sink_without_url_falls_back(self) -> None:
        cfg = Config(notify_sink="webhook", notify_webhook_url=None)
        self.assertTrue(notify.dispatch(cfg, "hello"))

    def test_shellular_sink_without_command_is_a_documented_noop(self) -> None:
        cfg = Config(notify_sink="shellular", shellular_command=None)
        self.assertTrue(notify.dispatch(cfg, "hello"))

    def test_unknown_sink_falls_back_to_stdout(self) -> None:
        cfg = Config(notify_sink="carrier-pigeon")
        self.assertTrue(notify.dispatch(cfg, "hello"))


class B7ReadPhaseNetworkExceptionTests(unittest.TestCase):
    """B7 audit fix: `urlopen()` wraps CONNECT-phase failures as `URLError`, but a READ-phase
    failure (after the connection succeeded) can raise a raw `OSError` subclass or
    `http.client.HTTPException` instead — measured in practice as `TimeoutError`,
    `ConnectionResetError`, `http.client.BadStatusLine`. Every "never raises" notify.py entry
    point must swallow these exactly like the documented `(HTTPError, URLError)` pair.
    """

    def test_send_telegram_never_raises_on_a_connection_reset(self) -> None:
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=ConnectionResetError("rst")):
            self.assertFalse(notify.send_telegram("tok", "123", "hello"))

    def test_send_telegram_never_raises_on_a_bad_status_line(self) -> None:
        with mock.patch.object(
            notify.urllib.request, "urlopen", side_effect=http.client.BadStatusLine("garbage")
        ):
            self.assertFalse(notify.send_telegram("tok", "123", "hello"))

    def test_send_telegram_never_raises_on_a_bare_timeout(self) -> None:
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            self.assertFalse(notify.send_telegram("tok", "123", "hello"))

    def test_get_updates_never_raises_on_a_connection_reset(self) -> None:
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=ConnectionResetError("rst")):
            self.assertEqual(notify.get_updates("tok", offset=0), [])

    def test_dispatch_webhook_never_raises_on_a_connection_reset(self) -> None:
        cfg = Config(notify_sink="webhook", notify_webhook_url="https://example.com/hook")
        with mock.patch.object(notify.urllib.request, "urlopen", side_effect=ConnectionResetError("rst")):
            self.assertFalse(notify.dispatch(cfg, "hello"))


class ResolveAndStatusRegexTests(unittest.TestCase):
    def test_resolve_regex_captures_id_and_answer(self) -> None:
        match = notify._RESOLVE_RE.match("resolve D1 use postgres, ship it")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "D1")
        self.assertEqual(match.group(2), "use postgres, ship it")

    def test_resolve_regex_is_case_insensitive_and_tolerates_whitespace(self) -> None:
        match = notify._RESOLVE_RE.match("  RESOLVE   D2   yes   ")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "D2")
        self.assertEqual(match.group(2), "yes")

    def test_resolve_regex_does_not_match_bare_status(self) -> None:
        self.assertIsNone(notify._RESOLVE_RE.match("status"))

    def test_status_regex_matches_only_the_bare_word(self) -> None:
        self.assertIsNotNone(notify._STATUS_RE.match("status"))
        self.assertIsNotNone(notify._STATUS_RE.match("  STATUS  "))
        self.assertIsNone(notify._STATUS_RE.match("status please"))
        self.assertIsNone(notify._STATUS_RE.match("resolve D1 x"))


class NotifyDedupeTests(unittest.TestCase):
    def test_first_call_sends_second_call_deduped(self) -> None:
        cfg = Config(notify_sink="stdout")
        state = _state()
        self.assertTrue(notify.notify(cfg, state, "k1", "first"))
        self.assertFalse(notify.notify(cfg, state, "k1", "second"))  # deduped, same key

    def test_force_bypasses_dedupe(self) -> None:
        cfg = Config(notify_sink="stdout")
        state = _state()
        self.assertTrue(notify.notify(cfg, state, "k1", "first"))
        self.assertTrue(notify.notify(cfg, state, "k1", "second", force=True))

    def test_two_different_reason_hard_errors_both_send_via_force(self) -> None:
        """Finding #2 regression: HARD_ERROR must never be permanently swallowed — every
        occurrence uses force=True regardless of key repetition.
        """
        cfg = Config(notify_sink="stdout")
        state = _state()
        key = "hard-error:/some/repo"
        self.assertTrue(notify.notify(cfg, state, key, "first hard error", force=True))
        self.assertTrue(notify.notify(cfg, state, key, "second, different hard error", force=True))

    def test_clear_notified_allows_resend(self) -> None:
        cfg = Config(notify_sink="stdout")
        state = _state()
        notify.notify(cfg, state, "k1", "first")
        cooldown.clear_notified(state, "k1")
        self.assertTrue(notify.notify(cfg, state, "k1", "again"))

    def test_two_different_decision_parks_use_different_keys_and_both_send(self) -> None:
        """Finding #2 regression: a park key that includes the blocking decision id(s) means a
        DIFFERENT decision blocking the same repo/gen is never deduped against a stale key from
        a previously-resolved, different decision.
        """
        cfg = Config(notify_sink="stdout")
        state = _state()
        key_d1 = "park:/repo:AWAITING_HUMAN:2:D1"
        key_d2 = "park:/repo:AWAITING_HUMAN:2:D2"
        self.assertTrue(notify.notify(cfg, state, key_d1, "gated on D1"))
        self.assertTrue(notify.notify(cfg, state, key_d2, "gated on D2"))  # different key -> sends
        self.assertFalse(notify.notify(cfg, state, key_d1, "gated on D1 again"))  # same key -> deduped


class PollTelegramUpdatesTests(unittest.TestCase):
    """The resolve-in poller: status reply, the new help fallback (+ its once-per-batch dedupe),
    and chat-id gating. get_updates/send_telegram are mocked — no network. A nonexistent repo is
    fine: open_owner_decisions() returns [] so help degrades to 'No open decisions right now.'"""

    def _cfg(self) -> Config:
        return Config(notify_sink="telegram", telegram_bot_token="tok", telegram_chat_id="123")

    def test_status_message_replies_with_status_provider(self) -> None:
        cfg, state = self._cfg(), _state()
        with mock.patch.object(notify, "get_updates", return_value=[_update(10, "status")]), \
             mock.patch.object(notify, "send_telegram", return_value=True) as sent:
            out = notify.poll_telegram_updates(
                cfg, state, Path("/no/repo"), status_provider=lambda: "STATUS-OK"
            )
        self.assertEqual(out, ["status"])
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[2], "STATUS-OK")
        self.assertEqual(cooldown.get_telegram_offset(state), 11)  # advanced past update 10

    def test_unrecognized_message_replies_with_help(self) -> None:
        cfg, state = self._cfg(), _state()
        with mock.patch.object(notify, "get_updates", return_value=[_update(5, "hello there")]), \
             mock.patch.object(notify, "send_telegram", return_value=True) as sent:
            out = notify.poll_telegram_updates(cfg, state, Path("/no/repo"))
        self.assertIn("help", out)
        sent.assert_called_once()
        help_msg = sent.call_args.args[2]
        self.assertIn("resolve <id> <answer>", help_msg)
        self.assertIn("status", help_msg)

    def test_help_is_sent_at_most_once_per_batch(self) -> None:
        cfg, state = self._cfg(), _state()
        updates = [_update(1, "hi"), _update(2, "what can you do"), _update(3, "??")]
        with mock.patch.object(notify, "get_updates", return_value=updates), \
             mock.patch.object(notify, "send_telegram", return_value=True) as sent:
            out = notify.poll_telegram_updates(cfg, state, Path("/no/repo"))
        self.assertEqual(out.count("help"), 1)  # one help for the whole flurry
        sent.assert_called_once()
        self.assertEqual(cooldown.get_telegram_offset(state), 4)  # still advanced past all three

    def test_message_from_other_chat_is_ignored_but_offset_advances(self) -> None:
        cfg, state = self._cfg(), _state()
        updates = [_update(7, "status", chat_id="999"), _update(8, "hello", chat_id="999")]
        with mock.patch.object(notify, "get_updates", return_value=updates), \
             mock.patch.object(notify, "send_telegram", return_value=True) as sent:
            out = notify.poll_telegram_updates(cfg, state, Path("/no/repo"))
        self.assertEqual(out, [])
        sent.assert_not_called()
        self.assertEqual(cooldown.get_telegram_offset(state), 9)  # don't re-fetch ignored messages


class PollTelegramResolveCommitStatusTests(unittest.TestCase):
    """B13 audit fix, second round: the Telegram `resolve <id> <answer>` reply must never read
    as an ordinary clean resolution when the underlying commit failed — this drives
    `poll_telegram_updates()`'s REAL `resolve_owner_decision()` call against a real repo (not a
    mock of it), so the reply text is genuinely derived from `ResolveResult.committed`.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        _init_gad_repo(self.repo)

    def _cfg(self) -> Config:
        return Config(notify_sink="telegram", telegram_bot_token="tok", telegram_chat_id="123")

    def test_a_clean_resolution_reply_carries_no_warning(self) -> None:
        cfg, state = self._cfg(), _state()
        updates = [_update(1, "resolve D1 use postgres")]
        with (
            mock.patch.object(notify, "get_updates", return_value=updates),
            mock.patch.object(notify, "send_telegram", return_value=True) as sent,
        ):
            out = notify.poll_telegram_updates(cfg, state, self.repo)
        self.assertEqual(out, ["resolve D1 -> found=True committed=True"])
        reply = sent.call_args.args[2]
        self.assertIn("resolved D1", reply)
        self.assertNotIn("WARNING", reply)

    def test_a_failed_commit_reply_carries_a_loud_warning(self) -> None:
        hooks_dir = self.repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        cfg, state = self._cfg(), _state()
        updates = [_update(1, "resolve D1 use postgres")]
        with (
            mock.patch.object(notify, "get_updates", return_value=updates),
            mock.patch.object(notify, "send_telegram", return_value=True) as sent,
        ):
            out = notify.poll_telegram_updates(cfg, state, self.repo)
        self.assertEqual(out, ["resolve D1 -> found=True committed=False"])
        reply = sent.call_args.args[2]
        # The resolution DID apply (the operator should not think it was rejected outright)...
        self.assertIn("resolved D1", reply)
        # ...but a failure to commit must never be silently reported as an ordinary resolution.
        self.assertIn("WARNING", reply)
        self.assertIn("FAILED", reply)


if __name__ == "__main__":
    unittest.main()
