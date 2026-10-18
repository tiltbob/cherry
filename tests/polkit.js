#!/usr/bin/env node
// The polkit rules allow the cherry user what the README says and nothing
// more: package/cherry/50-cherry.rules, evaluated with a stub of polkit's
// objects (polkit.addRule, polkit.Result, action.id, action.lookup(key),
// subject.user) over the cases that must be YES and those that must be left
// to polkit's defaults (NOT_HANDLED). Needs node.
"use strict";
const fs = require("fs");
const path = require("path");

const rules = fs.readFileSync(path.join(__dirname, "..", "package", "cherry", "50-cherry.rules"), "utf8");
const handlers = [];
const polkit = {
  Result: { YES: "YES", NO: "NO", NOT_HANDLED: "NOT_HANDLED" },
  addRule(fn) { handlers.push(fn); },
};
new Function("polkit", rules)(polkit);

// As polkitd does: the first rule that returns a result other than
// NOT_HANDLED decides. action.lookup(key) is the detail, or undefined.
function evaluate(user, id, details) {
  const action = { id, lookup: (key) => details[key] };
  const subject = { user };
  for (const fn of handlers) {
    const result = fn(action, subject);
    if (result !== undefined && result !== polkit.Result.NOT_HANDLED) return result;
  }
  return polkit.Result.NOT_HANDLED;
}

const MACHINE1 = "org.freedesktop.machine1.";
const MANAGE_MACHINES = MACHINE1 + "manage-machines";
const MANAGE_IMAGES = MACHINE1 + "manage-images";
const MANAGE_UNITS = "org.freedesktop.systemd1.manage-units";
const NSPAWN = "systemd-nspawn@box.service";
const BOOTSTRAP = "cherry-bootstrap@debian:trixie:box.service";
const IMAGE = "cherry-image@build:box.service";
const STORAGE = "cherry-storage@claim:sdb:0123456789ab.service";
const yes = [];
const notHandled = [];

