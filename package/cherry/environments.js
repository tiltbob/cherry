// cherry environments: what the cherry user may do with container
// environments on this machine, as one module for the two front ends that
// expose it: the OpenCode MCP server (cherry-machines-mcp.js) and the
// OpenChamber extension's service (openchamber-extension/service/main.js).
// Everything goes through what that user may do (50-cherry.rules):
// cherry-image@.service builds images, machinectl starts, stops, enters and
// removes them, and machined's bus API lists them. Images stay on this
// machine: nothing here writes one out.
//
// An environment is an image in /var/lib/machines built from a profile: a
// recipe as `cherry-image build` reads it (image.conf, extra/, postinst,
// image.nspawn), plus profile.conf with Title= and Description=, in
// /usr/lib/cherry/profiles/<id>/ (the image's) or /var/lib/cherry/profiles/<id>/
// (the machine's own, which win). create copies the profile to
// /var/lib/cherry/images/<name>/ and builds it. Nothing here takes a package
// list, a file or a script: those are a profile's, written by a person.
//
// $CHERRY_IMAGES_DIR, $CHERRY_IMAGE_LOGS and $CHERRY_PROFILE_DIRS (colon
// separated) replace the paths above (tests/mcp.js, tests/extension.js).
"use strict";
const { spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const RECIPES = process.env.CHERRY_IMAGES_DIR || "/var/lib/cherry/images";
const LOGS = process.env.CHERRY_IMAGE_LOGS || "/var/log/cherry-image";
const PROFILE_DIRS = (process.env.CHERRY_PROFILE_DIRS || "/var/lib/cherry/profiles:/usr/lib/cherry/profiles")
  .split(":").filter(Boolean);

// Names cherry-image builds: valid machine names and hostnames.
const NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,47}$/;
// Any image machinectl knows, as cherry-bootstrap@ names them.
const MACHINE_NAME = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;
// A profile's directory name.
const PROFILE_ID = /^[a-z0-9][a-z0-9-]{0,47}$/;
const ESCAPES = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|[=>78cDEHM])/g;

class EnvironmentError extends Error {}

function check(condition, message) {
  if (!condition) throw new EnvironmentError(message);
}

function imageName(name) {
  check(typeof name === "string" && NAME.test(name),
    `bad image name ${JSON.stringify(name)}: up to 48 letters, digits and '-', starting with a letter or digit`);
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
    child.on("exit", () => { if (killed) finish(null); });
    child.on("close", (code) => finish(code));
  });
}

const sleep = (seconds) => new Promise((resolve) => setTimeout(resolve, seconds * 1000));

