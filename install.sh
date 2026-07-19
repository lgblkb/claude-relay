#!/usr/bin/env bash
# claude-relay installer — two auto-detected modes:
#
#   * LOCAL (run from a checkout, e.g. `./install.sh`): symlinks bin/claude-relay into
#     ~/.local/bin, seeds ~/.claude-relay/config.toml from config.example.toml, runs verify.sh.
#
#   * STANDALONE (`curl -fsSL https://raw.githubusercontent.com/lgblkb/claude-relay/main/install.sh | bash`):
#     installs from GitHub — prefers `uv tool install`, then `pipx install`, else clones to
#     ~/.local/share/claude-relay and symlinks. Then seeds config via `claude-relay init`.
#
# No hardcoded paths — everything derives from this script's own location and $HOME.
set -euo pipefail

REPO_URL="https://github.com/lgblkb/claude-relay.git"
GIT_SPEC="git+https://github.com/lgblkb/claude-relay"
STATE_DIR="${HOME}/.claude-relay"
BIN_DIR="${HOME}/.local/bin"

# Resolve this script's directory only when it is a real file on disk. Under `curl | bash`,
# BASH_SOURCE is not a readable path, so SCRIPT_DIR stays empty and we take the standalone path.
SOURCE="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=""
if [ -n "${SOURCE}" ] && [ -f "${SOURCE}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${SOURCE}")" && pwd)"
fi

seed_config() {  # $1 = optional path to a documented example config to copy
  mkdir -p "${STATE_DIR}" "${STATE_DIR}/logs"
  if [ ! -f "${STATE_DIR}/config.toml" ]; then
    if [ -n "${1:-}" ] && [ -f "${1}" ]; then
      cp "${1}" "${STATE_DIR}/config.toml"
    elif command -v claude-relay >/dev/null 2>&1; then
      claude-relay init >/dev/null 2>&1 || true
    fi
    echo "Wrote default config to ${STATE_DIR}/config.toml — edit \`repo\` and [telegram] before your first run."
  fi
  chmod 600 "${STATE_DIR}/config.toml" 2>/dev/null || true
}

path_note() {
  case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "NOTE: ${BIN_DIR} is not on your PATH — add it (e.g. in ~/.bashrc) to run \`claude-relay\` directly." ;;
  esac
}

# ---------------------------------------------------------------------------
# LOCAL checkout mode
# ---------------------------------------------------------------------------
if [ -n "${SCRIPT_DIR}" ] && [ -x "${SCRIPT_DIR}/bin/claude-relay" ] && [ -d "${SCRIPT_DIR}/relay" ]; then
  REPO_DIR="${SCRIPT_DIR}"
  mkdir -p "${BIN_DIR}"
  chmod +x "${REPO_DIR}/bin/claude-relay"
  ln -sf "${REPO_DIR}/bin/claude-relay" "${BIN_DIR}/claude-relay"
  echo "Installed claude-relay -> ${BIN_DIR}/claude-relay (symlink to ${REPO_DIR}/bin/claude-relay)"
  seed_config "${REPO_DIR}/config.example.toml"
  path_note
  echo
  exec "${REPO_DIR}/verify.sh"
fi

# ---------------------------------------------------------------------------
# STANDALONE (curl | bash) mode
# ---------------------------------------------------------------------------
echo "Installing claude-relay from ${REPO_URL} ..."
if command -v uv >/dev/null 2>&1; then
  uv tool install --force "${GIT_SPEC}"
  echo "Installed via uv tool."
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "${GIT_SPEC}"
  echo "Installed via pipx."
elif command -v git >/dev/null 2>&1; then
  # Fallback: clone + symlink. claude-relay is stdlib-only, so a checkout runs as-is.
  DEST="${HOME}/.local/share/claude-relay"
  mkdir -p "$(dirname "${DEST}")" "${BIN_DIR}"
  if [ -d "${DEST}/.git" ]; then
    git -C "${DEST}" pull --ff-only
  else
    git clone --depth 1 "${REPO_URL}" "${DEST}"
  fi
  chmod +x "${DEST}/bin/claude-relay"
  ln -sf "${DEST}/bin/claude-relay" "${BIN_DIR}/claude-relay"
  echo "Installed claude-relay -> ${BIN_DIR}/claude-relay (clone at ${DEST})."
else
  echo "error: need one of uv, pipx, or git to install." >&2
  echo "       Install uv (https://astral.sh/uv) or git, then re-run this installer." >&2
  exit 1
fi

seed_config ""
path_note
echo
if command -v claude-relay >/dev/null 2>&1; then
  echo "Smoke test — discovered seats:"
  claude-relay login-check || true
fi
echo
echo "Done. Next: edit ${STATE_DIR}/config.toml, then run \`claude-relay login-check\`."
