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
const MANAGE_UNITS = "org.freedesktop.systemd1.manage-units";
const NSPAWN = "systemd-nspawn@box.service";
const BOOTSTRAP = "cherry-bootstrap@debian:trixie:box.service";
const yes = [];
const notHandled = [];

// machinectl, on the containers, but not on the host itself.
for (const a of ["login", "shell", "open-pty", "manage-machines", "manage-images"]) yes.push(["cherry", MACHINE1 + a, {}]);
for (const a of ["host-login", "host-shell", "host-open-pty"]) notHandled.push(["cherry", MACHINE1 + a, {}]);
// machinectl start/stop/restart: systemd-nspawn@ units.
for (const verb of ["start", "stop", "restart"]) yes.push(["cherry", MANAGE_UNITS, { unit: NSPAWN, verb }]);
for (const verb of ["reload", "kill", "try-restart", "reload-or-restart"]) notHandled.push(["cherry", MANAGE_UNITS, { unit: NSPAWN, verb }]);
// Creating a container: starting cherry-bootstrap@, nothing else.
yes.push(["cherry", MANAGE_UNITS, { unit: BOOTSTRAP, verb: "start" }]);
for (const verb of ["stop", "restart", "kill"]) notHandled.push(["cherry", MANAGE_UNITS, { unit: BOOTSTRAP, verb }]);
// No other unit, however it is named.
for (const unit of ["sshd.service", "cherry.service", "cherry-connect-url.service", "systemd-nspawn.service",
                    "xsystemd-nspawn@box.service", "systemd-nspawnd@box.service", "cherry-bootstrap.service"]) {
  notHandled.push(["cherry", MANAGE_UNITS, { unit, verb: "start" }]);
}
notHandled.push(["cherry", MANAGE_UNITS, {}]);
notHandled.push(["cherry", MANAGE_UNITS, { verb: "start" }]);
notHandled.push(["cherry", MANAGE_UNITS, { unit: NSPAWN }]);
// Nothing beyond containers: no unit files, no power.
notHandled.push(["cherry", "org.freedesktop.systemd1.manage-unit-files", { unit: NSPAWN, verb: "enable" }]);
notHandled.push(["cherry", "org.freedesktop.login1.power-off", {}]);
notHandled.push(["cherry", "org.freedesktop.login1.reboot", {}]);
// Nobody else gets what cherry gets.
for (const user of ["root", "ipfs", "openchamber", "opencode", "cherryx", ""]) {
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
