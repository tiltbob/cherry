// cherry environments: what the cherry user may do with the container images
// and environments of this machine, as one module for the two front ends
// that expose it: the OpenCode MCP server (cherry-machines-mcp.js) and the
// OpenChamber extension's service (openchamber-extension/service/main.js).
// Everything goes through what that user may do (50-cherry.rules):
// cherry-image@.service builds images, machinectl clones, starts, stops,
// enters and removes them, and machined's bus API lists them. Images stay on
// this machine: nothing here writes one out. Running commands and starting
// units is mcp-stdio.js's, next to it.
//
// Two kinds of thing, both directories machinectl knows in /var/lib/machines:
//
// - An image is built from a profile: a recipe as `cherry-image build` reads
//   it (image.conf, extra/, postinst, image.nspawn), plus profile.conf with
//   Title= and Description=, in /usr/lib/cherry/profiles/<id>/ (the image's)
//   or /var/lib/cherry/profiles/<id>/ (the machine's own, which win). build
//   copies the profile to /var/lib/cherry/images/<name>/, the recipe, and
//   builds it; the recipe is what marks a machine as an image. An image is
//   never started: it is what environments are made from.
// - An environment is a clone of an image (machinectl clone: a copy, with the
//   image's nspawn settings), started, stopped and removed on its own, with
//   its own changes. /var/lib/cherry/environments/<name>/environment.conf
//   records the image it came from; that record is what marks a machine as an
//   environment. A machine with neither, made by hand with cherry-bootstrap,
//   is listed as "other": it can be cloned, started and stopped, but not
//   built or rebuilt.
//
// Nothing here takes a package list, a file or a script: those are a
// profile's, written by a person. $CHERRY_IMAGES_DIR, $CHERRY_IMAGE_LOGS,
// $CHERRY_ENVIRONMENTS_DIR and $CHERRY_PROFILE_DIRS (colon separated) replace
// the paths above (tests/mcp.js, tests/extension.js).
"use strict";
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { ToolError, check, run, sleep, tail, startUnit } = require("./mcp-stdio.js");

const RECIPES = process.env.CHERRY_IMAGES_DIR || "/var/lib/cherry/images";
const LOGS = process.env.CHERRY_IMAGE_LOGS || "/var/log/cherry-image";
const ENVIRONMENTS = process.env.CHERRY_ENVIRONMENTS_DIR || "/var/lib/cherry/environments";
const PROFILE_DIRS = (process.env.CHERRY_PROFILE_DIRS || "/var/lib/cherry/profiles:/usr/lib/cherry/profiles")
  .split(":").filter(Boolean);

// Names cherry-image builds, and environments take: valid machine names and
// hostnames.
const NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,47}$/;
// Any image machinectl knows, as cherry-bootstrap@ names them.
const MACHINE_NAME = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;
// A profile's directory name.
const PROFILE_ID = /^[a-z0-9][a-z0-9-]{0,47}$/;
const ESCAPES = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|[=>78cDEHM])/g;

function imageName(name) {
  check(typeof name === "string" && NAME.test(name),
    `bad name ${JSON.stringify(name)}: up to 48 letters, digits and '-', starting with a letter or digit`);
  return name;
}

function machineName(name) {
  check(typeof name === "string" && MACHINE_NAME.test(name), `bad machine name ${JSON.stringify(name)}`);
  return name;
}

function profileId(id) {
  check(typeof id === "string" && PROFILE_ID.test(id),
    `bad profile ${JSON.stringify(id)}: up to 48 lowercase letters, digits and '-'`);
  return id;
}

// The build log of an image: its file, and its last lines, or null without one.
function log(name, lines = 60) {
  imageName(name);
  const file = path.join(LOGS, `build:${name}.log`);
  try {
    return { file, text: tail(fs.readFileSync(file, "utf8"), lines) };
  } catch {
    return { file, text: null };
  }
}

// The reply of a machined Manager method, as busctl --json=short gives it.
async function machined(method, args = []) {
  const r = await run("busctl", ["--json=short", "call", "org.freedesktop.machine1", "/org/freedesktop/machine1",
    "org.freedesktop.machine1.Manager", method, ...args]);
  if (r.code !== 0) throw new ToolError(`machined ${method}: ${(r.err || r.out).trim()}`);
  return JSON.parse(r.out).data;
}