function tail(text, lines) {
  return text.replace(/\n+$/, "").split("\n").slice(-lines).join("\n").trim();
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

function describeLog(name, lines) {
  const l = log(name, lines);
  return l.text === null ? `no log in ${l.file}` : `${l.file}:\n${l.text}`;
}

// cherry-image@<instance>.service: started, waited for; its log's tail.
async function cherryImage(name, signal) {
  const unit = `cherry-image@build:${name}.service`;
  const r = await run("systemctl", ["start", "--no-ask-password", unit], { signal });
  if (r.code !== 0) throw new EnvironmentError(`${unit} failed. Log, ${describeLog(name, 60)}\n${r.err.trim()}`);
  return describeLog(name, 15);
}

// The reply of a machined Manager method, as busctl --json=short gives it.
async function machined(method, args = []) {
  const r = await run("busctl", ["--json=short", "call", "org.freedesktop.machine1", "/org/freedesktop/machine1",
    "org.freedesktop.machine1.Manager", method, ...args]);
  if (r.code !== 0) throw new EnvironmentError(`machined ${method}: ${(r.err || r.out).trim()}`);
  return JSON.parse(r.out).data;
}

async function runningMachines() {
  const [machines] = await machined("ListMachines");
  return machines.filter(([, cls]) => cls !== "host").map(([name, cls, service]) => ({ name, class: cls, service }));
}

async function running(name) {
  return (await runningMachines()).some((m) => m.name === name);
}

async function hasImage(name) {
  return (await run("machinectl", ["show-image", name])).code === 0;
}

async function machinectl(args, signal) {
  const r = await run("machinectl", ["--no-ask-password", ...args], { signal });
  if (r.code !== 0) throw new EnvironmentError(`machinectl ${args.join(" ")}: ${(r.err || r.out).trim()}`);
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

// Key=value lines, as image.conf and profile.conf hold them; null without
// the file. Packages= lines add up, as cherry-image reads them.
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

// The profile an image was built from, from the recipe create left: null
// for an image made some other way.
function recipeProfile(name) {
  const meta = readConf(path.join(RECIPES, name, "profile.conf"));
  return meta && meta.Profile ? { id: meta.Profile, title: meta.Title || meta.Profile } : null;
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

// The images, the containers running, and the profiles.
async function list() {
  const [images] = await machined("ListImages");
  const machines = await runningMachines();
  const up = new Set(machines.map((m) => m.name));
  const unknown = (n) => (n >= 2 ** 63 ? null : n);
  return {
    images: images
      .filter(([name]) => name !== ".host")
      .map(([name, type, readOnly, , mtime, usage]) => ({
        name,
        type,
        readOnly,
        modified: mtime ? new Date(mtime / 1000).toISOString() : null,
        bytes: unknown(usage),
        running: up.has(name),
        profile: recipeProfile(name),
      })),
    running: machines,
    profiles: profiles().map(publicProfile),
  };
}

// Build an image from a profile: minutes. With replace, an image of that
// name goes first (it must not be running).
async function create(name, id, { replace = false, signal } = {}) {
  imageName(name);
  const p = profile(id);
  if (replace && (await hasImage(name))) await machinectl(["remove", name], signal);
  const dir = writeRecipe(name, p);
  const built = await cherryImage(name, signal);
  return { name, profile: p.id, dir, log: built };
}

// Boot an image as a container and wait until a shell gets in.
async function start(name, { timeout = 120, signal } = {}) {
  machineName(name);
  if (!(await running(name))) await machinectl(["start", name], signal);
  const deadline = Date.now() + timeout * 1000;
  let last;
  while (Date.now() < deadline && !signal?.aborted) {
    last = await shell(name, "true", 15, signal);
    if (last.exit === 0) return `${name} is running`;
    await sleep(2);
  }
  const why = signal?.aborted ? "cancelled" : last ? last.error || last.output : "no answer";
  throw new EnvironmentError(`${name} did not come up within ${timeout} s: ${why}`);
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
  throw new EnvironmentError(`${name} is still running`);
}

// Remove an image, which must not be running. Its recipe stays.
async function remove(name, { signal } = {}) {
  machineName(name);
  await machinectl(["remove", name], signal);
  return `${name} removed`;
}

// What a running container reports, through fixed commands: systemd's state,
// the address of its veth link, and its failed units.
const STATUS_SCRIPT = [
  "printf 'system=%s\\n' \"$(systemctl is-system-running 2>/dev/null)\"",
  "printf 'address=%s\\n' \"$(ip -4 -o addr show host0 2>/dev/null | sed -n 's/.*inet \\([0-9.][0-9.]*\\/[0-9]*\\).*/\\1/p' | head -n 1)\"",
  "printf 'failed=%s\\n' \"$(systemctl --failed --no-legend --plain 2>/dev/null | cut -d ' ' -f 1 | tr '\\n' ' ')\"",
].join("; ");

async function status(name, { signal } = {}) {
  machineName(name);
  const result = { name, image: await hasImage(name), running: await running(name), profile: recipeProfile(name) };
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
  EnvironmentError, NAME, MACHINE_NAME, PROFILE_ID, RECIPES, LOGS, PROFILE_DIRS,
  imageName, machineName, profileId, profiles, profile, publicProfile,
  list, create, start, stop, status, log, remove,
};
