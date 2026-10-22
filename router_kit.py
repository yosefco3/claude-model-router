#!/usr/bin/env python3
"""router_kit.py - install the model router into a project repo for cloud sessions.

Cloud Claude Code sessions do not load plugins or ~/.claude; they only see the
repo's own .claude/. `install-repo` copies the router kit there:

    <repo>/.claude/agents/<the five agents>.md
    <repo>/.claude/model-router/router_nudge.py
    <repo>/.claude/model-router/POLICY.md
    <repo>/.claude/model-router/manifest.json   (sha256 of the agent files we installed)
    <repo>/.claude/settings.json                (router hooks merged in, everything else kept)

usage: router_kit.py install-repo <repo_path> [--kit <dir>] [--force]

Idempotent, and no half installs: everything is read and validated in memory
before the first file is written. An agent file that already exists and is
neither our untouched earlier copy nor identical to the new one is treated as
foreign and blocks the install; --force replaces it after saving <name>.bak.
Never commits. The only git call is the read-only `git check-ignore`, used for a
warning. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOOK_COMMAND = 'python3 "$CLAUDE_PROJECT_DIR/.claude/model-router/router_nudge.py" --repo'
HOOK_MARKER = "model-router/router_nudge.py"
HOOK_TIMEOUT = 10
POST_TOOL_MATCHER = "Read|Grep|Glob|Bash|WebFetch|WebSearch|Agent|Task"
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse")
MANIFEST_NAME = "manifest.json"


class KitError(Exception):
    pass


def default_kit_dir() -> Path:
    local = Path(__file__).resolve().parent / "plugin"
    if local.is_dir():
        return local
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return config / "model-router" / "kit"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _line(state: str, path: Path, repo: Path) -> str:
    return f"{state:<9}  {path.relative_to(repo)}"


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via a temp file in the same directory + os.replace; keeps mode and symlinks."""
    if path.is_symlink():
        path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_existing(path: Path) -> bytes | None:
    """Bytes of an existing regular file, None if absent, KitError for anything else."""
    if not os.path.lexists(path):
        return None
    if not path.is_file():
        raise KitError(f"{path} exists and is not a regular file")
    return path.read_bytes()


# ---- settings.json ---------------------------------------------------------

def _detect_indent(text: str):
    """Indent of the first indented line (JSON strings cannot contain raw newlines)."""
    m = re.search(r"^([ \t]+)\S", text, re.M)
    return m.group(1) if m else 2


def _has_router_hook(groups) -> bool:
    """True if any hook command in these groups already points at our script. Never raises."""
    if not isinstance(groups, list):
        return False
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if isinstance(hook, dict) and HOOK_MARKER in str(hook.get("command", "")):
                return True
    return False


def merge_settings(settings, label: str = "settings.json") -> bool:
    """Validate the shape, then add one router group per event unless one is there.

    Mutates `settings` in memory only. Returns True if anything was added. Raises
    KitError, before anything has been changed, for a shape we cannot merge into.
    """
    if not isinstance(settings, dict):
        raise KitError(f"{label}: top level is not a JSON object")
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise KitError(f'{label}: "hooks" is not an object')

    for event in EVENTS:  # validate everything we are about to touch first
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise KitError(f'{label}: "hooks.{event}" is not a list')
        for i, group in enumerate(groups):
            if not isinstance(group, dict):
                raise KitError(f'{label}: "hooks.{event}[{i}]" is not an object')
            if not isinstance(group.get("hooks"), list):
                raise KitError(f'{label}: "hooks.{event}[{i}].hooks" is not a list')
            for j, hook in enumerate(group["hooks"]):
                if not isinstance(hook, dict):
                    raise KitError(f'{label}: "hooks.{event}[{i}].hooks[{j}]" is not an object')

    changed = False
    for event in EVENTS:
        groups = hooks.get(event, [])
        if _has_router_hook(groups):
            continue
        group: dict = {}
        if event == "PostToolUse":
            group["matcher"] = POST_TOOL_MATCHER
        group["hooks"] = [{"type": "command", "command": HOOK_COMMAND, "timeout": HOOK_TIMEOUT}]
        groups.append(group)
        hooks[event] = groups
        changed = True
    if changed:
        settings["hooks"] = hooks
    return changed


# ---- manifest --------------------------------------------------------------

