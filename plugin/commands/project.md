---
description: This project's overall stats, or set its tracking mode: track, tasks or off (tally)
argument-hint: "[track|tasks|off]"
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/tally" slash --session "${CLAUDE_SESSION_ID}" project -- "$ARGUMENTS" 2>&1 || true`

The Tally hook normally answers this command without involving you. It did not run, so the
command output is above. Reply with that output verbatim inside a code block and nothing else.
