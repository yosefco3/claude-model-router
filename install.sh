#!/usr/bin/env bash
# Installs the model-router CLI and its support files. Safe to re-run.
#
#   router                 -> ~/.local/bin/router
#   router_stats.py        -> <config>/model-router/
#   router_kit.py          -> <config>/model-router/
#   plugin/                -> <config>/model-router/kit   (used by `router install-repo`)
#
# It does NOT touch ~/.claude/agents, settings.json or CLAUDE.md. The router itself
# ships as a Claude Code plugin; the commands to install it are printed at the end.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
DATA_DIR="$CONFIG_DIR/model-router"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$DATA_DIR" "$BIN_DIR"

cp "$HERE/router" "$BIN_DIR/router"
chmod +x "$BIN_DIR/router"
echo "Installed CLI:   $BIN_DIR/router"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "  NOTE: $BIN_DIR is not on your PATH; add it or call it by full path.";; esac

cp "$HERE/router_stats.py" "$HERE/router_kit.py" "$DATA_DIR/"
echo "Installed tools: $DATA_DIR/router_stats.py, router_kit.py"

rm -rf "$DATA_DIR/kit"
cp -R "$HERE/plugin" "$DATA_DIR/kit"
find "$DATA_DIR/kit" -name __pycache__ -type d -prune -exec rm -rf {} +
echo "Installed kit:   $DATA_DIR/kit"

cat <<'MSG'

CLI installed. Now install the plugin itself (this is what delegates, in every session):

  claude plugin marketplace add yosefco3/claude-model-router
  claude plugin install model-router@claude-model-router

or on claude.ai: Customize -> Plugins -> Add -> Add marketplace -> yosefco3/claude-model-router
(that route also reaches Cowork and syncs to every machine signed in to the account).

Then:  router status | router off | router on | router stats | router install-repo <path>
MSG
