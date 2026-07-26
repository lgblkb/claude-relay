"""LIVE test for the `LS-2-telegram-sendmessage` seam.
Sends a REAL Telegram message via `notify.send_telegram()` — no mock.

Classified `operator-receipt` (metered/interactive, not auto-verifiable): it costs the
operator a real notification and requires their own bot_token/chat_id, so it NEVER runs
unattended. It requires BOTH:
  - [telegram] bot_token/chat_id configured (env or config.toml), AND
  - explicit opt-in via CLAUDE_RELAY_LIVE_TELEGRAM=1
Skips (does not fail) otherwise — this is exactly why it was never executed during this
generation's implementation (the task explicitly forbids sending a live Telegram message
during implementation).

Run explicitly once you want to spend the receipt:
  CLAUDE_RELAY_LIVE_TELEGRAM=1 python3 -m unittest tests_live.test_telegram_live -v
"""

from __future__ import annotations

import os
import unittest

from relay import config as config_mod
from relay import notify


class TelegramLiveTest(unittest.TestCase):
    def test_send_real_message(self) -> None:
        if os.environ.get("CLAUDE_RELAY_LIVE_TELEGRAM") != "1":
            self.skipTest(
                "operator-receipt seam: set CLAUDE_RELAY_LIVE_TELEGRAM=1 to actually send a "
                "live Telegram message (never run automatically/unattended)"
            )
        cfg = config_mod.load_config()
        if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
            self.skipTest("PROBE-SKIPPED: [telegram].bot_token/chat_id (or the env vars) are not configured")

        sent = notify.send_telegram(
            cfg.telegram_bot_token,
            cfg.telegram_chat_id,
            "claude-relay live-verification: real test message from tests_live/test_telegram_live.py",
        )
        self.assertTrue(sent)


if __name__ == "__main__":
    unittest.main()
