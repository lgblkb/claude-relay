#!/usr/bin/env bash
# claude-relay installer: symlinks bin/claude-relay into ~/.local/bin, seeds
# ~/.claude-relay/config.toml from the example if it does not already exist, then runs
# verify.sh. No hardcoded paths — everything is derived from this script's own location and
# $HOME.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${HOME}/.claude-relay"
BIN_DIR="${HOME}/.local/bin"

mkdir -p "${BIN_DIR}" "${STATE_DIR}" "${STATE_DIR}/logs"

chmod +x "${REPO_DIR}/bin/claude-relay"
ln -sf "${REPO_DIR}/bin/claude-relay" "${BIN_DIR}/claude-relay"
echo "Installed claude-relay -> ${BIN_DIR}/claude-relay (symlink to ${REPO_DIR}/bin/claude-relay)"

if [ ! -f "${STATE_DIR}/config.toml" ]; then
  cp "${REPO_DIR}/config.example.toml" "${STATE_DIR}/config.toml"
  echo "Wrote default config to ${STATE_DIR}/config.toml — edit \`repo\` and [telegram] before your first run."
fi
chmod 600 "${STATE_DIR}/config.toml" 2>/dev/null || true

case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) echo "NOTE: ${BIN_DIR} is not on your PATH — add it (e.g. in ~/.bashrc) to run \`claude-relay\` directly." ;;
esac

echo
exec "${REPO_DIR}/verify.sh"
