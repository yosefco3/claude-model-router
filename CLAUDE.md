# claude-model-router

A Claude Code plugin and marketplace that routes light tasks (lookups, bounded builds, fully specified edits) to cheaper haiku/sonnet subagents. A hook injects a routing policy at session start and nudges the main model to delegate. MIT licensed; the plugin version is in `plugin/.claude-plugin/plugin.json`.
Working language: English.

## Layout
- `.claude-plugin/marketplace.json` – marketplace `claude-model-router`; its one plugin has `source: ./plugin`
- `plugin/` – the plugin itself, and also the kit that `router install-repo` copies into a repo
  - `plugin/.claude-plugin/plugin.json` – plugin manifest (holds the version)
  - `plugin/agents/` – five subagents: `scout`, `mechanic` (haiku); `builder`, `test-writer`, `reviewer` (sonnet)
  - `plugin/hooks/hooks.json`, `plugin/hooks/router_nudge.py` – SessionStart, UserPromptSubmit and PostToolUse hook
  - `plugin/POLICY.md` – the routing policy text injected at session start
- `router` – bash CLI: `on|off|status|stats|install-repo`
- `router_kit.py` – `install-repo` helper (copies the kit into a repo's `.claude/`)
- `router_stats.py` – cost and delegation report from local transcripts
- `install.sh` – installs the CLI and support files; safe to re-run
- `tests/` – stdlib `unittest` suite

## Commands
- Unit tests (about 16 s, 130 tests): `python3 -m unittest discover -s tests`
- Validate the plugin manifest and components: `claude plugin validate ./plugin`
- Validate the marketplace manifest: `claude plugin validate .`
- Sandboxed CLI, hook and install-repo walkthrough: the "Acceptance test" section of `README.md` (check 2)
- Manual hook check: `echo '{"hook_event_name":"SessionStart","session_id":"x"}' | python3 plugin/hooks/router_nudge.py`

## Rules for every session
- Python is stdlib only (the hooks call `python3` on PATH); do not add third-party dependencies.
- The hook must never block a prompt: it always exits 0, and repo-installed hook commands end in `|| true`.
- Never test against a real config dir. Use a throwaway `HOME` and `CLAUDE_CONFIG_DIR`, as the tests and README do.
- `install-repo` never commits or stages, and never overwrites a foreign agent file without `--force` (which saves a `.bak`).
- If the hook command written by `install-repo` changes, add the old command to `LEGACY_HOOK_COMMANDS` in `router_kit.py` so re-running upgrades it in place.
- Behaviour changes need a test in `tests/` and a matching update to `README.md` (including its Layout block).

## Release
1. Bump `version` in `plugin/.claude-plugin/plugin.json` (the only place the version lives).
2. Update `README.md` if behaviour changed; run the unit tests and both `claude plugin validate` commands.
3. Commit with a message of the form `vX.Y.Z: short summary` (see `git log`). There are no tags.

## Ask or act
- Ask before: changing which model an agent runs on, the tripwires in `plugin/POLICY.md`, or other routing behaviour.
- Just do it: tests, refactors, docs fixes, validation, version bumps for a release already agreed.

## Where things live
- User-facing docs, install routes and the acceptance test: `README.md`
- Routing policy (tune here): `plugin/POLICY.md`
- Agent definitions: `plugin/agents/*.md`
