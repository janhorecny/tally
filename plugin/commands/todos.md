---
description: List open todos of this project (or a project, all, done) (tally)
argument-hint: "[project|all|done]"
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally" slash --session "${CLAUDE_SESSION_ID}" todos -- "$ARGUMENTS" 2>&1 || true`

The Tally hook normally answers this command without involving you. It did not run, so the
command output is above. Reply with that output verbatim inside a code block and nothing else.
