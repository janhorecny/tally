/**
 * Tally for pi: tokens and time per task, todos and projects.
 *
 * Installed by tally's install.sh into ~/.pi/agent/extensions/tally.ts.
 * Everything runs locally through the `tally` CLI (python3, SQLite in
 * ~/.claude/tally). Commands are handled here and never reach the model.
 *
 *   /tally start [name]   /tally status   /tally stats   /tally end [note]
 *   /tally todo <text>    /tally todos    /tally take <id>   /tally do <id>   /tally done
 *   /tally project [track|tasks|off]      /tally sessions [days|all]   /tally settings    /tally help
 *
 * The same commands also work typed as a prompt, "tally:<cmd> args" (as in
 * Codex); tally:do / tally:new then turn the prompt itself into the work,
 * which also works in print mode (pi -p).
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { resolve as resolvePath } from "node:path";

// Path to the tally launcher; install.sh replaces the placeholder.
const TALLY = process.env.TALLY_BIN || "__TALLY_BIN__";
const PYTHON = process.env.TALLY_PYTHON || "python3";

const SUBCOMMANDS = [
  "start", "new", "status", "stats", "end", "list", "show", "resume", "rename",
  "todo", "todos", "take", "do", "done", "mark", "project", "projects", "sessions", "settings", "dashboard", "help",
];

type RunResult = { code: number; stdout: string; stderr: string };

function run(args: string[], input?: string, timeoutMs = 60_000): Promise<RunResult> {
  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let child;
    try {
      child = spawn(PYTHON, [TALLY, ...args], { stdio: ["pipe", "pipe", "pipe"], env: process.env });
    } catch (e) {
      resolve({ code: -1, stdout: "", stderr: String(e) });
      return;
    }
    const timer = setTimeout(() => child.kill("SIGKILL"), timeoutMs);
    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.on("error", (e: Error) => {
      clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: stderr + String(e) });
    });
    child.on("close", (code: number | null) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr });
    });
    child.stdin.end(input ?? "");
  });
}

function info(ctx: ExtensionContext) {
  const sm = ctx.sessionManager;
  const file = sm.getSessionFile();
  // getSessionFile() can be relative (e.g. --session-dir ./x): store absolute paths
  return { session_id: sm.getSessionId(), transcript_path: file ? resolvePath(file) : null, cwd: ctx.cwd, agent: "pi" };
}

async function hook(event: string, ctx: ExtensionContext, extra: Record<string, unknown> = {}) {
  const payload = { ...info(ctx), hook_event_name: event, ...extra };
  const r = await run(["hook", event, "--agent", "pi"], JSON.stringify(payload), event === "SessionEnd" ? 5_000 : 60_000);
  try {
    return r.stdout.trim() ? JSON.parse(r.stdout) : null;
  } catch {
    return null;
  }
}

function show(ctx: ExtensionContext, text: string | null | undefined) {
  if (!text) return;
  text = text.replace(/\/tally:/g, "/tally "); // pi syntax: one /tally command with subcommands
  if (!ctx.hasUI) {
    console.log(text); // print / json mode
    return;
  }
  const lines = text.split("\n");
  if (lines.length <= 2) ctx.ui.notify(text, "info");
  else ctx.ui.setWidget("tally", lines); // above the editor, cleared with the next prompt
}

async function refreshStatus(ctx: ExtensionContext) {
  try {
    if (!ctx.hasUI) return;
    const r = await run(["statusline"], JSON.stringify({ session_id: ctx.sessionManager.getSessionId(), cwd: ctx.cwd }), 10_000);
    ctx.ui.setStatus("tally", r.stdout.trim() || undefined);
  } catch {
    // the context went stale (session switched); the next event refreshes the status
  }
}

function userText(entry: any): string | null {
  if (!entry || entry.type !== "message" || entry.message?.role !== "user") return null;
  const c = entry.message.content;
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.filter((x: any) => x?.type === "text").map((x: any) => x.text).join("\n");
  return "";
}

type SlashResult = { text: string | null; block: boolean; context: string | null };

async function slash(ctx: ExtensionContext, cmd: string, rest: string): Promise<SlashResult | null> {
  const i = info(ctx);
  const argv = ["slash", "--json", "--agent", "pi", "--session", i.session_id, "--cwd", ctx.cwd];
  if (i.transcript_path) argv.push("--transcript", i.transcript_path);
  argv.push(cmd, "--", rest);
  const r = await run(argv);
  try {
    return JSON.parse(r.stdout) as SlashResult;
  } catch {
    show(ctx, (r.stdout || r.stderr || "tally: no output").trim());
    return null;
  }
}

export default function (pi: ExtensionAPI) {
  let previousSessionId: string | null = null;

  pi.on("session_start", async (event, ctx) => {
    const reason = event.reason === "reload" ? "resume" : event.reason;
    const out = await hook("SessionStart", ctx, { source: reason, previous_session_id: previousSessionId });
    if (out?.systemMessage) show(ctx, out.systemMessage);
    refreshStatus(ctx);
  });

  // "tally:<cmd> args" typed as a prompt (same syntax as in Codex). Handled here
  // synchronously, so tally:do / tally:new can turn the prompt into the work.
  pi.on("input", async (event, ctx) => {
    if (ctx.hasUI) ctx.ui.setWidget("tally", undefined);
    const m = /^\s*tally:([a-z][a-z-]*)(?:\s+([\s\S]*))?$/.exec(event.text || "");
    if (!m || event.source === "extension") return { action: "continue" };
    const res = await slash(ctx, m[1], (m[2] || "").trim());
    if (!res) return { action: "handled" };
    if (res.block) {
      show(ctx, res.text);
      refreshStatus(ctx);
      return { action: "handled" };
    }
    if (res.context) return { action: "transform", text: res.context };
    return { action: "continue" };
  });

  // A run starts: Tally times the turn. The prompt itself is read from the
  // session file (its entry id is what the model calls are attributed to).
  pi.on("agent_start", async (_event, ctx) => {
    const leaf = ctx.sessionManager.getLeafEntry() as any;
    const text = userText(leaf);
    await hook("UserPromptSubmit", ctx, { prompt: text ?? "", prompt_id: text !== null ? leaf.id : null, record_prompt: false });
  });

  pi.on("agent_settled", async (_event, ctx) => {
    await hook("Stop", ctx);
    refreshStatus(ctx);
  });

  pi.on("session_compact", async (_event, ctx) => {
    await hook("PreCompact", ctx);
  });

  pi.on("session_shutdown", async (event, ctx) => {
    previousSessionId = ctx.sessionManager.getSessionId();
    await hook("SessionEnd", ctx, { reason: event.reason === "new" ? "clear" : event.reason });
  });

  pi.registerCommand("tally", {
    description: "Tally: tokens & time per task, todos, projects (/tally help)",
    getArgumentCompletions: (prefix: string) => {
      if (prefix.includes(" ")) return null;
      const items = SUBCOMMANDS.filter((c) => c.startsWith(prefix)).map((c) => ({ value: c, label: c }));
      return items.length ? items : null;
    },
    handler: async (args, ctx) => {
      const trimmed = (args || "").trim() || "help";
      const space = trimmed.search(/\s/);
      const cmd = space < 0 ? trimmed : trimmed.slice(0, space);
      const rest = space < 0 ? "" : trimmed.slice(space + 1).trim();
      const res = await slash(ctx, cmd, rest);
      if (!res) return;
      if (res.block) {
        show(ctx, res.text);
        refreshStatus(ctx);
      } else if (res.context) {
        // /tally new, /tally do: hand the work to the model. Do not touch ctx
        // afterwards (pi may replace it while it starts the run); the status
        // refreshes when the run settles.
        if (ctx.mode !== "tui" && ctx.mode !== "rpc") {
          // print/json mode exit when the command returns, before a queued run starts
          show(ctx, `In ${ctx.mode} mode use tally:${cmd} ${rest} as the prompt instead (it hands the work to the model directly).`);
          return;
        }
        if (ctx.isIdle()) pi.sendUserMessage(res.context);
        else pi.sendUserMessage(res.context, { deliverAs: "followUp" });
      }
    },
  });
}
