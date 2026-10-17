#!/usr/bin/env node
// package/cherry/cherry-machines-mcp.js speaks MCP over stdio, writes the
// recipe that cherry-image reads, and drives cherry-image@.service and
// machinectl as the README says: run with stand-ins for systemctl, machinectl
// and busctl on PATH, which log their arguments and keep the machine's state
// (images, running containers) in files, and the recipe and log directories
// in a temporary one. Needs node, sh and coreutils' timeout; the server runs
// on node too, or on $MCP_RUNTIME (e.g. bun, as on the image).
"use strict";
const assert = require("assert");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

const server = path.join(__dirname, "..", "package", "cherry", "cherry-machines-mcp.js");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cherry-mcp-"));
const dirs = Object.fromEntries(["bin", "recipes", "logs", "state"].map((d) => [d, path.join(tmp, d)]));
Object.values(dirs).forEach((d) => fs.mkdirSync(d));
const calls = path.join(tmp, "calls");

// The machine's state, in files of $STATE with a name per line: images,
// running containers, and dead ones (running, but never letting a shell in).
const STANDINS = {
  // cherry-image@<verb>:<name>.service: a log, and a failure for $FAIL_UNIT.
  systemctl: `
echo "systemctl $*" >>"$CALLS"
unit=$3 instance=\${3#cherry-image@}; instance=\${instance%.service}
printf 'line %s\\n' $(seq 1 100) >"$LOGS/$instance.log"
echo "end of $instance" >>"$LOGS/$instance.log"
[ "$unit" != "$FAIL_UNIT" ] || { echo "Job for $unit failed." >&2; exit 1; }`,
  // shell runs the command here, through a stand-in pty: CRLF line ends, an
  // escape sequence first, and nothing at all once stdin is at EOF, as
  // systemd's pty forwarder ends then.
  machinectl: `
echo "machinectl $*" >>"$CALLS"
[ "$1" != --no-ask-password ] || shift
while case $1 in --setenv=*) export "\${1#--setenv=}" ;; *) false ;; esac; do shift; done
case $1 in
show-image) grep -qxF "$2" "$STATE/images" ;;
remove) grep -qxF "$2" "$STATE/images" || { echo "No image '$2' known" >&2; exit 1; }
	grep -qxF "$2" "$STATE/running" && { echo "$2 is running" >&2; exit 1; }
	grep -vxF "$2" "$STATE/images" >"$STATE/i"; mv "$STATE/i" "$STATE/images" ;;
start) grep -qxF "$2" "$STATE/images" || { echo "No image '$2' known" >&2; exit 1; }
	echo "$2" >>"$STATE/running" ;;
poweroff) [ -n "$KEEP_RUNNING" ] || { grep -vxF "$2" "$STATE/running" >"$STATE/r"; mv "$STATE/r" "$STATE/running"; } ;;
terminate) grep -vxF "$2" "$STATE/running" >"$STATE/r"; mv "$STATE/r" "$STATE/running" ;;
-q) [ "$2" = shell ] && [ "$4" = /bin/sh ] && [ "$5" = -c ] || exit 1
	grep -qxF "$3" "$STATE/running" && ! grep -qxF "$3" "$STATE/dead" ||
		{ echo "Failed to get shell PTY: machine $3 is not up" >&2; exit 1; }
	if timeout 0.5 cat >/dev/null; then exit 0; fi
	printf '\\033]3008;start=x\\033\\\\'
	/bin/sh -c "$6" "$7" "$8" 2>&1 | sed 's/$/\\r/' ;;
*) exit 1 ;;
esac`,
  busctl: `
echo "busctl $*" >>"$CALLS"
case $6 in
ListImages) printf '{"type":"a(ssbttto)","data":[[["web","directory",false,1,1700000000000000,123456,"/i/web"],["arch","directory",true,1,0,18446744073709551615,"/i/arch"]]]}\\n' ;;
ListMachines) printf '{"type":"a(ssso)","data":[[[".host","host","","/m/host"]'
	while read -r m; do printf ',["%s","container","nspawn","/m/%s"]' "$m" "$m"; done <"$STATE/running"
	printf ']]}\\n' ;;
*) exit 1 ;;
esac`,
};
for (const [name, body] of Object.entries(STANDINS)) {
  fs.writeFileSync(path.join(dirs.bin, name), `#!/bin/sh\n${body}\n`, { mode: 0o755 });
}

