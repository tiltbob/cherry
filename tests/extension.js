#!/usr/bin/env node
// package/cherry/openchamber-extension is what OpenChamber installs as a
// built-in: its manifest parses as the host's rules say (apiVersion 1, the
// id the registry names, the pages and the scripts they load on disk,
// commands and tool matches within their patterns), its scripts parse, and
// its service (service/main.js) answers the routes the panel calls, as the
// cherry user would through OpenChamber's proxy: run with the stand-ins of
// tests/machines-standins.js, the environments module of this tree, and a
// port and token of this test's. Needs node, sh and coreutils' timeout; the
// service runs on node, or on $MCP_RUNTIME (e.g. bun, as on the image).
"use strict";
const assert = require("assert");
const { spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const path = require("path");
const { standins } = require("./machines-standins.js");

const root = path.join(__dirname, "..", "package", "cherry");
const extension = path.join(root, "openchamber-extension");
const world = standins("cherry-extension-");
const { dirs, reset } = world;

let failures = 0;
let cases = 0;
async function test(name, fn) {
  try {
    await fn();
    cases++;
  } catch (e) {
    failures++;
    console.error(`extension: ${name}: ${e.stack || e}`);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(predicate, seconds = 10) {
  for (const end = Date.now() + seconds * 1000; Date.now() < end; await sleep(100)) {
    const value = await predicate();
    if (value) return value;
  }
  throw new Error("timed out");
}

// ---- The manifest, as the host inspects a package. ----
async function manifest() {
  const pkg = JSON.parse(fs.readFileSync(path.join(extension, "package.json"), "utf8"));
  const registry = JSON.parse(fs.readFileSync(path.join(root, "openchamber-registry.json"), "utf8"));
  assert.match(pkg.version, /^\d+\.\d+\.\d+$/);
  const oc = pkg.openchamber;
  assert.strictEqual(oc.apiVersion, 1);
  const c = oc.contributes;
  assert.strictEqual(registry.version, 1);
  assert.deepStrictEqual(registry.extensions, [{ id: c.panel.id, directory: "cherry-environments" }]);
  assert.match(c.panel.id, /^openchamber-builtin-[a-z][a-z0-9-]*$/);
  assert.match(c.panel.icon, /^[a-z][a-z0-9-]*$/, "a Remixicon name, no file to ship");
  const inside = (p) => {
    assert(typeof p === "string" && !p.startsWith("/") && !p.split("/").some((s) => s === "" || s === "." || s === ".."), p);
    assert(fs.statSync(path.join(extension, p)).isFile(), `${p} is not a file`);
    return p;
  };
  for (const page of [c.panel.entry, c.background.entry, c.page.entry]) {
    inside(page);
    const html = fs.readFileSync(path.join(extension, page), "utf8");
    const scripts = [...html.matchAll(/<script[^>]*\ssrc="([^"]+)"/g)].map((m) => m[1]);
    assert(scripts.length > 0, `${page} loads no script`);
    for (const src of scripts) {
      assert(!src.includes("://") && src.endsWith(".js"), `${page}: ${src} is not a relative built script`);
      inside(path.posix.join(path.posix.dirname(page), src));
    }
    assert.doesNotMatch(html, /type="module"/, `${page}: the sandbox loads classic scripts only`);
  }
  for (const command of c.commands) {
    assert.match(command.name, /^[a-z][a-z0-9-]{0,23}$/);
    assert(command.description.length >= 1 && command.description.length <= 80, command.description);
  }
  assert(c.commands.length <= 8 && c.tools.length <= 16);
  for (const tool of c.tools) {
    assert.match(tool.match, /^[A-Za-z0-9_.:-]{1,127}\*?$/);
    for (const key of ["name", "title", "subtitle"]) if (tool[key]) assert(tool[key].length <= 200, key);
  }
  assert(c.tools.some((t) => t.match === "machines_*"), "the MCP server's tools render through it");
  assert.strictEqual(c.service.runtime, "host");
  inside(c.service.entry);
  assert(!c.capabilities, "no capability beyond the service: nothing here needs a model, a session or files");
  for (const script of ["panel/main.js", "service/main.js"]) {
    const r = spawnSync(process.execPath, ["--check", path.join(extension, script)], { encoding: "utf8" });
    assert.strictEqual(r.status, 0, `${script}: ${r.stderr}`);
  }
}

// ---- The service. ----
async function main() {
  await test("the manifest is a built-in the host accepts", manifest);

  const port = await new Promise((resolve) => {
    const probe = net.createServer();
    probe.listen(0, "127.0.0.1", () => { const { port: p } = probe.address(); probe.close(() => resolve(p)); });
  });
  const token = crypto.randomBytes(16).toString("hex");
  const child = spawn(process.env.MCP_RUNTIME || process.execPath, [path.join(extension, "service", "main.js")], {
    env: { ...process.env, ...world.env, OPENCHAMBER_SERVICE_PORT: String(port), OPENCHAMBER_SERVICE_TOKEN: token,
      CHERRY_ENVIRONMENTS: path.join(root, "environments.js") },
    stdio: ["ignore", "inherit", "inherit"],
  });

  // request(method, path, {body, query, auth}): {status, body (parsed JSON)}.
  // A string body goes as it is.
  function request(method, route, { body, query, auth = token } = {}) {
    const search = query ? "?" + new URLSearchParams(query).toString() : "";
    const text = body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body);
    return new Promise((resolve, reject) => {
      const req = http.request({ host: "127.0.0.1", port, method, path: route + search,
        headers: { ...(auth !== null && { Authorization: `Bearer ${auth}` }), "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(text) } }, (res) => {
        let data = "";
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          let parsed;
          try { parsed = JSON.parse(data); } catch { parsed = data; }
          resolve({ status: res.statusCode, body: parsed });
        });
      });
      req.on("error", reject);
      req.end(text);
    });
  }

  await test("health, behind the token", async () => {
    await until(async () => (await request("GET", "/health").catch(() => null))?.status === 200, 15);
    assert.strictEqual((await request("GET", "/health", { auth: null })).status, 401);
    assert.strictEqual((await request("GET", "/health", { auth: "wrong" })).status, 401);
    assert.strictEqual((await request("GET", "/environments", { auth: null })).status, 401);
    assert.deepStrictEqual(await request("GET", "/health"), { status: 200, body: { ok: true } });
  });

  await test("the routes that are not there", async () => {
    assert.strictEqual((await request("GET", "/nope")).status, 404);
    assert.strictEqual((await request("GET", "/environments/web/nope")).status, 404);
    assert.strictEqual((await request("PUT", "/environments")).status, 405);
    assert.strictEqual((await request("PUT", "/environments/web")).status, 405);
    assert.strictEqual((await request("POST", "/environments/web/log")).status, 404);
  });

  await test("list: images, containers, profiles and jobs", async () => {
    reset({ images: ["web"], running: ["web"] });
    const r = await request("GET", "/environments");
    assert.strictEqual(r.status, 200);
    assert.deepStrictEqual(Object.keys(r.body), ["images", "running", "profiles", "jobs"]);
    assert.deepStrictEqual(r.body.images.map((i) => [i.name, i.running]), [["web", true]]);
    assert.deepStrictEqual(r.body.profiles.map((p) => p.id), ["plain", "web"]);
    assert.deepStrictEqual(r.body.jobs, []);
  });

  await test("create: refused with a reason, or a job to follow", async () => {
    reset();
    for (const [body, status, message] of [
      ["not json", 400, /JSON object/],
      [{ name: "web" }, 400, /bad profile/],
      [{ name: "my_web", profile: "web" }, 400, /bad image name/],
      [{ name: "web", profile: "nope" }, 400, /no profile "nope"/],
    ]) {
      const r = await request("POST", "/environments", { body });
      assert.strictEqual(r.status, status, JSON.stringify([body, r]));
      assert.match(r.body.error, message);
    }
    assert(!fs.existsSync(path.join(dirs.recipes, "web")) && !fs.existsSync(path.join(dirs.recipes, "my_web")));
    assert.deepStrictEqual(world.called(), []);

    const r = await request("POST", "/environments", { body: { name: "web", profile: "web" } });
    assert.strictEqual(r.status, 202, JSON.stringify(r));
    assert.strictEqual(r.body.name, "web");
    assert.strictEqual(r.body.profile, "web");
    assert.strictEqual(r.body.verb, "create");
    const done = await until(async () => {
      const s = await request("GET", "/environments/web");
      return s.body.job && s.body.job.state !== "running" ? s.body : null;
    });
    assert.strictEqual(done.job.state, "done", JSON.stringify(done.job));
    assert.match(done.job.message, /end of build:web/);
    assert.strictEqual(done.image, true);
    assert.strictEqual(done.running, false);
    assert.deepStrictEqual(done.profile, { id: "web", title: "Web (local)" });
    assert.strictEqual(fs.readFileSync(path.join(dirs.recipes, "web", "profile.conf"), "utf8"), "Title=Web (local)\nProfile=web\n");
    assert(world.called().includes("systemctl start --no-ask-password cherry-image@build:web.service"));
    const list = await request("GET", "/environments");
    assert.deepStrictEqual(list.body.jobs.map((j) => [j.name, j.verb, j.state]), [["web", "create", "done"]]);
  });

  await test("one job at a time per name, and a failed one says why", async () => {
    reset({}, { SLOW_UNIT: "cherry-image@build:slow.service" });
    const first = await request("POST", "/environments", { body: { name: "slow", profile: "plain" } });
    assert.strictEqual(first.status, 202);
    const again = await request("POST", "/environments", { body: { name: "slow", profile: "plain" } });
    assert.strictEqual(again.status, 409);
    assert.match(again.body.error, /create is still running/);
    const other = await request("POST", "/environments", { body: { name: "other", profile: "plain" } });
    assert.strictEqual(other.status, 202, "another name is not held up");
    await until(async () => (await request("GET", "/environments/slow")).body.job.state === "done", 15);

    reset({}, { FAIL_UNIT: "cherry-image@build:broken.service" });
    const broken = await request("POST", "/environments", { body: { name: "broken", profile: "web" } });
    assert.strictEqual(broken.status, 202);
    const failed = await until(async () => {
      const s = await request("GET", "/environments/broken");
      return s.body.job.state === "failed" ? s.body : null;
    });
    assert.match(failed.job.error, /cherry-image@build:broken\.service failed/);
    assert.match(failed.job.error, /end of build:broken/);
    assert.strictEqual(failed.image, false);
    const log = await request("GET", "/environments/broken/log", { query: { lines: "2" } });
    assert.deepStrictEqual(log.body, { file: path.join(dirs.logs, "build:broken.log"), text: "line 100\nend of build:broken" });
    assert.strictEqual((await request("GET", "/environments/never/log")).body.text, null);
    assert.strictEqual((await request("GET", "/environments/bad_name/log")).status, 400);
  });

  await test("start, status, stop, remove", async () => {
    reset({ images: ["web"] });
    const start = await request("POST", "/environments/web/start", { body: {} });
    assert.strictEqual(start.status, 202, JSON.stringify(start));
    assert.strictEqual(start.body.verb, "start");
    const up = await until(async () => {
      const s = await request("GET", "/environments/web");
      return s.body.job.state === "done" ? s.body : null;
    });
    assert.strictEqual(up.job.message, "web is running");
    assert.strictEqual(up.running, true);
    assert.strictEqual(up.system, "running");
    assert.strictEqual(up.address, "10.0.0.5/28");
    assert.deepStrictEqual(up.failed, ["bad.service"]);

    assert.strictEqual((await request("DELETE", "/environments/web")).status, 400, "a running image is not removed");
    assert.strictEqual((await request("POST", "/environments/web/start", { body: { timeout: -1 } })).status, 400);

    const stop = await request("POST", "/environments/web/stop", { body: { timeout: 5 } });
    assert.strictEqual(stop.status, 202);
    const down = await until(async () => {
      const s = await request("GET", "/environments/web");
      return s.body.job.verb === "stop" && s.body.job.state === "done" ? s.body : null;
    });
    assert.strictEqual(down.job.message, "web is stopped");
    assert.strictEqual(down.running, false);

    const removed = await request("DELETE", "/environments/web");
    assert.deepStrictEqual(removed, { status: 200, body: { message: "web removed" } });
    const gone = await request("GET", "/environments/web");
    assert.strictEqual(gone.body.image, false);
    assert.strictEqual(gone.body.job, null, "the job went with the image");
    assert.strictEqual((await request("DELETE", "/environments/web")).status, 400);
    assert.strictEqual((await request("GET", "/environments/-x")).status, 400);
    assert.strictEqual((await request("POST", "/environments/-x/start", { body: {} })).status, 400);
  });

  child.kill("SIGTERM");
  await new Promise((resolve) => child.once("exit", resolve));
}

main().then(() => {
  world.remove();
  if (failures) {
    console.error(`extension: ${failures} of ${failures + cases} cases failed`);
    process.exit(1);
  }
  console.log(`extension: ${cases} cases passed`);
});
