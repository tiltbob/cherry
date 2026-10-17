// cherry-storage-mcp: an MCP server (stdio) that gives the OpenCode agent
// tools for this machine's storage, as the cherry user, through what that user
// may do (50-cherry.rules): cherry-storage@.service, which runs as root, lists
// the disks, pools and volumes, claims disks, which wipes them, and makes pools
// and volumes of them. /etc/opencode/opencode.json starts it with Bun.
//
// Tools: list, claim, release, create_pool, add_disks, set_cache, remove_disk,
// destroy_pool, create_volume, resize_volume, remove_volume. Each runs
// cherry-storage@<verb>:<arguments>.service, and those that change something
// return its log's end and what the pool is now. $CHERRY_STORAGE_LOGS and
// $CHERRY_VOLUMES replace /var/log/cherry-storage and /var/lib/cherry/volumes
// (tests/storage-mcp.js). The protocol is mcp-stdio.js's, next to it.
"use strict";
const fs = require("fs");
const path = require("path");
const { ToolError, check, startUnit, serve } = require("./mcp-stdio.js");

const LOGS = process.env.CHERRY_STORAGE_LOGS || "/var/log/cherry-storage";
const VOLUMES = process.env.CHERRY_VOLUMES || "/var/lib/cherry/volumes";
const INSTRUCTIONS = `These tools manage this Cherry machine's storage with LVM: they list its disks, claim \
disks (which erases them) for Cherry, make storage pools of claimed disks, hard disks or SSDs, with SSDs as \
the pool's cache, and volumes in the pools, which are mounted for you under ${VOLUMES}. Load the storage skill \
before changing anything, then configure-storage to set the machine's storage up, or manage-storage to change it.`;

// As cherry-storage checks them.
const NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,31}$/;
const DISK = /^[a-z][a-z0-9]*$/;
const FINGERPRINT = /^[0-9a-f]{12}$/;
const SIZE = /^(?:[1-9][0-9]*[MGT]|all)$/;
const MODES = ["writethrough", "writeback"];

function name(kind, value) {
  check(typeof value === "string" && NAME.test(value),
    `bad ${kind} name ${JSON.stringify(value)}: up to 32 letters, digits and '-', starting with a letter or digit`);
  check(kind !== "pool" || value !== "cherry", "bad pool name \"cherry\": it is Cherry's own");
  return value;
}

function disk(value) {
  check(typeof value === "string" && DISK.test(value),
    `bad disk ${JSON.stringify(value)}: its kernel name, as list shows it (sda, nvme0n1)`);
  return value;
}

function disks(value, least = 1) {
  check(Array.isArray(value) && value.length >= least && value.length <= 32, `disks must be an array of ${least} to 32 disks`);
  value.forEach(disk);
  check(new Set(value).size === value.length, `a disk is given twice: ${JSON.stringify(value)}`);
  return value;
}

function size(value) {
  check(typeof value === "string" && SIZE.test(value),
    `bad size ${JSON.stringify(value)}: a whole number and M, G or T (MiB, GiB, TiB), e.g. 500G, or all`);
  return value;
}

function storage(instance, signal) {
  return startUnit("cherry-storage", instance, LOGS, signal);
}

function human(bytes) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return null;
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return i === 0 ? `${bytes} B` : `${bytes.toFixed(1)} ${units[i]}`;
}

const report = (r, key) => r?.report?.[0]?.[key] ?? [];
const tags = (s) => (s ? s.split(",") : []);
const sum = (items, key) => items.reduce((n, item) => n + Number(item[key]), 0);

