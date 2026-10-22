#!/usr/bin/env bash
# Installs the model-router agents and routing policy into ~/.claude (global, all projects).
# Safe to re-run: agents are overwritten, the policy block in CLAUDE.md is replaced in place.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
AGENTS_DIR="$CLAUDE_DIR/agents"
CLAUDE_MD="$CLAUDE_DIR/CLAUDE.md"
START='<!-- model-router:start -->'
END='<!-- model-router:end -->'

mkdir -p "$AGENTS_DIR"

echo "Installing agents to $AGENTS_DIR"
for f in "$HERE"/agents/*.md; do
  name="$(basename "$f")"
  if [ -f "$AGENTS_DIR/$name" ]; then
    cp "$AGENTS_DIR/$name" "$AGENTS_DIR/$name.bak"
  fi
  cp "$f" "$AGENTS_DIR/$name"
  echo "  $name"
done

DATA_DIR="$CLAUDE_DIR/model-router"
mkdir -p "$DATA_DIR"
cp "$HERE/ROUTING.md" "$HERE/ROUTING-OFF.md" "$DATA_DIR/"

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
cp "$HERE/router" "$BIN_DIR/router" && chmod +x "$BIN_DIR/router"
echo "Installed toggle: $BIN_DIR/router (router on|off|status)"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "  NOTE: $BIN_DIR is not on your PATH; add it or call it by full path.";; esac

echo "Updating $CLAUDE_MD"
touch "$CLAUDE_MD"
if grep -qF "$START" "$CLAUDE_MD"; then
  # replace existing block
  python3 - "$CLAUDE_MD" "$HERE/ROUTING.md" "$START" "$END" <<'PY'
import sys, re
path, src, start, end = sys.argv[1:5]
text = open(path, encoding="utf-8").read()
block = open(src, encoding="utf-8").read().strip()
pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
text = pattern.sub(lambda m: block, text, count=1)
open(path, "w", encoding="utf-8").write(text)
PY
  echo "  policy block replaced"
else
  cp "$CLAUDE_MD" "$CLAUDE_MD.bak" 2>/dev/null || true
  { [ -s "$CLAUDE_MD" ] && printf '\n\n'; cat "$HERE/ROUTING.md"; } >> "$CLAUDE_MD"
  echo "  policy block appended"
fi

echo
echo "Done. Routing is ON. Toggle with: router off / router on / router status"
echo "Start a new 'claude' session and run /agents to confirm scout, mechanic, builder, test-writer, reviewer are listed."
echo "Backups: *.bak next to any file that was overwritten."
