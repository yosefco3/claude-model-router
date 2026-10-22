#!/usr/bin/env python3
"""model-router hook. Never blocks, always exits 0, stdlib only.

SessionStart:     injects the routing policy (POLICY.md) and leaves a per-session
                  marker (<state>/<session>.policy).
UserPromptSubmit: injects a short routing reminder next to the user's message and
                  resets the per-session tool-call counter. If the marker is
                  missing (SessionStart was skipped, e.g. the router was off or
                  the plugin was installed mid-session) the policy is delivered
                  first, once.
PostToolUse:      counts main-thread tool calls since the last user message or
                  the last delegation; after FIRST calls (then every EVERY) it
                  injects a nudge to hand the rest to a subagent. The counter is
                  updated under an flock, so parallel hooks cannot lose counts.

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

try:
    import fcntl
except ImportError:  # non-POSIX: no locking
    fcntl = None

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


def remote_session() -> bool:
    return os.environ.get("CLAUDE_CODE_REMOTE", "").strip().lower() in {"1", "true", "yes"}


def repo_copy_inactive() -> bool:
    """True for a --repo copy running outside a cloud session."""
    return "--repo" in sys.argv[1:] and not remote_session()


def plugin_yields_to_repo_copy(data: dict) -> bool:
    """True for the plugin copy (no --repo) in a cloud session whose project has its own copy."""
    if "--repo" in sys.argv[1:] or not remote_session():
        return False
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd")
    if not project or not isinstance(project, str):
        return False
    try:
        repo_script = Path(project) / ".claude" / "model-router" / "router_nudge.py"
        return repo_script.is_file() and repo_script.resolve() != Path(__file__).resolve()
    except OSError:
        return False


def find_policy() -> str | None:
    here = Path(__file__).resolve().parent
    for d in (here, here.parent):
        p = d / "POLICY.md"
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    return None


def emit(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def locked_update(path: Path, fn) -> int:
    """Read the integer in `path` (0 if missing or garbled), store fn(old) and return it.

    The read-modify-write runs under an exclusive flock, so parallel hook processes
    for one session each see a distinct value.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            old = int(os.read(fd, 64).decode("ascii", "replace").strip() or 0)
        except ValueError:
            old = 0
        new = fn(old)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(new).encode("ascii"))
        if fcntl:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return new
    finally:
        os.close(fd)


def claim_policy_marker(marker: Path) -> bool:
    """Create the per-session policy marker; True only for the caller that created it."""
    try:
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
        return True
    except OSError:
        return False


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
    if router_off() or repo_copy_inactive() or plugin_yields_to_repo_copy(data):
        return
    event = data.get("hook_event_name", "")
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("session_id") or "unknown"))[:80] or "unknown"
    counter = STATE_DIR / sid
    marker = STATE_DIR / (sid + ".policy")  # "." is stripped from sid, so it cannot collide with a counter

    if event == "SessionStart":
        policy = find_policy()
        if policy:
            try:  # best effort: the policy must go out even if the marker cannot be written
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                marker.write_text("1")
            except Exception:
                pass
            emit(event, policy)
        return

    if event not in {"UserPromptSubmit", "PostToolUse"}:
        return

    if event == "UserPromptSubmit":
        text = PROMPT_NUDGE
        try:  # state is best effort: the reminder must go out even if it fails
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            locked_update(counter, lambda _: 0)
            if os.path.lexists(marker):
                os.utime(marker)  # a live session keeps its marker out of the 7-day prune
            else:
                policy = find_policy()
                if policy and claim_policy_marker(marker):  # SessionStart was skipped: deliver the policy now
                    text = policy + "\n\n" + PROMPT_NUDGE
            prune_old_state()
        except Exception:
            pass
        emit(event, text)
    else:  # PostToolUse
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if data.get("tool_name") in DELEGATION_TOOLS:
            locked_update(counter, lambda _: 0)
            return
        n = locked_update(counter, lambda old: old + 1)
        if n == FIRST or (n > FIRST and (n - FIRST) % EVERY == 0):
            emit(event, WATCH_NUDGE.format(n=n))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
