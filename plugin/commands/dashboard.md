---
description: Open the local tally web dashboard, or stop it (tally)
argument-hint: "[stop]"
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally" slash --session "${CLAUDE_SESSION_ID}" --stdin dashboard 2>&1 <<'TALLY_ARGS' || true
$ARGUMENTS
TALLY_ARGS`

The tally hook normally answers this command without involving you. It did not run, so the
command output is above. Reply with that output verbatim inside a code block and nothing else.
