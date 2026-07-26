"""Back-channel: notify-out (push events to the operator) + resolve-in (durable, disk-backed
answers flow back), decoupled from any chat session (DESIGN.md §7).

notify-out sinks: `telegram` (default), `stdout`, `command`, `webhook`; `shellular` is kept as
an optional sink only, never depended on. Every send is deduped via `state.lastNotified`
(cooldown.py) so a steady-state condition (e.g. "all seats exhausted") doesn't spam the
operator every loop iteration.

resolve-in: `resolve_owner_decision()` (the actual disk edit) lives in gadkit.py — the only
module that knows generations-index.json's shape. This module calls it from two places: the
`claude-relay resolve` CLI command (bin/claude-relay) and the Telegram poller below, so both
paths go through the exact same function.

Only stdlib `urllib` is used for HTTP. The bot token appears only inside the request URL,
which is redacted before ever appearing in a log line.
"""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import cooldown

if TYPE_CHECKING:
    from .config import Config

TELEGRAM_API_BASE = "https://api.telegram.org"
_BOT_TOKEN_RE = re.compile(r"/bot[^/]+/")

_RESOLVE_RE = re.compile(r"^\s*resolve\s+(\S+)\s+(.+?)\s*$", re.IGNORECASE)
_STATUS_RE = re.compile(r"^\s*status\s*$", re.IGNORECASE)


def _redact(url: str) -> str:
    """Never let the bot token reach a log line, even in an error message."""
    return _BOT_TOKEN_RE.sub("/bot***/", url)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram transport (used by both the notify-out sink and the resolve-in poller)
# ─────────────────────────────────────────────────────────────────────────────


def send_telegram(bot_token: str, chat_id: str, text: str, timeout: float = 15.0) -> bool:
    """POST sendMessage. Returns True on a 2xx response, False (never raises) otherwise —
    a notifier failure must never crash the supervisor loop.
    """
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    # `url` is our own fixed https://api.telegram.org constant + bot_token/chat_id, not
    # attacker-controlled input — the flake8-bandit "audit url open" check is a false positive
    # for this fixed scheme, silenced explicitly on both the Request build and the open below.
    request = urllib.request.Request(url, data=payload, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            response.read()
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # B7 audit fix: a READ-phase failure (mid-stream RST, read timeout, malformed status
        # line) can raise a raw `TimeoutError`/`ConnectionResetError`/`http.client.BadStatusLine`
        # instead of `URLError` — none of which this function's "never raises" contract excluded
        # before. Caught here alongside the documented pair so nothing escapes (Invariant #3).
        print(f"[claude-relay] telegram sendMessage failed ({_redact(url)}): {exc}", file=sys.stderr)
        return False


def get_updates(bot_token: str, offset: int, timeout: float = 5.0) -> list[dict[str, Any]]:
    """`getUpdates` long-poll. `timeout` is Telegram's own long-poll wait (seconds); the local
    socket timeout is padded above it so our own read doesn't race Telegram's. Returns an
    empty list (never raises) on any failure — the poller is opportunistic and must be safe
    to call even when offline.
    """
    params = urllib.parse.urlencode({"timeout": int(timeout), "offset": offset})
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/getUpdates?{params}"
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - fixed https://api.telegram.org
    try:
        with urllib.request.urlopen(request, timeout=timeout + 10) as response:  # noqa: S310
            body = json.load(response)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        json.JSONDecodeError,
        OSError,
        http.client.HTTPException,
    ) as exc:
        # B7 audit fix: see `send_telegram`'s comment — a read-phase failure here previously
        # escaped raw and would have crash-looped the Telegram poller (and, via `loop.run`'s
        # missing top-level guard, the whole supervisor).
        print(f"[claude-relay] telegram getUpdates failed ({_redact(url)}): {exc}", file=sys.stderr)
        return []
    if not isinstance(body, dict) or not body.get("ok"):
        return []
    result = body.get("result")
    return result if isinstance(result, list) else []


# ─────────────────────────────────────────────────────────────────────────────
# notify-out: sinks + dedupe
# ─────────────────────────────────────────────────────────────────────────────