let failures = 0;
let cases = 0;
function test(name, fn) {
  return fn().then(() => { cases++; }, (e) => { failures++; console.error(`mcp: ${name}: ${e.stack || e}`); });
}

class Client {
  constructor() {
    this.child = spawn(process.env.MCP_RUNTIME || process.execPath, [server], {
      env: { ...process.env, PATH: `${dirs.bin}:${process.env.PATH}`, CALLS: calls, STATE: dirs.state, LOGS: dirs.logs,
        CHERRY_IMAGES_DIR: dirs.recipes, CHERRY_IMAGE_LOGS: dirs.logs },
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
  async call(name, args, env) {
    // Stand-ins read their switches from files, as the server's environment is fixed.
    for (const key of ["FAIL_UNIT", "KEEP_RUNNING"]) fs.writeFileSync(path.join(dirs.state, key), env?.[key] ?? "");
    const m = await this.request("tools/call", { name, arguments: args });
    assert(m.result, `no result: ${JSON.stringify(m)}`);
    return { text: m.result.content[0].text, isError: m.result.isError };
  }
  close() { this.child.stdin.end(); }
}

// The switches the stand-ins take from the state directory.
for (const name of ["systemctl", "machinectl"]) {
  const file = path.join(dirs.bin, name);
  fs.writeFileSync(file, fs.readFileSync(file, "utf8").replace("#!/bin/sh\n",
    '#!/bin/sh\nFAIL_UNIT=$(cat "$STATE/FAIL_UNIT") KEEP_RUNNING=$(cat "$STATE/KEEP_RUNNING")\n'));
}

function reset(state = {}) {
  fs.writeFileSync(calls, "");
  for (const key of ["images", "running", "dead"]) {
    fs.writeFileSync(path.join(dirs.state, key), (state[key] ?? []).map((i) => i + "\n").join(""));
  }
}
const called = () => fs.readFileSync(calls, "utf8").trim().split("\n").filter(Boolean);
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

  await test("tools/list", async () => {
    const { tools } = (await c.request("tools/list")).result;
    assert.deepStrictEqual(tools.map((t) => t.name), ["list", "build", "start", "stop", "exec", "remove"]);
    for (const t of tools) {
      assert.strictEqual(t.inputSchema.type, "object", t.name);
      assert(t.description.length > 40, t.name);
      for (const key of t.inputSchema.required ?? []) assert(key in t.inputSchema.properties, `${t.name}: ${key}`);
    }
  });

  await test("errors", async () => {
    assert.strictEqual((await c.request("tools/call", { name: "nope", arguments: {} })).error.code, -32602);
    assert.strictEqual((await c.request("nope/nope")).error.code, -32601);
  });

  const full = {
    name: "web", distribution: "debian", release: "trixie", packages: ["nginx-light", "curl"],
    files: [
      { path: "/etc/motd", content: "hello\n" },
      { path: "//usr/bin/hi", content: "#!/bin/sh\necho hi\n", mode: "0755" },
      { path: "/usr/share/blob", content: Buffer.from([0, 1, 255]).toString("base64"), encoding: "base64" },
      { path: "/etc/systemd/system/multi-user.target.wants/nginx.service", symlink: "/usr/lib/systemd/system/nginx.service" },
    ],
    postinst: "systemctl enable systemd-networkd",
    nspawn: "[Exec]\nEnvironment=MODE=test",
  };

  await test("build writes the recipe and starts cherry-image@build:", async () => {
    reset();
    fs.mkdirSync(recipe("web", "extra", "old"), { recursive: true });
    const r = await c.call("build", full);
    assert(!r.isError, r.text);
    assert.match(r.text, /end of build:web/);
    assert.doesNotMatch(r.text, /line 50\n/, "more than the log's end");
    assert.strictEqual(fs.readFileSync(recipe("web", "image.conf"), "utf8"),
      "Distribution=debian\nRelease=trixie\nPackages=nginx-light curl\n");
    assert.strictEqual(fs.readFileSync(recipe("web", "extra", "etc", "motd"), "utf8"), "hello\n");
    assert.strictEqual(fs.statSync(recipe("web", "extra", "etc", "motd")).mode & 0o7777, 0o644);
    assert.strictEqual(fs.statSync(recipe("web", "extra", "usr", "bin", "hi")).mode & 0o7777, 0o755);
    assert.deepStrictEqual([...fs.readFileSync(recipe("web", "extra", "usr", "share", "blob"))], [0, 1, 255]);
    assert.strictEqual(fs.readlinkSync(recipe("web", "extra", "etc", "systemd", "system", "multi-user.target.wants",
      "nginx.service")), "/usr/lib/systemd/system/nginx.service");
    assert.strictEqual(fs.readFileSync(recipe("web", "postinst"), "utf8"),
      "#!/bin/sh\nset -eu\nsystemctl enable systemd-networkd\n");
    assert.strictEqual(fs.statSync(recipe("web", "postinst")).mode & 0o111, 0o111);
    assert.strictEqual(fs.readFileSync(recipe("web", "image.nspawn"), "utf8"), "[Exec]\nEnvironment=MODE=test\n");
    assert(!fs.existsSync(recipe("web", "extra", "old")), "the old recipe was not replaced");
    assert.deepStrictEqual(called(), ["systemctl start --no-ask-password cherry-image@build:web.service"]);
  });

  await test("build keeps a postinst's own #! line, and leaves out what is not given", async () => {
    reset();
    const r = await c.call("build", { name: "a1", distribution: "arch", postinst: "#!/bin/bash\necho hi" });
    assert(!r.isError, r.text);
    assert.strictEqual(fs.readFileSync(recipe("a1", "image.conf"), "utf8"), "Distribution=arch\nRelease=\n");
    assert.strictEqual(fs.readFileSync(recipe("a1", "postinst"), "utf8"), "#!/bin/bash\necho hi\n");
    assert(!fs.existsSync(recipe("a1", "image.nspawn")));
  });

  await test("build refuses bad arguments before writing anything", async () => {
    const bad = [
      [{ ...full, name: "my_web" }, /bad image name/],
      [{ ...full, name: "-web" }, /bad image name/],
      [{ ...full, name: "w".repeat(49) }, /bad image name/],
      [{ ...full, name: "bad1", distribution: "" }, /bad distribution/],
      [{ ...full, name: "bad2", release: "-x" }, /bad release/],
      [{ ...full, name: "bad3", packages: ["-o", "x"] }, /bad packages/],
      [{ ...full, name: "bad4", packages: ["a b"] }, /bad packages/],
      [{ ...full, name: "bad5", files: [{ path: "etc/motd", content: "" }] }, /bad file path/],
      [{ ...full, name: "bad6", files: [{ path: "/etc/../../x", content: "" }] }, /bad file path/],
      [{ ...full, name: "bad7", files: [{ path: "/", content: "" }] }, /bad file path/],
      [{ ...full, name: "bad8", files: [{ path: "/x", content: "", mode: "u+x" }] }, /bad mode/],
      [{ ...full, name: "bad9", files: [{ path: "/x" }] }, /no content/],
    ];
    for (const [args, message] of bad) {
      reset();
      const r = await c.call("build", args);
      assert(r.isError, `accepted: ${JSON.stringify(args)}`);
      assert.match(r.text, message);
      assert.deepStrictEqual(called(), [], `called for ${JSON.stringify(args)}`);
      assert(!fs.existsSync(recipe(args.name)), `wrote a recipe for ${args.name}`);
    }
  });

  await test("a failed build returns the log's end", async () => {
    reset();
    const r = await c.call("build", { ...full, name: "broken" }, { FAIL_UNIT: "cherry-image@build:broken.service" });
    assert(r.isError);
    assert.match(r.text, /cherry-image@build:broken\.service failed/);
    assert.match(r.text, new RegExp(`${dirs.logs}/build:broken\\.log`));
    assert.match(r.text, /line 99\nline 100\nend of build:broken/);
  });

  await test("build with replace removes the image first", async () => {
    reset({ images: ["web"] });
    const r = await c.call("build", { ...full, replace: true });
    assert(!r.isError, r.text);
    assert.deepStrictEqual(called(), ["machinectl show-image web", "machinectl --no-ask-password remove web",
      "systemctl start --no-ask-password cherry-image@build:web.service"]);
    reset({ images: ["web"], running: ["web"] });
    const running = await c.call("build", { ...full, replace: true });
    assert(running.isError && /web is running/.test(running.text), running.text);
    assert(!called().some((l) => l.startsWith("systemctl")), "built over a running image");
  });

  // Images never leave the machine: no tool writes one out.
  await test("no export", async () => {
    reset();
    const m = await c.request("tools/call", { name: "export", arguments: { name: "web", format: "tar" } });
    assert.strictEqual(m.error?.code, -32602, JSON.stringify(m));
    assert.deepStrictEqual(called(), []);
  });

  await test("list", async () => {
    reset({ running: ["web"] });
    const r = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(r.images, [
      { name: "web", type: "directory", readOnly: false, modified: "2023-11-14T22:13:20.000Z", bytes: 123456 },
      { name: "arch", type: "directory", readOnly: true, modified: null, bytes: null },
    ]);
    assert.deepStrictEqual(r.running, [{ name: "web", class: "container", service: "nspawn" }]);
    assert(r.recipes.includes("web") && r.recipes.includes("a1"), JSON.stringify(r.recipes));
    assert.deepStrictEqual(Object.keys(r), ["images", "running", "recipes"]);
  });

  await test("start, exec, stop", async () => {
    reset({ images: ["web"] });
    const started = await c.call("start", { name: "web" });
    assert(!started.isError, started.text);
    assert.strictEqual(started.text, "web is running");
    assert(called().includes("machinectl --no-ask-password start web"), called().join("\n"));

    const r = await c.call("exec", { name: "web", command: "echo one; echo two >&2; exit 3" });
    assert(!r.isError, r.text);
    assert.strictEqual(r.text, "exit status 3\none\ntwo");
    const quoted = await c.call("exec", { name: "web", command: "printf '%s\\n' \"$0\" 'it'\"'\"'s'" });
    assert.strictEqual(quoted.text, "exit status 0\nsh\nit's");
    const env = await c.call("exec", { name: "web", command: "echo $TERM $SYSTEMD_PAGER $DEBIAN_FRONTEND" });
    assert.strictEqual(env.text, "exit status 0\ndumb cat noninteractive");
    const slow = await c.call("exec", { name: "web", command: "sleep 10", timeout: 1 });
    assert(slow.isError && /timed out after 1 s/.test(slow.text), slow.text);

    const id = c.id + 1;
    const pending = c.call("exec", { name: "web", command: "sleep 10" });
    setTimeout(() => c.send({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: id } }), 300);
    const cancelled = await pending;
    assert(cancelled.isError && /cancelled/.test(cancelled.text), cancelled.text);

    const stopped = await c.call("stop", { name: "web" });
    assert.strictEqual(stopped.text, "web is stopped");
    assert((await c.call("exec", { name: "web", command: "true" })).isError, "exec in a stopped container");
    assert.strictEqual((await c.call("stop", { name: "web" })).text, "web is not running");
  });

  await test("stop terminates what does not shut down", async () => {
    reset({ running: ["web"] });
    const r = await c.call("stop", { name: "web", timeout: 1 }, { KEEP_RUNNING: "1" });
    assert.match(r.text, /terminated/);
    assert.deepStrictEqual(called().filter((l) => l.startsWith("machinectl")),
      ["machinectl --no-ask-password poweroff web", "machinectl --no-ask-password terminate web"]);
  });

  await test("start fails for an image that is missing or does not come up", async () => {
    reset({ images: ["dead"], dead: ["dead"] });
    const missing = await c.call("start", { name: "ghost" });
    assert(missing.isError && /No image 'ghost' known/.test(missing.text), missing.text);
    const dead = await c.call("start", { name: "dead", timeout: 1 });
    assert(dead.isError && /did not come up within 1 s: Failed to get shell PTY/.test(dead.text), dead.text);
    const bad = await c.call("start", { name: "-x" });
    assert(bad.isError && /bad machine name/.test(bad.text), bad.text);
    assert(!called().some((l) => l.includes("-x")), "called for -x");
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
  fs.rmSync(tmp, { recursive: true, force: true });
  if (failures) {
    console.error(`mcp: ${failures} of ${failures + cases} cases failed`);
    process.exit(1);
  }
  console.log(`mcp: ${cases} cases passed`);
});