def _load_manifest(path: Path) -> dict[str, str]:
    """agent file name -> sha256 of what we installed. An unreadable or odd manifest counts as empty."""
    try:
        agents = json.loads(path.read_text(encoding="utf-8")).get("agents", {})
        return {k: v for k, v in agents.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def _backup_path(path: Path) -> Path:
    bak = path.with_name(path.name + ".bak")
    n = 1
    while os.path.lexists(bak):
        n += 1
        bak = path.with_name(f"{path.name}.bak.{n}")
    return bak


# ---- install ---------------------------------------------------------------

def install_repo(kit_dir: Path | str, repo_path: Path | str, force: bool = False) -> list[str]:
    try:
        return _install_repo(Path(kit_dir), Path(repo_path), force)
    except OSError as e:
        raise KitError(f"filesystem error: {e}") from e


def _install_repo(kit: Path, repo: Path, force: bool) -> list[str]:
    if not repo.is_dir():
        raise KitError(f"not a directory: {repo}")
    repo = repo.resolve()

    # ---- phase 1: read and validate everything; nothing is written here ----
    agent_files = sorted((kit / "agents").glob("*.md"))
    nudge_src = kit / "hooks" / "router_nudge.py"
    policy_src = kit / "POLICY.md"
    if not agent_files or not nudge_src.is_file() or not policy_src.is_file():
        raise KitError(f"router kit incomplete or missing at {kit} (re-run install.sh, or pass --kit <dir>)")

    claude = repo / ".claude"
    agents_dir = claude / "agents"
    data_dir = claude / "model-router"
    for d in (claude, agents_dir, data_dir):
        if os.path.lexists(d) and not d.is_dir():
            raise KitError(f"{d} exists and is not a directory")

    settings_path = claude / "settings.json"
    old_settings = _read_existing(settings_path)
    indent = 2
    if old_settings is None:
        settings: object = {}
    else:
        try:
            text = old_settings.decode("utf-8")
            settings = json.loads(text)
        except ValueError as e:
            raise KitError(f"{settings_path} is not valid JSON ({e}); fix it and re-run") from e
        indent = _detect_indent(text)
    settings_changed = merge_settings(settings, str(settings_path.relative_to(repo)))

    manifest_path = data_dir / MANIFEST_NAME
    manifest = _load_manifest(manifest_path)

    plan = []  # (new bytes, destination, state)
    conflicts = []
    new_hashes: dict[str, str] = {}
    for src in agent_files:
        new = src.read_bytes()
        sha = _sha256(new)
        dst = agents_dir / src.name
        cur = _read_existing(dst)
        new_hashes[src.name] = sha
        if cur is None:
            state = "created"
        elif _sha256(cur) == sha:
            state = "unchanged"
        elif _sha256(cur) == manifest.get(src.name):
            state = "updated"  # our own earlier copy, untouched
        else:
            state = "replaced"  # foreign or hand-edited
            conflicts.append(dst)
        plan.append((new, dst, state))

    if conflicts and not force:
        names = "\n".join(f"  {c.relative_to(repo)}" for c in conflicts)
        raise KitError(
            "refusing to overwrite agent files that are not untouched router copies "
            f"(foreign or edited by hand):\n{names}\n"
            "Nothing was written. Re-run with --force to replace them (each is saved as <name>.bak first), "
            "or move them away."
        )

    extras = []  # (new bytes, destination, state)
    for src, dst in ((nudge_src, data_dir / "router_nudge.py"), (policy_src, data_dir / "POLICY.md")):
        new = src.read_bytes()
        cur = _read_existing(dst)
        extras.append((new, dst, "created" if cur is None else "unchanged" if cur == new else "updated"))

    manifest_bytes = (json.dumps({"version": 1, "agents": {**manifest, **dict(sorted(new_hashes.items()))}},
                                 indent=2) + "\n").encode("utf-8")
    old_manifest = _read_existing(manifest_path)
    manifest_state = "created" if old_manifest is None else "unchanged" if old_manifest == manifest_bytes else "updated"

    settings_bytes = (json.dumps(settings, indent=indent, ensure_ascii=False) + "\n").encode("utf-8")

    # ---- phase 2: write ----
    report: list[str] = []
    for new, dst, state in plan:
        if state == "replaced":
            bak = _backup_path(dst)
            _atomic_write(bak, dst.read_bytes())
            report.append(_line("backup", bak, repo))
        if state != "unchanged":
            _atomic_write(dst, new)
        report.append(_line(state, dst, repo))
    if manifest_state != "unchanged":
        _atomic_write(manifest_path, manifest_bytes)
    report.append(_line(manifest_state, manifest_path, repo))
    for new, dst, state in extras:
        if state != "unchanged":
            _atomic_write(dst, new)
        report.append(_line(state, dst, repo))
    if old_settings is None or settings_changed:
        _atomic_write(settings_path, settings_bytes)
        report.append(_line("created" if old_settings is None else "updated", settings_path, repo))
    else:
        report.append(_line("unchanged", settings_path, repo))
    return report


def repo_warnings(repo_path: Path | str) -> list[str]:
    """Non-fatal problems that would stop cloud sessions from seeing the install."""
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        return [f"{repo} has no .git; cloud sessions only see what you commit and push to the repo's remote"]
    git = shutil.which("git")
    if not git:
        return []
    try:
        r = subprocess.run([git, "-C", str(repo), "check-ignore", "-q", ".claude/settings.json"],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode == 0:
        return [".claude/settings.json is git-ignored, so cloud sessions will not get the hooks; "
                "remove the ignore rule or add the files with `git add -f .claude`"]
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="router_kit.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("install-repo", help="install the router into a repo's .claude/ for cloud sessions")
    p.add_argument("repo_path")
    p.add_argument("--kit", help="router kit directory (default: ./plugin next to this script, else <CONFIG>/model-router/kit)")
    p.add_argument("--force", action="store_true",
                   help="replace agent files that are foreign or edited by hand (saved as <name>.bak first)")
    args = ap.parse_args(argv)

    try:
        kit = Path(args.kit) if args.kit else default_kit_dir()
        report = install_repo(kit, args.repo_path, force=args.force)
    except KitError as e:
        print(f"router: {e}", file=sys.stderr)
        return 1
    print(f"Installed model router into {Path(args.repo_path).resolve()}")
    for line in report:
        print(f"  {line}")
    print("Commit .claude/ so cloud sessions pick it up (this tool never commits).")
    for w in repo_warnings(args.repo_path):
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