// Every machine machinectl knows, but the host.
async function knownMachines() {
  const [images] = await machined("ListImages");
  const unknown = (n) => (n >= 2 ** 63 ? null : n);
  return images
    .filter(([name]) => name !== ".host")
    .map(([name, type, readOnly, , mtime, usage]) =>
      ({ name, type, readOnly, modified: mtime ? new Date(mtime / 1000).toISOString() : null, bytes: unknown(usage) }));
}

async function runningMachines() {
  const [machines] = await machined("ListMachines");
  return machines.filter(([, cls]) => cls !== "host").map(([name, cls, service]) => ({ name, class: cls, service }));
}

async function running(name) {
  return (await runningMachines()).some((m) => m.name === name);
}

async function known(name) {
  return (await run("machinectl", ["show-image", name])).code === 0;
}

async function machinectl(args, signal) {
  const r = await run("machinectl", ["--no-ask-password", ...args], { signal });
  if (r.code !== 0) throw new ToolError(`machinectl ${args.join(" ")}: ${(r.err || r.out).trim()}`);
  return r.out.trim();
}

// A command in a running container, through machinectl shell: a login session
// on a pty, with no exit status of its own, so the command prints a marker
// with its status. Nobody answers the pty: no pager, colours or dialogs. The
// pty forwarder ends at EOF on its stdin, which stays open.
const SHELL_ENVIRONMENT = ["TERM=dumb", "PAGER=cat", "SYSTEMD_PAGER=cat", "NO_COLOR=1", "DEBIAN_FRONTEND=noninteractive"];
async function shell(name, command, timeout, signal) {
  const marker = `@@cherry-exit-${crypto.randomBytes(6).toString("hex")}:`;
  const script = `sh -c "$1"; printf '\\n%s%d@@\\n' '${marker}' "$?"`;
  const r = await run("machinectl", ["--no-ask-password", ...SHELL_ENVIRONMENT.map((e) => `--setenv=${e}`),
    "-q", "shell", name, "/bin/sh", "-c", script, "sh", command], { timeout, signal, keepStdin: true });
  const out = r.out.replace(/\r/g, "").replace(ESCAPES, "");
  const at = out.lastIndexOf(marker);
  if (at < 0) {
    const why = signal?.aborted ? "cancelled" : r.code === null ? `timed out after ${timeout} s` :
      (r.err.trim() || `machinectl exited with ${r.code}`);
    return { exit: null, output: out.trim(), error: why };
  }
  const exit = parseInt(out.slice(at + marker.length), 10);
  return { exit, output: out.slice(0, at).replace(/\n+$/, "") };
}

// Key=value lines, as image.conf, profile.conf and environment.conf hold
// them; null without the file. Packages= lines add up, as cherry-image reads
// them.
function readConf(file) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return null;
  }
  const conf = {};
  for (const line of text.split("\n")) {
    if (!line.trim() || line.startsWith("#")) continue;
    const at = line.indexOf("=");
    if (at < 0) continue;
    const key = line.slice(0, at);
    const value = line.slice(at + 1);
    conf[key] = key === "Packages" && conf[key] ? `${conf[key]} ${value}` : value;
  }
  return conf;
}

// ---- Profiles ----

function describeProfile(id, dir) {
  const image = readConf(path.join(dir, "image.conf"));
  if (!image) return null;
  const meta = readConf(path.join(dir, "profile.conf")) || {};
  return {
    id,
    title: meta.Title || id,
    description: meta.Description || "",
    distribution: image.Distribution || "",
    release: image.Release || "",
    packages: (image.Packages || "").split(/\s+/).filter(Boolean),
    dir,
  };
}

// The profiles, by id: the first directory that has one wins.
function profiles() {
  const found = new Map();
  for (const dir of PROFILE_DIRS) {
    let entries;
    try {
      entries = fs.readdirSync(dir);
    } catch {
      continue;
    }
    for (const id of entries) {
      if (!PROFILE_ID.test(id) || found.has(id)) continue;
      const p = describeProfile(id, path.join(dir, id));
      if (p) found.set(id, p);
    }
  }
  return [...found.values()].sort((a, b) => a.id.localeCompare(b.id));
}

function profile(id) {
  profileId(id);
  const all = profiles();
  const p = all.find((candidate) => candidate.id === id);
  check(p, `no profile ${JSON.stringify(id)}; the profiles are ${all.map((c) => c.id).join(", ") || "none"}`);
  return p;
}

const publicProfile = ({ dir, ...p }) => p;

// ---- What marks an image, and an environment ----