// What cherry-storage@list found, as the agent reads it: the disks, which
// claim may take and why not, and the pools with their disks and volumes.
function view(facts) {
  const pvs = report(facts.pvs, "pv");
  const lvs = report(facts.lvs, "lv");
  const diskOf = new Map(facts.disks.filter((d) => d.pv).map((d) => [d.pv, d.name]));
  const mounts = new Map(facts.mounts.map((m) => [m.path, m]));
  const disks = facts.disks.map((d) => {
    const o = { name: d.name, size: human(d.size), kind: d.rotational ? "hdd" : "ssd", model: d.model || null,
      serial: d.serial || null, state: d.state };
    if (d.reason) o.reason = d.reason;
    if (["free", "locked", "claimed"].includes(d.state)) o.fingerprint = d.fingerprint;
    if (d.state === "free") {
      // What claiming it erases.
      o.blank = d.signatures.length === 0 && d.partitions.length === 0;
      if (!o.blank) {
        o.signatures = d.signatures;
        o.partitions = d.partitions.map((p) => ({ name: p.name, size: human(p.size), label: p.label || null,
          filesystem: p.fstype || null }));
      }
    }
    if (d.state === "claimed") {
      const pv = pvs.find((p) => p.pv_name === d.pv);
      o.pool = pv?.vg_name || null;
      o.role = o.pool ? (tags(pv.pv_tags).includes("cache") ? "cache" : "data") : null;
    }
    return o;
  });
  const pools = report(facts.vgs, "vg").map((vg) => {
    const mine = pvs.filter((p) => p.vg_name === vg.vg_name);
    const data = mine.filter((p) => tags(p.pv_tags).includes("data"));
    const cache = mine.filter((p) => tags(p.pv_tags).includes("cache"));
    const names = (list) => list.map((p) => diskOf.get(p.pv_name) ?? p.pv_name);
    const volumes = lvs.filter((lv) => lv.vg_name === vg.vg_name && !/[_[]/.test(lv.lv_name)).map((lv) => {
      const mountpoint = path.join(VOLUMES, vg.vg_name, lv.lv_name);
      const mounted = mounts.get(mountpoint);
      const cvol = lvs.find((c) => c.vg_name === vg.vg_name && c.lv_name === `[${lv.lv_name}_cache_cpool]`);
      return {
        name: lv.lv_name, size: human(Number(lv.lv_size)), path: mountpoint, mounted: Boolean(mounted),
        ...(mounted && { used: human(mounted.used), available: human(mounted.available) }),
        cache: lv.segtype === "cache" ? { size: human(cvol ? Number(cvol.lv_size) : null), mode: lv.cache_mode } : null,
      };
    });
    const pool = {
      name: vg.vg_name, disks: names(data), size: human(sum(data, "pv_size")), free: human(sum(data, "pv_free")),
      cache: cache.length ? { disks: names(cache), size: human(sum(cache, "pv_size")),
        mode: tags(vg.vg_tags).includes("writeback") ? "writeback" : "writethrough" } : null,
      volumes,
    };
    if (Number(vg.vg_missing_pv_count) > 0) pool.missing_disks = Number(vg.vg_missing_pv_count);
    return pool;
  });
  return { tpm: facts.tpm, var: facts.var, disks, pools };
}

async function state(signal) {
  await storage("list", signal);
  return view(JSON.parse(fs.readFileSync(path.join(LOGS, "list.json"), "utf8")));
}

// The log's end, and the pool as it is now.
async function changed(log, pool, signal) {
  const now = (await state(signal)).pools.find((p) => p.name === pool);
  return `${log}\n\n${now ? JSON.stringify(now, null, 2) : `no pool ${pool} any more`}`;
}

const POOL = { type: "string", description: "The pool's name" };
const DISKS = { type: "array", items: { type: "string" }, description: "Disks by their kernel names, as list shows them" };
const FINGERPRINTED = {
  type: "array", minItems: 1, description: "Each disk as list showed it: its name and its fingerprint",
  items: { type: "object", required: ["name", "fingerprint"], additionalProperties: false, properties: {
    name: { type: "string", description: "Its kernel name, e.g. sdb or nvme0n1" },
    fingerprint: { type: "string", description: "Its fingerprint from list, which must still match" },
  } },
};
const SIZE_ARG = { type: "string", description: "A whole number and M, G or T (MiB, GiB, TiB), e.g. 500G; or all, " +
  "the pool's free space" };

// claim and release, disk by disk: what was done, then the failure.
async function eachDisk(verb, list, signal) {
  check(Array.isArray(list) && list.length >= 1, "disks must be a non-empty array");
  const items = list.map((d) => {
    check(d && typeof d === "object", `bad disk ${JSON.stringify(d)}`);
    disk(d.name);
    check(typeof d.fingerprint === "string" && FINGERPRINT.test(d.fingerprint),
      `bad fingerprint ${JSON.stringify(d.fingerprint)} for ${d.name}: as list shows it`);
    return d;
  });
  check(new Set(items.map((d) => d.name)).size === items.length, "a disk is given twice");
  const done = [];
  for (const d of items) {
    try {
      done.push(await storage(`${verb}:${d.name}:${d.fingerprint}`, signal));
    } catch (e) {
      if (e instanceof ToolError && done.length) e.message = `${done.join("\n\n")}\n\n${e.message}`;
      throw e;
    }
  }
  return done.join("\n\n");
}

const TOOLS = [
  {
    name: "list",
    description: "List this machine's disks and storage pools. Each disk: its kernel name, size, kind (ssd or hdd, " +
      "as the kernel reports it), model, serial, and state: free (claim may take it; blank, or what is on it), " +
      "claimed (Cherry's: in a pool, as data or cache, or not yet), locked (another machine's claimed disk, or " +
      "this one's before its TPM was cleared: claim may take it), in-use (mounted, swap, held, or this machine's " +
      "/var), unsuitable (removable, read-only, USB, under 1 GiB); and its fingerprint, which claim and release " +
      "take. Each pool: its data disks, size and free space, its cache, and its volumes, where they are mounted and " +
      "how full. Also whether /var, where containers live, is on a disk or in RAM.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    async call(args, signal) {
      return JSON.stringify(await state(signal), null, 2);
    },
  },
  {
    name: "claim",
    description: "Claim disks for Cherry: ERASES each disk, then makes it one LUKS2 volume that this machine's TPM " +
      "unlocks, holding an LVM physical volume, ready for a pool. Only disks list shows as free or locked, with " +
      "the fingerprint list showed: a disk that changed since (or was renamed by a reboot) is refused. Only the " +
      "disks the user chose to have erased.",
    inputSchema: { type: "object", properties: { disks: FINGERPRINTED }, required: ["disks"], additionalProperties: false },
    annotations: { destructiveHint: true },
    call(args, signal) {
      return eachDisk("claim", args.disks, signal);
    },
  },
  {
    name: "release",
    description: "Give claimed disks back: ERASES each one, which must be in no pool, and leaves it blank.",
    inputSchema: { type: "object", properties: { disks: FINGERPRINTED }, required: ["disks"], additionalProperties: false },
    annotations: { destructiveHint: true },
    call(args, signal) {
      return eachDisk("release", args.disks, signal);
    },
  },
  {
    name: "create_pool",
    description: "Make a storage pool, an LVM volume group, of claimed disks that are in no pool: the data disks, " +
      "which hold the volumes (their sizes add up; no redundancy), and optionally cache disks, SSDs that cache " +
      "every volume of the pool (dm-cache).",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Up to 32 letters, digits and '-', starting with a letter or digit" },
        disks: { ...DISKS, description: "The data disks" },
        cache: { ...DISKS, description: "SSDs to cache the pool on, if any" },
        cache_mode: { type: "string", enum: MODES, description: "writethrough (default): every write reaches the " +
          "data disks, and losing a cache disk loses nothing; writeback: faster writes, but losing a cache disk " +
          "loses what it had not written back yet" },
      },
      required: ["name", "disks"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const pool = name("pool", args.name);
      const data = disks(args.disks);
      const cache = args.cache === undefined ? [] : disks(args.cache, 0);
      check(args.cache_mode === undefined || (MODES.includes(args.cache_mode) && cache.length),
        "cache_mode is writethrough or writeback, for a pool with cache disks");
      check(!cache.some((d) => data.includes(d)), "a disk cannot be both data and cache");
      const logs = [await storage(`create:${pool}:${data.join(":")}`, signal)];
      if (cache.length) logs.push(await storage(`cache:${pool}:${args.cache_mode ?? "writethrough"}:${cache.join(":")}`, signal));
      return changed(logs.join("\n\n"), pool, signal);
    },
  },
  {
    name: "add_disks",
    description: "Add claimed disks that are in no pool to a pool's data disks: the pool grows by their size. " +
      "For cache disks, use set_cache.",
    inputSchema: { type: "object", properties: { pool: POOL, disks: DISKS }, required: ["pool", "disks"],
      additionalProperties: false },
    async call(args, signal) {
      const pool = name("pool", args.pool);
      return changed(await storage(`extend:${pool}:${disks(args.disks).join(":")}`, signal), pool, signal);
    },
  },
  {
    name: "set_cache",
    description: "Set a pool's cache disks to these SSDs (the pool's cache disks already, or claimed disks in no " +
      "pool), in this mode; none takes the cache away, and disks no longer listed leave the pool, still claimed. " +
      "Every volume's cache is detached (a writeback cache written back first) and made anew, each volume getting " +
      "a share of the cache as large as its share of the data disks: the caches start cold.",
    inputSchema: {
      type: "object",
      properties: {
        pool: POOL,
        disks: { ...DISKS, description: "The cache disks, all of them; empty for no cache" },
        mode: { type: "string", enum: MODES, description: "writethrough (default) or writeback, as for create_pool" },
      },
      required: ["pool", "disks"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const pool = name("pool", args.pool);
      const list = disks(args.disks, 0);
      check(args.mode === undefined || MODES.includes(args.mode), "mode is writethrough or writeback");
      const instance = [`cache:${pool}:${args.mode ?? "writethrough"}`, ...list].join(":");
      return changed(await storage(instance, signal), pool, signal);
    },
  },
  {
    name: "remove_disk",
    description: "Take a disk out of a pool; it stays claimed. A data disk's data is moved to the pool's other data " +
      "disks first (pvmove: as long as copying it takes), which must have room for it; the volumes' caches are " +
      "detached meanwhile. A cache disk: as set_cache without it. Not the pool's last data disk.",
    inputSchema: { type: "object", properties: { pool: POOL, disk: { type: "string", description: "Its kernel name" } },
      required: ["pool", "disk"], additionalProperties: false },
    async call(args, signal) {
      const pool = name("pool", args.pool);
      return changed(await storage(`reduce:${pool}:${disk(args.disk)}`, signal), pool, signal);
    },
  },
  {
    name: "destroy_pool",
    description: "Destroy a pool that has no volumes left (remove them first): its disks stay claimed, in no pool.",
    inputSchema: { type: "object", properties: { name: POOL }, required: ["name"], additionalProperties: false },
    annotations: { destructiveHint: true },
    async call(args, signal) {
      return storage(`destroy:${name("pool", args.name)}`, signal);
    },
  },
  {
    name: "create_volume",
    description: "Make a volume in a pool: an LVM logical volume on its data disks, cached on its cache disks if it " +
      `has any, holding ext4 that you own, mounted on ${VOLUMES}/<pool>/<name> now and at every boot.`,
    inputSchema: {
      type: "object",
      properties: {
        pool: POOL,
        name: { type: "string", description: "Up to 32 letters, digits and '-', starting with a letter or digit" },
        size: SIZE_ARG,
      },
      required: ["pool", "name", "size"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const pool = name("pool", args.pool);
      return changed(await storage(`volume-create:${pool}:${name("volume", args.name)}:${size(args.size)}`, signal),
        pool, signal);
    },
  },
  {
    name: "resize_volume",
    description: "Grow a volume, and its filesystem, while mounted: to this size, or by all of the pool's free " +
      "space. Volumes do not shrink.",
    inputSchema: {
      type: "object",
      properties: { pool: POOL, name: { type: "string", description: "The volume's name" }, size: SIZE_ARG },
      required: ["pool", "name", "size"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const pool = name("pool", args.pool);
      return changed(await storage(`volume-resize:${pool}:${name("volume", args.name)}:${size(args.size)}`, signal),
        pool, signal);
    },
  },
  {
    name: "remove_volume",
    description: "Remove a volume and ALL ITS DATA: unmounted (nothing may be using it), then deleted.",
    inputSchema: { type: "object", properties: { pool: POOL, name: { type: "string", description: "The volume's name" } },
      required: ["pool", "name"], additionalProperties: false },
    annotations: { destructiveHint: true },
    async call(args, signal) {
      const pool = name("pool", args.pool);
      return changed(await storage(`volume-remove:${pool}:${name("volume", args.name)}`, signal), pool, signal);
    },
  },
];

serve({ name: "cherry-storage", instructions: INSTRUCTIONS, tools: TOOLS });
