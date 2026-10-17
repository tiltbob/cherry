#!/usr/bin/env node
// package/cherry/cherry-machines-mcp.js speaks MCP over stdio, offers the
// environment verbs and nothing free-form, copies the profile into the recipe
// that cherry-image reads, and drives cherry-image@.service and machinectl
// as the README says: run with the stand-ins of tests/machines-standins.js.
// Needs node, sh and coreutils' timeout; the server runs on node too, or on
// $MCP_RUNTIME (e.g. bun, as on the image).
"use strict";
const assert = require("assert");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const readline = require("readline");
const { standins } = require("./machines-standins.js");

const server = path.join(__dirname, "..", "package", "cherry", "cherry-machines-mcp.js");
const world = standins("cherry-mcp-");
const { dirs, reset, called } = world;

let failures = 0;
let cases = 0;
function test(name, fn) {
  return fn().then(() => { cases++; }, (e) => { failures++; console.error(`mcp: ${name}: ${e.stack || e}`); });
}

class Client {
  constructor() {
    this.child = spawn(process.env.MCP_RUNTIME || process.execPath, [server], {
      env: { ...process.env, ...world.env },
      stdio: ["pipe", "pipe", "inherit"],
    });
    this.id = 0;
    this.pending = new Map();
    readline.createInterface({ input: this.child.stdout }).on("line", (line) => {
      const m = JSON.parse(line);
      this.pending.get(m.id)?.(m);
      this.pending.delete(m.id);
    });
  }
  send(message) { this.child.stdin.write(JSON.stringify(message) + "\n"); }
  request(method, params) {
    const id = ++this.id;
    return new Promise((resolve) => { this.pending.set(id, resolve); this.send({ jsonrpc: "2.0", id, method, params }); });
  }
  async call(name, args) {
    const m = await this.request("tools/call", { name, arguments: args });
    assert(m.result, `no result: ${JSON.stringify(m)}`);
    return { text: m.result.content[0].text, isError: m.result.isError };
  }
  close() { this.child.stdin.end(); }
}

const recipe = (...p) => path.join(dirs.recipes, ...p);

