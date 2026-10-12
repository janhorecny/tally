---
description: Start tracking tokens and time for a task in this session (task-counter)
argument-hint: "[task name]"
disable-model-invocation: true
---
!`python3 "${CLAUDE_PLUGIN_ROOT}/bin/task-counter" slash --session "${CLAUDE_SESSION_ID}" start -- "$ARGUMENTS" 2>&1 || true`

The task-counter hook normally answers this command without involving you. It did not run, so the
command output is above. Reply with that output verbatim inside a code block and nothing else.
