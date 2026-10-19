#!/usr/bin/env node
// package/cherry/cherry-machines-mcp.js speaks MCP over stdio, offers the
// image and environment verbs and nothing free-form, copies the profile into
// the recipe that cherry-image reads, clones images into environments and
// records them, enables them to start at boot in those records, and drives
// cherry-image@.service and machinectl as the README says: run with the
// stand-ins of tests/machines-standins.js. Needs node, sh
// and coreutils' timeout; the server runs on node too, or on $MCP_RUNTIME
// (e.g. bun, as on the image).
"use strict";
const assert = require("assert");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const readline = require("readline");
const { standins } = require("./machines-standins.js");

const server = path.join(__dirname, "..", "package", "cherry", "cherry-machines-mcp.js");
const world = standins("cherry-mcp-");
const { dirs, reset, called, markImage, markEnvironment } = world;

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
const record = (name) => path.join(dirs.environments, name, "environment.conf");

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
    assert.deepStrictEqual(tools.map((t) => t.name),
      ["list", "build", "remove_image", "log", "create", "start", "stop", "enable", "disable", "status", "remove"]);
    for (const t of tools) {
      assert.strictEqual(t.inputSchema.type, "object", t.name);
      assert.strictEqual(t.inputSchema.additionalProperties, false, t.name);
      assert(t.description.length > 40, t.name);
      for (const key of t.inputSchema.required ?? []) assert(key in t.inputSchema.properties, `${t.name}: ${key}`);
    }
    const build = tools.find((t) => t.name === "build");
    assert.deepStrictEqual(Object.keys(build.inputSchema.properties).sort(), ["name", "profile", "replace"]);
    assert.deepStrictEqual(build.inputSchema.required, ["name", "profile"]);
    const create = tools.find((t) => t.name === "create");
    assert.deepStrictEqual(Object.keys(create.inputSchema.properties).sort(), ["image", "name", "profile"]);
    assert.deepStrictEqual(create.inputSchema.required, ["name"]);
    for (const name of ["enable", "disable"]) {
      const t = tools.find((tool) => tool.name === name);
      assert.deepStrictEqual(Object.keys(t.inputSchema.properties).sort(), ["name", "now"], name);
      assert.deepStrictEqual(t.inputSchema.required, ["name"], name);
    }
  });

  await test("errors, and the tools that are gone", async () => {
    assert.strictEqual((await c.request("tools/call", { name: "nope", arguments: {} })).error.code, -32602);
    assert.strictEqual((await c.request("nope/nope")).error.code, -32601);
    for (const gone of ["exec", "export", "shell"]) {
      const m = await c.request("tools/call", { name: gone, arguments: { name: "web", command: "id" } });
      assert.strictEqual(m.error?.code, -32602, `${gone}: ${JSON.stringify(m)}`);
    }
    assert.deepStrictEqual(called(), []);
  });

  await test("list: images with their environments, environments with their image, the other machines", async () => {
    reset({ images: ["base", "yarp", "handmade"], running: ["yarp"] });
    markImage("base", "web");
    markEnvironment("yarp", "base");
    const r = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(Object.keys(r), ["images", "environments", "other", "running", "profiles"]);
    assert.deepStrictEqual(r.images, [{ name: "base", type: "directory", readOnly: false, modified: "2023-11-14T22:13:20.000Z",
      bytes: 123456, running: false, profile: { id: "web", title: "web title" }, environments: ["yarp"] }]);
    assert.deepStrictEqual(r.environments, [{ name: "yarp", type: "directory", readOnly: false,
      modified: "2023-11-14T22:13:20.000Z", bytes: 123456, running: true, image: "base", created: "2026-01-01T00:00:00.000Z",
      enabled: false }]);
    assert.deepStrictEqual(r.other, [{ name: "handmade", type: "directory", readOnly: false,
      modified: "2023-11-14T22:13:20.000Z", bytes: 123456, running: false }]);
    assert.deepStrictEqual(r.running, [{ name: "yarp", kind: "environment" }]);
    assert.deepStrictEqual(r.profiles, [
      { id: "plain", title: "plain", description: "", distribution: "arch", release: "", packages: [] },
      { id: "web", title: "Web (local)", description: "", distribution: "debian", release: "trixie", packages: [] },
    ]);
  });

  await test("build copies the profile into the recipe and starts cherry-image@build:", async () => {
    reset();
    fs.rmSync(path.join(dirs.local, "web"), { recursive: true });
    fs.mkdirSync(recipe("web", "extra", "old"), { recursive: true });
    const r = await c.call("build", { name: "web", profile: "web" });
    assert(!r.isError, r.text);
    assert.match(r.text, /^Built the image web from the profile web\./);
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
    const list = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(list.images.map((i) => [i.name, i.profile]), [["web", { id: "web", title: "Web (image)" }]]);
    const plain = await c.call("build", { name: "a1", profile: "plain" });
    assert(!plain.isError, plain.text);
    assert.strictEqual(fs.readFileSync(recipe("a1", "profile.conf"), "utf8"), "Profile=plain\n");
    assert(!fs.existsSync(recipe("a1", "postinst")));
  });

  await test("build refuses bad arguments before writing anything", async () => {
    const bad = [
      [{ name: "my_web", profile: "web" }, /bad name/],
      [{ name: "-web", profile: "web" }, /bad name/],
      [{ name: "w".repeat(49), profile: "web" }, /bad name/],
      [{ name: "bad1", profile: "" }, /bad profile/],
      [{ name: "bad2", profile: "Web" }, /bad profile/],
      [{ name: "bad3", profile: "../web" }, /bad profile/],
      [{ name: "bad4", profile: "nope" }, /no profile "nope"; the profiles are plain, web/],
      [{ name: "bad5", profile: "empty" }, /no profile "empty"/],
      [{ name: "bad6", profile: "web", packages: ["curl"] }, /build takes no packages/],
      [{ name: "bad7", profile: "web", postinst: "id", files: [] }, /build takes no postinst, files/],
      [{ name: "yarp", profile: "web" }, /yarp is an environment, not an image/],
    ];
    for (const [args, message] of bad) {
      reset({ images: ["yarp"] });
      markEnvironment("yarp", "base");
      const r = await c.call("build", args);
      assert(r.isError, `accepted: ${JSON.stringify(args)}`);
      assert.match(r.text, message);
      assert.deepStrictEqual(called(), [], `called for ${JSON.stringify(args)}`);
      assert(!fs.existsSync(recipe(args.name)), `wrote a recipe for ${args.name}`);
    }
  });

  await test("a failed build returns the log's end, and log shows more", async () => {
    reset({}, { FAIL_UNIT: "cherry-image@build:broken.service" });
    const r = await c.call("build", { name: "broken", profile: "web" });
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

  await test("build with replace removes the image first, unless it has environments", async () => {
    reset({ images: ["web"] });
    markImage("web");
    const r = await c.call("build", { name: "web", profile: "web", replace: true });
    assert(!r.isError, r.text);
    assert.deepStrictEqual(called(), ["machinectl show-image web", "machinectl --no-ask-password remove web",
      "systemctl start --no-ask-password cherry-image@build:web.service"]);
    reset({ images: ["web"], running: ["web"] });
    markImage("web");
    const running = await c.call("build", { name: "web", profile: "web", replace: true });
    assert(running.isError && /web is running/.test(running.text), running.text);
    assert(!called().some((l) => l.startsWith("systemctl")), "built over a running image");
    reset({ images: ["web", "yarp"] });
    markImage("web");
    markEnvironment("yarp", "web");
    const held = await c.call("build", { name: "web", profile: "web", replace: true });
    assert(held.isError && /web still has environments \(yarp\): remove them first/.test(held.text), held.text);
    assert.deepStrictEqual(called().filter((l) => !l.includes("show-image")), []);
    reset({ images: ["web"] });
    markImage("web");
    const kept = await c.call("build", { name: "web", profile: "web" });
    assert(!kept.isError, kept.text);
    assert(!called().some((l) => l.includes("remove")), "removed without replace");
  });

  await test("create clones an image into an environment, and records it", async () => {
    reset({ images: ["web"] });
    markImage("web");
    const r = await c.call("create", { name: "yarp", image: "web" });
    assert(!r.isError, r.text);
    assert.strictEqual(r.text, "Created the environment yarp from the image web.");
    assert(called().includes("machinectl --no-ask-password clone web yarp"), called().join("\n"));
    assert(!called().some((l) => l.startsWith("systemctl")), "built an image that was there");
    assert.match(fs.readFileSync(record("yarp"), "utf8"), /^Image=web\nCreated=\d{4}-.*\n$/);
    const list = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(list.images.map((i) => [i.name, i.environments]), [["web", ["yarp"]]]);
    assert.deepStrictEqual(list.environments.map((e) => [e.name, e.image]), [["yarp", "web"]]);
    // From a machine made by hand, too.
    reset({ images: ["handmade"] });
    const other = await c.call("create", { name: "copy", image: "handmade" });
    assert(!other.isError, other.text);
    assert(fs.readFileSync(record("copy"), "utf8").startsWith("Image=handmade\n"));
  });

  await test("create from a profile builds the image first when there is none", async () => {
    reset();
    const r = await c.call("create", { name: "yarp", profile: "web" });
    assert(!r.isError, r.text);
    assert.match(r.text, /^Created the environment yarp from the image web\. The image was built first from the profile web\. Log, /);
    assert.deepStrictEqual(called().filter((l) => !l.includes("show-image") && !l.startsWith("busctl")), [
      "systemctl start --no-ask-password cherry-image@build:web.service",
      "machinectl --no-ask-password clone web yarp",
    ]);
    assert.strictEqual(fs.readFileSync(recipe("web", "profile.conf"), "utf8"), "Title=Web (image)\nDescription=nginx on Debian\nProfile=web\n");
    const before = called().length;
    const again = await c.call("create", { name: "yarp2", profile: "web" });
    assert.strictEqual(again.text, "Created the environment yarp2 from the image web.");
    assert(!called().slice(before).some((l) => l.startsWith("systemctl")), "rebuilt an image that was there");
  });

  await test("create refuses what it must, before cloning", async () => {
    reset({ images: ["web", "yarp", "busy"], running: ["busy"] });
    markImage("web");
    markImage("busy");
    markEnvironment("yarp", "web");
    const bad = [
      [{ name: "yarp", image: "web" }, /yarp exists already/],
      [{ name: "new", image: "ghost" }, /no image "ghost"/],
      [{ name: "new", image: "yarp" }, /yarp is an environment, not an image/],
      [{ name: "new", image: "busy" }, /busy is running/],
      [{ name: "new" }, /give an image or a profile, not both/],
      [{ name: "new", image: "web", profile: "web" }, /give an image or a profile, not both/],
      [{ name: "new", profile: "nope" }, /no profile "nope"/],
      [{ name: "bad name", image: "web" }, /bad name/],
      [{ name: "new", image: "web", packages: [] }, /create takes no packages/],
    ];
    for (const [args, message] of bad) {
      const r = await c.call("create", args);
      assert(r.isError, `accepted: ${JSON.stringify(args)}`);
      assert.match(r.text, message);
      if (args.name !== "yarp") assert(!fs.existsSync(record(args.name)), `recorded ${args.name}`);
    }
    assert(!called().some((l) => l.includes("clone")), called().join("\n"));
  });

  await test("start, status, stop, on environments and not on images", async () => {
    reset({ images: ["web", "yarp"] });
    markImage("web");
    markEnvironment("yarp", "web");
    const image = await c.call("start", { name: "web" });
    assert(image.isError && /web is an image: create an environment from it/.test(image.text), image.text);
    assert(!called().some((l) => l.includes("start")), "started an image");

    const started = await c.call("start", { name: "yarp" });
    assert(!started.isError, started.text);
    assert.strictEqual(started.text, "yarp is running");
    assert(called().includes("machinectl --no-ask-password start yarp"), called().join("\n"));

    const s = JSON.parse((await c.call("status", { name: "yarp" })).text);
    assert.deepStrictEqual(s, { name: "yarp", kind: "environment", exists: true, running: true, image: "web",
      created: "2026-01-01T00:00:00.000Z", enabled: false, system: "running", address: "10.0.0.5/28",
      failed: ["bad.service"] });
    // The stand-in's echo unfolds the script's \n, so the call spans lines.
    const calls = called().join("\n");
    assert(calls.includes("--setenv=TERM=dumb") && calls.includes("-q shell yarp") && calls.includes("is-system-running"),
      "status runs its fixed script through machinectl shell");
    assert.deepStrictEqual(JSON.parse((await c.call("status", { name: "web" })).text),
      { name: "web", kind: "image", exists: true, running: false, profile: { id: "web", title: "web title" } });
    assert.deepStrictEqual(JSON.parse((await c.call("status", { name: "ghost" })).text),
      { name: "ghost", kind: "other", exists: false, running: false });

    const stopped = await c.call("stop", { name: "yarp" });
    assert.strictEqual(stopped.text, "yarp is stopped");
    assert.strictEqual((await c.call("stop", { name: "yarp" })).text, "yarp is not running");
    assert.deepStrictEqual(JSON.parse((await c.call("status", { name: "yarp" })).text),
      { name: "yarp", kind: "environment", exists: true, running: false, image: "web", created: "2026-01-01T00:00:00.000Z",
        enabled: false });
    assert((await c.call("status", { name: "-x" })).isError);
  });

  await test("stop terminates what does not shut down", async () => {
    reset({ running: ["yarp"] }, { KEEP_RUNNING: "1" });
    const r = await c.call("stop", { name: "yarp", timeout: 1 });
    assert.match(r.text, /terminated/);
    assert.deepStrictEqual(called().filter((l) => l.startsWith("machinectl")),
      ["machinectl --no-ask-password poweroff yarp", "machinectl --no-ask-password terminate yarp"]);
  });

  await test("start fails for a machine that is missing or does not come up, and can be cancelled", async () => {
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

  await test("enable and disable: the record, which list and status show; now starts or stops too", async () => {
    reset({ images: ["web", "yarp", "handmade"] });
    markImage("web");
    markEnvironment("yarp", "web");
    const image = await c.call("enable", { name: "web" });
    assert(image.isError && /web is an image: create an environment from it, and enable that/.test(image.text), image.text);
    const other = await c.call("enable", { name: "handmade" });
    assert(other.isError && /handmade is not an environment: only an environment is enabled/.test(other.text), other.text);
    const bad = await c.call("enable", { name: "-x" });
    assert(bad.isError && /bad machine name/.test(bad.text), bad.text);
    const extra = await c.call("enable", { name: "yarp", timeout: 5 });
    assert(extra.isError && /enable takes no timeout/.test(extra.text), extra.text);
    assert.strictEqual(fs.readFileSync(record("yarp"), "utf8"), "Image=web\nCreated=2026-01-01T00:00:00.000Z\n");

    const on = await c.call("enable", { name: "yarp" });
    assert(!on.isError, on.text);
    assert.strictEqual(on.text, "yarp is enabled: it starts at boot");
    assert.strictEqual(fs.readFileSync(record("yarp"), "utf8"), "Image=web\nCreated=2026-01-01T00:00:00.000Z\nEnabled=yes\n");
    assert(!called().some((l) => l.includes("start")), "enable without now started it");
    let list = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(list.environments.map((e) => [e.name, e.running, e.enabled]), [["yarp", false, true]]);
    assert.strictEqual(JSON.parse((await c.call("status", { name: "yarp" })).text).enabled, true);
    assert.strictEqual((await c.call("enable", { name: "yarp" })).text, "yarp is enabled: it starts at boot");

    const off = await c.call("disable", { name: "yarp" });
    assert.strictEqual(off.text, "yarp is disabled: it does not start at boot");
    assert.strictEqual(fs.readFileSync(record("yarp"), "utf8"), "Image=web\nCreated=2026-01-01T00:00:00.000Z\nEnabled=no\n");
    assert(!called().some((l) => l.includes("poweroff")), "disable without now stopped it");
    list = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(list.environments.map((e) => [e.name, e.enabled]), [["yarp", false]]);

    // With now: started, or stopped, as well.
    const onNow = await c.call("enable", { name: "yarp", now: true });
    assert(!onNow.isError, onNow.text);
    assert.strictEqual(onNow.text, "yarp is enabled: it starts at boot. yarp is running");
    assert(called().includes("machinectl --no-ask-password start yarp"), called().join("\n"));
    assert.match(fs.readFileSync(record("yarp"), "utf8"), /\nEnabled=yes\n$/);
    list = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(list.environments.map((e) => [e.name, e.running, e.enabled]), [["yarp", true, true]]);
    const offNow = await c.call("disable", { name: "yarp", now: true });
    assert.strictEqual(offNow.text, "yarp is disabled: it does not start at boot. yarp is stopped");
    assert(called().includes("machinectl --no-ask-password poweroff yarp"), called().join("\n"));
    assert.match(fs.readFileSync(record("yarp"), "utf8"), /\nEnabled=no\n$/);
    assert.strictEqual((await c.call("disable", { name: "yarp", now: true })).text,
      "yarp is disabled: it does not start at boot. yarp is not running");

    // A stale record, its machine gone: disabled, not enabled.
    markEnvironment("ghost", "web");
    const ghost = await c.call("enable", { name: "ghost" });
    assert(ghost.isError && /no machine "ghost"/.test(ghost.text), ghost.text);
    assert.strictEqual(fs.readFileSync(record("ghost"), "utf8"), "Image=web\nCreated=2026-01-01T00:00:00.000Z\n");
    assert.strictEqual((await c.call("disable", { name: "ghost" })).text, "ghost is disabled: it does not start at boot");
    assert.match(fs.readFileSync(record("ghost"), "utf8"), /^Image=web\nCreated=2026-01-01T00:00:00.000Z\nEnabled=no\n$/);
  });

  await test("remove takes environments and their records; remove_image takes images without environments", async () => {
    reset({ images: ["web", "yarp"] });
    markImage("web");
    markEnvironment("yarp", "web");
    const image = await c.call("remove", { name: "web" });
    assert(image.isError && /web is an image: remove_image removes it/.test(image.text), image.text);
    const held = await c.call("remove_image", { name: "web" });
    assert(held.isError && /web still has environments \(yarp\): remove them first/.test(held.text), held.text);
    const env = await c.call("remove_image", { name: "yarp" });
    assert(env.isError && /yarp is an environment: remove removes it/.test(env.text), env.text);
    assert(!called().some((l) => l.includes("remove ")), called().join("\n"));

    assert.strictEqual((await c.call("remove", { name: "yarp" })).text, "yarp removed");
    assert(!fs.existsSync(record("yarp")), "the record stayed");
    assert.strictEqual((await c.call("remove_image", { name: "web" })).text, "web removed");
    assert(fs.existsSync(recipe("web", "profile.conf")), "the recipe went with the image");
    assert.deepStrictEqual(called().filter((l) => l.includes("remove ")),
      ["machinectl --no-ask-password remove yarp", "machinectl --no-ask-password remove web"]);
    const again = await c.call("remove", { name: "yarp" });
    assert(again.isError && /No image 'yarp' known/.test(again.text), again.text);
    reset({ images: ["handmade"] });
    assert.strictEqual((await c.call("remove", { name: "handmade" })).text, "handmade removed");
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
