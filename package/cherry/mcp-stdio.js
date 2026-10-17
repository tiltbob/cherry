// mcp-stdio: what Cherry's MCP servers for the OpenCode agent share
// (cherry-machines-mcp.js, cherry-storage-mcp.js): the protocol, over stdio,
// running commands, and starting the units that do the privileged work for
// the cherry user, whose logs they read. Installed next to them, as
// /usr/lib/cherry/mcp-stdio.js.
"use strict";
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const readline = require("readline");

const PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

class ToolError extends Error {}

function check(condition, message) {
  if (!condition) throw new ToolError(message);
}

// run(command, args, options): {code, out, err}, never rejected for a failing
// command. options.timeout in seconds kills it (code null); options.signal
// (an AbortSignal) too; options.keepStdin leaves stdin open until it exits.
function run(command, args, options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { stdio: ["pipe", "pipe", "pipe"] });
    let out = "";
    let err = "";
    const keep = (s, chunk) => (s + chunk).slice(-1 << 20);
    child.stdout.on("data", (chunk) => { out = keep(out, chunk); });
    child.stderr.on("data", (chunk) => { err = keep(err, chunk); });
    if (!options.keepStdin) child.stdin.end();
    child.stdin.on("error", () => {});
    let killed = false;
    const kill = () => { killed = true; child.kill("SIGTERM"); };
    const timer = options.timeout ? setTimeout(kill, options.timeout * 1000) : null;
    if (options.signal) options.signal.addEventListener("abort", kill, { once: true });
    let done = false;
    const finish = (code) => {
      if (done) return;
      done = true;
      if (timer) clearTimeout(timer);
      if (options.signal) options.signal.removeEventListener("abort", kill);
      for (const stream of [child.stdin, child.stdout, child.stderr]) stream.destroy();
      resolve({ code, out, err });
    };
    child.on("error", (e) => { err += String(e); finish(null); });
    // Once killed, what it started may still hold its output open: done anyway.
    child.on("exit", (code) => { if (killed) finish(null); });
    child.on("close", (code) => finish(code));
  });
}

const sleep = (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));

function tail(text, lines) {
  return text.split("\n").slice(-lines).join("\n").trim();
}

function readLog(dir, instance, lines) {
  const file = path.join(dir, `${instance}.log`);
  try {
    return `${file}:\n${tail(fs.readFileSync(file, "utf8"), lines)}`;
  } catch {
    return `no log in ${file}`;
  }
}

// startUnit(template, instance, logs, signal): <template>@<instance>.service
// started and waited for; its log's tail, from <logs>/<instance>.log.
async function startUnit(template, instance, logs, signal) {
  const unit = `${template}@${instance}.service`;
  const r = await run("systemctl", ["start", "--no-ask-password", unit], { signal });
  if (r.code !== 0) throw new ToolError(`${unit} failed. Log, ${readLog(logs, instance, 60)}\n${r.err.trim()}`);
  return readLog(logs, instance, 15);
}

// serve({name, instructions, tools}): answers MCP over stdin and stdout until
// stdin ends. Each tool is {name, description, inputSchema, annotations?,
// call(args, signal)}, call returning the text of its result or throwing:
// a ToolError's message is the result, an error, and anything else its stack.
function serve({ name, instructions, tools }) {
  const reply = (id, result) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
  const fail = (id, code, message) =>
    process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }) + "\n");
  const calls = new Map();

  async function callTool(id, params) {
    const tool = tools.find((t) => t.name === params?.name);
    if (!tool) return fail(id, -32602, `unknown tool ${JSON.stringify(params?.name)}`);
    const abort = new AbortController();
    calls.set(id, abort);
    try {
      const text = await tool.call(params.arguments ?? {}, abort.signal);
      reply(id, { content: [{ type: "text", text }], isError: false });
    } catch (e) {
      reply(id, { content: [{ type: "text", text: e instanceof ToolError ? e.message : String(e.stack || e) }], isError: true });
    } finally {
      calls.delete(id);
    }
  }

  function handle(message) {
    const { id, method, params } = message;
    if (id === undefined) {
      if (method === "notifications/cancelled") calls.get(params?.requestId)?.abort();
      return;
    }
    switch (method) {
      case "initialize":
        return reply(id, {
          protocolVersion: PROTOCOLS.includes(params?.protocolVersion) ? params.protocolVersion : PROTOCOLS[0],
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name, version: "1" },
          instructions,
        });
      case "ping":
        return reply(id, {});
      case "tools/list":
        return reply(id, { tools: tools.map(({ name, description, inputSchema, annotations }) =>
          ({ name, description, inputSchema, ...(annotations && { annotations }) })) });
      case "tools/call":
        return callTool(id, params);
      default:
        return fail(id, -32601, `unknown method ${method}`);
    }
  }

  readline.createInterface({ input: process.stdin }).on("line", (line) => {
    if (!line.trim()) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      return fail(null, -32700, "parse error");
    }
    for (const m of Array.isArray(message) ? message : [message]) handle(m);
  });
}

module.exports = { ToolError, check, run, sleep, tail, readLog, startUnit, serve };