def _dispatch_command(config: Config, message: str) -> bool:
    command_template = getattr(config, "notify_command", None)
    if not command_template:
        print(f"[claude-relay] notify_sink=command but no [notify].command set; stdout fallback:\n{message}")
        return True
    try:
        subprocess.run(  # noqa: S602 - operator's own configured local command, not remote input
            command_template, shell=True, input=message, text=True, timeout=15, check=False
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[claude-relay] notify command failed: {exc}", file=sys.stderr)
        return False


def _dispatch_webhook(config: Config, message: str) -> bool:
    url = getattr(config, "notify_webhook_url", None)
    if not url:
        print(f"[claude-relay] notify_sink=webhook but no [notify].webhook_url set; stdout: {message}")
        return True
    payload = json.dumps({"text": message}).encode("utf-8")
    # `url` is an operator-configured local setting (config.toml), not remote/untrusted input.
    request = urllib.request.Request(  # noqa: S310
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            response.read()
        return True
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # B7 audit fix: same read-phase-failure gap as `send_telegram`.
        print(f"[claude-relay] notify webhook failed: {exc}", file=sys.stderr)
        return False


def _dispatch_shellular(config: Config, message: str) -> bool:
    """Optional sink, never depended on (DESIGN.md §7: shellular's own notify hook is
    one-way/local/undrained and is explicitly NOT part of the reliable back-channel). If the
    operator configured a `shellular_command`, run it the same way as the `command` sink;
    otherwise this is a documented no-op, not an error.
    """
    command_template = getattr(config, "shellular_command", None)
    if not command_template:
        print("[claude-relay] notify_sink=shellular but no [notify].shellular_command configured; no-op")
        return True
    return _dispatch_command_with(command_template, message)


def _dispatch_command_with(command_template: str, message: str) -> bool:
    try:
        subprocess.run(command_template, shell=True, input=message, text=True, timeout=15, check=False)  # noqa: S602
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[claude-relay] shellular command failed: {exc}", file=sys.stderr)
        return False


def dispatch(config: Config, message: str) -> bool:
    """Send `message` via `config.notify_sink`, degrading gracefully (never raising) if the
    chosen sink is misconfigured — Invariant #3 (graceful degradation is normal) extends to
    notification failures: a bad Telegram token must never crash the rotation loop.
    """
    sink = (config.notify_sink or "telegram").strip().lower()
    if sink == "stdout":
        print(message)
        return True
    if sink == "telegram":
        if not config.telegram_bot_token or not config.telegram_chat_id:
            print(f"[claude-relay] notify_sink=telegram but bot_token/chat_id missing; stdout: {message}")
            return True
        return send_telegram(config.telegram_bot_token, config.telegram_chat_id, message)
    if sink == "command":
        return _dispatch_command(config, message)
    if sink == "webhook":
        return _dispatch_webhook(config, message)
    if sink == "shellular":
        return _dispatch_shellular(config, message)
    print(f"[claude-relay] unknown notify_sink {sink!r}; stdout fallback:\n{message}", file=sys.stderr)
    return True


def notify(config: Config, state: dict[str, Any], key: str, message: str, *, force: bool = False) -> bool:
    """Send `message` unless `key` was already sent and not since cleared (deduped via
    `state.lastNotified`, DESIGN.md §6). Callers own clearing a key when its condition
    resolves (e.g. `cooldown.clear_notified(state, "needs-login:<seat>")` once that seat logs
    back in) — this module never guesses when a condition is "over."
    """
    if not force and cooldown.was_notified(state, key):
        return False
    sent = dispatch(config, message)
    if sent:
        cooldown.mark_notified(state, key)
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# resolve-in: Telegram reply poller (opportunistic, safe to disable/skip)
# ─────────────────────────────────────────────────────────────────────────────


def _help_text(repo: Path) -> str:
    """Self-documenting reply to any unrecognized operator message: the command menu plus the
    repo's current open decisions (id + question + the literal `resolve` line), so the operator
    never has to know the syntax cold — sending anything at all surfaces exactly what to do.
    """
    from . import gadkit  # deferred (same reason as in poll_telegram_updates)

    lines = [
        "claude-relay — I understand two commands:",
        "  • status — current seats + what I'm doing",
        "  • resolve <id> <answer> — answer an open decision",
        "",
    ]
    decisions = gadkit.open_owner_decisions(Path(repo))
    lines.append(
        gadkit.format_decisions_for_operator(decisions) if decisions else "No open decisions right now."
    )
    return "\n".join(lines)


def poll_telegram_updates(
    config: Config,
    state: dict[str, Any],
    repo: Path,
    *,
    status_provider: Callable[[], str] | None = None,
    timeout: float = 5.0,
) -> list[str]:
    """Opportunistically long-poll Telegram for operator replies and apply them. Recognizes:
      - `resolve <id> <answer>` -> `gadkit.resolve_owner_decision(repo, id, answer)` (the SAME
        disk-edit function `claude-relay resolve` uses), then replies with the outcome.
      - `status` -> replies with `status_provider()` (or a generic message if none given).
    Safe to call with no Telegram configured (returns immediately, no I/O) and safe to call
    on every loop iteration or none at all — this poller owns no state the rest of the tool
    depends on beyond the `telegramUpdateOffset` cursor (so skipping calls just delays
    processing operator replies, never loses them, until Telegram's own retention expires).
    """
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return []
    from . import gadkit  # deferred: keeps notify.py's import graph minimal for callers that

    # never touch Telegram/resolve-in at all (e.g. the pure notify() stdout/webhook path).

    offset = cooldown.get_telegram_offset(state)
    updates = get_updates(config.telegram_bot_token, offset=offset, timeout=timeout)
    processed: list[str] = []
    highest_seen = offset - 1
    help_sent = False  # at most one help reply per batch, so a flurry of chatter can't spam

    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            highest_seen = max(highest_seen, update_id)

        message = update.get("message") if isinstance(update, dict) else None
        text = message.get("text") if isinstance(message, dict) else None
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = str(chat.get("id")) if isinstance(chat, dict) and chat.get("id") is not None else None
        if not text or chat_id != str(config.telegram_chat_id):
            continue  # ignore messages from anyone but the configured operator chat

        resolve_match = _RESOLVE_RE.match(text)
        if resolve_match:
            decision_id, answer = resolve_match.group(1), resolve_match.group(2)
            result = gadkit.resolve_owner_decision(Path(repo), decision_id, answer)
            if result.found:
                question = (result.decision or {}).get("question", "")
                reply = f"resolved {decision_id}: {question}"
                if not result.committed:
                    # B13 audit fix, second round: the JSON write applied (the repo IS unblocked
                    # already) but the resolution's own git commit failed — a rejecting
                    # pre-commit hook or missing git identity in the repo. The reply must NEVER
                    # read as an ordinary clean resolution when this happens.
                    reply += (
                        " -- WARNING: applied but the commit FAILED (check the repo's git "
                        "identity / pre-commit hooks); claude-relay will keep retrying "
                        "automatically, but consider committing it by hand."
                    )
            else:
                reply = f"no open ownerDecision with id={decision_id!r} found in {result.index_path}"
            send_telegram(config.telegram_bot_token, config.telegram_chat_id, reply)
            processed.append(f"resolve {decision_id} -> found={result.found} committed={result.committed}")
            continue

        if _STATUS_RE.match(text):
            status_text = status_provider() if status_provider else "claude-relay is running."
            send_telegram(config.telegram_bot_token, config.telegram_chat_id, status_text)
            processed.append("status")
            continue

        # Anything else from the operator: reply with the self-documenting help menu (once per
        # batch) instead of silently ignoring it — so an unrecognized message teaches the syntax
        # + surfaces the open decisions, rather than leaving the operator guessing.
        if not help_sent:
            send_telegram(config.telegram_bot_token, config.telegram_chat_id, _help_text(Path(repo)))
            processed.append("help")
            help_sent = True

    if highest_seen >= offset:
        cooldown.set_telegram_offset(state, highest_seen + 1)
    return processed
