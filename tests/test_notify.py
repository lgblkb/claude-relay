"""Offline tests for relay.notify: token redaction, per-sink graceful degradation, the
resolve/status message regexes, and notify() dedupe/force/clear semantics. No real network
calls — any path that would hit Telegram is either the "missing config -> stdout fallback"
branch (genuinely offline) or `send_telegram`/`get_updates` themselves mocked out.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from relay import cooldown, notify
from relay.config import Config


def _state() -> dict:
    return cooldown.load_state(Path("/nonexistent-claude-relay-state.json"))


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
            out = notify.poll_telegram_updates(cfg, state, Path("/no/repo"), status_provider=lambda: "STATUS-OK")
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


if __name__ == "__main__":
    unittest.main()
