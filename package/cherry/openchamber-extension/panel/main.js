// The Environments panel of OpenChamber, and its /env command: a page in a
// sandboxed iframe that talks to the OpenChamber host over postMessage, as
// @openchamber/sdk's connectHost does (the part of it this needs, written
// out: the sandbox loads classic scripts only, and the image's build has no
// bundler), and through the host to the extension's service
// (service/main.js), which has the verbs: images built from profiles, and
// environments cloned from images. background.html loads this too, with no
// page: it answers the /env command while the panel is closed.
//
// No model anywhere: a request, typed in the panel's box or after /env, is
// read by a small parser (verb, image or profile, name) and shown back
// before it runs, or answered with what was missing.
(() => {
  "use strict";

  // ---- The host: hello, then requests answered by id, and its pushes. ----
  const CHANNEL = "openchamber.sdk";
  const VERSION = 1;
  const pending = new Map();
  let counter = 0;
  let ready = null;
  const readyListeners = [];
  let resolver = null;

  class HostError extends Error {
    constructor(code, message) {
      super(message);
      this.code = code;
    }
  }

  const post = (message) => window.parent.postMessage({ channel: CHANNEL, v: VERSION, ...message }, "*");

  function send(type, payload, timeoutMs = 20000) {
    if (window.parent === window) return Promise.reject(new HostError("HOST_UNAVAILABLE", "This page is not in OpenChamber."));
    const id = `env-${++counter}`;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new HostError("HOST_TIMEOUT", "OpenChamber did not answer in time."));
      }, timeoutMs);
      pending.set(id, { resolve, reject, timer });
      post(payload === undefined ? { type, id } : { type, id, payload });
    });
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const m = event.data;
    if (!m || typeof m !== "object" || m.channel !== CHANNEL || m.v !== VERSION) return;
    if (m.type === "result") {
      const waiter = pending.get(m.id);
      if (!waiter) return;
      clearTimeout(waiter.timer);
      pending.delete(m.id);
      if (m.ok === true) waiter.resolve(m.payload);
      else waiter.reject(new HostError(m.code || "HOST_REJECTED", m.error || "OpenChamber refused the request."));
      return;
    }
    if (m.type === "ready") {
      ready = m.payload;
      for (const listener of readyListeners) listener(ready);
      return;
    }
    if (m.type === "resolve") {
      const answer = (payload) => post({ type: "resolve-result", id: m.id, payload });
      if (!resolver) return answer({ error: "This extension does not resolve commands." });
      Promise.resolve().then(() => resolver(m.payload)).then(
        (item) => answer({ item: item || null }),
        (e) => answer({ error: String((e && e.message) || e).slice(0, 500) }),
      );
      return;
    }
    if (m.type === "action") {
      post({ type: "action-result", id: m.id, payload: { ok: false, error: "This extension has no actions." } });
    }
  });
  post({ type: "hello" });

  const host = {
    onReady(listener) {
      readyListeners.push(listener);
      if (ready) listener(ready);
    },
    onResolve(handler) {
      resolver = handler;
    },
    serviceRequest: (request) => send("service-request", request, 30000),
    toast: (kind, message) => send("toast", { kind, message: String(message).slice(0, 500) }).catch(() => {}),
    setBadge: (count) => send("badge", { count }).catch(() => {}),
  };

  // ---- The service, through OpenChamber's proxy: JSON in, JSON out. ----
  function serviceError(e) {
    if (e instanceof HostError && e.code === "NO_SERVICE") {
      return new Error("The Environments service is not allowed yet: approve it in Settings, Extensions.");
    }
    if (e instanceof HostError && e.code === "DISABLED") return new Error("The Environments extension is paused.");
    if (e instanceof HostError && e.code === "SERVICE_FAILED") return new Error("The Environments service did not start.");
    return e;
  }

  async function api(method, path, body, query) {
    const request = { method, path };
    if (body !== undefined) request.body = JSON.stringify(body);
    if (query) request.query = query;
    let r;
    try {
      r = await host.serviceRequest(request);
    } catch (e) {
      throw serviceError(e);
    }
    let data;
    try {
      data = JSON.parse(r.body);
    } catch {
      data = { error: r.body };
    }
    if (r.status >= 400) throw new Error((data && data.error) || `the service answered ${r.status}`);
    return data;
  }

  const encode = (name) => encodeURIComponent(name);
  const service = {
    list: () => api("GET", "/environments"),
    build: (name, profile, replace) => api("POST", "/images", { name, profile, replace: Boolean(replace) }),
    removeImage: (name) => api("DELETE", `/images/${encode(name)}`),
    log: (name, lines) => api("GET", `/images/${encode(name)}/log`, undefined, { lines: String(lines || 60) }),
    create: (name, from) => api("POST", "/environments", { name, ...from }),
    start: (name) => api("POST", `/environments/${encode(name)}/start`, {}),
    stop: (name) => api("POST", `/environments/${encode(name)}/stop`, {}),
    status: (name) => api("GET", `/environments/${encode(name)}`),
    remove: (name) => api("DELETE", `/environments/${encode(name)}`),
  };

  // ---- Words to a verb, an image or a profile, and a name, with no model. ----
  const NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,47}$/;
  const VERBS = [
    ["build", ["build", "bake", "prepare", "rebuild"]],
    ["create", ["create", "make", "new", "spin", "launch", "add", "provision", "setup", "clone", "instantiate"]],
    ["start", ["start", "boot", "run", "bring", "wake", "resume"]],
    ["stop", ["stop", "shutdown", "shut", "halt", "poweroff", "kill", "pause"]],
    ["remove", ["remove", "delete", "destroy", "rm", "drop", "purge"]],
    ["log", ["log", "logs", "why"]],
    ["status", ["status", "state", "check", "inspect", "info", "health", "ip", "address"]],
    ["list", ["list", "ls", "environments", "envs", "images", "profiles", "what", "show"]],
  ];
  const USAGE = "list | build <profile> [name] | create <image or profile> <name> | start, stop, status or remove <name> " +
    "| log or remove image <name>";
  const FILLER = new Set(["a", "an", "the", "environment", "env", "container", "box", "machine", "vm", "image",
    "named", "called", "name", "with", "from", "please", "me", "up", "of", "it", "and", "to", "for", "using",
    "based", "on", "in", "my", "this", "that", "is", "do", "we", "have", "again", "replace", "replacing",
    "one", "fresh", "down", "out", "into", "as", "be", "there", "server", "instance", "its", "off", "another"]);

  // parse(text, list): {verb, name?, image?, profile?, replace?} or {help}.
  function parse(text, list) {
    const profiles = list ? list.profiles : [];
    const images = list ? list.images.map((i) => i.name) : [];
    const words = String(text || "").toLowerCase().replace(/[^a-z0-9\s_-]/g, " ").split(/\s+/).filter(Boolean);
    if (!words.length) return { help: `Say what to do: ${USAGE}.` };
    let verb = null;
    const rest = [];
    for (const word of words) {
      const match = verb ? null : VERBS.find(([, synonyms]) => synonyms.includes(word));
      if (match) verb = match[0];
      else rest.push(word);
    }
    const replace = words.includes("rebuild") || words.includes("replace") || words.includes("again");
    const aboutImage = words.includes("image") || words.includes("images");
    if (!verb) return { help: `I did not find what to do in that. ${USAGE}.` };
    if (verb === "list") return { verb };
    if (verb === "remove" && aboutImage) verb = "remove_image";

    let profile = null;
    let image = null;
    const named = [];
    for (const word of rest) {
      if (FILLER.has(word)) continue;
      const wantsSource = verb === "build" || verb === "create";
      if (wantsSource && !image && !profile && images.includes(word) && verb === "create") {
        image = word;
        continue;
      }
      const byId = profiles.find((p) => p.id === word);
      const byWord = profiles.filter((p) => [p.distribution, p.release, ...p.title.toLowerCase().split(/[\s()]+/)]
        .filter(Boolean).includes(word));
      if (wantsSource && !image && !profile && byId) profile = byId;
      else if (wantsSource && !image && !profile && byWord.length === 1) profile = byWord[0];
      else if (wantsSource && !image && !profile && byWord.length > 1) {
        return { help: `Which one: ${byWord.map((p) => p.id).join(", ")}?` };
      } else named.push(word);
    }
    const name = named.find((word) => NAME.test(word)) || null;
    if (verb === "build") {
      if (!profile) return { help: `Which profile? ${profiles.map((p) => `${p.id} (${p.title})`).join(", ") || "none is installed"}.` };
      return { verb, name: name || profile.id, profile: profile.id, replace };
    }
    if (verb === "create") {
      if (!image && !profile) {
        return { help: `From which image or profile? Images: ${images.join(", ") || "none"}. Profiles: ${profiles.map((p) => p.id).join(", ") || "none"}.` };
      }
      if (!name) return { help: "What should the environment be called? A name is up to 48 letters, digits and '-'." };
      return image ? { verb, name, image } : { verb, name, profile: profile.id };
    }
    if (!name) return { help: `Which ${verb === "remove_image" || verb === "log" ? "image" : "environment"}? ${USAGE}.` };
    return { verb, name };
  }

  function describe(plan, list) {
    const profiles = list ? list.profiles : [];
    const title = (id) => (profiles.find((p) => p.id === id) || { title: id }).title;
    const exists = (name) => list && list.images.some((i) => i.name === name);
    switch (plan.verb) {
      case "list": return "List the images and environments.";
      case "build": return `Build the image ${plan.name} from ${title(plan.profile)}${plan.replace ? ", replacing the one there" : ""}. A build takes minutes.`;
      case "create": return plan.image
        ? `Create the environment ${plan.name} from the image ${plan.image}.`
        : `Create the environment ${plan.name} from ${title(plan.profile)}${exists(plan.profile) ? "" : ", building the image first (minutes)"}.`;
      case "start": return `Start ${plan.name}.`;
      case "stop": return `Stop ${plan.name}.`;
      case "status": return `Show the status of ${plan.name}.`;
      case "log": return `Show the build log of the image ${plan.name}.`;
      case "remove": return `Remove the environment ${plan.name}. Its files are deleted; its image stays.`;
      case "remove_image": return `Remove the image ${plan.name}. It must have no environments left.`;
      default: return "";
    }
  }

  function summarizeList(list) {
    const images = list.images.map((i) => `${i.name}${i.environments.length ? ` (${i.environments.length})` : ""}`);
    const envs = list.environments.map((e) => `${e.name}${e.running ? " (running)" : ""}`);
    const jobs = (list.jobs || []).filter((j) => j.state === "running").map((j) => `${j.name} (${j.verb}...)`);
    return `Images: ${images.join(", ") || "none"}. Environments: ${[...envs, ...jobs].join(", ") || "none"}. ` +
      `Profiles: ${list.profiles.map((p) => p.id).join(", ") || "none"}.`;
  }

  function summarizeStatus(s) {
    const job = s.job && s.job.state === "failed" ? `; ${s.job.verb} failed: ${s.job.error}` : "";
    if (!s.exists) return `${s.name}: no such machine${job}.`;
    const of = s.kind === "environment" ? ` of ${s.image}` : s.kind === "image" && s.profile ? ` from ${s.profile.title}` : "";
    if (!s.running) return `${s.name}: ${s.kind}${of}, stopped${job}.`;
    const failed = s.failed && s.failed.length ? `, failed: ${s.failed.join(" ")}` : "";
    return `${s.name}: ${s.kind}${of}, running, systemd ${s.system || "?"}, host0 ${s.address || "no address yet"}${failed}.`;
  }

  // Run a plan; the text to tell the user.
  async function perform(plan) {
    switch (plan.verb) {
      case "list": return summarizeList(await service.list());
      case "build": {
        const job = await service.build(plan.name, plan.profile, plan.replace);
        return `Building the image ${job.name} from ${job.profile}: minutes. Its status follows the build.`;
      }
      case "create": {
        const job = await service.create(plan.name, plan.image ? { image: plan.image } : { profile: plan.profile });
        return `Creating the environment ${job.name}${job.image ? ` from ${job.image}` : ` from the profile ${job.profile}`}.`;
      }
      case "start": await service.start(plan.name); return `Starting ${plan.name}.`;
      case "stop": await service.stop(plan.name); return `Stopping ${plan.name}.`;
      case "status": return summarizeStatus(await service.status(plan.name));
      case "log": {
        const l = await service.log(plan.name, 20);
        return l.text === null ? `No build log for ${plan.name}.` : l.text;
      }
      case "remove": return (await service.remove(plan.name)).message;
      case "remove_image": return (await service.removeImage(plan.name)).message;
      default: throw new Error(`unknown verb ${plan.verb}`);
    }
  }

  // /env <words>: parsed, run, and told as a toast. Nothing is attached.
  host.onResolve(async ({ args }) => {
    try {
      const list = await service.list();
      const plan = parse(args, list);
      if (plan.help) await host.toast("info", plan.help);
      else await host.toast("info", await perform(plan));
    } catch (e) {
      await host.toast("error", e.message);
    }
    return null;
  });

  // ---- The page. background.html has none and stops here. ----
  const app = document.getElementById("app");
  if (!app) return;

  const h = (tag, attrs, ...children) => {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return el;
  };

  const state = {
    list: null, error: null, selected: null, detail: null, logText: null,
    busy: new Set(), notice: null, proposal: null, draft: "",
  };
  let timer = null;

  host.onReady((context) => {
    const t = context && context.theme && context.theme.tokens;
    if (!t) return;
    const root = document.documentElement.style;
    const map = { bg: t.background, fg: t.foreground, muted: t.muted, border: t.border, elevated: t.elevated,
      primary: t.primary, "primary-fg": t.primaryForeground, error: t.error, success: t.success, warning: t.warning };
    for (const [key, value] of Object.entries(map)) if (value) root.setProperty(`--${key}`, value);
  });

  function schedule() {
    clearTimeout(timer);
    const working = state.list && (state.list.jobs || []).some((j) => j.state === "running");
    timer = setTimeout(refresh, working ? 2500 : 10000);
  }

  const knownNames = (list) => new Set([
    ...list.images.map((i) => i.name), ...list.environments.map((e) => e.name), ...list.other.map((o) => o.name),
    ...(list.jobs || []).map((j) => j.name)]);

  async function refresh() {
    try {
      state.list = await service.list();
      state.error = null;
      host.setBadge(state.list.running.length || null);
      if (state.selected && !knownNames(state.list).has(state.selected)) {
        state.selected = null;
        state.detail = null;
        state.logText = null;
      }
    } catch (e) {
      state.error = e.message;
    }
    render();
    schedule();
  }

  async function select(name) {
    state.selected = name;
    state.detail = null;
    state.logText = null;
    render();
    try {
      state.detail = await service.status(name);
      const job = state.detail.job;
      if (job && job.verb === "build") state.logText = (await service.log(name, 40)).text;
    } catch (e) {
      state.detail = { name, error: e.message };
    }
    render();
  }

  async function act(plan, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    const key = `${plan.verb}:${plan.name}`;
    state.busy.add(key);
    state.notice = null;
    render();
    try {
      state.notice = { kind: "info", text: await perform(plan) };
    } catch (e) {
      state.notice = { kind: "error", text: e.message };
    } finally {
      state.busy.delete(key);
    }
    await refresh();
    if (state.selected === plan.name) select(plan.name);
  }

  async function submitBuild(event) {
    event.preventDefault();
    const form = event.target;
    const profile = form.elements.profile.value;
    const name = form.elements.name.value.trim() || profile;
    if (!NAME.test(name)) {
      state.notice = { kind: "error", text: "A name is up to 48 letters, digits and '-', starting with a letter or digit." };
      return render();
    }
    await act({ verb: "build", name, profile, replace: form.elements.replace.checked });
    form.reset();
    select(name);
  }

  async function submitCreate(event) {
    event.preventDefault();
    const form = event.target;
    const name = form.elements.name.value.trim();
    const [kind, source] = form.elements.source.value.split(":");
    if (!NAME.test(name)) {
      state.notice = { kind: "error", text: "A name is up to 48 letters, digits and '-', starting with a letter or digit." };
      return render();
    }
    await act({ verb: "create", name, ...(kind === "image" ? { image: source } : { profile: source }) });
    form.reset();
    select(name);
  }

  function submitAsk(event) {
    event.preventDefault();
    const text = event.target.elements.ask.value;
    state.draft = text;
    const plan = parse(text, state.list);
    state.proposal = plan.help ? { help: plan.help } : { plan, text: describe(plan, state.list) };
    render();
  }

  async function confirmProposal() {
    const { plan } = state.proposal;
    state.proposal = null;
    state.busy.add("ask");
    render();
    try {
      state.notice = { kind: "info", text: await perform(plan) };
      state.draft = "";
    } catch (e) {
      state.notice = { kind: "error", text: e.message };
    } finally {
      state.busy.delete("ask");
    }
    await refresh();
    if (plan.name) select(plan.name);
  }

  function stateOf(entry, job) {
    if (job && job.state === "running") return ["working", `${job.verb}...`];
    if (job && job.state === "failed" && (!entry || job.verb === "build" || job.verb === "create")) return ["failed", `${job.verb} failed`];
    if (entry && entry.running) return ["running", "running"];
    if (entry) return ["stopped", "stopped"];
    return ["failed", "gone"];
  }

  const busy = (verb, name) => state.busy.has(`${verb}:${name}`);

  function imageRows() {
    const list = state.list;
    const jobs = new Map((list.jobs || []).filter((j) => j.verb === "build").map((j) => [j.name, j]));
    const names = [...new Set([...list.images.map((i) => i.name), ...jobs.keys()])].sort();
    return names.map((name) => {
      const image = list.images.find((i) => i.name === name) || null;
      const job = jobs.get(name) || null;
      const [cls, label] = image && !(job && job.state === "running") && !image.running
        ? ["stopped", image.environments.length ? `${image.environments.length} env${image.environments.length === 1 ? "" : "s"}` : "no envs"]
        : stateOf(image, job);
      const working = job && job.state === "running";
      const inUse = image && (image.environments.length > 0 || image.running);
      return h("tr", { class: state.selected === name ? "selected" : null },
        h("td", { class: "name", onclick: () => select(name), title: "Details" }, name),
        h("td", {}, (image && image.profile && image.profile.title) || (job && job.profile) || ""),
        h("td", {}, h("span", { class: `state ${cls}` }, label)),
        h("td", { class: "actions" },
          h("button", { class: "small", onclick: async () => { state.selected = name; state.detail = state.detail && state.detail.name === name ? state.detail : null; state.logText = (await service.log(name, 80)).text || "no build log"; render(); } }, "Log"),
          image && h("button", { class: "small danger", disabled: working || inUse || busy("remove_image", name),
            title: inUse ? "Remove its environments first" : null,
            onclick: () => act({ verb: "remove_image", name }, `Remove the image ${name}?`) }, "Remove")));
    });
  }

  function environmentRows(entries, jobVerbs) {
    const list = state.list;
    const jobs = new Map((list.jobs || []).filter((j) => jobVerbs.includes(j.verb)).map((j) => [j.name, j]));
    const names = [...new Set([...entries.map((e) => e.name), ...jobs.keys()])].sort();
    return names.map((name) => {
      const entry = entries.find((e) => e.name === name) || null;
      const job = jobs.get(name) || null;
      const [cls, label] = stateOf(entry, job);
      const working = job && job.state === "running";
      return h("tr", { class: state.selected === name ? "selected" : null },
        h("td", { class: "name", onclick: () => select(name), title: "Details" }, name),
        h("td", {}, (entry && entry.image) || (job && (job.image || job.profile)) || ""),
        h("td", {}, h("span", { class: `state ${cls}` }, label)),
        h("td", { class: "actions" },
          entry && !entry.running && h("button", { class: "small", disabled: working || busy("start", name), onclick: () => act({ verb: "start", name }) }, "Start"),
          entry && entry.running && h("button", { class: "small", disabled: working || busy("stop", name), onclick: () => act({ verb: "stop", name }) }, "Stop"),
          entry && h("button", { class: "small danger", disabled: working || entry.running || busy("remove", name),
            title: entry.running ? "Stop it first" : null,
            onclick: () => act({ verb: "remove", name }, `Remove ${name}? Its files are deleted.`) }, "Remove")));
    });
  }

  function detail() {
    const d = state.detail;
    if (!state.selected) return null;
    if (!d) return h("div", { class: "detail" }, `${state.selected}: reading...`);
    const job = d.job;
    const items = [];
    if (d.error && !d.exists) items.push(["Error", d.error]);
    if (d.kind) items.push(["Kind", d.kind]);
    if (d.image) items.push(["Image", d.image]);
    if (d.profile) items.push(["Profile", d.profile.title]);
    if (job) items.push([job.verb[0].toUpperCase() + job.verb.slice(1),
      `${job.state}${job.profile ? `, from ${job.profile}` : ""}${job.error ? `: ${job.error}` : ""}${job.message && job.verb !== "build" ? `: ${job.message}` : ""}`]);
    if (d.exists !== undefined) items.push(["Exists", d.exists ? "yes" : "no"]);
    if (d.kind !== "image") items.push(["Running", d.running ? "yes" : "no"]);
    if (d.running) {
      items.push(["systemd", d.system || "unknown"]);
      items.push(["host0", d.address || "no address yet"]);
      if (d.failed && d.failed.length) items.push(["Failed units", d.failed.join(" ")]);
      if (d.error) items.push(["Error", d.error]);
    }
    const logOf = d.kind === "environment" ? d.image : d.name;
    return h("div", { class: "detail" },
      h("div", { style: "display:flex;align-items:center;gap:8px" },
        h("strong", {}, d.name),
        h("span", { class: "spacer", style: "flex:1" }),
        h("button", { class: "small", onclick: () => select(d.name) }, "Refresh"),
        logOf && h("button", { class: "small", onclick: async () => { state.logText = (await service.log(logOf, 80)).text || "no build log"; render(); } }, "Build log"),
        h("button", { class: "small", onclick: () => { state.selected = null; state.detail = null; state.logText = null; render(); } }, "Close")),
      h("dl", {}, items.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
      state.logText !== null && h("pre", {}, state.logText || "no build log"));
  }

  function render() {
    const list = state.list;
    const profiles = list ? list.profiles : [];
    const images = list ? list.images : [];
    const running = list ? list.running.length : 0;
    const sources = [
      ...images.map((i) => h("option", { value: `image:${i.name}` }, `${i.name} (image)`)),
      ...profiles.map((p) => h("option", { value: `profile:${p.id}`, title: p.description },
        `${p.title}${images.some((i) => i.name === p.id) ? "" : " (builds the image first)"}`)),
    ];
    app.replaceChildren(
      h("header", {},
        h("h1", {}, "Environments"),
        h("span", { class: "count" }, list
          ? `${images.length} image${images.length === 1 ? "" : "s"}, ${list.environments.length} environment${list.environments.length === 1 ? "" : "s"}, ${running} running`
          : "connecting..."),
        h("span", { class: "spacer" }),
        h("button", { class: "small", onclick: refresh }, "Refresh")),
      state.error && h("div", { class: "banner error" }, state.error),
      state.notice && h("div", { class: `banner ${state.notice.kind === "error" ? "error" : ""}` }, state.notice.text),

      h("h2", {}, "Ask"),
      h("form", { class: "ask", onsubmit: submitAsk },
        h("input", { name: "ask", type: "text", value: state.draft, placeholder: "create a new ubuntu environment named yarp",
          "aria-label": "What to do", autocomplete: "off" }),
        h("button", { type: "submit", disabled: !list || state.busy.has("ask") }, "Go")),
      h("p", { class: "hint" }, `Also /env in the chat: ${USAGE}. Nothing runs before you confirm it.`),
      state.proposal && h("div", { class: "proposal" },
        state.proposal.help || state.proposal.text,
        !state.proposal.help && h("div", { class: "buttons" },
          h("button", { class: "primary", onclick: confirmProposal }, "Do it"),
          h("button", { onclick: () => { state.proposal = null; render(); } }, "Cancel"))),

      h("h2", {}, "Environments"),
      h("form", { class: "create", onsubmit: submitCreate },
        h("select", { name: "source", "aria-label": "Image or profile", required: true, disabled: !sources.length },
          sources.length ? sources : h("option", { value: "" }, "no images or profiles")),
        h("input", { name: "name", type: "text", placeholder: "name", "aria-label": "Name", required: true,
          pattern: "[A-Za-z0-9][A-Za-z0-9-]{0,47}", autocomplete: "off" }),
        h("button", { type: "submit", class: "primary", disabled: !sources.length || state.busy.has("ask") }, "Create")),
      h("p", { class: "hint" }, "An environment is a clone of an image, with changes of its own. From a profile, the image is named after it and built first when there is none."),
      !list ? h("div", { class: "empty" }, "Waiting for the service...") :
        !environmentRows(list.environments, ["create", "start", "stop"]).length ? h("div", { class: "empty" }, "No environments yet.") :
          h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Image"), h("th", {}, "State"), h("th", {}))),
            h("tbody", {}, environmentRows(list.environments, ["create", "start", "stop"]))),

      h("h2", {}, "Images"),
      h("form", { class: "create", onsubmit: submitBuild },
        h("select", { name: "profile", "aria-label": "Profile", required: true, disabled: !profiles.length },
          profiles.length ? profiles.map((p) => h("option", { value: p.id, title: p.description }, p.title)) :
            h("option", { value: "" }, "no profiles installed")),
        h("input", { name: "name", type: "text", placeholder: "name (the profile's by default)", "aria-label": "Name",
          pattern: "[A-Za-z0-9][A-Za-z0-9-]{0,47}", autocomplete: "off" }),
        h("button", { type: "submit", disabled: !profiles.length || state.busy.has("ask") }, "Build"),
        h("label", { class: "check wide" }, h("input", { type: "checkbox", name: "replace" }), "Rebuild an image of that name (it must have no environments)")),
      h("p", { class: "hint" }, "An image is built from a profile, a recipe written by a person, and never started. ",
        profiles.length ? `Profiles: ${profiles.map((p) => p.title).join(", ")}.` : ""),
      !list ? null :
        !imageRows().length ? h("div", { class: "empty" }, "No images yet. Build one above, or in the chat: /env build debian-trixie") :
          h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Profile"), h("th", {}, "Environments"), h("th", {}))),
            h("tbody", {}, imageRows())),

      list && list.other.length ? [
        h("h2", {}, "Other machines"),
        h("p", { class: "hint" }, "Made by hand, outside the profiles: started and stopped here, not built."),
        h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}), h("th", {}, "State"), h("th", {}))),
          h("tbody", {}, environmentRows(list.other, ["start", "stop"]))),
      ] : null,
      detail(),
    );
  }

  render();
  refresh();
})();