// machinectl, on the containers, but not on the host itself.
for (const a of ["login", "shell", "open-pty"]) yes.push(["cherry", MACHINE1 + a, {}]);
for (const a of ["host-login", "host-shell", "host-open-pty"]) notHandled.push(["cherry", MACHINE1 + a, {}]);
// machined's manage-machines and manage-images, by the details it passes for each call (systemd 258's
// src/machine/*-dbus.c; its *-varlink.c names the machine "name"). terminate, and kill: poweroff and reboot too.
for (const machine of ["machine", "name"]) {
  const m = (details) => Object.assign({ [machine]: "box" }, details);
  for (const verb of ["terminate", "kill"]) yes.push(["cherry", MANAGE_MACHINES, m({ verb })]);
  // Not copy or bind, which machined does as root on any host path: machinectl copy-to, a host file into the
  // container to read there; copy-from, a container's file onto the host, e.g. root's .profile; bind, a host
  // directory into the container.
  for (const [verb, src, dest] of [["copy", "/var/lib/cherry/.config/openchamber", "/stolen"],
                                   ["copy", "/profile", "/var/roothome/.profile"],
                                   ["bind", "/var/roothome", "/mnt"]]) {
    notHandled.push(["cherry", MANAGE_MACHINES, m({ verb, src, dest })]);
  }
  // Nor its root directory's file descriptor, or unregistering it.
  for (const verb of ["open_root_directory", "unregister"]) notHandled.push(["cherry", MANAGE_MACHINES, m({ verb })]);
  // Nor a verb of manage-images, or none.
  for (const verb of ["remove", "clone", undefined]) notHandled.push(["cherry", MANAGE_MACHINES, m({ verb })]);
}
// Nor the pool of images as a whole: machinectl clean, and set-limit with no image.
notHandled.push(["cherry", MANAGE_MACHINES, { verb: "clean_pool", mode: "all" }]);
notHandled.push(["cherry", MANAGE_MACHINES, { verb: "set_pool_limit" }]);
notHandled.push(["cherry", MANAGE_MACHINES, {}]);
// Images: remove, clone, rename, read-only and set-limit (whose details name the image "machine"), and update,
// those three over varlink.
yes.push(["cherry", MANAGE_IMAGES, { image: "box", verb: "remove" }]);
yes.push(["cherry", MANAGE_IMAGES, { image: "box", verb: "clone", new_name: "box2" }]);
yes.push(["cherry", MANAGE_IMAGES, { image: "box", verb: "rename", new_name: "box2" }]);
yes.push(["cherry", MANAGE_IMAGES, { image: "box", verb: "mark_read_only", read_only: "1" }]);
yes.push(["cherry", MANAGE_IMAGES, { machine: "box", verb: "set_limit" }]);
yes.push(["cherry", MANAGE_IMAGES, { image: "box", verb: "update" }]);
// But not the pool as a whole (over varlink), a verb of manage-machines, or none.
notHandled.push(["cherry", MANAGE_IMAGES, { verb: "clean_pool", mode: "all" }]);
notHandled.push(["cherry", MANAGE_IMAGES, { verb: "set_pool_limit" }]);
for (const verb of ["copy", "bind", "kill", "terminate"]) notHandled.push(["cherry", MANAGE_IMAGES, { image: "box", verb }]);
notHandled.push(["cherry", MANAGE_IMAGES, { image: "box" }]);
notHandled.push(["cherry", MANAGE_IMAGES, {}]);
// machinectl start/stop/restart: systemd-nspawn@ units.
for (const verb of ["start", "stop", "restart"]) yes.push(["cherry", MANAGE_UNITS, { unit: NSPAWN, verb }]);
for (const verb of ["reload", "kill", "try-restart", "reload-or-restart"]) notHandled.push(["cherry", MANAGE_UNITS, { unit: NSPAWN, verb }]);
// Creating a container: starting cherry-bootstrap@, nothing else; building an image: starting cherry-image@,
// nothing else; storage: starting cherry-storage@, nothing else.
for (const unit of [BOOTSTRAP, IMAGE, STORAGE]) {
  yes.push(["cherry", MANAGE_UNITS, { unit, verb: "start" }]);
  for (const verb of ["stop", "restart", "kill"]) notHandled.push(["cherry", MANAGE_UNITS, { unit, verb }]);
}
// No other unit, however it is named.
for (const unit of ["systemd-networkd.service", "cherry.service", "cherry-connect-url.service", "systemd-nspawn.service",
                    "xsystemd-nspawn@box.service", "systemd-nspawnd@box.service", "cherry-bootstrap.service",
                    "cherry-image.service", "xcherry-image@build:box.service",
                    // The boot unit that mounts the volumes, which a stop would unmount.
                    "cherry-storage.service", "xcherry-storage@list.service"]) {
  notHandled.push(["cherry", MANAGE_UNITS, { unit, verb: "start" }]);
}
notHandled.push(["cherry", MANAGE_UNITS, {}]);
notHandled.push(["cherry", MANAGE_UNITS, { verb: "start" }]);
notHandled.push(["cherry", MANAGE_UNITS, { unit: NSPAWN }]);
for (const verb of ["stop", "restart"]) notHandled.push(["cherry", MANAGE_UNITS, { unit: "cherry-storage.service", verb }]);
// Nothing beyond containers: no unit files, no power.
notHandled.push(["cherry", "org.freedesktop.systemd1.manage-unit-files", { unit: NSPAWN, verb: "enable" }]);
notHandled.push(["cherry", "org.freedesktop.login1.power-off", {}]);
notHandled.push(["cherry", "org.freedesktop.login1.reboot", {}]);
// Nobody else gets what cherry gets.
for (const user of ["root", "openchamber", "opencode", "cherryx", ""]) {
  for (const [, id, details] of yes) notHandled.push([user, id, details]);
}

let failures = 0;
for (const [cases, expected] of [[yes, polkit.Result.YES], [notHandled, polkit.Result.NOT_HANDLED]]) {
  for (const [user, id, details] of cases) {
    const result = evaluate(user, id, details);
    if (result !== expected) {
      failures++;
      console.error(`polkit: user ${JSON.stringify(user)}, ${id} ${JSON.stringify(details)}: ${result}, expected ${expected}`);
    }
  }
}
if (failures) {
  console.error(`polkit: ${failures} cases failed`);
  process.exit(1);
}
console.log(`polkit: ${yes.length} cases allowed for cherry, ${notHandled.length} left to polkit's defaults`);
