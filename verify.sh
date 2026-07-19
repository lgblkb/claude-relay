#!/usr/bin/env bash
# claude-relay verify.sh — OFFLINE checks only (no network call is required to pass):
#   1. python3 >= 3.11 (stdlib tomllib)
#   2. `claude` on PATH
#   3. >= 1 usable seat discovered (via relay.fleet directly — dogfoods the real code)
#   4. the gad-kit plugin is installed for Claude Code
#   5. Telegram config present (warn only — Telegram is the default sink but not required to
#      pass verify; `stdout`/`command`/`webhook` sinks work without it)
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATUS=0

ok()   { printf '  [OK]   %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; STATUS=1; }

echo "claude-relay verify"
echo "===================="

# 1. python3 >= 3.11
if command -v python3 >/dev/null 2>&1; then
  PYVER="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    ok "python3 ${PYVER} (>= 3.11, tomllib available)"
  else
    fail "python3 ${PYVER} is below 3.11 — stdlib tomllib unavailable; upgrade Python (claude-relay is stdlib-only and will not vendor a TOML parser)"
  fi
else
  fail "python3 not found on PATH"
fi

# 2. claude on PATH
if command -v claude >/dev/null 2>&1; then
  ok "claude found: $(command -v claude)"
else
  fail "claude not found on PATH — install the Claude Code CLI first"
fi

# 3 + 4 + 5: delegate to the real library so this check exercises the actual discovery code
# (no reimplementation drift), entirely offline (no network I/O happens below).
python3 - "$REPO_DIR" <<'PYEOF'
import json
import sys
from pathlib import Path

repo_dir = sys.argv[1]
sys.path.insert(0, repo_dir)

from relay import config as config_mod
from relay import fleet

RESULTS = []


def ok(msg):
    RESULTS.append(("OK", msg))


def warn(msg):
    RESULTS.append(("WARN", msg))


def fail(msg):
    RESULTS.append(("FAIL", msg))


try:
    cfg = config_mod.load_config()
except config_mod.ConfigError as exc:
    fail(f"config.toml failed to parse: {exc}")
    cfg = config_mod.Config()

seats = fleet.discover_seats(cfg.effective_exclude())
usable = [s for s in seats if s.usable]
needs_login = [s for s in seats if s.needs_login]

if not seats:
    fail("no seat directories discovered (looked for ~/.claude-* with .credentials.json); log in to at least one named seat")
elif not usable:
    fail(f"{len(seats)} seat(s) discovered but none are usable (all need login): {[s.name for s in needs_login]}")
else:
    ok(f"{len(usable)} usable seat(s): {[s.name for s in usable]}" + (f"; needs-login: {[s.name for s in needs_login]}" if needs_login else ""))

# gad-kit plugin present (portable: derived from Path.home(), never a hardcoded path)
plugin_cache = Path.home() / ".claude" / "plugins" / "cache" / "gad-kit"
installed_plugins_path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
plugin_found = plugin_cache.is_dir()
if not plugin_found and installed_plugins_path.exists():
    try:
        installed = json.loads(installed_plugins_path.read_text(encoding="utf-8"))
        plugins = installed.get("plugins", {}) if isinstance(installed, dict) else {}
        plugin_found = any(str(key).startswith("gad-kit@") for key in plugins)
    except (OSError, json.JSONDecodeError):
        pass
if plugin_found:
    ok("gad-kit plugin found for Claude Code")
else:
    fail("gad-kit plugin not found (checked ~/.claude/plugins/cache/gad-kit and installed_plugins.json) — install it before running claude-relay")

# Telegram config (warn only)
if cfg.telegram_bot_token and cfg.telegram_chat_id:
    ok("Telegram bot_token + chat_id configured")
else:
    warn("Telegram not configured ([telegram].bot_token/chat_id or CLAUDE_RELAY_TELEGRAM_BOT_TOKEN/_CHAT_ID) — notify_sink=telegram will fall back to stdout until set")

for level, msg in RESULTS:
    tag = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}[level]
    print(f"  {tag} {msg}")

sys.exit(1 if any(level == "FAIL" for level, _ in RESULTS) else 0)
PYEOF
PYTHON_CHECKS_STATUS=$?
if [ "${PYTHON_CHECKS_STATUS}" -ne 0 ]; then
  STATUS=1
fi

echo "===================="
if [ "${STATUS}" -eq 0 ]; then
  echo "claude-relay verify: PASS"
else
  echo "claude-relay verify: FAIL (see [FAIL] lines above)"
fi
exit "${STATUS}"
