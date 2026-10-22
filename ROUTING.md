<!-- model-router:start -->
# Model routing policy (delegate by difficulty)

The main model is the architect and the last resort, not the default worker. Before doing any task yourself, grade it and delegate when a cheaper agent can do it without loss of quality. The user's quota is consumed by model cost, so every task done by a subagent instead of the main model is a direct saving.

## Difficulty grades

**Grade 1, mechanical (delegate to `mechanic`, haiku):** the outcome is fully specified and verifiable by running something; no design choice remains. Renames, formatting, docstrings, config edits, boilerplate mirroring an existing file, Hebrew RTL/bidi fixes, simple CRUD that copies a neighbor.

**Grade 1, lookup (delegate to `scout`, haiku):** finding where something lives, which files reference it, summarizing a module, reading logs. Use `scout` instead of reading many files into your own context.

**Grade 2, bounded (delegate to `builder` / `test-writer` / `reviewer`, sonnet):** real reasoning, but inside a fence you can draw: one function or endpoint with a clear spec, a bug with a known reproduction, an in-file refactor, tests for existing code, a pre-commit review.

**Grade 3, open (do it yourself):** architecture and data-model decisions, multi-file changes whose shape is not yet known, debugging without a reproduction, anything touching auth, payments, migrations, or deletion of data, performance work, and any prose that will be published in the user's own voice (articles, book chapters, site text).

## How to delegate

- First plan at Grade 3 yourself: read enough to know the exact scope, then write the brief. Never delegate a task you cannot fully specify.
- Every brief is self-contained: goal, the exact files, the pattern to copy if any, the acceptance check (command to run or behavior to observe), and what is out of scope. Subagents have no memory of this conversation.
- Independent Grade 1/2 tasks go out in parallel in a single turn.
- Escalation ladder: if a subagent reports failure or missing information twice, move the task up one grade (haiku to sonnet, sonnet to yourself). Do not retry the same brief a third time.
- Trust but verify: read the subagent's report, spot-check the diff on anything Grade 2, and run the acceptance check yourself when the change touches shared code.
- When grading is a close call between 2 and 3, choose 3. Cost savings never justify a broken result.

## Defaults
- `scout` for codebase questions before you open files yourself.
- `reviewer` before any commit that touches more than one file.
- `test-writer` after `builder` finishes anything with logic in it.
<!-- model-router:end -->
