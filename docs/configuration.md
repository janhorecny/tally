# Configuration

Tally works with no configuration. Everything optional lives in one JSON file and a few environment variables.

## `~/.claude/tally/config.json`

Edit it by hand, or use the CLI. The CLI parses values as JSON and falls back to a plain string:

```bash
tally config                          # show the effective config and the file path
tally config auto_start session       # set a key
tally config dashboard_port 8766
```

| key | default | meaning |
|---|---|---|
| `auto_start` | `"off"` | `"off"`: tasks start only with `/tally:start` or `/tally:new`. `"session"`: the first prompt of every new session starts a task named after that prompt. |
| `dashboard_host` | `"127.0.0.1"` | Interface the dashboard binds to. Keep it on localhost, because the API has no authentication. |
| `dashboard_port` | `8765` | Port of the dashboard. |
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
| `TALLY_HOME` | `~/.claude/tally` | Data directory: `tally.db`, `config.json`, `errors.log` and `dashboard.log`. |
| `TALLY_PROJECTS_DIR` | `$CLAUDE_CONFIG_DIR/projects` or `~/.claude/projects` | Where Claude Code transcripts are read from. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Respected the same way Claude Code respects it. |

Hooks inherit Claude Code's environment. Any of these variables must therefore be set before Claude Code starts, for example in your shell profile or in the `env` block of `~/.claude/settings.json`.

## Plugin installation scope

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
