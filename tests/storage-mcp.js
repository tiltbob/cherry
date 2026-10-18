#!/usr/bin/env node
// package/cherry/cherry-storage-mcp.js speaks MCP over stdio, checks its
// arguments, starts the cherry-storage@ instances the README says, and turns
// what cherry-storage@list found into the agent's view: run with a stand-in
// systemctl on PATH, which logs its arguments, writes a log for each instance,
// fails the one $STATE/FAIL_UNIT names, and for list copies a fixture as
// list.json. Needs node, sh and coreutils; the server runs on node too, or on
// $MCP_RUNTIME (e.g. bun, as on the image).
"use strict";
const assert = require("assert");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

const server = path.join(__dirname, "..", "package", "cherry", "cherry-storage-mcp.js");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cherry-storage-mcp-"));
const dirs = Object.fromEntries(["bin", "logs", "state"].map((d) => [d, path.join(tmp, d)]));
Object.values(dirs).forEach((d) => fs.mkdirSync(d));
const calls = path.join(tmp, "calls");
const fixture = path.join(tmp, "list.json");
const VOLUMES = "/var/lib/cherry/volumes";
const GIB = 2 ** 30;

fs.writeFileSync(path.join(dirs.bin, "systemctl"), `#!/bin/sh
echo "systemctl $*" >>"$CALLS"
unit=$3 instance=\${3#cherry-storage@}; instance=\${instance%.service}
if [ "$instance" = list ]; then
	cp "$FIXTURE" "$LOGS/list.json"
else
	printf 'line %s\\n' $(seq 1 30) >"$LOGS/$instance.log"
	echo "done $instance" >>"$LOGS/$instance.log"
fi
[ "$unit" != "$(cat "$STATE/FAIL_UNIT")" ] || { echo "Job for $unit failed." >&2; exit 1; }
`, { mode: 0o755 });

// What cherry-storage@list writes: the pool cherry on one disk, with var, /var,
// on it; a pool tank of two hard disks, cached writeback on an SSD, with a
// cached volume, mounted, and another one, not; a claimed disk in no pool, and
// disks in every other state.
const pvs = (rows) => ({ report: [{ pv: rows }] });
const disk = (name, state, extra = {}) => ({ name, size: 100 * GIB, rotational: true, model: `Model ${name}`,
  serial: `S${name}`, bus: "ata", state, reason: "", fingerprint: `${name}0000000000`.slice(0, 12), pv: "",
  signatures: [], partitions: [], ...extra });