// The profile an image was built from, from the recipe build left: null for
// a machine that is no image of ours.
function recipeProfile(name) {
  const meta = readConf(path.join(RECIPES, name, "profile.conf"));
  return meta && meta.Profile ? { id: meta.Profile, title: meta.Title || meta.Profile } : null;
}

// An environment's record: the image it was cloned from; null for a machine
// that is no environment.
function record(name) {
  const conf = readConf(path.join(ENVIRONMENTS, name, "environment.conf"));
  return conf && conf.Image ? { image: conf.Image, created: conf.Created || null } : null;
}

function writeRecord(name, image) {
  const dir = path.join(ENVIRONMENTS, name);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "environment.conf"), `Image=${image}\nCreated=${new Date().toISOString()}\n`);
}

function dropRecord(name) {
  fs.rmSync(path.join(ENVIRONMENTS, name), { recursive: true, force: true });
}

// The environments cloned from an image, by their records.
function environmentsOf(image) {
  let names;
  try {
    names = fs.readdirSync(ENVIRONMENTS);
  } catch {
    return [];
  }
  return names.filter((name) => MACHINE_NAME.test(name) && record(name)?.image === image).sort();
}

function kindOf(name) {
  if (recipeProfile(name)) return "image";
  if (record(name)) return "environment";
  return "other";
}

// cp -a, without following symbolic links, and with the modes kept.
function copyTree(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    const source = path.join(from, entry.name);
    const target = path.join(to, entry.name);
    if (entry.isSymbolicLink()) {
      fs.symlinkSync(fs.readlinkSync(source), target);
    } else if (entry.isDirectory()) {
      copyTree(source, target);
    } else {
      fs.copyFileSync(source, target);
      fs.chmodSync(target, fs.statSync(source).mode & 0o7777);
    }
  }
}

// The recipe directory, written anew from the profile, with the profile's id
// recorded in its profile.conf.
function writeRecipe(name, p) {
  const dir = path.join(RECIPES, name);
  fs.rmSync(dir, { recursive: true, force: true });
  copyTree(p.dir, dir);
  const conf = path.join(dir, "profile.conf");
  let text = "";
  try {
    text = fs.readFileSync(conf, "utf8");
  } catch {
    text = "";
  }
  if (text && !text.endsWith("\n")) text += "\n";
  fs.writeFileSync(conf, `${text}Profile=${p.id}\n`);
  return dir;
}

// ---- The verbs ----

// The images, with the environments made from each; the environments, with
// their image and whether they run; the other machines; the containers
// running; the profiles.
async function list() {
  const machines = await knownMachines();
  const up = new Set((await runningMachines()).map((m) => m.name));
  const images = [];
  const environments = [];
  const other = [];
  for (const m of machines) {
    const entry = { ...m, running: up.has(m.name) };
    const kind = kindOf(m.name);
    if (kind === "image") images.push({ ...entry, profile: recipeProfile(m.name), environments: environmentsOf(m.name) });
    else if (kind === "environment") environments.push({ ...entry, ...record(m.name) });
    else other.push(entry);
  }
  return {
    images,
    environments,
    other,
    running: [...up].sort().map((name) => ({ name, kind: kindOf(name) })),
    profiles: profiles().map(publicProfile),
  };
}

// Build an image from a profile: minutes. With replace, an image of that
// name goes first, which must have no environments left.
async function build(name, id, { replace = false, signal } = {}) {
  imageName(name);
  const p = profile(id);
  check(!record(name), `${name} is an environment, not an image`);
  if (replace && (await known(name))) {
    const envs = environmentsOf(name);
    check(envs.length === 0, `${name} still has environments (${envs.join(", ")}): remove them first`);
    await machinectl(["remove", name], signal);
  }
  const dir = writeRecipe(name, p);
  const built = await startUnit("cherry-image", `build:${name}`, LOGS, signal);
  return { name, profile: p.id, dir, log: built };
}

// Remove an image that has no environments left. Its recipe stays.
async function removeImage(name, { signal } = {}) {
  machineName(name);
  check(kindOf(name) !== "environment", `${name} is an environment: remove removes it`);
  const envs = environmentsOf(name);
  check(envs.length === 0, `${name} still has environments (${envs.join(", ")}): remove them first`);
  await machinectl(["remove", name], signal);
  return `${name} removed`;
}

