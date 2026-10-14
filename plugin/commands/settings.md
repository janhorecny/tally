---
description: Show or change Tally settings (log.depth, log.prompts, todos.reminder, ...) (tally)
argument-hint: "[key value]"
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally" slash --session "${CLAUDE_SESSION_ID}" --stdin settings 2>&1 <<'TALLY_ARGS' || true
$ARGUMENTS
TALLY_ARGS`

The Tally hook normally answers this command without involving you. It did not run, so the
command output is above. Reply with that output verbatim inside a code block and nothing else.
