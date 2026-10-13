# Configuration

Tally works with no configuration. Everything optional lives in one JSON file and a few environment variables.

## `~/.claude/tally/config.json`

Change settings in any of these ways. All of them validate the value:

- `tally setup`: questions about the main settings, in a terminal (the first install asks them too);
- `/tally:settings <key> <value>` inside an agent;
- `tally settings <key> <value>`;
- the dashboard's *Settings* tab.

`dashboard_host` and `pricing` have no validation; set them with `tally config`, which shows or sets raw keys.

```bash
tally settings                          # every setting, its value and allowed values
tally settings log.depth calls
tally settings log.prompts off
tally config pricing                    # raw access
```

Per-project tracking modes (`track`, `tasks`, `off`) are stored in the database, not in this file. Set them with `/tally:project <mode>`, `tally project --set <mode>`, or the dashboard's *Projects* tab.

| key | default | meaning |
|---|---|---|
| `log.depth` | `"prompts"` | **Logging depth.** `summary`: task totals and the agent tree. `prompts`: adds the per-prompt breakdown. `calls`: adds tool-call statistics (each tool, how often, errors). |
| `log.prompts` | `"full"` | **Logging type for prompt text.** `full`, `preview` (first 300 characters) or `off` (never stored; prompts are still counted). |
| `todos.reminder` | `true` | Show the project's open todos when a session starts. |
| `todos.reminder_limit` | `5` | How many todos the reminder lists. |
| `auto_start` | `"off"` | `"off"`: tasks start only with `/tally:start` or `/tally:new`. `"session"`: the first prompt of every new session starts a task named after that prompt. |
| `projects.default_mode` | `"ask"` | The mode of a project Tally sees for the first time. `"ask"`: show the setup question. `"track"` or `"tasks"`: use that mode and show a one-line notice instead. |
| `sessions.scope` | `"enabled"` | What the sessions overview reads from the agents' transcripts. `"enabled"`: projects set to `track` or `tasks`, sessions a task counted, and sessions added one by one in the dashboard; the rest is only counted. `"all"`: every session, except projects set to `off`. |
| `dashboard_host` | `"127.0.0.1"` | Interface the dashboard binds to. Keep it on localhost, because the API has no authentication. |
| `dashboard_port` | `8765` | Port of the dashboard. |
| `dashboard_idle_minutes` | `120` | A dashboard started in the background (`/tally:dashboard`, `tally dashboard --background`) stops after this many minutes without an open page. `0`: never. |
| `pricing` | `{}` | Per-model price overrides or additions, see below. |

### Pricing overrides

Prices are in USD per million tokens. Each model key is normalized:

- the `[1m]` suffix is removed;
- date suffixes are removed;
- Bedrock and Vertex prefixes are removed.

So `claude-opus-5-5`, `claude-opus-5-5[1m]` and `us.anthropic.claude-opus-5-5-v1:0` all match the same entry. Missing cache rates are derived from `input`: 0.1× for cache reads, 1.25× for 5-minute cache writes and 2× for 1-hour cache writes.

```json
{
  "pricing": {
    "claude-sonnet-5-5": {"cache_read": 0.20},
    "my-proxy-model":    {"input": 1.0, "output": 4.0}
  }
}
```

The built-in table is `DEFAULT_PRICES` in [`plugin/tally/pricing.py`](../plugin/tally/pricing.py). It follows what Claude Code itself bills, which you can check with `scripts/validate_against_claude.py`:

- Fast mode (`usage.speed == "fast"`) is billed at 2×.
- Claude Haiku 5.5 switches to its higher tier above 100K prompt tokens.

Models with no known price show the cost with a `+` suffix (for example `$1.20+`). This means part of the usage could not be priced.

## Environment variables

| variable | default | meaning |
|---|---|---|
| `TALLY_HOME` | `~/.claude/tally` | Data directory: `tally.db`, `sessions.db`, `config.json`, `errors.log` and `dashboard.log`. |
| `TALLY_PROJECTS_DIR` | `$CLAUDE_CONFIG_DIR/projects` or `~/.claude/projects` | Where Claude Code transcripts are read from. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Respected the same way Claude Code respects it. |
| `CODEX_HOME` | `~/.codex` | Where Codex keeps `hooks.json` and its rollouts (`sessions/`). Used by the installer, the Codex reader and the sessions overview. |
| `PI_CODING_AGENT_DIR` | `~/.pi/agent` | pi's config directory. The installer puts the extension and the skill there; the sessions overview reads `sessions/`. |
| `TALLY_BIN`, `TALLY_PYTHON` | (path baked in at install), `python3` | Overrides for the pi extension: which `tally` launcher and which Python to run. |
| `NO_COLOR` | (unset) | Any value turns off the CLI's colours. They are also off when the output is not a terminal. |

Hooks inherit the agent's environment. Any of these variables must therefore be set before the agent starts, for example in your shell profile or, for Claude Code, in the `env` block of `~/.claude/settings.json`.

## Choosing agents

`./install.sh` (or `tally install`) detects Claude Code, Codex and pi and asks which ones to install into. You can also choose non-interactively:

```bash
./install.sh --agents claude,pi     # just these
./install.sh --yes                  # every detected agent, no questions
./uninstall.sh --agents codex       # remove from one agent only
tally doctor                        # what is installed where
```

## Plugin installation scope (Claude Code)

`install.sh` installs at **user** scope, so Tally is active in every project. To limit it to one project, uninstall it and install it at project scope instead:

```bash
./uninstall.sh
claude plugin marketplace add /path/to/tally
claude plugin install tally@tally --scope project     # writes .claude/settings.json of the current project
```

To try it for a single session without installing:

```bash
claude --plugin-dir /path/to/tally/plugin
```
