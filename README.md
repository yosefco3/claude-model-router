# claude-model-router

Delegate-by-difficulty routing for [Claude Code](https://claude.com/claude-code).

Claude Code lets you define subagents with a fixed `model:`. Usage on Max plans is metered by model cost, so every task a haiku or sonnet subagent does instead of the main model is quota you keep. This repo packages:

- **Five global subagents** (`~/.claude/agents/`): `scout` and `mechanic` on haiku, `builder`, `test-writer` and `reviewer` on sonnet.
- **A routing policy** appended to `~/.claude/CLAUDE.md`: the main model grades each task 1/2/3, delegates grades 1 and 2, keeps grade 3 (architecture, unknown bugs, auth/migrations/data deletion, published prose) for itself. Includes an escalation ladder and a "when in doubt, go up a grade" rule.
- **A toggle**: `router on|off|status`.

## Install

```bash
git clone https://github.com/yosefco3/claude-model-router.git
cd claude-model-router && ./install.sh
```

Existing content in `~/.claude/CLAUDE.md` is left untouched; the policy lives between `<!-- model-router:start/end -->` markers. Re-running the installer updates in place. Overwritten files get a `.bak`.

## Toggle

```bash
router status
router off   # main model does everything; agents only on explicit request
router on
```

Takes effect in new `claude` sessions. For a one-off, tell Claude "no delegation this session".

## Verify

In a new session, `/agents` should list the five agents. Try a mixed task ("find where X is validated and add a docstring to each validator") and watch for `scout` and `mechanic` calls.

## Honest expectations

Delegation is the main model's judgment call guided by rules, not a hard router. Expect some misgrades in both directions; tune `ROUTING.md` to taste. Savings show on long sessions, not short chats; 30-50% is a realistic range, not 80%.

## Layout

```
agents/           the five subagent definitions
ROUTING.md        policy block (routing on)
ROUTING-OFF.md    policy block (routing off)
router            toggle script, installed to ~/.local/bin
install.sh
```

MIT.
