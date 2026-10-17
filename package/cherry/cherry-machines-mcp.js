// cherry-machines-mcp: an MCP server (stdio) that gives the OpenCode agent
// tools for systemd-nspawn container images, as the cherry user, through what
// that user may do (50-cherry.rules): cherry-image@.service builds images,
// machinectl starts, stops, enters and removes them, and machined's bus API
// lists them. /etc/opencode/opencode.json starts it with Bun. Images stay on
// this machine: there is no tool to export one.
//
// Tools: list, build, start, stop, exec, remove. build writes the image's
// recipe, as `cherry-image build` reads it, to /var/lib/cherry/images/<name>/
// first. $CHERRY_IMAGES_DIR and $CHERRY_IMAGE_LOGS replace
// /var/lib/cherry/images and /var/log/cherry-image (tests/mcp.js). The
// protocol is mcp-stdio.js's, next to it.
"use strict";
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { ToolError, check, run, sleep, startUnit, serve } = require("./mcp-stdio.js");

const RECIPES = process.env.CHERRY_IMAGES_DIR || "/var/lib/cherry/images";
const LOGS = process.env.CHERRY_IMAGE_LOGS || "/var/log/cherry-image";
const INSTRUCTIONS = `These tools build and run systemd-nspawn container images on this Cherry machine, \
which machinectl manages in /var/lib/machines: build an image from a distribution, packages, files and a \
setup script, start it, run commands in it, and stop it. Images stay on this machine. Load the machine-images \
skill before building one.`;

// Names cherry-image builds: valid machine names and hostnames.
const IMAGE_NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,47}$/;
// Any image machinectl knows, as cherry-bootstrap@ names them.
const MACHINE_NAME = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;
const WORD = /^(?!-)[A-Za-z0-9_.-]*$/;
const PACKAGE = /^[^\s#-]\S*$/;
const ESCAPES = /\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Za-z0-9]|[=>78cDEHM])/g;

function imageName(args) {
  check(typeof args.name === "string" && IMAGE_NAME.test(args.name),
    `bad image name ${JSON.stringify(args.name)}: up to 48 letters, digits and '-', starting with a letter or digit`);
  return args.name;
}

function machineName(args) {
  check(typeof args.name === "string" && MACHINE_NAME.test(args.name), `bad machine name ${JSON.stringify(args.name)}`);
  return args.name;
}

// cherry-image@<instance>.service: started, waited for; its log's tail.
function cherryImage(instance, signal) {
  return startUnit("cherry-image", instance, LOGS, signal);
}

// The reply of a machined Manager method, as busctl --json=short gives it.
async function machined(method, args = []) {
  const r = await run("busctl", ["--json=short", "call", "org.freedesktop.machine1", "/org/freedesktop/machine1",
    "org.freedesktop.machine1.Manager", method, ...args]);
  if (r.code !== 0) throw new ToolError(`machined ${method}: ${(r.err || r.out).trim()}`);
  return JSON.parse(r.out).data;
}

