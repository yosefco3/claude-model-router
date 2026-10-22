#!/usr/bin/env python3
"""model-router hook. Never blocks, always exits 0, stdlib only.

SessionStart:     injects the routing policy (POLICY.md).
UserPromptSubmit: injects a short routing reminder next to the user's message and
                  resets the per-session tool-call counter.
PostToolUse:      counts main-thread tool calls since the last user message or
                  the last delegation; after FIRST calls (then every EVERY) it
                  injects a nudge to hand the rest to a subagent.

Silent (no output, no state change) when
  * the call is inside a subagent (input has agent_id),
  * the router is off (file <CONFIG>/model-router/OFF exists, or env
    CLAUDE_ROUTER is off/0/false),
  * the script was started with --repo and this is not a cloud session
    (CLAUDE_CODE_REMOTE not truthy). Repo copies (.claude/model-router/) only act
    in cloud sessions; locally the plugin already covers the session.
"""
from __future__ import annotations

import json, os, re, sys, time
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
DATA_DIR = CONFIG_DIR / "model-router"
STATE_DIR = DATA_DIR / "state"
OFF_FLAG = DATA_DIR / "OFF"
FIRST, EVERY = 6, 10
DELEGATION_TOOLS = {"Agent", "Task"}

PROMPT_NUDGE = (
    "Model router: ON. The user's routing policy (model-router) applies to this request: lookups go "
    "to scout, fully specified edits to mechanic, bounded builds / tests / reviews to builder / "
    "test-writer / reviewer (independent briefs in parallel). Design, debugging without a repro, "
    "risky changes and the user's own prose stay with the main model."
)
WATCH_NUDGE = (
    "Model router: the main model has made {n} read/search/shell/web tool calls in a row since "
    "the user's last message or the last delegation. "
    "Under the user's routing policy, remaining lookups go to scout and bounded builds to "
    "builder; design and debugging stay with the main model."
)


def router_off() -> bool:
    if os.environ.get("CLAUDE_ROUTER", "").strip().lower() in {"off", "0", "false"}:
        return True
    return OFF_FLAG.exists()


def repo_copy_inactive() -> bool:
    """True for a --repo copy running outside a cloud session."""
    if "--repo" not in sys.argv[1:]:
        return False
    return os.environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() not in {"1", "true", "yes"}


def find_policy() -> str | None:
    here = Path(__file__).resolve().parent
    for d in (here, here.parent):
        p = d / "POLICY.md"
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    return None


def emit(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def prune_old_state() -> None:
    cutoff = time.time() - 7 * 86400
    for f in STATE_DIR.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def main() -> None:
    data = json.load(sys.stdin)
    if not isinstance(data, dict) or data.get("agent_id"):  # agent_id is set only inside subagents
        return
    if router_off() or repo_copy_inactive():
        return
    event = data.get("hook_event_name", "")

    if event == "SessionStart":
        policy = find_policy()
        if policy:
            emit(event, policy)
        return

    if event not in {"UserPromptSubmit", "PostToolUse"}:
        return
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id") or "unknown"))[:80] or "unknown"
    counter = STATE_DIR / sid

    if event == "UserPromptSubmit":
        try:  # state is best effort: the reminder must go out even if it fails
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            counter.write_text("0")
            prune_old_state()
        except Exception:
            pass
        emit(event, PROMPT_NUDGE)
    else:  # PostToolUse
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if data.get("tool_name") in DELEGATION_TOOLS:
            counter.write_text("0")
            return
        try:
            n = int(counter.read_text().strip() or 0) + 1
        except (OSError, ValueError):
            n = 1
        counter.write_text(str(n))
        if n == FIRST or (n > FIRST and (n - FIRST) % EVERY == 0):
            emit(event, WATCH_NUDGE.format(n=n))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
