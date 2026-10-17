// The Environments panel of OpenChamber, and its /env command: a page in a
// sandboxed iframe that talks to the OpenChamber host over postMessage, as
// @openchamber/sdk's connectHost does (the part of it this needs, written
// out: the sandbox loads classic scripts only, and the image's build has no
// bundler), and through the host to the extension's service
// (service/main.js), which has the verbs. background.html loads this too,
// with no page: it answers the /env command while the panel is closed.
//
// No model anywhere: a request, typed in the panel's box or after /env, is
// read by a small parser (verb, profile, name) and shown back before it
// runs, or answered with what was missing.
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
    create: (name, profile, replace) => api("POST", "/environments", { name, profile, replace: Boolean(replace) }),
    start: (name) => api("POST", `/environments/${encode(name)}/start`, {}),
    stop: (name) => api("POST", `/environments/${encode(name)}/stop`, {}),
    status: (name) => api("GET", `/environments/${encode(name)}`),
    log: (name, lines) => api("GET", `/environments/${encode(name)}/log`, undefined, { lines: String(lines || 60) }),
    remove: (name) => api("DELETE", `/environments/${encode(name)}`),
  };

  // ---- Words to a verb, a profile and a name, with no model. ----
  const NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,47}$/;
  const VERBS = [
    ["create", ["create", "make", "new", "build", "spin", "launch", "add", "provision", "setup", "rebuild"]],
    ["start", ["start", "boot", "run", "bring", "wake", "resume"]],
    ["stop", ["stop", "shutdown", "shut", "halt", "poweroff", "kill", "pause"]],
    ["remove", ["remove", "delete", "destroy", "rm", "drop", "purge"]],
    ["log", ["log", "logs", "why"]],
    ["status", ["status", "state", "check", "inspect", "info", "health", "ip", "address"]],
    ["list", ["list", "ls", "environments", "envs", "images", "profiles", "what"]],
  ];
  const USAGE = "list | create <profile> <name> | start, stop, status, log or remove <name>";
  const FILLER = new Set(["a", "an", "the", "environment", "env", "container", "box", "machine", "image", "vm",
    "named", "called", "name", "with", "from", "please", "me", "up", "of", "it", "and", "to", "for", "using",
    "based", "on", "in", "my", "this", "that", "is", "do", "we", "have", "again", "replace", "replacing",
    "one", "fresh", "down", "out", "into", "as", "be", "there", "server", "instance"]);

  function parse(text, profiles) {
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
    if (!verb) return { help: `I did not find what to do in that. ${USAGE}.` };
    if (verb === "list") return { verb };

    let profile = null;
    const named = [];
    for (const word of rest) {
      if (FILLER.has(word)) continue;
      const byId = profiles.find((p) => p.id === word);
      const byWord = profiles.filter((p) => [p.distribution, p.release, ...p.title.toLowerCase().split(/[\s()]+/)]
        .filter(Boolean).includes(word));
      if (verb === "create" && !profile && byId) profile = byId;
      else if (verb === "create" && !profile && byWord.length === 1) profile = byWord[0];
      else if (verb === "create" && !profile && byWord.length > 1) {
        return { help: `Which one: ${byWord.map((p) => p.id).join(", ")}?` };
      } else named.push(word);
    }
    const name = named.find((word) => NAME.test(word)) || null;
    if (verb === "create" && !profile) {
      return { help: `Which profile? ${profiles.map((p) => `${p.id} (${p.title})`).join(", ") || "none is installed"}.` };
    }
    if (!name) return { help: verb === "create" ? "What should it be called? A name is up to 48 letters, digits and '-'." : `Which environment? ${USAGE}.` };
    return { verb, name, profile: profile ? profile.id : undefined, replace: verb === "create" ? replace : undefined };
  }

  function describe(plan, profiles) {
    const title = (id) => (profiles.find((p) => p.id === id) || { title: id }).title;
    switch (plan.verb) {
      case "list": return "List the environments.";
      case "create": return `Create ${plan.name} from ${title(plan.profile)}${plan.replace ? ", replacing the one there" : ""}. A build takes minutes.`;
      case "start": return `Start ${plan.name}.`;
      case "stop": return `Stop ${plan.name}.`;
      case "status": return `Show the status of ${plan.name}.`;
      case "log": return `Show the build log of ${plan.name}.`;
      case "remove": return `Remove ${plan.name}. Its image is deleted.`;
      default: return "";
    }
  }

  function summarizeList(list) {
    const images = list.images.map((i) => `${i.name}${i.running ? " (running)" : ""}`);
    const jobs = (list.jobs || []).filter((j) => j.state === "running").map((j) => `${j.name} (${j.verb}...)`);
    const all = [...images, ...jobs];
    return `${all.length ? all.join(", ") : "no environments"}. Profiles: ${list.profiles.map((p) => p.id).join(", ") || "none"}.`;
  }

  function summarizeStatus(s) {
    if (!s.image && !s.running) return `${s.name}: no such environment${s.job ? ` (${s.job.verb}: ${s.job.state})` : ""}.`;
    if (!s.running) return `${s.name}: stopped${s.job && s.job.state === "failed" ? `; ${s.job.verb} failed: ${s.job.error}` : ""}.`;
    const failed = s.failed && s.failed.length ? `, failed: ${s.failed.join(" ")}` : "";
    return `${s.name}: running, systemd ${s.system || "?"}, host0 ${s.address || "no address yet"}${failed}.`;
  }

  // Run a plan; the text to tell the user.
  async function perform(plan) {
    switch (plan.verb) {
      case "list": return summarizeList(await service.list());
      case "create": {
        const job = await service.create(plan.name, plan.profile, plan.replace);
        return `Creating ${job.name} from ${job.profile}: minutes. Its status follows the build.`;
      }
      case "start": await service.start(plan.name); return `Starting ${plan.name}.`;
      case "stop": await service.stop(plan.name); return `Stopping ${plan.name}.`;
      case "status": return summarizeStatus(await service.status(plan.name));
      case "log": {
        const l = await service.log(plan.name, 20);
        return l.text === null ? `No build log for ${plan.name}.` : l.text;
      }
      case "remove": return (await service.remove(plan.name)).message;
      default: throw new Error(`unknown verb ${plan.verb}`);
    }
  }

  // /env <words>: parsed, run, and told as a toast. Nothing is attached.
  host.onResolve(async ({ args }) => {
    try {
      const list = await service.list();
      const plan = parse(args, list.profiles);
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

  async function refresh() {
    try {
      state.list = await service.list();
      state.error = null;
      host.setBadge(state.list.running.length || null);
      if (state.selected) {
        const known = state.list.images.some((i) => i.name === state.selected) ||
          (state.list.jobs || []).some((j) => j.name === state.selected);
        if (!known) {
          state.selected = null;
          state.detail = null;
          state.logText = null;
        }
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
      if (job && job.verb === "create") state.logText = (await service.log(name, 40)).text;
    } catch (e) {
      state.detail = { name, error: e.message };
    }
    render();
  }

  async function act(name, verb, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return;
    const key = `${verb}:${name}`;
    state.busy.add(key);
    state.notice = null;
    render();
    try {
      const text = await perform({ verb, name });
      state.notice = { kind: "info", text };
    } catch (e) {
      state.notice = { kind: "error", text: e.message };
    } finally {
      state.busy.delete(key);
    }
    await refresh();
    if (state.selected === name) select(name);
  }

  async function submitCreate(event) {
    event.preventDefault();
    const form = event.target;
    const plan = { verb: "create", name: form.elements.name.value.trim(), profile: form.elements.profile.value,
      replace: form.elements.replace.checked };
    if (!NAME.test(plan.name)) {
      state.notice = { kind: "error", text: "A name is up to 48 letters, digits and '-', starting with a letter or digit." };
      return render();
    }
    state.busy.add("create");
    render();
    try {
      state.notice = { kind: "info", text: await perform(plan) };
      form.reset();
    } catch (e) {
      state.notice = { kind: "error", text: e.message };
    } finally {
      state.busy.delete("create");
    }
    await refresh();
    select(plan.name);
  }

  function submitAsk(event) {
    event.preventDefault();
    const text = event.target.elements.ask.value;
    state.draft = text;
    const plan = parse(text, state.list ? state.list.profiles : []);
    state.proposal = plan.help ? { help: plan.help } : { plan, text: describe(plan, state.list ? state.list.profiles : []) };
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

  function stateOf(image, job) {
    if (job && job.state === "running") return ["working", `${job.verb}...`];
    if (job && job.state === "failed" && (!image || job.verb === "create")) return ["failed", `${job.verb} failed`];
    if (image && image.running) return ["running", "running"];
    if (image) return ["stopped", "stopped"];
    return ["failed", "gone"];
  }

  function rows() {
    const list = state.list;
    const jobs = new Map((list.jobs || []).map((j) => [j.name, j]));
    const names = [...new Set([...list.images.map((i) => i.name), ...jobs.keys()])].sort();
    return names.map((name) => {
      const image = list.images.find((i) => i.name === name) || null;
      const job = jobs.get(name) || null;
      const [cls, label] = stateOf(image, job);
      const profile = (image && image.profile && image.profile.title) || (job && job.profile) || "";
      const working = job && job.state === "running";
      const busy = (verb) => state.busy.has(`${verb}:${name}`);
      return h("tr", { class: state.selected === name ? "selected" : null },
        h("td", { class: "name", onclick: () => select(name), title: "Details" }, name),
        h("td", {}, profile),
        h("td", {}, h("span", { class: `state ${cls}` }, label)),
        h("td", { class: "actions" },
          image && !image.running && h("button", { class: "small", disabled: working || busy("start"), onclick: () => act(name, "start") }, "Start"),
          image && image.running && h("button", { class: "small", disabled: working || busy("stop"), onclick: () => act(name, "stop") }, "Stop"),
          image && h("button", { class: "small danger", disabled: working || image.running || busy("remove"),
            title: image.running ? "Stop it first" : null,
            onclick: () => act(name, "remove", `Remove ${name}? Its image is deleted.`) }, "Remove")));
    });
  }

  function detail() {
    const d = state.detail;
    if (!state.selected) return null;
    if (!d) return h("div", { class: "detail" }, `${state.selected}: reading...`);
    const job = d.job;
    const items = [];
    if (d.error && !d.image) items.push(["Error", d.error]);
    if (job) items.push([job.verb === "create" ? "Build" : job.verb[0].toUpperCase() + job.verb.slice(1),
      `${job.state}${job.profile ? `, from ${job.profile}` : ""}${job.error ? `: ${job.error}` : ""}${job.message && job.verb !== "create" ? `: ${job.message}` : ""}`]);
    if (d.profile) items.push(["Profile", d.profile.title]);
    items.push(["Image", d.image ? "yes" : "no"]);
    items.push(["Running", d.running ? "yes" : "no"]);
    if (d.running) {
      items.push(["systemd", d.system || "unknown"]);
      items.push(["host0", d.address || "no address yet"]);
      if (d.failed && d.failed.length) items.push(["Failed units", d.failed.join(" ")]);
      if (d.error) items.push(["Error", d.error]);
    }
    return h("div", { class: "detail" },
      h("div", { style: "display:flex;align-items:center;gap:8px" },
        h("strong", {}, d.name),
        h("span", { class: "spacer", style: "flex:1" }),
        h("button", { class: "small", onclick: () => select(d.name) }, "Refresh"),
        h("button", { class: "small", onclick: async () => { state.logText = (await service.log(d.name, 80)).text || "no build log"; render(); } }, "Build log"),
        h("button", { class: "small", onclick: () => { state.selected = null; state.detail = null; state.logText = null; render(); } }, "Close")),
      h("dl", {}, items.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
      state.logText !== null && h("pre", {}, state.logText || "no build log"));
  }

  function render() {
    const list = state.list;
    const profiles = list ? list.profiles : [];
    const running = list ? list.running.length : 0;
    app.replaceChildren(
      h("header", {},
        h("h1", {}, "Environments"),
        h("span", { class: "count" }, list ? `${list.images.length} image${list.images.length === 1 ? "" : "s"}, ${running} running` : "connecting..."),
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

      h("h2", {}, "Create"),
      h("form", { class: "create", onsubmit: submitCreate },
        h("select", { name: "profile", "aria-label": "Profile", required: true, disabled: !profiles.length },
          profiles.length ? profiles.map((p) => h("option", { value: p.id, title: p.description }, p.title)) :
            h("option", { value: "" }, "no profiles installed")),
        h("input", { name: "name", type: "text", placeholder: "name", "aria-label": "Name", required: true,
          pattern: "[A-Za-z0-9][A-Za-z0-9-]{0,47}", autocomplete: "off" }),
        h("button", { type: "submit", class: "primary", disabled: !profiles.length || state.busy.has("create") }, "Create"),
        h("label", { class: "check wide" }, h("input", { type: "checkbox", name: "replace" }), "Replace an environment of that name (it must be stopped)")),
      list && list.profiles.length ? h("p", { class: "hint" },
        "Profiles are written by a person: ", list.profiles.map((p) => p.title).join(", "), ".") : null,

      h("h2", {}, "Environments"),
      !list ? h("div", { class: "empty" }, "Waiting for the service...") :
        !rows().length ? h("div", { class: "empty" }, "None yet. Create one above, or in the chat: /env create debian-trixie web") :
          h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Profile"), h("th", {}, "State"), h("th", {}))),
            h("tbody", {}, rows())),
      detail(),
    );
  }

  render();
  refresh();
})();
