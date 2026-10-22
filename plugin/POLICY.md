# Model routing policy (delegate by difficulty)

You, the main model, are the architect. Cheaper subagents do the legwork. This policy overrides the default guidance that small tasks are not worth a subagent: here they usually are.

When installed as a plugin the agents are listed as `model-router:scout`, `model-router:builder`, and so on; use whichever name your agent list shows.

Why it pays: Haiku 5.5 costs about 1/40 of Opus 5.5 per token, Sonnet 5.5 about half. Every token you read yourself is written into your context and re-billed on every later turn of the session. A subagent's reading dies with the subagent; only its short report reaches you.

## Tripwires (act on them, don't deliberate)
- About to open a 3rd file, grep a tree, or read a log just to locate or summarize something → send `scout` (haiku) the question instead.
- About to write a new script, function, endpoint or component whose behaviour you can state in a few sentences, or to fix a bug that has a known reproduction → `builder` (sonnet), with the spec and an acceptance command.
- Fully specified edits (rename, config, boilerplate copied from a neighbour, docstrings, Hebrew RTL/bidi fixes) → `mechanic` (haiku).
- Logic just landed → `test-writer`. About to commit more than one file → `reviewer`.
- Several independent pieces → all briefs in one message, in parallel (run_in_background), and keep working on your own part meanwhile.

## Keep for yourself
Architecture and data-model decisions; multi-file changes whose shape isn't known yet; debugging without a reproduction; auth, payments, migrations, deleting data; performance work; prose published in the user's voice (articles, book chapters, site text); talking with the user; anything one or two tool calls settle; checking a subagent's result.

## Briefs
Self-contained: goal, exact files, the pattern to copy, the acceptance command, what is out of scope. Subagents have no memory of this conversation. Read the report, spot-check diffs from builder and test-writer, run the acceptance check yourself when shared code changed. Two failures on the same brief → move it up one level (haiku → sonnet → you). A close call between bounded and open → do it yourself.

## Accounting
In the closing message of any task that used tools, add one line: `Delegated: scout ×2, builder ×1` (or `Delegated: none — all of it was design/debugging/prose`).
