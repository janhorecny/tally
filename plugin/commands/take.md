---
description: Pick up a todo: status doing and Tally starts measuring it (you write the prompt) (tally)
argument-hint: "<id>"
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally" slash --session "${CLAUDE_SESSION_ID}" --stdin take 2>&1 <<'TALLY_ARGS' || true
$ARGUMENTS
TALLY_ARGS`

The Tally hook normally answers this command without involving you. It did not run, so the
command output is above. Reply with that output verbatim inside a code block and nothing else.
