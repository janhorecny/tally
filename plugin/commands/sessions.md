---
description: All sessions across agents with tokens, cost, models and time (tally)
argument-hint: "[days|all] [claude|codex|pi] [here]"
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally" slash --session "${CLAUDE_SESSION_ID}" --stdin sessions 2>&1 <<'TALLY_ARGS' || true
$ARGUMENTS
TALLY_ARGS`

The Tally hook normally answers this command without involving you. It did not run, so the
command output is above. Reply with that output verbatim inside a code block and nothing else.