async function running(name) {
  const [machines] = await machined("ListMachines");
  return machines.some(([machine]) => machine === name);
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

function relativeInImage(file) {
  check(typeof file === "string" && file.startsWith("/") && !file.includes("\0") && !file.includes("\n"),
    `bad file path ${JSON.stringify(file)}: an absolute path in the image`);
  const parts = file.split("/").filter((p) => p !== "");
  check(parts.length > 0 && parts.every((p) => p !== "." && p !== ".."), `bad file path ${JSON.stringify(file)}`);
  return parts.join("/");
}

// The recipe directory, written anew from the build tool's arguments.
function writeRecipe(name, args) {
  check(typeof args.distribution === "string" && args.distribution !== "" && WORD.test(args.distribution),
    `bad distribution ${JSON.stringify(args.distribution)}`);
  const release = args.release ?? "";
  check(typeof release === "string" && WORD.test(release), `bad release ${JSON.stringify(release)}`);
  const packages = args.packages ?? [];
  check(Array.isArray(packages) && packages.every((p) => typeof p === "string" && PACKAGE.test(p)),
    `bad packages ${JSON.stringify(packages)}: names without spaces, not starting with '-' or '#'`);
  const files = args.files ?? [];
  check(Array.isArray(files), "files must be an array");
  const entries = files.map((f) => {
    check(f && typeof f === "object", `bad file ${JSON.stringify(f)}`);
    const rel = relativeInImage(f.path);
    if (f.symlink !== undefined) {
      check(typeof f.symlink === "string" && f.symlink !== "" && !f.symlink.includes("\0"),
        `bad symlink target for ${f.path}`);
      return { rel, symlink: f.symlink };
    }
    check(typeof f.content === "string", `no content for ${f.path}`);
    const mode = f.mode ?? "0644";
    check(typeof mode === "string" && /^[0-7]{3,4}$/.test(mode), `bad mode ${JSON.stringify(mode)} for ${f.path}`);
    const encoding = f.encoding ?? "utf-8";
    check(encoding === "utf-8" || encoding === "base64", `bad encoding ${JSON.stringify(encoding)} for ${f.path}`);
    return { rel, data: Buffer.from(f.content, encoding === "base64" ? "base64" : "utf8"), mode: parseInt(mode, 8) };
  });
  for (const key of ["postinst", "nspawn"]) {
    check(args[key] === undefined || typeof args[key] === "string", `${key} must be a string`);
  }

  const dir = path.join(RECIPES, name);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(path.join(dir, "extra"), { recursive: true });
  const conf = [`Distribution=${args.distribution}`, `Release=${release}`];
  if (packages.length) conf.push(`Packages=${packages.join(" ")}`);
  fs.writeFileSync(path.join(dir, "image.conf"), conf.join("\n") + "\n");
  for (const entry of entries) {
    const target = path.join(dir, "extra", entry.rel);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    if (entry.symlink !== undefined) {
      fs.symlinkSync(entry.symlink, target);
    } else {
      fs.writeFileSync(target, entry.data);
      fs.chmodSync(target, entry.mode);
    }
  }
  if (args.postinst) {
    const script = args.postinst.startsWith("#!") ? args.postinst : `#!/bin/sh\nset -eu\n${args.postinst}`;
    fs.writeFileSync(path.join(dir, "postinst"), script.endsWith("\n") ? script : script + "\n", { mode: 0o755 });
  }
  if (args.nspawn) fs.writeFileSync(path.join(dir, "image.nspawn"), args.nspawn.endsWith("\n") ? args.nspawn : args.nspawn + "\n");
  return dir;
}

const NAME = { type: "string", description: "The image's name, as machinectl shows it" };
const TOOLS = [
  {
    name: "list",
    description: "List the container images in /var/lib/machines, the containers running, and the image recipes " +
      `in ${RECIPES}.`,
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    async call() {
      const [images] = await machined("ListImages");
      const [machines] = await machined("ListMachines");
      const unknown = (n) => (n >= 2 ** 63 ? null : n);
      const list = (dir) => { try { return fs.readdirSync(dir).filter((f) => !f.startsWith(".")).sort(); } catch { return []; } };
      return JSON.stringify({
        images: images.map(([name, type, readOnly, , mtime, usage]) =>
          ({ name, type, readOnly, modified: mtime ? new Date(mtime / 1000).toISOString() : null, bytes: unknown(usage) }))
          .filter((i) => i.name !== ".host"),
        running: machines.filter(([, cls]) => cls !== "host").map(([name, cls, service]) => ({ name, class: cls, service })),
        recipes: list(RECIPES),
      }, null, 2);
    },
  },
  {
    name: "build",
    description: "Build a container image in /var/lib/machines that machinectl and systemd-nspawn run: bootstrap " +
      "a distribution, install packages with its package manager, copy files in, run a setup script in it as " +
      "root, and give it nspawn settings. Takes minutes; returns the end of the build log, or fails with it. " +
      `The arguments become the recipe in ${RECIPES}/<name>/, replacing any recipe there. The new image has an ` +
      "empty /etc/machine-id, no /etc/hostname unless the files set one (a container takes its machine's name), and " +
      "no package caches. Start it with the start tool.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Up to 48 letters, digits and '-', starting with a letter or digit" },
        distribution: { type: "string", description: "debian, fedora, centos-stream, almalinux, rocky or arch" },
        release: { type: "string", description: "e.g. trixie (debian), 44 (fedora), 10 (EL); empty for arch" },
        packages: { type: "array", items: { type: "string" },
          description: "More packages for the image's apt-get, dnf or pacman, beyond the base: enough to boot and " +
            "machinectl shell into, but no networking for Debian or Arch (add systemd-networkd's package and enable " +
            "it in postinst, or use systemd-resolved)" },
        files: { type: "array", description: "Files to put in the image, after the packages, owned by root",
          items: { type: "object", required: ["path"], additionalProperties: false, properties: {
            path: { type: "string", description: "Absolute path in the image, e.g. /etc/myapp.conf; put programs in /usr/bin, not /bin" },
            content: { type: "string" },
            encoding: { type: "string", enum: ["utf-8", "base64"], description: "Of content; default utf-8" },
            mode: { type: "string", description: "Octal, default 0644; 0755 for scripts" },
            symlink: { type: "string", description: "Make path a symbolic link to this, instead of a file" },
          } } },
        postinst: { type: "string", description: "Shell script run last, in the image, as root, with network: enable " +
          "services, create users, configure. Without a #! line it runs with /bin/sh and set -eu. A failure fails the build." },
        nspawn: { type: "string", description: "systemd.nspawn(5) settings for the image, e.g. \"[Exec]\\nEnvironment=MODE=prod\\n\". " +
          "machinectl start already gives it a veth link and a user namespace. Only unprivileged settings apply to images " +
          "in /var/lib/machines ([Exec] Environment=, Timezone=, ResolvConf= and the like): Bind=, Capability=, " +
          "network settings and Port= are ignored" },
        replace: { type: "boolean", description: "Remove an image of that name first (it must not be running)" },
      },
      required: ["name", "distribution"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const name = imageName(args);
      const dir = writeRecipe(name, args);
      if (args.replace && (await run("machinectl", ["show-image", name])).code === 0) {
        await machinectl(["remove", name], signal);
      }
      return `Built ${name} from the recipe in ${dir}. Log, ${await cherryImage(`build:${name}`, signal)}`;
    },
  },
  {
    name: "start",
    description: "Boot an image as a container (machinectl start: a veth link, DHCP and NAT from the host, a user " +
      "namespace), and wait until exec can reach it.",
    inputSchema: {
      type: "object",
      properties: { name: NAME, timeout: { type: "number", description: "Seconds to wait, default 120" } },
      required: ["name"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const name = machineName(args);
      const timeout = args.timeout ?? 120;
      if (!(await running(name))) await machinectl(["start", name], signal);
      const deadline = Date.now() + timeout * 1000;
      let last;
      while (Date.now() < deadline && !signal.aborted) {
        last = await shell(name, "true", 15, signal);
        if (last.exit === 0) return `${name} is running`;
        await sleep(2);
      }
      throw new ToolError(`${name} did not come up within ${timeout} s: ${last ? last.error || last.output : "aborted"}`);
    },
  },
  {
    name: "stop",
    description: "Shut a running container down (machinectl poweroff), or kill it (terminate) if it is still up after timeout.",
    inputSchema: {
      type: "object",
      properties: { name: NAME, timeout: { type: "number", description: "Seconds to wait, default 60" } },
      required: ["name"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const name = machineName(args);
      const wait = async (seconds) => {
        for (const end = Date.now() + seconds * 1000; Date.now() < end; await sleep(1)) {
          if (!(await running(name))) return true;
        }
        return false;
      };
      if (!(await running(name))) return `${name} is not running`;
      await machinectl(["poweroff", name], signal);
      if (await wait(args.timeout ?? 60)) return `${name} is stopped`;
      await machinectl(["terminate", name], signal);
      if (await wait(30)) return `${name} did not shut down in time and was terminated`;
      throw new ToolError(`${name} is still running`);
    },
  },
  {
    name: "exec",
    description: "Run a shell command in a running container, as root (machinectl shell): its exit status and " +
      "output, stdout and stderr together, through a terminal that nobody types into (TERM=dumb, no pager, " +
      "DEBIAN_FRONTEND=noninteractive). Login messages may come first.",
    inputSchema: {
      type: "object",
      properties: {
        name: NAME,
        command: { type: "string", description: "Run with sh -c" },
        timeout: { type: "number", description: "Seconds, default 300" },
      },
      required: ["name", "command"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const name = machineName(args);
      check(typeof args.command === "string" && args.command !== "", "no command");
      const r = await shell(name, args.command, args.timeout ?? 300, signal);
      const output = r.output.length > 30000 ? `[...]\n${r.output.slice(-30000)}` : r.output;
      if (r.exit === null) throw new ToolError(`${r.error}\n${output}`);
      return `exit status ${r.exit}\n${output}`;
    },
  },
  {
    name: "remove",
    description: "Remove an image, which must not be running, with its nspawn settings (machinectl remove). Its " +
      "recipe stays.",
    inputSchema: { type: "object", properties: { name: NAME }, required: ["name"], additionalProperties: false },
    annotations: { destructiveHint: true },
    async call(args, signal) {
      const name = machineName(args);
      await machinectl(["remove", name], signal);
      return `${name} removed`;
    },
  },
];

serve({ name: "cherry-machines", instructions: INSTRUCTIONS, tools: TOOLS });
