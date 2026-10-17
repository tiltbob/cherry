// The Environments extension's service: the environments API on loopback,
// run by OpenChamber (as the cherry user, in cherry.service's sandbox) when
// the panel first asks, on the port and with the bearer token it gives
// (OPENCHAMBER_SERVICE_PORT, OPENCHAMBER_SERVICE_TOKEN). The panel reaches it
// only through OpenChamber's proxy. The verbs are /usr/lib/cherry/environments.js,
// shared with the OpenCode MCP server ($CHERRY_ENVIRONMENTS replaces the
// path: tests/extension.js).
//
// Routes, JSON in and out, each with Authorization: Bearer <token>:
//   GET    /health
//   GET    /environments               images, containers running, profiles, jobs
//   POST   /environments               {name, profile, replace?}: a build, 202
//   GET    /environments/<name>        status, with the last job on it
//   GET    /environments/<name>/log    ?lines=N: the build log's end
//   POST   /environments/<name>/start  {timeout?}: 202
//   POST   /environments/<name>/stop   {timeout?}: 202
//   DELETE /environments/<name>
// A build runs for minutes and a start or stop for up to two, longer than
// the proxy waits: those answer at once with a job, which GET /environments
// and GET /environments/<name> report until it is done (state running, done
// or failed, with its message or error). One job at a time per name.
"use strict";
const crypto = require("crypto");
const http = require("http");

const env = require(process.env.CHERRY_ENVIRONMENTS || "/usr/lib/cherry/environments.js");

const port = Number(process.env.OPENCHAMBER_SERVICE_PORT);
const token = process.env.OPENCHAMBER_SERVICE_TOKEN || "";
if (!Number.isInteger(port) || port <= 0 || !token) {
  console.error("cherry-environments: OPENCHAMBER_SERVICE_PORT and OPENCHAMBER_SERVICE_TOKEN are required");
  process.exit(2);
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

// The last job on each name: {name, verb, profile?, state, started, finished, message, error}.
const jobs = new Map();

function authorized(req) {
  const header = req.headers.authorization || "";
  const given = Buffer.from(header.startsWith("Bearer ") ? header.slice(7) : "");
  const expected = Buffer.from(token);
  return given.length === expected.length && crypto.timingSafeEqual(given, expected);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => {
      data += chunk;
      if (data.length > 65536) {
        reject(new HttpError(413, "the body is too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

function parseBody(text) {
  if (!text.trim()) return {};
  try {
    const value = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  } catch {
    // below
  }
  throw new HttpError(400, "the body must be a JSON object");
}

function seconds(value, fallback, max) {
  if (value === undefined || value === null) return fallback;
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) throw new HttpError(400, "timeout must be a positive number of seconds");
  return Math.min(n, max);
}

// Run a verb that outlasts the proxy as a job: answered at once, followed
// through GET. One per name at a time.
function startJob(name, verb, extra, work) {
  const current = jobs.get(name);
  if (current && current.state === "running") throw new HttpError(409, `${name}: ${current.verb} is still running`);
  const job = { name, verb, ...extra, state: "running", started: new Date().toISOString(), finished: null,
    message: null, error: null };
  jobs.set(name, job);
  work().then((message) => {
    job.state = "done";
    job.message = message;
  }, (e) => {
    job.state = "failed";
    job.error = e instanceof env.ToolError ? e.message : String(e);
  }).finally(() => {
    job.finished = new Date().toISOString();
  });
  return job;
}

async function handle(req, url) {
  const parts = url.pathname.split("/").filter(Boolean);
  if (req.method === "GET" && url.pathname === "/health") return [200, { ok: true }];
  if (parts[0] !== "environments") throw new HttpError(404, "not found");
  if (parts.length === 1) {
    if (req.method === "GET") return [200, { ...(await env.list()), jobs: [...jobs.values()] }];
    if (req.method === "POST") {
      const body = parseBody(await readBody(req));
      const name = env.imageName(body.name);
      const profile = env.profile(body.profile).id;
      const replace = body.replace === true;
      const job = startJob(name, "create", { profile }, async () => (await env.create(name, profile, { replace })).log);
      return [202, job];
    }
    throw new HttpError(405, "method not allowed");
  }
  const name = parts[1];
  if (parts.length === 2) {
    if (req.method === "GET") {
      // The job first: one that reads as done was done before the status
      // was read, so the status shows its outcome.
      const job = jobs.has(name) ? { ...jobs.get(name) } : null;
      return [200, { ...(await env.status(name)), job }];
    }
    if (req.method === "DELETE") {
      const message = await env.remove(name);
      jobs.delete(name);
      return [200, { message }];
    }
    throw new HttpError(405, "method not allowed");
  }
  if (parts.length === 3) {
    const action = parts[2];
    if (action === "log" && req.method === "GET") {
      const lines = Math.floor(seconds(url.searchParams.get("lines"), 60, 1000));
      return [200, env.log(name, lines)];
    }
    if ((action === "start" || action === "stop") && req.method === "POST") {
      env.machineName(name);
      const body = parseBody(await readBody(req));
      const timeout = seconds(body.timeout, action === "start" ? 120 : 60, 600);
      return [202, startJob(name, action, {}, () => env[action](name, { timeout }))];
    }
  }
  throw new HttpError(404, "not found");
}

const server = http.createServer(async (req, res) => {
  const answer = (status, body) => {
    const text = JSON.stringify(body);
    res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Content-Length": Buffer.byteLength(text) });
    res.end(text);
  };
  if (!authorized(req)) return answer(401, { error: "unauthorized" });
  try {
    const [status, body] = await handle(req, new URL(req.url, "http://127.0.0.1"));
    answer(status, body);
  } catch (e) {
    if (e instanceof HttpError) return answer(e.status, { error: e.message });
    if (e instanceof env.ToolError) return answer(400, { error: e.message });
    console.error(e);
    answer(500, { error: String(e && e.message || e) });
  }
});

server.listen(port, "127.0.0.1");
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => {
    server.close();
    process.exit(0);
  });
}