const FACTS = {
  tpm: true,
  var: { fstype: "btrfs", source: "/dev/mapper/cherry-var", ram: false },
  disks: [
    disk("nvme0n1", "claimed", { size: 10 * GIB, rotational: false, pv: "/dev/mapper/cherry-pv-nvme0n1p1" }),
    disk("sda", "claimed", { pv: "/dev/mapper/cherry-pv-sda1" }),
    disk("sdb", "claimed", { pv: "/dev/mapper/cherry-pv-sdb1" }),
    disk("sdc", "free"),
    disk("sdd", "free", { rotational: false, model: "", serial: "", signatures: ["gpt", "PMBR"],
      partitions: [{ name: "sdd1", size: 2 * GIB, label: "EFI", fstype: "vfat" }] }),
    disk("sde", "locked", { reason: "its cherry-pv volume does not unlock with this machine's TPM" }),
    disk("sdf", "unsuitable", { reason: "on USB" }),
    disk("sdg", "claimed", { pv: "/dev/mapper/cherry-pv-sdg1" }),
    disk("vda", "claimed", { pv: "/dev/mapper/cherry-pv-vda1" }),
  ],
  pvs: pvs([
    { pv_name: "/dev/mapper/cherry-pv-vda1", vg_name: "cherry", pv_size: String(100 * GIB), pv_free: String(60 * GIB), pv_tags: "data" },
    { pv_name: "/dev/mapper/cherry-pv-nvme0n1p1", vg_name: "tank", pv_size: String(10 * GIB), pv_free: String(4 * GIB), pv_tags: "cache" },
    { pv_name: "/dev/mapper/cherry-pv-sda1", vg_name: "tank", pv_size: String(100 * GIB), pv_free: String(20 * GIB), pv_tags: "data" },
    { pv_name: "/dev/mapper/cherry-pv-sdb1", vg_name: "tank", pv_size: String(100 * GIB), pv_free: String(100 * GIB), pv_tags: "data" },
    { pv_name: "/dev/mapper/cherry-pv-sdg1", vg_name: "", pv_size: String(100 * GIB), pv_free: String(100 * GIB), pv_tags: "" },
  ]),
  vgs: { report: [{ vg: [
    { vg_name: "cherry", vg_size: String(100 * GIB), vg_free: String(60 * GIB), vg_tags: "", vg_extent_size: "4194304",
      pv_count: "1", lv_count: "1", vg_missing_pv_count: "0" },
    { vg_name: "tank", vg_size: String(210 * GIB), vg_free: String(124 * GIB), vg_tags: "writeback",
      vg_extent_size: "4194304", pv_count: "3", lv_count: "2", vg_missing_pv_count: "0" },
  ] }] },
  lvs: { report: [{ lv: [
    { vg_name: "cherry", lv_name: "var", lv_size: String(40 * GIB), segtype: "linear", cache_mode: "" },
    { vg_name: "tank", lv_name: "[web_cache_cpool]", lv_size: String(6 * GIB), segtype: "linear", cache_mode: "" },
    { vg_name: "tank", lv_name: "db", lv_size: String(20 * GIB), segtype: "linear", cache_mode: "" },
    { vg_name: "tank", lv_name: "web", lv_size: String(60 * GIB), segtype: "cache", cache_mode: "writeback" },
  ] }] },
  mounts: [{ path: "/var", size: 40 * GIB, used: 2 * GIB, available: 38 * GIB },
    { path: `${VOLUMES}/tank/web`, size: 60 * GIB, used: GIB, available: 59 * GIB }],
};
// The same machine before the pool cherry had disks: in RAM, on its zram device, var 128 MiB, /var in RAM too.
const RAM = {
  ...FACTS,
  var: { ...FACTS.var, ram: true },
  disks: FACTS.disks.map((d) => (d.name === "vda" ? disk("vda", "free") : d)),
  pvs: pvs([{ pv_name: "/dev/zram1", vg_name: "cherry", pv_size: String(128 * 2 ** 20), pv_free: "0", pv_tags: "ram" },
    ...FACTS.pvs.report[0].pv.slice(1)]),
  vgs: { report: [{ vg: [{ ...FACTS.vgs.report[0].vg[0], vg_size: String(128 * 2 ** 20), vg_free: "0" },
    FACTS.vgs.report[0].vg[1]] }] },
  lvs: { report: [{ lv: [{ ...FACTS.lvs.report[0].lv[0], lv_size: String(128 * 2 ** 20) }, ...FACTS.lvs.report[0].lv.slice(1)] }] },
  mounts: [{ path: "/var", size: 128 * 2 ** 20, used: 8 * 2 ** 20, available: 120 * 2 ** 20 }, FACTS.mounts[1]],
};
fs.writeFileSync(fixture, JSON.stringify(FACTS));

let failures = 0;
let cases = 0;
function test(name, fn) {
  return fn().then(() => { cases++; }, (e) => { failures++; console.error(`storage-mcp: ${name}: ${e.stack || e}`); });
}

