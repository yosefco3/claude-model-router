# claude-model-router

Delegate-by-difficulty routing for [Claude Code](https://claude.com/claude-code), packaged as a plugin.

Usage on Max plans is metered by model cost, so every task a haiku or sonnet subagent does instead of the main model is quota you keep. This repo is a Claude Code **plugin + marketplace** that carries a routing policy into every Claude Code surface:

- **Five subagents**: `scout` and `mechanic` on haiku, `builder`, `test-writer` and `reviewer` on sonnet. As a plugin they are listed as `model-router:scout`, `model-router:builder`, and so on.
- **A routing policy** (`plugin/POLICY.md`) injected at session start by a hook. The main model delegates by tripwires (a lookup, a bounded build, a fully specified edit) and keeps architecture, unknown bugs, risky changes and your own prose for itself.
- **Nudges**: a short reminder on every prompt, and a nudge after 6 read/search/shell/web tool calls since your last message or the last delegation (edits don't count), then every 10.
- **A CLI**: `router on|off|status|stats|install-repo`.

## Where it works

| Surface | How | Status |
|---|---|---|
| Local CLI, Desktop Code tab | the plugin | works |
| SSH sessions | the plugin (Desktop copies it to the remote host) | per Anthropic's docs; not yet verified |
| Cowork | the plugin, added on claude.ai (Customize, Plugins) | per Anthropic's docs; not yet verified |
| Cloud sessions (single-repo, outside a project's threads) and routines | per repo: `router install-repo <path>`, then commit `.claude/` | works |
| Claude in Slack | repo hooks do not run there | not supported |
| claude.ai chat | no subagents; pick the model yourself | not possible |

Cloud sessions load neither plugins nor `~/.claude`, only the repo's own `.claude/`. That is why `install-repo` exists. The repo copy of the hook only acts when `CLAUDE_CODE_REMOTE` is set (cloud), so the hooks never double up on your machine (and if a cloud session ever loads the plugin too, the plugin copy steps aside for the repo copy). If you open a repo that has the repo copy while the plugin is installed, `/agents` lists both `scout` and `model-router:scout` (same definition, harmless).

## Install

```bash
git clone https://github.com/yosefco3/claude-model-router.git
cd claude-model-router && ./install.sh     # installs the `router` CLI to ~/.local/bin
```

Requires `python3` on PATH (the hooks call it).

Then install the plugin. Pick **one** route:

- **Recommended, on claude.ai or in the Claude desktop app:** Customize, Plugins, Add, Add marketplace, `yosefco3/claude-model-router`, then open model-router and make sure its toggle is on. This route also reaches Cowork and syncs to every machine signed in to the account. Start a new session (or Cowork task) afterwards.
- **Terminal only, if you don't use the desktop app:**

  ```bash
  claude plugin marketplace add yosefco3/claude-model-router
  claude plugin install model-router@claude-model-router
  ```

> **Don't use both routes on one machine.** The desktop app adds the marketplace through the local Claude Code CLI using the full URL, while `claude plugin marketplace add owner/repo` registers it under the GitHub shorthand. The second registration of the same name is refused; the desktop dialog only says "Failed to add marketplace", and its log (`~/.config/Claude/logs/main.log` on Linux) shows `its source doesn't match its extraKnownMarketplaces entry`. To switch to the desktop route, remove the terminal registration first:
>
> ```bash
> claude plugin uninstall model-router@claude-model-router
> claude plugin marketplace remove claude-model-router
> ```

`install.sh` is safe to re-run. It does not touch `~/.claude/agents`, `settings.json` or `CLAUDE.md`.

## Toggle

```bash
router status    # "on" or "off", then "plugin: installed (claude-model-router)", "plugin: installed (synced from claude.ai)" or "plugin: not installed"
router off       # main model does everything; agents only on explicit request
router on
```

The hook reads the flag on every prompt, so it applies from the next prompt with no restart. `router off` stops the reminders and nudges from the next prompt; the policy text already in context stays until a new session. `router on` delivers the policy on the next prompt if the session started while off. For a single shell or session, `CLAUDE_ROUTER=off` does the same. `router off` creates `~/.claude/model-router/OFF`.

## Stats

```bash
router stats                  # all sessions
router stats --since today    # or 7d, or YYYY-MM-DD
router stats --json
```

Reads your local transcripts and reports spend, subagent share and delegations by agent, plus the money saved by running router-agent work on a cheaper model as two figures: `saved (est.)`, what the same steps would have cost on the main model re-reading its context at each step, and `saved (repriced)`, a floor that only reprices the subagent's own tokens at the main model's rates.

## Cloud repos

```bash
router install-repo ~/code/my-project
cd ~/code/my-project && git add .claude && git commit -m "Add model router for cloud sessions"
```

This copies the five agents to `.claude/agents/`, the hook, policy and a `manifest.json` to `.claude/model-router/`, and merges three hook entries into `.claude/settings.json`. Other agents, keys and hooks are left alone, and re-running changes nothing.

- **Validated before writing:** a settings.json that is invalid or has an unexpected shape, a foreign agent file, or an unwritable target stops the install with exit 1 and no changes. The hooks run with `|| true`, so a missing script can never block prompts. settings.json is re-serialized (indent and CRLF kept; escapes normalized, minified files expanded, duplicate keys collapse to the last). Each file is replaced atomically.
- **Your own agents are protected.** `manifest.json` records the sha256 of each agent file the installer wrote. On re-install, a file that still matches the manifest (our untouched earlier copy) is updated to the new kit version. A file with one of the five names that is neither of those (yours, or hand-edited) blocks the whole install, and the files are named. `router install-repo <path> --force` replaces them, saving each as `<name>.md.bak` first.
- **It warns, without failing,** if the repo has no `.git`, or if git ignores any installed path (`.claude/settings.json`, the script, the policy, the manifest or an agent file). The warning names the ignored paths: cloud sessions only see what you commit, so they would get a broken install (hooks without the script, or the script without hooks).
- **Git:** it never commits or stages. The only git call is the read-only `git check-ignore` for that warning. Committing is up to you.

## Upgrading from v1

v1 wrote to `~/.claude` directly. The plugin now provides all three pieces, so remove the v1 copies or they will run twice:

1. Delete the five agent files in `~/.claude/agents/`: `scout.md`, `mechanic.md`, `builder.md`, `test-writer.md`, `reviewer.md` (and any `.bak` next to them).
2. Remove the router hooks from `~/.claude/settings.json`, if present (the `router_nudge.py` entries under `UserPromptSubmit` and `PostToolUse`).
3. Remove the block between `<!-- model-router:start -->` and `<!-- model-router:end -->` from `~/.claude/CLAUDE.md`, if present.
4. Delete the leftover `~/.claude/model-router/ROUTING.md` and `ROUTING-OFF.md` (and any `.bak` copies); nothing reads them any more.

Then run `./install.sh` again to get the new CLI, and install the plugin.

## Honest expectations

Delegation is the main model's judgment call guided by rules, not a hard router. Expect some misgrades in both directions; tune `plugin/POLICY.md` to taste. Savings depend entirely on how much the main model actually delegates; `router stats` measures it (the `saved (est.)` figure). On the author's first day with the plugin the estimate was about a fifth of total spend, most of it from one session that delegated heavily; a normal day lands lower. Treat the figure as an estimate, not a bill.

## Acceptance test

Run from the repo root. Checks 1 and 2 need only Python 3 and bash, and use a throwaway config dir, so your real `~/.claude` is never touched.

**1. Unit tests**

```bash
python3 -m unittest discover -s tests
```

Expected: all tests pass (the last line is `OK`).

**2. CLI, hook and repo install in a sandbox**

The subshell keeps `HOME` and `CLAUDE_CONFIG_DIR` pointed at a throwaway dir, so nothing leaks into your shell.

```bash
(
  T=$(mktemp -d); export HOME=$T/home CLAUDE_CONFIG_DIR=$T/cfg; mkdir -p $HOME $T/repo
  ./install.sh > /dev/null
  R=$HOME/.local/bin/router

  $R status
  # expected: on
  #           plugin: not installed

  echo '{"hook_event_name":"SessionStart","session_id":"x"}' | python3 plugin/hooks/router_nudge.py | head -c 100; echo
  # expected: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "# Model routing ...

  $R off && echo '{"hook_event_name":"SessionStart","session_id":"x"}' | python3 plugin/hooks/router_nudge.py | wc -c
  # expected: "router: off ..." then 0 (silent while off)

  $R install-repo $T/repo
  # expected: "created" lines for .claude/agents/{builder,mechanic,reviewer,scout,test-writer}.md,
  #           .claude/model-router/{manifest.json,router_nudge.py,POLICY.md} and .claude/settings.json,
  #           plus a "warning: ... has no .git" line on stderr (the sandbox repo is not a git repo)
  $R install-repo $T/repo 2> /dev/null | grep -c created
  # expected: 0 (a second run changes nothing)

  echo "my own agent" > $T/repo/.claude/agents/reviewer.md
  $R install-repo $T/repo 2>&1 | grep -c "reviewer.md"; $R install-repo $T/repo > /dev/null 2>&1; echo "exit $?"
  # expected: a count of 1 or more, then "exit 1" (your reviewer.md is not overwritten)
  $R install-repo $T/repo --force 2> /dev/null | grep -E "backup|replaced"
  # expected: a "backup" line for .claude/agents/reviewer.md.bak and a "replaced" line for reviewer.md

  $R install-repo $T/nope; echo "exit $?"
  # expected: "router: not a directory: ..." then "exit 1"
)
```

**3. In a real Claude Code session with the plugin installed**

1. `/agents` lists `model-router:scout`, `model-router:mechanic`, `model-router:builder`, `model-router:test-writer` and `model-router:reviewer`.
2. Ask: "find where X is validated and add a docstring to each validator". Expect a `scout` call, then `mechanic` or `builder`, and a closing `Delegated: ...` line.
3. Run `router off` in a terminal, then send another prompt: no reminder is injected and the main model does the work itself.

## Layout

```
.claude-plugin/marketplace.json   marketplace "claude-model-router"
plugin/                           the plugin (also the kit that install-repo copies)
  .claude-plugin/plugin.json
  agents/                         the five subagents
  hooks/hooks.json                SessionStart, UserPromptSubmit, PostToolUse
  hooks/router_nudge.py           the hook script
  POLICY.md                       the routing policy
router                            CLI, installed to ~/.local/bin
router_kit.py                     install-repo helper
router_stats.py                   cost and delegation report
install.sh                        installs the CLI and support files
tests/                            stdlib unittest
```

MIT.
