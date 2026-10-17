// cherry-machines-mcp: an MCP server (stdio) that gives the OpenCode agent
// tools for the container environments of this machine, as the cherry user,
// through what that user may do (50-cherry.rules): environments.js has the
// verbs, shared with the OpenChamber extension's service, and the rules.
// /etc/opencode/opencode.json starts it with Bun, and names its tools
// machines_<tool>. The protocol is mcp-stdio.js's, next to it.
//
// Tools: list, create, start, stop, status, log, remove. create takes a name
// and the id of a profile, one of the recipes written by a person, and
// nothing else: no package list, file or script, and no command in a
// container. Images stay on this machine: there is no tool to export one.
"use strict";
const { serve } = require("./mcp-stdio.js");
const env = require("./environments.js");

const INSTRUCTIONS = `These tools manage the container environments of this Cherry machine: images that machinectl \
runs in /var/lib/machines, each built from one of the machine's profiles (a distribution with its setup, chosen \
by a person). Create an environment from a profile, start it, read its status, stop it, remove it. Load the \
machine-images skill before creating one.`;

const NAME = { type: "string", description: "The environment's name, as machinectl shows it" };
const TOOLS = [
  {
    name: "list",
    description: "List the environments: the images in /var/lib/machines (with the profile each was created " +
      "from, when known), the containers running, and the profiles an environment can be created from.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    async call() {
      return JSON.stringify(await env.list(), null, 2);
    },
  },
  {
    name: "create",
    description: "Create an environment: build an image from one of the machine's profiles (see list). Takes " +
      "minutes; returns the end of the build log, or fails with it. Start it with the start tool.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Up to 48 letters, digits and '-', starting with a letter or digit" },
        profile: { type: "string", description: "The id of a profile from list" },
        replace: { type: "boolean", description: "Remove an environment of that name first (it must not be running)" },
      },
      required: ["name", "profile"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const r = await env.create(args.name, args.profile, { replace: args.replace === true, signal });
      return `Created ${r.name} from the profile ${r.profile}. Log, ${r.log}`;
    },
  },
  {
    name: "start",
    description: "Boot an environment as a container (machinectl start: a veth link, DHCP and NAT from the host, " +
      "a user namespace), and wait until it lets a shell in.",
    inputSchema: {
      type: "object",
      properties: { name: NAME, timeout: { type: "number", description: "Seconds to wait, default 120" } },
      required: ["name"],
      additionalProperties: false,
    },
    call: (args, signal) => env.start(args.name, { timeout: args.timeout ?? 120, signal }),
  },
  {
    name: "stop",
    description: "Shut a running environment down (machinectl poweroff), or kill it (terminate) if it is still " +
      "up after timeout.",
    inputSchema: {
      type: "object",
      properties: { name: NAME, timeout: { type: "number", description: "Seconds to wait, default 60" } },
      required: ["name"],
      additionalProperties: false,
    },
    call: (args, signal) => env.stop(args.name, { timeout: args.timeout ?? 60, signal }),
  },
  {
    name: "status",
    description: "What an environment reports: whether its image exists and it is running, and then systemd's " +
      "state in it (running, degraded, ...), the address of its veth link host0, and its failed units.",
    inputSchema: { type: "object", properties: { name: NAME }, required: ["name"], additionalProperties: false },
    annotations: { readOnlyHint: true },
    async call(args, signal) {
      return JSON.stringify(await env.status(args.name, { signal }), null, 2);
    },
  },
  {
    name: "log",
    description: "The end of an environment's build log, to see why a create failed.",
    inputSchema: {
      type: "object",
      properties: { name: NAME, lines: { type: "number", description: "Lines from the end, default 60" } },
      required: ["name"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    async call(args) {
      const l = env.log(args.name, Math.max(1, Math.min(1000, Math.floor(args.lines ?? 60))));
      return l.text === null ? `no log in ${l.file}` : `${l.file}:\n${l.text}`;
    },
  },
  {
    name: "remove",
    description: "Remove an environment's image, which must not be running, with its nspawn settings " +
      "(machinectl remove). Its recipe stays.",
    inputSchema: { type: "object", properties: { name: NAME }, required: ["name"], additionalProperties: false },
    annotations: { destructiveHint: true },
    call: (args, signal) => env.remove(args.name, { signal }),
  },
];

serve({ name: "cherry-machines", instructions: INSTRUCTIONS, tools: TOOLS });
