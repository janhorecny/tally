---
description: List recent tasks with tokens, cost and time (tally)
argument-hint: "[n|all]"
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally" slash --session "${CLAUDE_SESSION_ID}" list -- "$ARGUMENTS" 2>&1 || true`

The tally hook normally answers this command without involving you. It did not run, so the
command output is above. Reply with that output verbatim inside a code block and nothing else.
