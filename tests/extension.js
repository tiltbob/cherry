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
const { dirs, reset, markImage, markEnvironment } = world;

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

  // The job on a name, once it is no longer running: the machine's status with it.
  const settled = (name, verb) => until(async () => {
    const s = await request("GET", `/environments/${name}`);
    const job = s.body.job;
    return job && job.state !== "running" && (!verb || job.verb === verb) ? s.body : null;
  }, 15);

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
    assert.strictEqual((await request("GET", "/images")).status, 404);
    assert.strictEqual((await request("GET", "/images/web")).status, 404);
    assert.strictEqual((await request("PUT", "/environments")).status, 405);
    assert.strictEqual((await request("PUT", "/environments/web")).status, 405);
    assert.strictEqual((await request("POST", "/images/web/log")).status, 404);
    assert.strictEqual((await request("GET", "/environments/web/enable")).status, 404);
    assert.strictEqual((await request("GET", "/environments/web/disable")).status, 404);
  });

  await test("list: images, environments, other machines, profiles and jobs", async () => {
    reset({ images: ["base", "yarp", "handmade"], running: ["yarp"] });
    markImage("base", "web");
    markEnvironment("yarp", "base");
    const r = await request("GET", "/environments");
    assert.strictEqual(r.status, 200);
    assert.deepStrictEqual(Object.keys(r.body), ["images", "environments", "other", "running", "profiles", "jobs"]);
    assert.deepStrictEqual(r.body.images.map((i) => [i.name, i.profile.id, i.environments]), [["base", "web", ["yarp"]]]);
    assert.deepStrictEqual(r.body.environments.map((e) => [e.name, e.image, e.running, e.enabled]),
      [["yarp", "base", true, false]]);
    assert.deepStrictEqual(r.body.other.map((o) => o.name), ["handmade"]);
    assert.deepStrictEqual(r.body.profiles.map((p) => p.id), ["plain", "web"]);
    assert.deepStrictEqual(r.body.jobs, []);
  });

  await test("build: refused with a reason, or a job to follow", async () => {
    reset();
    for (const [body, status, message] of [
      ["not json", 400, /JSON object/],
      [{ name: "web" }, 400, /bad profile/],
      [{ name: "my_web", profile: "web" }, 400, /bad name/],
      [{ name: "web", profile: "nope" }, 400, /no profile "nope"/],
    ]) {
      const r = await request("POST", "/images", { body });
      assert.strictEqual(r.status, status, JSON.stringify([body, r]));
      assert.match(r.body.error, message);
    }
    assert(!fs.existsSync(path.join(dirs.recipes, "web")) && !fs.existsSync(path.join(dirs.recipes, "my_web")));
    assert.deepStrictEqual(world.called(), []);

    const r = await request("POST", "/images", { body: { name: "web", profile: "web" } });
    assert.strictEqual(r.status, 202, JSON.stringify(r));
    assert.deepStrictEqual([r.body.name, r.body.profile, r.body.verb, r.body.state], ["web", "web", "build", "running"]);
    const done = await settled("web", "build");
    assert.strictEqual(done.job.state, "done", JSON.stringify(done.job));
    assert.match(done.job.message, /end of build:web/);
    assert.deepStrictEqual([done.kind, done.exists, done.running, done.profile], ["image", true, false, { id: "web", title: "Web (local)" }]);
    assert.strictEqual(fs.readFileSync(path.join(dirs.recipes, "web", "profile.conf"), "utf8"), "Title=Web (local)\nProfile=web\n");
    assert(world.called().includes("systemctl start --no-ask-password cherry-image@build:web.service"));
    const list = await request("GET", "/environments");
    assert.deepStrictEqual(list.body.jobs.map((j) => [j.name, j.verb, j.state]), [["web", "build", "done"]]);
    const log = await request("GET", "/images/web/log", { query: { lines: "2" } });
    assert.deepStrictEqual(log.body, { file: path.join(dirs.logs, "build:web.log"), text: "line 100\nend of build:web" });
    assert.strictEqual((await request("GET", "/images/never/log")).body.text, null);
    assert.strictEqual((await request("GET", "/images/bad_name/log")).status, 400);
  });

  await test("one job at a time per name, and a failed one says why", async () => {
    reset({}, { SLOW_UNIT: "cherry-image@build:slow.service" });
    const first = await request("POST", "/images", { body: { name: "slow", profile: "plain" } });
    assert.strictEqual(first.status, 202);
    const again = await request("POST", "/images", { body: { name: "slow", profile: "plain" } });
    assert.strictEqual(again.status, 409);
    assert.match(again.body.error, /build is still running/);
    const other = await request("POST", "/images", { body: { name: "other", profile: "plain" } });
    assert.strictEqual(other.status, 202, "another name is not held up");
    await settled("slow", "build");

    reset({}, { FAIL_UNIT: "cherry-image@build:broken.service" });
    const broken = await request("POST", "/images", { body: { name: "broken", profile: "web" } });
    assert.strictEqual(broken.status, 202);
    const failed = await settled("broken", "build");
    assert.strictEqual(failed.job.state, "failed");
    assert.match(failed.job.error, /cherry-image@build:broken\.service failed/);
    assert.match(failed.job.error, /end of build:broken/);
    assert.strictEqual(failed.exists, false);
  });

  await test("create: a clone of an image, or of a profile's image built first", async () => {
    reset({ images: ["web"] });
    markImage("web");
    for (const [body, status, message] of [
      [{ name: "yarp" }, 400, /give an image or a profile, not both/],
      [{ name: "yarp", image: "web", profile: "web" }, 400, /give an image or a profile, not both/],
      [{ name: "yarp", image: "ghost" }, 400, /no image "ghost"/],
      [{ name: "yarp", profile: "nope" }, 400, /no profile "nope"/],
      [{ name: "bad name", image: "web" }, 400, /bad name/],
    ]) {
      const r = await request("POST", "/environments", { body });
      assert.strictEqual(r.status, status, JSON.stringify([body, r]));
      assert.match(r.body.error, message);
    }
    assert(!world.called().some((l) => l.includes("clone")), world.called().join("\n"));

    const r = await request("POST", "/environments", { body: { name: "yarp", image: "web" } });
    assert.strictEqual(r.status, 202, JSON.stringify(r));
    assert.deepStrictEqual([r.body.verb, r.body.image], ["create", "web"]);
    const done = await settled("yarp", "create");
    assert.strictEqual(done.job.message, "yarp created from the image web");
    assert.deepStrictEqual([done.kind, done.exists, done.running, done.image], ["environment", true, false, "web"]);
    assert(world.called().includes("machinectl --no-ask-password clone web yarp"));
    assert.strictEqual((await request("POST", "/environments", { body: { name: "yarp", image: "web" } })).status, 409,
      "a done job on the name does not hold it up, but the name is taken");

    reset();
    const built = await request("POST", "/environments", { body: { name: "fresh", profile: "plain" } });
    assert.strictEqual(built.status, 202);
    assert.deepStrictEqual([built.body.verb, built.body.profile], ["create", "plain"]);
    const fresh = await settled("fresh", "create");
    assert.strictEqual(fresh.job.message, "fresh created from the image plain, built first");
    assert.deepStrictEqual(world.called().filter((l) => !l.includes("show-image") && !l.startsWith("busctl")), [
      "systemctl start --no-ask-password cherry-image@build:plain.service",
      "machinectl --no-ask-password clone plain fresh",
    ]);
    const list = await request("GET", "/environments");
    assert.deepStrictEqual(list.body.images.map((i) => [i.name, i.environments]), [["plain", ["fresh"]]]);
  });

  await test("start, status, stop, remove, remove image", async () => {
    reset({ images: ["web", "yarp"] });
    markImage("web");
    markEnvironment("yarp", "web");
    const image = await request("POST", "/environments/web/start", { body: {} });
    assert.strictEqual(image.status, 202);
    const refused = await settled("web", "start");
    assert.strictEqual(refused.job.state, "failed");
    assert.match(refused.job.error, /web is an image/);

    const start = await request("POST", "/environments/yarp/start", { body: {} });
    assert.strictEqual(start.status, 202, JSON.stringify(start));
    const up = await settled("yarp", "start");
    assert.strictEqual(up.job.message, "yarp is running");
    assert.deepStrictEqual([up.kind, up.image, up.running, up.system, up.address, up.failed],
      ["environment", "web", true, "running", "10.0.0.5/28", ["bad.service"]]);

    assert.strictEqual((await request("DELETE", "/environments/yarp")).status, 400, "a running environment is not removed");
    assert.strictEqual((await request("DELETE", "/images/web")).status, 400, "an image with environments is not removed");
    assert.strictEqual((await request("POST", "/environments/yarp/start", { body: { timeout: -1 } })).status, 400);

    const stop = await request("POST", "/environments/yarp/stop", { body: { timeout: 5 } });
    assert.strictEqual(stop.status, 202);
    const down = await settled("yarp", "stop");
    assert.strictEqual(down.job.message, "yarp is stopped");
    assert.strictEqual(down.running, false);

    assert.strictEqual((await request("DELETE", "/environments/web")).status, 400, "remove does not take an image");
    const removed = await request("DELETE", "/environments/yarp");
    assert.deepStrictEqual(removed, { status: 200, body: { message: "yarp removed" } });
    assert(!fs.existsSync(path.join(dirs.environments, "yarp")), "the record stayed");
    const gone = await request("GET", "/environments/yarp");
    assert.deepStrictEqual([gone.body.kind, gone.body.exists, gone.body.job], ["other", false, null]);
    assert.strictEqual((await request("DELETE", "/images/yarp")).status, 400, "remove image does not take a name that is no image");
    assert.deepStrictEqual(await request("DELETE", "/images/web"), { status: 200, body: { message: "web removed" } });
    assert.strictEqual((await request("GET", "/environments/-x")).status, 400);
    assert.strictEqual((await request("POST", "/environments/-x/start", { body: {} })).status, 400);
  });

  await test("enable and disable: the record at once, or a job with now", async () => {
    reset({ images: ["web", "yarp"] });
    markImage("web");
    markEnvironment("yarp", "web");
    const image = await request("POST", "/environments/web/enable", { body: {} });
    assert.strictEqual(image.status, 400, JSON.stringify(image));
    assert.match(image.body.error, /web is an image/);
    assert.strictEqual((await request("POST", "/environments/-x/enable", { body: {} })).status, 400);
    assert.strictEqual((await request("POST", "/environments/yarp/enable", { body: "nope" })).status, 400);
    assert.strictEqual((await request("POST", "/environments/yarp/enable", { body: { now: true, timeout: 0 } })).status, 400);
    const conf = path.join(dirs.environments, "yarp", "environment.conf");
    assert.strictEqual(fs.readFileSync(conf, "utf8"), "Image=web\nCreated=2026-01-01T00:00:00.000Z\n", "refused, yet written");

    const on = await request("POST", "/environments/yarp/enable", { body: {} });
    assert.deepStrictEqual(on, { status: 200, body: { message: "yarp is enabled: it starts at boot" } });
    assert.strictEqual(fs.readFileSync(conf, "utf8"), "Image=web\nCreated=2026-01-01T00:00:00.000Z\nEnabled=yes\n");
    assert(!world.called().some((l) => l.includes("start")), "enable without now started it");
    let list = await request("GET", "/environments");
    assert.deepStrictEqual(list.body.environments.map((e) => [e.name, e.running, e.enabled]), [["yarp", false, true]]);
    assert.deepStrictEqual(list.body.jobs.filter((j) => j.name === "yarp"), [], "an enable alone is no job");
    const status = await request("GET", "/environments/yarp");
    assert.deepStrictEqual([status.body.kind, status.body.enabled, status.body.job], ["environment", true, null]);

    const off = await request("POST", "/environments/yarp/disable", { body: {} });
    assert.deepStrictEqual(off, { status: 200, body: { message: "yarp is disabled: it does not start at boot" } });
    assert.strictEqual(fs.readFileSync(conf, "utf8"), "Image=web\nCreated=2026-01-01T00:00:00.000Z\nEnabled=no\n");

    const onNow = await request("POST", "/environments/yarp/enable", { body: { now: true } });
    assert.strictEqual(onNow.status, 202, JSON.stringify(onNow));
    assert.deepStrictEqual([onNow.body.verb, onNow.body.state], ["enable", "running"]);
    const up = await settled("yarp", "enable");
    assert.strictEqual(up.job.message, "yarp is enabled: it starts at boot. yarp is running");
    assert.deepStrictEqual([up.running, up.enabled], [true, true]);
    assert(world.called().includes("machinectl --no-ask-password start yarp"));
    const offNow = await request("POST", "/environments/yarp/disable", { body: { now: true, timeout: 5 } });
    assert.strictEqual(offNow.status, 202, JSON.stringify(offNow));
    const down = await settled("yarp", "disable");
    assert.strictEqual(down.job.message, "yarp is disabled: it does not start at boot. yarp is stopped");
    assert.deepStrictEqual([down.running, down.enabled], [false, false]);
    list = await request("GET", "/environments");
    assert.deepStrictEqual(list.body.jobs.filter((j) => j.name === "yarp").map((j) => [j.verb, j.state]), [["disable", "done"]]);
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
