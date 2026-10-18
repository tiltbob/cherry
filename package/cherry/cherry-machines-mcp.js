// cherry-machines-mcp: an MCP server (stdio) that gives the OpenCode agent
// tools for the container images and environments of this machine, as the
// cherry user, through what that user may do (50-cherry.rules):
// environments.js has the verbs, shared with the OpenChamber extension's
// service, and the rules. /etc/opencode/opencode.json starts it with Bun,
// and names its tools machines_<tool>. The protocol is mcp-stdio.js's, next
// to it.
//
// Tools: list; build, remove_image and log, for images, each built from a
// profile (a recipe written by a person) and never started; create, start,
// stop, status and remove, for environments, each a clone of an image. No
// tool takes a package list, a file, a script or a command, and none writes
// an image out: images stay on this machine.
"use strict";
const { serve } = require("./mcp-stdio.js");
const env = require("./environments.js");

const INSTRUCTIONS = `These tools manage the container images and environments of this Cherry machine. An image is \
built from one of the machine's profiles (a distribution with its setup, chosen by a person) and never started; an \
environment is a clone of an image, with its own changes, which you start, read the status of, stop and remove. \
Several environments can come from one image. Load the machine-images skill before building or creating one.`;

const NAME = { type: "string", description: "The environment's name, as machinectl shows it" };
const IMAGE = { type: "string", description: "The image's name, as machinectl shows it" };
const TOOLS = [
  {
    name: "list",
    description: "List the images (each with the profile it was built from and the environments made from it), " +
      "the environments (each with its image, and whether it runs), the other machines in /var/lib/machines, " +
      "the containers running, and the profiles an image can be built from.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    async call() {
      return JSON.stringify(await env.list(), null, 2);
    },
  },
  {
    name: "build",
    description: "Build an image from one of the machine's profiles (see list). Takes minutes; returns the end of " +
      "the build log, or fails with it. Name it after the profile unless there is a reason not to. An image is not " +
      "started: create an environment from it.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Up to 48 letters, digits and '-', starting with a letter or digit" },
        profile: { type: "string", description: "The id of a profile from list" },
        replace: { type: "boolean", description: "Rebuild: remove an image of that name first (it must have no environments)" },
      },
      required: ["name", "profile"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const r = await env.build(args.name, args.profile, { replace: args.replace === true, signal });
      return `Built the image ${r.name} from the profile ${r.profile}. Log, ${r.log}`;
    },
  },
  {
    name: "remove_image",
    description: "Remove an image that has no environments left (machinectl remove). Its recipe stays, so build " +
      "can make it again.",
    inputSchema: { type: "object", properties: { name: IMAGE }, required: ["name"], additionalProperties: false },
    annotations: { destructiveHint: true },
    call: (args, signal) => env.removeImage(args.name, { signal }),
  },
  {
    name: "log",
    description: "The end of an image's build log, to see why a build failed.",
    inputSchema: {
      type: "object",
      properties: { name: IMAGE, lines: { type: "number", description: "Lines from the end, default 60" } },
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
    name: "create",
    description: "Create an environment: a clone of an image (machinectl clone, with the image's nspawn settings), " +
      "given the image by name, or by the profile it is built from, in which case the image is named after the " +
      "profile and built first if there is none yet (minutes). Start it with the start tool.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Up to 48 letters, digits and '-', starting with a letter or digit" },
        image: { ...IMAGE, description: "The image to clone, from list" },
        profile: { type: "string", description: "Instead of image: the id of a profile from list" },
      },
      required: ["name"],
      additionalProperties: false,
    },
    async call(args, signal) {
      const r = await env.create(args.name, { image: args.image, profile: args.profile }, { signal });
      const built = r.built ? ` The image was built first from the profile ${r.built.profile}. Log, ${r.built.log}` : "";
      return `Created the environment ${r.name} from the image ${r.image}.${built}`;
    },
  },
  {
    name: "start",
    description: "Boot an environment as a container (machinectl start: a veth link, DHCP and NAT from the host, " +
      "a user namespace), and wait until it lets a shell in. Images are not started.",
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
    description: "What a machine reports: its kind (image, environment or other), whether it exists and runs, " +
      "its image or profile, and then systemd's state in it (running, degraded, ...), the address of its veth " +
      "link host0, and its failed units.",
    inputSchema: { type: "object", properties: { name: NAME }, required: ["name"], additionalProperties: false },
    annotations: { readOnlyHint: true },
    async call(args, signal) {
      return JSON.stringify(await env.status(args.name, { signal }), null, 2);
    },
  },
  {
    name: "remove",
    description: "Remove an environment, which must not be running, with its nspawn settings and its record " +
      "(machinectl remove). Its image stays; remove_image removes that.",
    inputSchema: { type: "object", properties: { name: NAME }, required: ["name"], additionalProperties: false },
    annotations: { destructiveHint: true },
    call: (args, signal) => env.remove(args.name, { signal }),
  },
];

serve({ name: "cherry-machines", instructions: INSTRUCTIONS, tools: TOOLS });