// What an environment is made from: an image by name, which must be there,
// or a profile, whose image is named after it and may still have to be
// built. Checked, with nothing done: {image, profile?, build} where build
// says the image is not there yet.
async function source({ image, profile: id } = {}) {
  check((image === undefined) !== (id === undefined), "give an image or a profile, not both");
  let p = null;
  if (id !== undefined) {
    p = profile(id);
    image = p.id;
  } else {
    machineName(image);
  }
  const there = await known(image);
  check(there || p, `no image ${JSON.stringify(image)}`);
  check(kindOf(image) !== "environment", `${image} is an environment, not an image`);
  if (there) {
    check(!(await running(image)), `${image} is running: an image is not started, and a running machine is not cloned`);
  }
  return { image, profile: p ? p.id : undefined, build: !there };
}

// Make an environment: a clone of an image, by name, or of the image built
// from a profile, named after it and built first when there is none yet.
async function create(name, from = {}, { signal } = {}) {
  imageName(name);
  const s = await source(from);
  check(!(await known(name)), `${name} exists already`);
  const built = s.build ? await build(s.image, s.profile, { signal }) : null;
  await machinectl(["clone", s.image, name], signal);
  writeRecord(name, s.image);
  return { name, image: s.image, built };
}

// Boot an environment as a container and wait until a shell gets in.
async function start(name, { timeout = 120, signal } = {}) {
  machineName(name);
  check(kindOf(name) !== "image", `${name} is an image: create an environment from it, and start that`);
  if (!(await running(name))) await machinectl(["start", name], signal);
  const deadline = Date.now() + timeout * 1000;
  let last;
  while (Date.now() < deadline && !signal?.aborted) {
    last = await shell(name, "true", 15, signal);
    if (last.exit === 0) return `${name} is running`;
    await sleep(2);
  }
  const why = signal?.aborted ? "cancelled" : last ? last.error || last.output : "no answer";
  throw new ToolError(`${name} did not come up within ${timeout} s: ${why}`);
}

// Shut a container down, or kill it if it is still up after timeout.
async function stop(name, { timeout = 60, signal } = {}) {
  machineName(name);
  const wait = async (seconds) => {
    for (const end = Date.now() + seconds * 1000; Date.now() < end; await sleep(1)) {
      if (!(await running(name))) return true;
    }
    return false;
  };
  if (!(await running(name))) return `${name} is not running`;
  await machinectl(["poweroff", name], signal);
  if (await wait(timeout)) return `${name} is stopped`;
  await machinectl(["terminate", name], signal);
  if (await wait(30)) return `${name} did not shut down in time and was terminated`;
  throw new ToolError(`${name} is still running`);
}

// Remove an environment (or another machine that is no image), which must
// not be running, with its settings and its record.
async function remove(name, { signal } = {}) {
  machineName(name);
  check(kindOf(name) !== "image", `${name} is an image: remove_image removes it, once it has no environments`);
  await machinectl(["remove", name], signal);
  dropRecord(name);
  return `${name} removed`;
}

// What a machine reports, through fixed commands: systemd's state, the
// address of its veth link, and its failed units.
const STATUS_SCRIPT = [
  "printf 'system=%s\\n' \"$(systemctl is-system-running 2>/dev/null)\"",
  "printf 'address=%s\\n' \"$(ip -4 -o addr show host0 2>/dev/null | sed -n 's/.*inet \\([0-9.][0-9.]*\\/[0-9]*\\).*/\\1/p' | head -n 1)\"",
  "printf 'failed=%s\\n' \"$(systemctl --failed --no-legend --plain 2>/dev/null | cut -d ' ' -f 1 | tr '\\n' ' ')\"",
].join("; ");

async function status(name, { signal } = {}) {
  machineName(name);
  const kind = kindOf(name);
  const result = { name, kind, exists: await known(name), running: await running(name) };
  if (kind === "image") result.profile = recipeProfile(name);
  if (kind === "environment") Object.assign(result, record(name));
  if (!result.running) return result;
  const r = await shell(name, STATUS_SCRIPT, 30, signal);
  if (r.exit === null) return { ...result, error: r.error };
  const field = (key) => {
    const lines = r.output.split("\n").filter((line) => line.startsWith(`${key}=`));
    return lines.length ? lines[lines.length - 1].slice(key.length + 1).trim() : "";
  };
  return {
    ...result,
    system: field("system") || null,
    address: field("address") || null,
    failed: field("failed").split(/\s+/).filter(Boolean),
  };
}

module.exports = {
  ToolError, NAME, MACHINE_NAME, PROFILE_ID, RECIPES, LOGS, ENVIRONMENTS, PROFILE_DIRS,
  imageName, machineName, profileId, profiles, profile, publicProfile, kindOf, exists: known, source,
  list, build, removeImage, log, create, start, stop, status, remove,
};