class Client {
  constructor() {
    this.child = spawn(process.env.MCP_RUNTIME || process.execPath, [server], {
      env: { ...process.env, PATH: `${dirs.bin}:${process.env.PATH}`, CALLS: calls, STATE: dirs.state, LOGS: dirs.logs,
        FIXTURE: fixture, CHERRY_STORAGE_LOGS: dirs.logs, CHERRY_VOLUMES: VOLUMES },
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
  request(method, params) {
    const id = ++this.id;
    return new Promise((resolve) => {
      this.pending.set(id, resolve);
      this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });
  }
  async call(name, args, failUnit) {
    fs.writeFileSync(path.join(dirs.state, "FAIL_UNIT"), failUnit ? `cherry-storage@${failUnit}.service` : "");
    const m = await this.request("tools/call", { name, arguments: args });
    assert(m.result, `no result: ${JSON.stringify(m)}`);
    return { text: m.result.content[0].text, isError: m.result.isError };
  }
  close() { this.child.stdin.end(); }
}

function reset() {
  fs.writeFileSync(calls, "");
}
// The instances started, in order.
const started = () => fs.readFileSync(calls, "utf8").trim().split("\n").filter(Boolean)
  .map((l) => l.replace(/^systemctl start --no-ask-password cherry-storage@(.*)\.service$/, "$1"));

async function main() {
  const c = new Client();
  reset();

  await test("initialize, tools/list", async () => {
    const m = await c.request("initialize", { protocolVersion: "2025-06-18", capabilities: {} });
    assert.strictEqual(m.result.protocolVersion, "2025-06-18");
    assert.strictEqual(m.result.serverInfo.name, "cherry-storage");
    assert.match(m.result.instructions, /storage skill/);
    const { tools } = (await c.request("tools/list")).result;
    assert.deepStrictEqual(tools.map((t) => t.name), ["list", "claim", "release", "create_pool", "add_disks",
      "set_cache", "remove_disk", "destroy_pool", "create_volume", "resize_volume", "remove_volume"]);
    for (const t of tools) {
      assert.strictEqual(t.inputSchema.type, "object", t.name);
      assert(t.description.length > 40, t.name);
      for (const key of t.inputSchema.required ?? []) assert(key in t.inputSchema.properties, `${t.name}: ${key}`);
    }
    for (const name of ["claim", "release", "destroy_pool", "remove_volume"]) {
      assert(tools.find((t) => t.name === name).annotations?.destructiveHint, `${name} is not marked destructive`);
    }
  });

  await test("list: the disks, which claim may take, and the pools", async () => {
    reset();
    const view = JSON.parse((await c.call("list", {})).text);
    assert.deepStrictEqual(started(), ["list"]);
    assert.deepStrictEqual(view.var, FACTS.var);
    const byName = Object.fromEntries(view.disks.map((d) => [d.name, d]));
    assert.deepStrictEqual(byName.sdc, { name: "sdc", size: "100.0 GiB", kind: "hdd", model: "Model sdc", serial: "Ssdc",
      state: "free", fingerprint: "sdc000000000", blank: true });
    assert.deepStrictEqual(byName.sdd, { name: "sdd", size: "100.0 GiB", kind: "ssd", model: null, serial: null,
      state: "free", fingerprint: "sdd000000000", blank: false, signatures: ["gpt", "PMBR"],
      partitions: [{ name: "sdd1", size: "2.0 GiB", label: "EFI", filesystem: "vfat" }] });
    assert.strictEqual(byName.sde.state, "locked");
    assert.strictEqual(byName.sde.fingerprint, "sde000000000");
    assert.match(byName.sde.reason, /does not unlock/);
    assert.deepStrictEqual([byName.sda.pool, byName.sda.role, byName.nvme0n1.pool, byName.nvme0n1.role, byName.nvme0n1.kind],
      ["tank", "data", "tank", "cache", "ssd"]);
    assert.deepStrictEqual([byName.sdg.state, byName.sdg.pool, byName.sdg.role], ["claimed", null, null]);
    assert.deepStrictEqual([byName.vda.state, byName.vda.pool, byName.vda.role], ["claimed", "cherry", "data"]);
    // Nothing to claim with, for disks claim does not take.
    for (const name of ["sdf"]) {
      assert(!("fingerprint" in byName[name]) && byName[name].reason, JSON.stringify(byName[name]));
    }
    assert.deepStrictEqual(view.pools, [{
      name: "cherry", disks: ["vda"], size: "100.0 GiB", free: "60.0 GiB", cache: null,
      volumes: [{ name: "var", size: "40.0 GiB", path: "/var", mounted: true, used: "2.0 GiB", available: "38.0 GiB",
        cache: null }],
    }, {
      name: "tank", disks: ["sda", "sdb"], size: "200.0 GiB", free: "120.0 GiB",
      cache: { disks: ["nvme0n1"], size: "10.0 GiB", mode: "writeback" },
      volumes: [
        { name: "db", size: "20.0 GiB", path: `${VOLUMES}/tank/db`, mounted: false, cache: null },
        { name: "web", size: "60.0 GiB", path: `${VOLUMES}/tank/web`, mounted: true, used: "1.0 GiB",
          available: "59.0 GiB", cache: { size: "6.0 GiB", mode: "writeback" } },
      ],
    }]);
  });

  await test("list: the pool cherry in RAM, on its zram device, and so /var", async () => {
    reset();
    fs.writeFileSync(fixture, JSON.stringify(RAM));
    const view = JSON.parse((await c.call("list", {})).text);
    fs.writeFileSync(fixture, JSON.stringify(FACTS));
    assert.deepStrictEqual(view.var, { fstype: "btrfs", source: "/dev/mapper/cherry-var", ram: true });
    assert.deepStrictEqual(view.pools[0], {
      name: "cherry", disks: [], size: "128.0 MiB", free: "0 B", cache: null, ram: true,
      volumes: [{ name: "var", size: "128.0 MiB", path: "/var", mounted: true, used: "8.0 MiB", available: "120.0 MiB",
        cache: null }],
    });
    assert.strictEqual(view.disks.find((d) => d.name === "vda").state, "free");
  });

  await test("claim and release: disk by disk, with their fingerprints, checked first", async () => {
    const bad = [
      [{}, /disks must be a non-empty array/],
      [{ disks: [] }, /disks must be a non-empty array/],
      [{ disks: ["sdc"] }, /bad disk "sdc"/],
      [{ disks: [{ name: "/dev/sdc", fingerprint: "sdc000000000" }] }, /bad disk "\/dev\/sdc"/],
      [{ disks: [{ name: "sdc", fingerprint: "SDC" }] }, /bad fingerprint "SDC" for sdc/],
      [{ disks: [{ name: "sdc", fingerprint: "0123456789ab" }, { name: "sdc", fingerprint: "0123456789ab" }] },
        /a disk is given twice/],
    ];
    for (const [args, message] of bad) {
      reset();
      const r = await c.call("claim", args);
      assert(r.isError, `accepted: ${JSON.stringify(args)}`);
      assert.match(r.text, message);
      assert.deepStrictEqual(started(), [], `started for ${JSON.stringify(args)}`);
    }
    reset();
    const two = [{ name: "sdc", fingerprint: "0123456789ab" }, { name: "sde", fingerprint: "ba9876543210" }];
    const r = await c.call("claim", { disks: two });
    assert(!r.isError, r.text);
    assert.deepStrictEqual(started(), ["claim:sdc:0123456789ab", "claim:sde:ba9876543210"]);
    assert.match(r.text, /done claim:sdc:0123456789ab[\s\S]*done claim:sde:ba9876543210/);
    // The second fails: the first's log comes first, then the failure, and no third.
    reset();
    const three = [...two, { name: "sdg", fingerprint: "000000000000" }];
    const failed = await c.call("claim", { disks: three }, "claim:sde:ba9876543210");
    assert(failed.isError);
    assert.match(failed.text, /done claim:sdc:0123456789ab[\s\S]*cherry-storage@claim:sde:ba9876543210\.service failed/);
    assert.deepStrictEqual(started(), ["claim:sdc:0123456789ab", "claim:sde:ba9876543210"]);
    reset();
    assert(!(await c.call("release", { disks: [two[0]] })).isError);
    assert.deepStrictEqual(started(), ["release:sdc:0123456789ab"]);
  });

  await test("create_pool: the pool, then its cache; the pool as it is after", async () => {
    const bad = [
      [{ name: "my_pool", disks: ["sda"] }, /bad pool name "my_pool"/],
      [{ name: "tank", disks: [] }, /disks must be an array of 1 to 32 disks/],
      [{ name: "tank", disks: ["sda", "sda"] }, /a disk is given twice/],
      [{ name: "tank", disks: ["sda"], cache: ["sda"] }, /both data and cache/],
      [{ name: "tank", disks: ["sda"], cache_mode: "writeback" }, /for a pool with cache disks/],
      [{ name: "tank", disks: ["sda"], cache: ["nvme0n1"], cache_mode: "fast" }, /writethrough or writeback/],
    ];
    for (const [args, message] of bad) {
      reset();
      const r = await c.call("create_pool", args);
      assert(r.isError && message.test(r.text), `${JSON.stringify(args)}: ${r.text}`);
      assert.deepStrictEqual(started(), []);
    }
    reset();
    const plain = await c.call("create_pool", { name: "tank", disks: ["sda", "sdb"] });
    assert(!plain.isError, plain.text);
    assert.deepStrictEqual(started(), ["create:tank:sda:sdb", "list"]);
    assert.match(plain.text, /done create:tank:sda:sdb\n\n\{\n {2}"name": "tank"/);
    reset();
    await c.call("create_pool", { name: "tank", disks: ["sda", "sdb"], cache: ["nvme0n1"] });
    assert.deepStrictEqual(started(), ["create:tank:sda:sdb", "cache:tank:writethrough:nvme0n1", "list"]);
    reset();
    await c.call("create_pool", { name: "tank", disks: ["sda"], cache: ["nvme0n1", "nvme1n1"], cache_mode: "writeback" });
    assert.deepStrictEqual(started(), ["create:tank:sda", "cache:tank:writeback:nvme0n1:nvme1n1", "list"]);
    // The pool fails: no cache.
    reset();
    const r = await c.call("create_pool", { name: "tank", disks: ["sda"], cache: ["nvme0n1"] }, "create:tank:sda");
    assert(r.isError && /cherry-storage@create:tank:sda\.service failed/.test(r.text), r.text);
    assert.deepStrictEqual(started(), ["create:tank:sda"]);
    // The pool cherry is there from boot: its disks are an extend, which moves /var onto them.
    reset();
    const own = await c.call("create_pool", { name: "cherry", disks: ["sda", "sdb"], cache: ["nvme0n1"] });
    assert(!own.isError, own.text);
    assert.deepStrictEqual(started(), ["extend:cherry:sda:sdb", "cache:cherry:writethrough:nvme0n1", "list"]);
    assert.match(own.text, /done extend:cherry:sda:sdb[\s\S]*"name": "cherry"/);
  });

  await test("the other tools: their instances", async () => {
    const cases = [
      ["add_disks", { pool: "tank", disks: ["sdg"] }, ["extend:tank:sdg", "list"]],
      ["set_cache", { pool: "tank", disks: ["nvme0n1", "nvme1n1"], mode: "writeback" },
        ["cache:tank:writeback:nvme0n1:nvme1n1", "list"]],
      ["set_cache", { pool: "tank", disks: [] }, ["cache:tank:writethrough", "list"]],
      ["remove_disk", { pool: "tank", disk: "sdb" }, ["reduce:tank:sdb", "list"]],
      ["destroy_pool", { name: "tank" }, ["destroy:tank"]],
      ["create_volume", { pool: "tank", name: "web", size: "500G" }, ["volume-create:tank:web:500G", "list"]],
      ["create_volume", { pool: "tank", name: "rest", size: "all" }, ["volume-create:tank:rest:all", "list"]],
      ["resize_volume", { pool: "tank", name: "web", size: "2T" }, ["volume-resize:tank:web:2T", "list"]],
      ["remove_volume", { pool: "tank", name: "web" }, ["volume-remove:tank:web", "list"]],
    ];
    for (const [tool, args, instances] of cases) {
      reset();
      const r = await c.call(tool, args);
      assert(!r.isError, `${tool}: ${r.text}`);
      assert.deepStrictEqual(started(), instances, tool);
    }
    reset();
    const gone = await c.call("remove_disk", { pool: "gone", disk: "sdb" });
    assert.match(gone.text, /no pool gone any more/);
    const bad = [
      ["create_volume", { pool: "tank", name: "web", size: "1.5T" }, /bad size "1\.5T"/],
      ["create_volume", { pool: "tank", name: "web", size: "500GB" }, /bad size "500GB"/],
      ["create_volume", { pool: "tank", name: "w_eb", size: "1G" }, /bad volume name "w_eb"/],
      ["resize_volume", { pool: "tank", name: "web", size: 10 }, /bad size 10/],
      ["set_cache", { pool: "tank", disks: ["nvme0n1"], mode: "wb" }, /writethrough or writeback/],
      ["remove_disk", { pool: "tank", disk: "sdb1:x" }, /bad disk "sdb1:x"/],
      ["add_disks", { pool: "tank", disks: ["sda", "sdb:create"] }, /bad disk "sdb:create"/],
    ];
    for (const [tool, args, message] of bad) {
      reset();
      const r = await c.call(tool, args);
      assert(r.isError && message.test(r.text), `${tool} ${JSON.stringify(args)}: ${r.text}`);
      assert.deepStrictEqual(started(), [], tool);
    }
  });

  c.close();
}

main().then(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
  if (failures) {
    console.error(`storage-mcp: ${failures} of ${failures + cases} cases failed`);
    process.exit(1);
  }
  console.log(`storage-mcp: ${cases} cases passed`);
});