async function main() {
  const c = new Client();
  reset();

  await test("initialize", async () => {
    const m = await c.request("initialize", { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "t" } });
    assert.strictEqual(m.result.protocolVersion, "2025-11-25");
    assert.deepStrictEqual(m.result.capabilities, { tools: { listChanged: false } });
    assert.match(m.result.instructions, /machine-images skill/);
    const old = await c.request("initialize", { protocolVersion: "1999-01-01" });
    assert.strictEqual(old.result.protocolVersion, "2025-11-25");
    c.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    assert.deepStrictEqual((await c.request("ping")).result, {});
  });

  await test("tools/list: the verbs, nothing free-form", async () => {
    const { tools } = (await c.request("tools/list")).result;
    assert.deepStrictEqual(tools.map((t) => t.name), ["list", "create", "start", "stop", "status", "log", "remove"]);
    for (const t of tools) {
      assert.strictEqual(t.inputSchema.type, "object", t.name);
      assert.strictEqual(t.inputSchema.additionalProperties, false, t.name);
      assert(t.description.length > 40, t.name);
      for (const key of t.inputSchema.required ?? []) assert(key in t.inputSchema.properties, `${t.name}: ${key}`);
    }
    const create = tools.find((t) => t.name === "create");
    assert.deepStrictEqual(Object.keys(create.inputSchema.properties).sort(), ["name", "profile", "replace"]);
    assert.deepStrictEqual(create.inputSchema.required, ["name", "profile"]);
  });

  await test("errors, and the tools that are gone", async () => {
    assert.strictEqual((await c.request("tools/call", { name: "nope", arguments: {} })).error.code, -32602);
    assert.strictEqual((await c.request("nope/nope")).error.code, -32601);
    for (const gone of ["build", "exec", "export"]) {
      const m = await c.request("tools/call", { name: gone, arguments: { name: "web", command: "id" } });
      assert.strictEqual(m.error?.code, -32602, `${gone}: ${JSON.stringify(m)}`);
    }
    assert.deepStrictEqual(called(), []);
  });

  await test("list: images with their profile, containers running, profiles with the local ones first", async () => {
    reset({ images: ["web", "arch"], running: ["web"] });
    const r = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(r.images, [
      { name: "web", type: "directory", readOnly: false, modified: "2023-11-14T22:13:20.000Z", bytes: 123456, running: true, profile: null },
      { name: "arch", type: "directory", readOnly: false, modified: "2023-11-14T22:13:20.000Z", bytes: 123456, running: false, profile: null },
    ]);
    assert.deepStrictEqual(r.running, [{ name: "web", class: "container", service: "nspawn" }]);
    assert.deepStrictEqual(r.profiles, [
      { id: "plain", title: "plain", description: "", distribution: "arch", release: "", packages: [] },
      { id: "web", title: "Web (local)", description: "", distribution: "debian", release: "trixie", packages: [] },
    ]);
    assert.deepStrictEqual(Object.keys(r), ["images", "running", "profiles"]);
  });

  await test("create copies the profile into the recipe and starts cherry-image@build:", async () => {
    reset();
    fs.rmSync(path.join(dirs.local, "web"), { recursive: true });
    fs.mkdirSync(recipe("web", "extra", "old"), { recursive: true });
    const r = await c.call("create", { name: "web", profile: "web" });
    assert(!r.isError, r.text);
    assert.match(r.text, /^Created web from the profile web\./);
    assert.match(r.text, /end of build:web/);
    assert.doesNotMatch(r.text, /line 50\n/, "more than the log's end");
    assert.strictEqual(fs.readFileSync(recipe("web", "image.conf"), "utf8"),
      "Distribution=debian\nRelease=trixie\nPackages=nginx-light curl\n");
    assert.strictEqual(fs.readFileSync(recipe("web", "profile.conf"), "utf8"),
      "Title=Web (image)\nDescription=nginx on Debian\nProfile=web\n");
    assert.strictEqual(fs.readFileSync(recipe("web", "extra", "etc", "motd"), "utf8"), "hello\n");
    assert.strictEqual(fs.statSync(recipe("web", "extra", "etc", "motd")).mode & 0o7777, 0o644);
    assert.strictEqual(fs.statSync(recipe("web", "extra", "usr", "bin", "hi")).mode & 0o7777, 0o755);
    assert.strictEqual(fs.readlinkSync(recipe("web", "extra", "etc", "systemd", "system", "multi-user.target.wants",
      "nginx.service")), "/usr/lib/systemd/system/nginx.service");
    assert.strictEqual(fs.readFileSync(recipe("web", "postinst"), "utf8"),
      "#!/bin/sh\nset -eu\nsystemctl enable systemd-networkd\n");
    assert.strictEqual(fs.statSync(recipe("web", "postinst")).mode & 0o111, 0o111);
    assert.strictEqual(fs.readFileSync(recipe("web", "image.nspawn"), "utf8"), "[Exec]\nEnvironment=MODE=test\n");
    assert(!fs.existsSync(recipe("web", "extra", "old")), "the old recipe was not replaced");
    assert.deepStrictEqual(called(), ["systemctl start --no-ask-password cherry-image@build:web.service"]);
    // The image now names its profile.
    const list = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(list.images.find((i) => i.name === "web").profile, { id: "web", title: "Web (image)" });
  });

  await test("create from a profile with image.conf alone", async () => {
    reset();
    const r = await c.call("create", { name: "a1", profile: "plain" });
    assert(!r.isError, r.text);
    assert.strictEqual(fs.readFileSync(recipe("a1", "image.conf"), "utf8"), "Distribution=arch\nRelease=\n");
    assert.strictEqual(fs.readFileSync(recipe("a1", "profile.conf"), "utf8"), "Profile=plain\n");
    assert(!fs.existsSync(recipe("a1", "postinst")));
    assert(!fs.existsSync(recipe("a1", "image.nspawn")));
  });

  await test("create refuses bad arguments before writing anything", async () => {
    const bad = [
      [{ name: "my_web", profile: "web" }, /bad image name/],
      [{ name: "-web", profile: "web" }, /bad image name/],
      [{ name: "w".repeat(49), profile: "web" }, /bad image name/],
      [{ name: "bad1", profile: "" }, /bad profile/],
      [{ name: "bad2", profile: "Web" }, /bad profile/],
      [{ name: "bad3", profile: "../web" }, /bad profile/],
      [{ name: "bad4", profile: "nope" }, /no profile "nope"; the profiles are plain, web/],
      [{ name: "bad5", profile: "empty" }, /no profile "empty"/],
      [{ name: "bad6", profile: "web", packages: ["curl"] }, /create takes no packages/],
      [{ name: "bad7", profile: "web", postinst: "id", files: [] }, /create takes no postinst, files/],
    ];
    for (const [args, message] of bad) {
      reset();
      const r = await c.call("create", args);
      assert(r.isError, `accepted: ${JSON.stringify(args)}`);
      assert.match(r.text, message);
      assert.deepStrictEqual(called(), [], `called for ${JSON.stringify(args)}`);
      assert(!fs.existsSync(recipe(args.name)), `wrote a recipe for ${args.name}`);
    }
  });

  await test("a failed create returns the log's end, and log shows more", async () => {
    reset({}, { FAIL_UNIT: "cherry-image@build:broken.service" });
    const r = await c.call("create", { name: "broken", profile: "web" });
    assert(r.isError);
    assert.match(r.text, /cherry-image@build:broken\.service failed/);
    assert.match(r.text, new RegExp(`${dirs.logs}/build:broken\\.log`));
    assert.match(r.text, /line 99\nline 100\nend of build:broken/);
    const l = await c.call("log", { name: "broken", lines: 3 });
    assert(!l.isError, l.text);
    assert.strictEqual(l.text, `${dirs.logs}/build:broken.log:\nline 99\nline 100\nend of build:broken`);
    const none = await c.call("log", { name: "never" });
    assert.strictEqual(none.text, `no log in ${dirs.logs}/build:never.log`);
    assert((await c.call("log", { name: "../x" })).isError);
  });

  await test("create with replace removes the image first", async () => {
    reset({ images: ["web"] });
    const r = await c.call("create", { name: "web", profile: "web", replace: true });
    assert(!r.isError, r.text);
    assert.deepStrictEqual(called(), ["machinectl show-image web", "machinectl --no-ask-password remove web",
      "systemctl start --no-ask-password cherry-image@build:web.service"]);
    reset({ images: ["web"], running: ["web"] });
    const running = await c.call("create", { name: "web", profile: "web", replace: true });
    assert(running.isError && /web is running/.test(running.text), running.text);
    assert(!called().some((l) => l.startsWith("systemctl")), "built over a running image");
    reset({ images: ["web"] });
    const kept = await c.call("create", { name: "web", profile: "web" });
    assert(!kept.isError, kept.text);
    assert(!called().some((l) => l.includes("remove")), "removed without replace");
  });

  await test("start, status, stop", async () => {
    reset({ images: ["web"] });
    const started = await c.call("start", { name: "web" });
    assert(!started.isError, started.text);
    assert.strictEqual(started.text, "web is running");
    assert(called().includes("machinectl --no-ask-password start web"), called().join("\n"));

    const s = JSON.parse((await c.call("status", { name: "web" })).text);
    assert.deepStrictEqual(s, { name: "web", image: true, running: true, profile: { id: "web", title: "Web (image)" },
      system: "running", address: "10.0.0.5/28", failed: ["bad.service"] });
    // The stand-in's echo unfolds the script's \n, so the call spans lines.
    const calls = called().join("\n");
    assert(calls.includes("--setenv=TERM=dumb") && calls.includes("-q shell web") && calls.includes("is-system-running"),
      "status runs its fixed script through machinectl shell");

    const stopped = await c.call("stop", { name: "web" });
    assert.strictEqual(stopped.text, "web is stopped");
    assert.strictEqual((await c.call("stop", { name: "web" })).text, "web is not running");
    assert.deepStrictEqual(JSON.parse((await c.call("status", { name: "web" })).text),
      { name: "web", image: true, running: false, profile: { id: "web", title: "Web (image)" } });
    assert.deepStrictEqual(JSON.parse((await c.call("status", { name: "ghost" })).text),
      { name: "ghost", image: false, running: false, profile: null });
    assert((await c.call("status", { name: "-x" })).isError);
  });

  await test("stop terminates what does not shut down", async () => {
    reset({ running: ["web"] }, { KEEP_RUNNING: "1" });
    const r = await c.call("stop", { name: "web", timeout: 1 });
    assert.match(r.text, /terminated/);
    assert.deepStrictEqual(called().filter((l) => l.startsWith("machinectl")),
      ["machinectl --no-ask-password poweroff web", "machinectl --no-ask-password terminate web"]);
  });

  await test("start fails for an image that is missing or does not come up, and can be cancelled", async () => {
    reset({ images: ["dead"], dead: ["dead"] });
    const missing = await c.call("start", { name: "ghost" });
    assert(missing.isError && /No image 'ghost' known/.test(missing.text), missing.text);
    const dead = await c.call("start", { name: "dead", timeout: 1 });
    assert(dead.isError && /did not come up within 1 s: Failed to get shell PTY/.test(dead.text), dead.text);
    const bad = await c.call("start", { name: "-x" });
    assert(bad.isError && /bad machine name/.test(bad.text), bad.text);
    assert(!called().some((l) => l.includes("-x")), "called for -x");

    const id = c.id + 1;
    const pending = c.call("start", { name: "dead", timeout: 30 });
    setTimeout(() => c.send({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: id } }), 300);
    const cancelled = await pending;
    assert(cancelled.isError && /cancelled/.test(cancelled.text), cancelled.text);
  });

  await test("remove", async () => {
    reset({ images: ["web"] });
    assert.strictEqual((await c.call("remove", { name: "web" })).text, "web removed");
    assert.deepStrictEqual(called(), ["machinectl --no-ask-password remove web"]);
    const again = await c.call("remove", { name: "web" });
    assert(again.isError && /No image 'web' known/.test(again.text), again.text);
  });

  c.close();
}

main().then(() => {
  world.remove();
  if (failures) {
    console.error(`mcp: ${failures} of ${failures + cases} cases failed`);
    process.exit(1);
  }
  console.log(`mcp: ${cases} cases passed`);
});
