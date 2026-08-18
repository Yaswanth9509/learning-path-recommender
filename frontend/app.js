/* Learning Path Recommender — client.
   No framework, no build step, no external requests. */

const API = "";
const LS_KEY = "lpr.learner_id";

const state = {
  learnerId: null,
  profile: null,
  path: null,
  goals: [],
  skills: [],
  items: [],
  paths: [],
  //: item ids the learner says they finished before this app saw them
  history: new Set(),
  dashboard: null,
  llm: false,
  user: null,
};

/* ------------------------------------------------------------------ utils */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  // Text only, deliberately: there is no innerHTML path in this client, so
  // catalogue and model output can never be interpreted as markup.
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  if (opts.on) for (const [k, v] of Object.entries(opts.on)) node.addEventListener(k, v);
  for (const child of [].concat(children)) {
    if (child == null) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

let toastTimer = null;
function toast(message, ms = 2800) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), ms);
}

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return body;
}

function pct(n) { return `${Math.round(n)}%`; }

/* Models write **bold**. Render it with real elements rather than leaving the
   asterisks on screen — and without ever touching innerHTML. */
function setMessageText(node, text) {
  node.textContent = "";
  String(text).split(/(\*\*[^*]+\*\*)/g).forEach((part) => {
    if (!part) return;
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) {
      node.appendChild(el("strong", { text: part.slice(2, -2) }));
    } else {
      node.appendChild(document.createTextNode(part));
    }
  });
}

/* ------------------------------------------------------------------ modal */
function openModal(title) {
  const modal = $("#modal");
  $("#modalTitle").textContent = title;
  const body = $("#modalBody");
  body.innerHTML = "";
  modal.hidden = false;
  $("#modalClose").focus();
  return body;
}

function closeModal() { $("#modal").hidden = true; }

$("#modalClose").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (event) => {
  if (event.target.dataset.close) closeModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#modal").hidden) closeModal();
});

/* ------------------------------------------------------------------- tabs
   Each tab is addressable as #chat, #path, #graph, … so a view can be linked,
   bookmarked, and reopened where you left off. */
const TABS = ["chat", "path", "graph", "plan", "dashboard", "explore", "profile"];

function showTab(name, { updateHash = true } = {}) {
  if (!TABS.includes(name)) return;
  $$(".tab").forEach((t) => t.classList.toggle("is-active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("is-active", p.id === `panel-${name}`));
  if (updateHash && window.location.hash.slice(1) !== name) {
    window.location.hash = name;
  }
  // Land at the top of the new view rather than halfway down the last one.
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  if (name === "path") renderPath();
  if (name === "graph") loadGraph();
  if (name === "plan") loadPlan();
  if (name === "dashboard") loadDashboard();
  if (name === "explore") loadRecommendations();
  if (name === "profile") renderProfileTab();
}

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
});

/* `#explain/<item-id>` opens one item's full justification, so a learner can
   send someone the exact reason a course is on their path. */
function routeHash() {
  const hash = window.location.hash.slice(1);
  if (hash.startsWith("explain/")) {
    showTab("path", { updateHash: false });
    explainItem(hash.slice("explain/".length));
    return;
  }
  showTab(hash, { updateHash: false });
}

window.addEventListener("hashchange", routeHash);

/* ------------------------------------------------------------------- auth */
let authMode = "login";

function showAuthGate(show) {
  $("#authGate").hidden = !show;
  document.body.classList.toggle("is-gated", show);
}

function authError(message) {
  const box = $("#authError");
  box.textContent = message || "";
  box.hidden = !message;
}

$$(".auth-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    authMode = tab.dataset.auth;
    $$(".auth-tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    $("#authNameRow").hidden = authMode !== "register";
    $("#authSubmit").textContent = authMode === "register" ? "Create account" : "Sign in";
    $("#authPassword").autocomplete =
      authMode === "register" ? "new-password" : "current-password";
    authError("");
  });
});

$("#authForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  authError("");
  const body = {
    email: $("#authEmail").value.trim(),
    password: $("#authPassword").value,
    display_name: $("#authName").value.trim(),
  };
  const submit = $("#authSubmit");
  submit.disabled = true;
  try {
    const endpoint = authMode === "register" ? "/api/auth/register" : "/api/auth/login";
    state.user = await api(endpoint, { method: "POST", body: JSON.stringify(body) });
    await enterApp();
  } catch (err) {
    authError(err.message);
  } finally {
    submit.disabled = false;
  }
});

$("#guestBtn").addEventListener("click", async () => {
  authError("");
  try {
    state.user = await api("/api/auth/guest", { method: "POST" });
    await enterApp();
  } catch (err) { authError(err.message); }
});

$("#signOutBtn").addEventListener("click", async () => {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (_) { /* going anyway */ }
  localStorage.removeItem(LS_KEY);
  window.location.reload();
});

async function upgradeGuest() {
  const email = (window.prompt("Email for your new account") || "").trim();
  if (!email) return;
  const password = (window.prompt("Choose a password (at least 8 characters)") || "").trim();
  if (!password) return;
  try {
    state.user = await api("/api/auth/upgrade", {
      method: "POST", body: JSON.stringify({ email, password }),
    });
    renderUserChip();
    toast("Account created. Everything you have done so far is saved to it.", 5000);
  } catch (err) { toast(err.message, 5000); }
}

function renderUserChip() {
  const chip = $("#userChip");
  const out = $("#signOutBtn");
  if (!state.user) { chip.hidden = true; out.hidden = true; return; }
  chip.hidden = false;
  out.hidden = false;
  chip.textContent = state.user.is_guest ? "Guest" : state.user.display_name;
  chip.title = state.user.is_guest
    ? "A throwaway account — save your work by creating a real one."
    : `${state.user.email} · ${state.user.learners} of ${state.user.max_learners} learners`;

  // A guest is offered a way to keep their work, not just a way out.
  const keep = $("#keepWorkBtn");
  keep.hidden = !state.user.is_guest;
  $("#signOutBtn").textContent = state.user.is_guest ? "Discard" : "Sign out";
}

/* --------------------------------------------------------------- bootstrap */
async function boot() {
  try {
    const me = await api("/api/auth/me");
    if (!me.signed_in) { showAuthGate(true); return; }
    state.user = me;
  } catch (_) {
    toast("Cannot reach the API. Is the server running?");
    return;
  }
  await enterApp();
}

/* Everything that needs a signed-in user. */
async function enterApp() {
  showAuthGate(false);
  renderUserChip();
  try {
    const health = await api("/api/health");
    state.llm = health.llm_enabled;
    state.providerName = health.provider;
    state.modelName = health.model;
    renderEngineSwitch();
  } catch (err) {
    toast("Cannot reach the API. Is the server running?");
    return;
  }

  state.goals = await api("/api/catalog/goals");
  state.skills = await api("/api/catalog/skills");
  state.items = await api("/api/catalog/items");

  const learners = await api("/api/learners");
  const stored = localStorage.getItem(LS_KEY);
  let chosen = learners.find((l) => l.learner_id === stored) || learners[0];
  if (!chosen) chosen = await api("/api/learners", { method: "POST", body: "{}" });

  renderLearnerSelect(learners.length ? learners : [chosen], chosen.learner_id);
  await selectLearner(chosen.learner_id);

  // Honour a deep link on load, e.g. /#dashboard or /#explain/c-js.
  if (window.location.hash.slice(1)) routeHash();
}

async function refreshUser() {
  try { state.user = await api("/api/auth/me"); renderUserChip(); } catch (_) {}
}

function renderLearnerSelect(learners, activeId) {
  const select = $("#learnerSelect");
  select.innerHTML = "";
  learners.forEach((l) => {
    const label = `${l.name || "Learner"} · ${l.learner_id.slice(0, 6)}`;
    select.appendChild(el("option", { text: label, attrs: { value: l.learner_id } }));
  });
  select.value = activeId;
}

$("#learnerSelect").addEventListener("change", (e) => selectLearner(e.target.value));

/* ---------------------------------------------------------- engine choice */
/* Two engines answer the chat: the configured model, or the deterministic
   Standard engine that ships with the app. The choice is per learner. */
function engineLabel() {
  return state.llm && state.providerName
    ? state.providerName.charAt(0).toUpperCase() + state.providerName.slice(1)
    : "AI model";
}

function renderEngineSwitch() {
  const chosen = state.profile?.assistant_engine || "auto";
  $$(".engine-switch .seg").forEach((btn) => {
    const on = btn.dataset.engine === chosen;
    btn.classList.toggle("is-on", on);
    btn.setAttribute("aria-pressed", String(on));
  });

  const ai = $("#engineAuto");
  ai.textContent = engineLabel();
  ai.disabled = !state.llm;
  ai.title = state.llm
    ? `Answers come from ${engineLabel()}, falling back to the Standard engine if it fails`
    : "No provider configured — add a key in .env to enable this";

  // The badge has to agree with the switch, or flipping it looks like nothing
  // happened. It reports what will actually answer the next message.
  const badge = $("#engineBadge");
  if (chosen === "rules") {
    badge.textContent = "Standard engine · offline";
    badge.className = "badge badge-rules";
    badge.title = "Deterministic answers computed from your path. No model call, "
      + "no network, identical every time.";
  } else if (state.llm) {
    badge.textContent = `${engineLabel()} · ${state.modelName || ""}`.trim();
    badge.className = "badge badge-live";
    badge.title = `Answers come from ${engineLabel()}. If it is unavailable or out `
      + "of quota, the Standard engine answers instead and the reply says so.";
  } else {
    badge.textContent = "Standard engine · no provider";
    badge.className = "badge badge-rules";
    badge.title = "No provider credential found, so the Standard engine answers "
      + "everything. The product works fully without one.";
  }
}

/* Delegated, so the handler cannot be missed however the header is rebuilt. */
document.addEventListener("click", async (event) => {
  const btn = event.target.closest?.(".engine-switch .seg");
  if (!btn || btn.disabled) return;
  const engine = btn.dataset.engine;
  if (!state.learnerId) return;
  if (state.profile?.assistant_engine === engine) {
    toast(engine === "rules"
      ? "Already using the Standard engine."
      : `Already using ${engineLabel()}.`);
    return;
  }
  try {
    state.profile = await api(
      `/api/learners/${state.learnerId}/profile?regenerate=false`,
      { method: "PATCH", body: JSON.stringify({ assistant_engine: engine }) },
    );
    renderEngineSwitch();
    toast(engine === "rules"
      ? "Standard engine — instant, offline, and identical every time."
      : `${engineLabel()} — replies will be more conversational.`, 3600);
  } catch (err) { toast(err.message); }
});

async function selectLearner(id) {
  state.learnerId = id;
  localStorage.setItem(LS_KEY, id);
  state.profile = await api(`/api/learners/${id}/profile`);
  state.history = new Set(state.profile.completed_item_ids || []);
  state.path = null;

  if (state.profile.goal_id) {
    try { state.path = await api(`/api/learners/${id}/path`); } catch (_) { /* none yet */ }
  }
  renderEngineSwitch();
  await loadPaths();
  await renderChatHistory();
  renderPath();
  renderProfileTab();
  renderSuggestions(state.path
    ? ["What should I start first?", "Why is this order?", "What skills am I still missing?"]
    : ["I want to become a data analyst", "Help me move into AI engineering", "I know some JavaScript and want a frontend job"]);
}

/* ------------------------------------------------------------------- chat */
async function renderChatHistory() {
  const log = $("#chatLog");
  log.innerHTML = "";
  let history = [];
  try { history = await api(`/api/learners/${state.learnerId}/conversation?limit=50`); } catch (_) {}

  if (!history.length) {
    log.appendChild(el("div", { class: "msg msg-bot", text:
      "Hi. Tell me what you want to be able to do — a role you're aiming for, a project you want to build, or a subject you want to master. Include anything you already know and how much time you have each week, and I'll build a path around it." }));
  }
  history.forEach((m) => {
    const node = el("div", { class: `msg msg-${m.role === "user" ? "user" : "bot"}` });
    setMessageText(node, m.content);
    log.appendChild(node);
  });
  log.scrollTop = log.scrollHeight;
}

function renderSuggestions(list) {
  const box = $("#suggestions");
  box.innerHTML = "";
  (list || []).forEach((s) => {
    box.appendChild(el("button", {
      class: "btn btn-mini", text: s, attrs: { type: "button" },
      on: { click: () => { $("#chatInput").value = s; $("#chatForm").requestSubmit(); } },
    }));
  });
}

$("#chatForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#chatInput");
  const message = input.value.trim();
  if (!message) return;
  input.value = "";

  const log = $("#chatLog");
  log.appendChild(el("div", { class: "msg msg-user", text: message }));
  const pending = el("div", { class: "msg msg-bot", text: "Thinking…" });
  log.appendChild(pending);
  log.scrollTop = log.scrollHeight;
  renderSuggestions([]);

  try {
    const res = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, learner_id: state.learnerId }),
    });
    setMessageText(pending, res.reply);
    pending.appendChild(el("div", {
      class: "msg-meta",
      text: res.source === "rules"
        ? "Answered by the Standard engine"
        : `Answered by ${res.source}`,
    }));
    state.profile = res.profile;
    renderInterpretation(res.interpretation);
    renderSuggestions(res.suggested_replies);
    if (res.needs_clarification) {
      pending.appendChild(el("div", { class: "msg-meta", text:
        "Pick one below, or describe the outcome you want in your own words." }));
    }

    if (res.path_generated) {
      state.path = await api(`/api/learners/${state.learnerId}/path`);
      await loadPaths();
      renderPath();
      renderProfileTab();
      const learners = await api("/api/learners");
      renderLearnerSelect(learners, state.learnerId);
      toast("Learning path generated — open the Path tab.");
    }
  } catch (err) {
    pending.textContent = `Something went wrong: ${err.message}`;
  }
  log.scrollTop = log.scrollHeight;
});

function renderInterpretation(interp) {
  const box = $("#interpretBox");
  box.innerHTML = "";
  if (!interp) return;
  const dl = el("dl");
  const add = (label, value) => {
    if (!value || (Array.isArray(value) && !value.length)) return;
    dl.appendChild(el("dt", { text: label }));
    dl.appendChild(el("dd", { text: Array.isArray(value) ? value.join(", ") : String(value) }));
  };
  box.appendChild(el("strong", { text: "What I understood" }));
  add("Goal", interp.goal_title || "—");
  add("Confidence", `${Math.round((interp.confidence || 0) * 100)}%`);
  add("Level", interp.experience_level);
  add("Hours/week", interp.weekly_hours);
  add("Already knows", interp.declared_skills.map(skillName));
  add("Formats", interp.preferred_formats);
  add("Read by", interp.source === "llm" ? "Claude" : "keyword rules");
  box.appendChild(dl);
}

/* A link for an item. Catalogue entries that publish a canonical URL link
   straight to it; the sample catalogue's fictional providers do not, so those
   get an honest search rather than a fabricated dead link. */
function videoLink(item) {
  const skills = (item.teaches_names || []).join(" ");
  const query = `${item.title} ${skills} tutorial`.trim();
  return el("a", {
    class: "item-link item-link-video",
    text: "Videos ↗",
    attrs: {
      href: `https://www.youtube.com/results?search_query=${encodeURIComponent(query)}`,
      target: "_blank", rel: "noopener noreferrer",
      title: "Free video tutorials covering the same skills",
    },
  });
}

function itemLink(item) {
  const url = item.url
    || `https://duckduckgo.com/?q=${encodeURIComponent(`${item.title} ${item.provider} course`)}`;
  return el("a", {
    class: "item-link",
    text: item.url ? "Open ↗" : "Find this ↗",
    attrs: {
      href: url, target: "_blank", rel: "noopener noreferrer",
      title: item.url ? url : "Search the web for this course",
    },
  });
}

function skillName(id) {
  const skill = state.skills.find((s) => s.id === id);
  return skill ? skill.name : id;
}

/* ---------------------------------------------------------- several paths */
const MAX_PATHS = 3;

async function loadPaths() {
  try { state.paths = await api(`/api/learners/${state.learnerId}/paths`); }
  catch (_) { state.paths = []; }
  renderPathSwitcher();
}

function renderPathSwitcher() {
  const host = $("#pathSwitcher");
  host.innerHTML = "";
  const paths = state.paths || [];
  if (!paths.length && !state.profile?.goal_id) return;

  paths.forEach((p) => {
    const chip = el("button", {
      class: "path-chip" + (p.is_active ? " is-active" : "") + (p.completed ? " is-done" : ""),
      attrs: { type: "button", title: `${p.items_completed} of ${p.items_total} items done` },
      on: { click: () => switchPath(p.goal_id) },
    }, [
      el("span", { class: "path-chip-title", text: p.goal_title }),
      el("span", { class: "path-chip-meta", text: `${Math.round(p.percent)}%` }),
    ]);
    const bar = el("span", { class: "path-chip-bar" });
    bar.appendChild(el("span", { attrs: { style: `width:${p.percent}%` } }));
    chip.appendChild(bar);
    if (paths.length > 1) {
      chip.appendChild(el("span", {
        class: "path-chip-x", text: "✕",
        attrs: { role: "button", title: `Remove ${p.goal_title}` },
        on: { click: (e) => { e.stopPropagation(); removePath(p.goal_id, p.goal_title); } },
      }));
    }
    host.appendChild(chip);
  });

  if (paths.length < MAX_PATHS) {
    const picker = el("select", { class: "path-add", attrs: { "aria-label": "Add another goal" } });
    picker.appendChild(el("option", { text: "+ Add a goal", attrs: { value: "" } }));
    const taken = new Set(paths.map((p) => p.goal_id));
    state.goals.filter((g) => !taken.has(g.id)).forEach((g) => {
      picker.appendChild(el("option", { text: g.title, attrs: { value: g.id } }));
    });
    picker.addEventListener("change", () => {
      if (picker.value) addPath(picker.value);
    });
    host.appendChild(picker);
  }
}

async function switchPath(goalId) {
  try {
    state.path = await api(`/api/learners/${state.learnerId}/paths/${goalId}/activate`,
                           { method: "POST" });
    state.profile = await api(`/api/learners/${state.learnerId}/profile`);
    await loadPaths();
    renderPath();
    toast(`Now showing ${state.path.goal_title}.`);
  } catch (err) { toast(err.message); }
}

async function addPath(goalId) {
  try {
    state.path = await api(`/api/learners/${state.learnerId}/paths`, {
      method: "POST", body: JSON.stringify({ goal_id: goalId }),
    });
    state.profile = await api(`/api/learners/${state.learnerId}/profile`);
    await loadPaths();
    renderPath();
    toast(`Added ${state.path.goal_title} — ${state.path.total_hours}h across ${state.path.milestones.length} stages.`, 4200);
  } catch (err) { toast(err.message, 5000); renderPathSwitcher(); }
}

async function removePath(goalId, title) {
  try {
    await api(`/api/learners/${state.learnerId}/paths/${goalId}`, { method: "DELETE" });
    state.profile = await api(`/api/learners/${state.learnerId}/profile`);
    state.path = state.profile.goal_id
      ? await api(`/api/learners/${state.learnerId}/path`)
      : null;
    await loadPaths();
    renderPath();
    toast(`Removed ${title}. Your progress on its courses is kept.`, 4200);
  } catch (err) { toast(err.message); }
}

/* -------------------------------------------------- shaping a path by hand */
async function addItemToPath(itemId, title) {
  try {
    state.path = await api(`/api/learners/${state.learnerId}/path/items`, {
      method: "POST", body: JSON.stringify({ item_id: itemId }),
    });
    await loadPaths();
    renderPath();
    toast(`Added “${title}” to your path.`, 4000);
  } catch (err) { toast(err.message, 4000); }
}

async function removeItemFromPath(itemId, title) {
  try {
    state.path = await api(`/api/learners/${state.learnerId}/path/items/${itemId}`,
                           { method: "DELETE" });
    state.lastRemoved = { itemId, title };
    await loadPaths();
    renderPath();
    const stranded = (state.path.uncovered_skills || []).length;
    toast(stranded
      ? `Removed “${title}”, but it was the only course covering ${stranded} skill(s) — undo it on the Path tab if that was not intended.`
      : `Removed “${title}” — I looked for another route to the same skill.`,
      stranded ? 6000 : 4200);
  } catch (err) { toast(err.message); }
}

/* ------------------------------------------------------------------- path */
function renderPath() {
  const header = $("#pathHeader");
  const gapBox = $("#gapBox");
  const list = $("#milestones");
  header.innerHTML = ""; gapBox.innerHTML = ""; list.innerHTML = "";

  if (!state.path) {
    list.appendChild(el("div", { class: "empty", text:
      "No path yet. Go to the Chat tab and describe what you want to learn." }));
    return;
  }
  renderPathSwitcher();
  const path = state.path;

  header.appendChild(el("div", { class: "path-title", text: path.goal_title }));
  header.appendChild(el("div", { class: "path-summary", text: path.summary }));
  if (path.adaptation_notes && path.adaptation_notes.length) {
    header.appendChild(el("div", { class: "hint", text: "Latest change: " + path.adaptation_notes.join(" ") }));
  }
  const meta = el("div", { class: "hint" });
  meta.appendChild(el("span", { text:
    `Revision ${path.revision} · generated ${new Date(path.generated_at).toLocaleString()}  ` }));
  meta.appendChild(el("button", {
    class: "btn btn-mini", text: "⬇ Export roadmap (Markdown)",
    attrs: { type: "button" },
    on: { click: exportRoadmap },
  }));
  header.appendChild(meta);

  // --- gap panel
  const gap = path.gap;
  gapBox.appendChild(el("strong", { text: "Skill gap analysis" }));
  // The engine's own plain-language reading of the gap, fetched separately so
  // the panel renders immediately from the path payload either way.
  const explanation = el("div", { class: "gap-explanation" });
  gapBox.appendChild(explanation);
  loadGapExplanation(explanation);
  gapBox.appendChild(el("div", { class: "hint", text:
    `${gap.known_skill_names.length} of ${gap.known_skill_names.length + gap.missing_skill_names.length} required skills already held (${gap.coverage_pct}%).` }));
  const bar = el("div", { class: "gap-bar" });
  bar.appendChild(el("span", { attrs: { style: `width:${gap.coverage_pct}%` } }));
  gapBox.appendChild(bar);

  if (gap.known_skill_names.length) {
    gapBox.appendChild(el("div", { class: "hint", text: "Already held:" }));
    const row = el("div", { class: "pill-row" });
    gap.known_skill_names.forEach((n) => row.appendChild(el("span", { class: "pill pill-known", text: n })));
    gapBox.appendChild(row);
  }
  if (gap.missing_skill_names.length) {
    gapBox.appendChild(el("div", { class: "hint", text: "To learn (in dependency order):" }));
    const row = el("div", { class: "pill-row" });
    gap.missing_skill_names.forEach((n) => row.appendChild(el("span", { class: "pill pill-missing", text: n })));
    gapBox.appendChild(row);
  }
  if (path.uncovered_skills && path.uncovered_skills.length) {
    const warn = el("div", { class: "uncovered" });
    warn.appendChild(el("strong", { text: "Not currently reachable" }));
    warn.appendChild(el("div", { class: "hint", text:
      "No course on offer covers " + path.uncovered_skills.join(", ") +
      ", so anything depending on them has left the path." }));
    if (state.lastRemoved) {
      warn.appendChild(el("button", {
        class: "btn btn-mini btn-accent",
        text: `Undo removing “${state.lastRemoved.title}”`,
        attrs: { type: "button" },
        on: { click: () => {
          const { itemId, title } = state.lastRemoved;
          state.lastRemoved = null;
          addItemToPath(itemId, title);
        } },
      }));
    }
    gapBox.appendChild(warn);
  }

  // --- milestones
  path.milestones.forEach((m) => list.appendChild(renderMilestone(m)));
}

async function loadGapExplanation(host) {
  try {
    const gap = await api(`/api/learners/${state.learnerId}/gap`);
    host.textContent = gap.explanation;
    if (gap.report.bridging_skills.length) {
      host.appendChild(el("div", { class: "hint", text:
        `${gap.report.bridging_skills.length} of them are prerequisites you did not ask for — they are on the path because the goal skills depend on them.` }));
    }
  } catch (_) { host.remove(); }   // the path panel is complete without it
}

function renderMilestone(m) {
  const card = el("div", { class: "milestone" });
  const head = el("div", { class: "milestone-head" });
  head.appendChild(el("div", { class: "milestone-title" }, [
    el("span", { text: m.title }),
    el("span", { class: "milestone-meta", text: `${m.hours}h · ~${m.est_weeks} weeks · by week ${m.cumulative_weeks}` }),
  ]));
  head.appendChild(el("div", { class: "milestone-obj", text: m.objective }));
  card.appendChild(head);

  m.items.forEach((item) => card.appendChild(renderPathItem(item)));
  return card;
}

function renderPathItem(item) {
  const row = el("div", { class: "item" + (item.status === "completed" ? " is-done" : "") });
  row.appendChild(el("span", { class: `item-role role-${item.role}`, text: item.role }));

  const body = el("div");
  body.appendChild(el("div", { class: "item-title" }, [
    item.title, itemLink(item), videoLink(item),
  ]));
  body.appendChild(el("div", { class: "item-sub", text:
    `${item.provider} · ${item.hours}h · level ${item.level} · ${item.format} · ${item.cost} · rated ${item.rating}/5` }));
  if (item.teaches_names.length) {
    body.appendChild(el("div", { class: "item-sub", text: "Teaches: " + item.teaches_names.join(", ") }));
  }
  body.appendChild(el("div", { class: "item-why" }, [
    el("strong", { text: "Why this, here: " }), item.rationale,
  ]));
  row.appendChild(body);

  const actions = el("div", { class: "item-actions" });
  const statusSelect = el("select", { attrs: { "aria-label": "Status" } });
  [["not_started", "Not started"], ["in_progress", "In progress"], ["completed", "Completed"]]
    .forEach(([value, label]) => {
      const opt = el("option", { text: label, attrs: { value } });
      if (item.status === value) opt.selected = true;
      statusSelect.appendChild(opt);
    });
  statusSelect.addEventListener("change", () => setProgress(item.item_id, statusSelect.value));
  actions.appendChild(statusSelect);
  actions.appendChild(explainButton(item.item_id));
  actions.appendChild(el("button", {
    class: "btn btn-mini btn-danger", text: "Remove",
    attrs: { type: "button", title: "Drop this from the path and find another route" },
    on: { click: () => removeItemFromPath(item.item_id, item.title) },
  }));

  const fbRow = el("div", { class: "row" });
  [["too_easy", "Too easy"], ["too_hard", "Too hard"], ["not_relevant", "Not relevant"], ["liked", "Liked"]]
    .forEach(([signal, label]) => {
      fbRow.appendChild(el("button", {
        class: "btn btn-mini", text: label, attrs: { type: "button", title: `Give feedback: ${label}` },
        on: { click: () => sendFeedback(item.item_id, signal) },
      }));
    });
  actions.appendChild(fbRow);
  row.appendChild(actions);
  return row;
}

async function exportRoadmap() {
  try {
    const res = await fetch(`/api/learners/${state.learnerId}/export`);
    if (!res.ok) throw new Error(`Export failed (${res.status})`);
    const text = await res.text();
    const url = URL.createObjectURL(new Blob([text], { type: "text/markdown" }));
    const link = el("a", { attrs: { href: url, download: "learning-path.md" } });
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast("Roadmap exported as Markdown.");
  } catch (err) { toast(err.message); }
}

async function setProgress(itemId, status) {
  try {
    await api(`/api/learners/${state.learnerId}/progress`, {
      method: "POST",
      body: JSON.stringify({ item_id: itemId, status }),
    });
    state.path = await api(`/api/learners/${state.learnerId}/path`);
    renderPath();
    toast("Progress saved.");
  } catch (err) { toast(err.message); }
}

async function sendFeedback(itemId, signal) {
  try {
    const res = await api(`/api/learners/${state.learnerId}/feedback`, {
      method: "POST",
      body: JSON.stringify({ item_id: itemId, signal }),
    });
    state.path = await api(`/api/learners/${state.learnerId}/path`);
    state.profile = await api(`/api/learners/${state.learnerId}/profile`);
    renderPath();
    renderProfileTab();
    toast(res.applied || `Feedback recorded — ${res.effect}.`, 4200);
  } catch (err) { toast(err.message); }
}

/* ---------------------------------------------------------------- explain */
const SCORE_LABELS = {
  goal_relevance: "Goal relevance",
  skill_readiness: "Readiness",
  level_fit: "Level fit",
  interest_match: "Interest match",
  format_fit: "Format/cost fit",
  quality: "Quality",
};

/* The full justification for one item: the same six numbers that produced the
   ranking, each multiplied by its weight, plus where the planner put it. */
async function explainItem(itemId) {
  let data;
  try {
    data = await api(`/api/learners/${state.learnerId}/explain/${itemId}`);
  } catch (err) { toast(err.message); return; }

  const rec = data.recommendation;
  const body = openModal(rec.title);

  body.appendChild(el("div", {}, [itemLink(rec)]));
  body.appendChild(el("div", { class: "item-sub", text:
    `${rec.provider} · ${rec.type} · ${rec.hours}h · level ${rec.level} · ${rec.format} · ${rec.cost} · rated ${rec.rating}/5` }));
  body.appendChild(el("div", { class: "explain-score" }, [
    el("span", { class: "explain-score-value", text: rec.score.toFixed(3) }),
    el("span", { class: "hint", text: "weighted match score, 0–1" }),
  ]));

  if (data.placement) {
    // The milestone title already carries its stage number; don't repeat it.
    body.appendChild(el("div", { class: "explain-block" }, [
      el("strong", { text: data.placement.milestone }),
      el("div", { class: "item-why", text: data.placement.rationale }),
    ]));
  } else {
    body.appendChild(el("div", { class: "explain-block hint", text:
      "Not scheduled in your current path — this is how it would score if you added it." }));
  }

  if (rec.reasons.length) {
    const reasons = el("ul", { class: "rec-reasons" });
    rec.reasons.forEach((reason) => reasons.appendChild(el("li", { text: reason })));
    body.appendChild(el("div", { class: "explain-block" }, [
      el("strong", { text: "Why it ranks here" }), reasons,
    ]));
  }

  // The score is a weighted sum, so show both halves of every product.
  const weights = data.weights || {};
  const widest = Math.max(...Object.values(weights), 0.01);
  const table = el("div", { class: "explain-table" });
  table.appendChild(el("div", { class: "explain-row explain-head" }, [
    el("span", { text: "Component" }), el("span", { text: "Score" }),
    el("span", { text: "× Weight" }), el("span", { text: "= Contribution" }),
  ]));
  Object.entries(rec.breakdown).forEach(([key, value]) => {
    const weight = weights[key] || 0;
    const contribution = value * weight;
    const bar = el("span", { class: "bd-bar" });
    bar.appendChild(el("span", { attrs: { style: `width:${(contribution / widest) * 100}%` } }));
    table.appendChild(el("div", { class: "explain-row" }, [
      el("span", { class: "bd-name", text: SCORE_LABELS[key] || key }),
      el("span", { text: value.toFixed(2) }),
      el("span", { class: "hint", text: weight.toFixed(2) }),
      el("span", { class: "explain-contrib" }, [
        el("span", { text: contribution.toFixed(3) }), bar,
      ]),
    ]));
  });
  body.appendChild(el("div", { class: "explain-block" }, [
    el("strong", { text: "How the score is built" }),
    el("div", { class: "hint", text:
      "Each component is computed independently, then multiplied by its weight. The six contributions sum to the score above." }),
    table,
  ]));

  const skills = el("div", { class: "explain-block" });
  skills.appendChild(el("strong", { text: "Skills" }));
  if (rec.teaches_names.length) {
    skills.appendChild(el("div", { class: "hint", text: "Teaches:" }));
    const row = el("div", { class: "pill-row" });
    rec.teaches_names.forEach((n) => row.appendChild(el("span", { class: "pill", text: n })));
    skills.appendChild(row);
  }
  if (rec.closes_gap_skills.length) {
    skills.appendChild(el("div", { class: "hint", text: "Closes these gaps for you:" }));
    const row = el("div", { class: "pill-row" });
    rec.closes_gap_skills.forEach((id) =>
      row.appendChild(el("span", { class: "pill pill-known", text: skillName(id) })));
    skills.appendChild(row);
  }
  if (!rec.prerequisites_met) {
    skills.appendChild(el("div", { class: "hint", text: "Blocked until you cover:" }));
    const row = el("div", { class: "pill-row" });
    rec.missing_prerequisites.forEach((n) =>
      row.appendChild(el("span", { class: "pill pill-missing", text: n })));
    skills.appendChild(row);
  }
  body.appendChild(skills);
}

function explainButton(itemId) {
  return el("button", {
    class: "btn btn-mini", text: "Why this?",
    attrs: { type: "button", title: "Full scoring breakdown and placement" },
    on: { click: () => explainItem(itemId) },
  });
}

/* ------------------------------------------------------------ skill graph */
const SVGNS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

async function loadGraph() {
  const host = $("#graphHost");
  const detail = $("#graphDetail");
  const legend = $("#graphLegend");
  host.innerHTML = ""; detail.innerHTML = ""; legend.innerHTML = "";

  if (!state.profile || !state.profile.goal_id) {
    host.appendChild(el("div", { class: "empty", text: "Set a goal in the Chat tab first." }));
    return;
  }

  let graph;
  try { graph = await api(`/api/learners/${state.learnerId}/graph`); }
  catch (err) { toast(err.message); return; }

  if (!graph.nodes.length) {
    host.appendChild(el("div", { class: "empty", text: "Nothing to plot for this goal." }));
    return;
  }

  const dot = (cls, label) => el("span", {}, [
    el("i", { class: cls, attrs: { style: "width:.6rem;height:.6rem;border-radius:50%;display:inline-block" } }),
    " " + label,
  ]);
  legend.appendChild(dot("dot-mastered", "mastered"));
  legend.appendChild(dot("dot-in_progress", "in progress"));
  legend.appendChild(dot("dot-planned", "planned"));
  legend.appendChild(el("span", { text: "dashed border = a headline skill for this goal" }));

  host.appendChild(buildGraphSvg(graph, detail));
  detail.appendChild(el("div", { class: "m", text: "Click any skill to see what teaches it and what it unlocks." }));
}

function buildGraphSvg(graph, detail) {
  const NW = 150, NH = 46, GAPX = 74, GAPY = 16, PADX = 26, PADY = 34;
  const colW = NW + GAPX;
  const width = PADX * 2 + graph.layer_count * NW + (graph.layer_count - 1) * GAPX;
  const height = PADY * 2 + graph.widest_layer * (NH + GAPY);

  const pos = {};
  graph.nodes.forEach((n) => {
    const layerCount = graph.nodes.filter((m) => m.layer === n.layer).length;
    // Centre each column vertically so the graph reads as a flow, not a grid.
    const offset = (graph.widest_layer - layerCount) * (NH + GAPY) / 2;
    pos[n.id] = {
      x: PADX + n.layer * colW,
      y: PADY + offset + n.slot * (NH + GAPY),
    };
  });

  const svg = svgEl("svg", {
    width, height, viewBox: `0 0 ${width} ${height}`,
    role: "img", "aria-label": `Prerequisite graph for ${graph.goal_title}`,
  });

  const defs = svgEl("defs");
  [["arrow", "var(--border)"], ["arrow-ok", "var(--good)"]].forEach(([id, fill]) => {
    const marker = svgEl("marker", {
      id, markerWidth: 8, markerHeight: 8, refX: 7, refY: 3,
      orient: "auto", markerUnits: "strokeWidth",
    });
    marker.appendChild(svgEl("path", { d: "M0,0 L0,6 L7,3 z", fill }));
    defs.appendChild(marker);
  });
  svg.appendChild(defs);

  // Column headings.
  for (let layer = 0; layer < graph.layer_count; layer++) {
    const label = svgEl("text", {
      x: PADX + layer * colW + NW / 2, y: 16,
      "text-anchor": "middle", class: "layer-label",
    });
    label.textContent = layer === 0 ? "start here" : `depth ${layer}`;
    svg.appendChild(label);
  }

  // Edges first so nodes paint over them.
  graph.edges.forEach((e) => {
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return;
    const x1 = a.x + NW, y1 = a.y + NH / 2;
    const x2 = b.x - 3, y2 = b.y + NH / 2;
    const mid = (x1 + x2) / 2;
    svg.appendChild(svgEl("path", {
      d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
      class: "edge" + (e.satisfied ? " satisfied" : ""),
      "marker-end": `url(#${e.satisfied ? "arrow-ok" : "arrow"})`,
    }));
  });

  const byId = {};
  graph.nodes.forEach((n) => { byId[n.id] = n; });

  graph.nodes.forEach((n) => {
    const p = pos[n.id];
    const group = svgEl("g", { class: "node-box", tabindex: "0", role: "button" });
    group.appendChild(svgEl("rect", {
      x: p.x, y: p.y, width: NW, height: NH,
      class: `node-rect ${n.state}${n.is_target ? " target" : ""}`,
    }));

    const name = svgEl("text", { x: p.x + 10, y: p.y + 19, class: "node-label" });
    name.textContent = n.name.length > 24 ? n.name.slice(0, 23) + "…" : n.name;
    group.appendChild(name);

    const sub = svgEl("text", { x: p.x + 10, y: p.y + 34, class: "node-sub" });
    sub.textContent = n.state === "mastered"
      ? "✓ already held"
      : (n.milestone ? `stage ${n.milestone}` : n.domain);
    group.appendChild(sub);

    const title = svgEl("title");
    title.textContent = `${n.name} — ${n.domain}, level ${n.level}, ${n.state}`;
    group.appendChild(title);

    const show = () => {
      $$(".node-box").forEach((g) => g.classList.remove("is-selected"));
      group.classList.add("is-selected");
      showNodeDetail(n, graph, byId, detail);
    };
    group.addEventListener("click", show);
    group.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); show(); }
    });
    svg.appendChild(group);
  });

  return svg;
}

function showNodeDetail(node, graph, byId, detail) {
  detail.innerHTML = "";
  detail.appendChild(el("div", { class: "t", text: node.name }));
  detail.appendChild(el("div", { class: "m", text:
    `${node.domain} · level ${node.level} · ${node.state.replace("_", " ")}` +
    (node.is_target ? " · headline skill for this goal" : "") }));

  const needs = graph.edges.filter((e) => e.target === node.id)
    .map((e) => byId[e.source] && byId[e.source].name).filter(Boolean);
  const unlocks = graph.edges.filter((e) => e.source === node.id)
    .map((e) => byId[e.target] && byId[e.target].name).filter(Boolean);

  detail.appendChild(el("div", { class: "m", text:
    needs.length ? `Requires first: ${needs.join(", ")}` : "No prerequisites — you can start here." }));
  if (unlocks.length) {
    detail.appendChild(el("div", { class: "m", text: `Unlocks: ${unlocks.join(", ")}` }));
  }
  if (node.taught_by) {
    detail.appendChild(el("div", { class: "m", text:
      `Covered by “${node.taught_by}”${node.milestone ? ` in stage ${node.milestone}` : ""}.` }));
  } else if (node.state === "mastered") {
    detail.appendChild(el("div", { class: "m", text: "You already hold this — nothing scheduled." }));
  }
}

/* ------------------------------------------------------------ weekly plan */
async function loadPlan() {
  const header = $("#planHeader");
  const list = $("#planWeeks");
  header.innerHTML = ""; list.innerHTML = "";

  if (!state.profile || !state.profile.goal_id) {
    list.appendChild(el("div", { class: "empty", text: "Set a goal in the Chat tab first." }));
    return;
  }

  let plan;
  try { plan = await api(`/api/learners/${state.learnerId}/plan`); }
  catch (err) { toast(err.message); return; }

  header.appendChild(el("div", { class: "path-title", text: `${plan.total_weeks}-week schedule` }));
  header.appendChild(el("div", { class: "path-summary", text:
    `Your remaining roadmap for ${plan.goal_title}, poured into ${plan.weekly_hours}-hour weeks. ` +
    "Long courses split across weeks, because that is what actually happens. " +
    "Change your weekly hours in the Profile tab and this rebuilds." }));

  if (!plan.weeks.length) {
    list.appendChild(el("div", { class: "empty", text:
      "Nothing left to schedule — you have completed every item on this path." }));
    return;
  }

  plan.weeks.forEach((week) => {
    const card = el("div", { class: "week" });
    const head = el("div", { class: "week-head" });
    head.appendChild(el("div", {}, [
      el("span", { class: "week-no", text: `Week ${week.week}` }),
      el("span", { class: "week-focus", text: "  " + week.focus }),
    ]));
    head.appendChild(el("div", { class: "week-load", text: `${week.hours} / ${week.capacity} h` }));
    card.appendChild(head);

    const body = el("div", { class: "week-body" });
    week.items.forEach((item) => {
      const row = el("div", { class: "week-item" });
      row.appendChild(el("div", {}, [
        el("span", { text: item.title }),
        item.continues ? el("span", { class: "tag-continues", text: "  (continued)" }) : null,
      ]));
      row.appendChild(el("div", { class: "hrs", text: `${item.hours_this_week}h of ${item.total_hours}h` }));
      const bar = el("div", { class: "bar" });
      bar.appendChild(el("span", { attrs: { style: `width:${Math.min(100, item.portion * 100)}%` } }));
      row.appendChild(bar);
      body.appendChild(row);
    });
    card.appendChild(body);
    list.appendChild(card);
  });

  if (plan.truncated) {
    list.appendChild(el("div", { class: "hint", text:
      "Schedule truncated — raise your weekly hours to bring the finish line closer." }));
  }
}

/* -------------------------------------------------------------- dashboard */
async function loadDashboard() {
  const kpis = $("#dashKpis");
  if (!state.profile || !state.profile.goal_id) {
    kpis.innerHTML = "";
    $("#dashMilestones").innerHTML = "";
    $("#dashNext").innerHTML = "";
    $("#dashSkills").innerHTML = "";
    $("#dashDomains").innerHTML = "";
    kpis.appendChild(el("div", { class: "empty", text: "Set a goal in the Chat tab first." }));
    return;
  }
  try {
    state.dashboard = await api(`/api/learners/${state.learnerId}/dashboard`);
    renderDashboard(state.dashboard);
    renderAchievements(await api(`/api/learners/${state.learnerId}/achievements`));
  } catch (err) { toast(err.message); }
}

function renderAchievements(ach) {
  const xp = $("#xpBar");
  const grid = $("#badgeGrid");
  xp.innerHTML = ""; grid.innerHTML = "";

  const top = el("div", { class: "xp-top" });
  top.appendChild(el("div", { class: "xp-level", text: `Level ${ach.level} · ${ach.level_title}` }));
  top.appendChild(el("div", { class: "xp-num", text:
    ach.xp_for_next_level
      ? `${ach.xp} XP · ${ach.xp_for_next_level - ach.xp_into_level} to next level`
      : `${ach.xp} XP · max level` }));
  xp.appendChild(top);

  const track = el("div", { class: "xp-track" });
  const filled = ach.xp_for_next_level
    ? Math.min(100, (ach.xp_into_level / ach.xp_for_next_level) * 100)
    : 100;
  track.appendChild(el("span", { class: "xp-fill", attrs: { style: `width:${filled}%` } }));
  xp.appendChild(track);
  xp.appendChild(el("div", { class: "hint", text:
    `${ach.earned_count} of ${ach.total_count} badges earned · 10 XP per hour completed` }));

  ach.badges.forEach((b) => {
    grid.appendChild(el("div", {
      class: "badge" + (b.earned ? " earned" : ""),
      attrs: { title: b.earned ? b.description : b.hint },
    }, [
      el("span", { class: "icon", text: b.icon }),
      el("div", {}, [
        el("div", { class: "n", text: b.name }),
        el("div", { class: "d", text: b.earned ? b.description : b.hint }),
      ]),
    ]));
  });
}

/* Every roadmap the learner is running, with the active one called out. */
function renderDashboardPaths(d) {
  const host = $("#dashPaths");
  host.innerHTML = "";
  const paths = d.paths || [];
  if (!paths.length) {
    host.appendChild(el("div", { class: "hint", text: "No paths yet." }));
    return;
  }

  paths.forEach((p, index) => {
    const card = el("div", {
      class: "path-card" + (p.is_active ? " is-active" : "") + (p.completed ? " is-done" : ""),
      attrs: { style: `animation-delay:${index * 60}ms` },
    });
    const head = el("div", { class: "path-card-head" });
    head.appendChild(el("div", {}, [
      el("div", { class: "path-card-title", text: p.goal_title }),
      el("div", { class: "path-card-sub", text:
        `${p.domain} · ${p.stages} stages · ${p.hours_remaining}h left of ${p.hours_total}h` }),
    ]));
    head.appendChild(el("span", {
      class: "badge " + (p.completed ? "badge-live" : p.is_active ? "badge-rules" : "badge-muted"),
      text: p.completed ? "Complete" : p.is_active ? "Active" : "Paused",
    }));
    card.appendChild(head);

    const bar = el("div", { class: "path-card-bar" });
    bar.appendChild(el("span", { attrs: { style: `width:${p.percent}%` } }));
    card.appendChild(bar);
    card.appendChild(el("div", { class: "path-card-foot", text:
      `${p.items_completed} of ${p.items_total} items · ${Math.round(p.percent)}%` }));

    if (!p.is_active) {
      card.appendChild(el("button", {
        class: "btn btn-mini", text: "Make active", attrs: { type: "button" },
        on: { click: () => switchPath(p.goal_id).then(() => loadDashboard()) },
      }));
    }
    host.appendChild(card);
  });
}

/* Observed study pace — what the learner does, not what they said they'd do. */
const PACE_LABELS = {
  unknown: ["Pace", "badge-muted"],
  on_track: ["On track", "badge-live"],
  ahead: ["Ahead of plan", "badge-live"],
  behind: ["Behind plan", "badge-rules"],
};

function renderPace(pace) {
  const card = $("#paceCard");
  card.innerHTML = "";
  if (!pace) { card.hidden = true; return; }
  card.hidden = false;

  const [label, badgeClass] = PACE_LABELS[pace.status] || PACE_LABELS.unknown;
  const head = el("div", { class: "pace-head" });
  head.appendChild(el("h3", { text: "Your actual pace" }));
  head.appendChild(el("span", { class: `badge ${badgeClass}`, text: label }));
  card.appendChild(head);
  card.appendChild(el("div", { class: "pace-message", text: pace.message }));

  if (pace.status !== "unknown") {
    const stats = el("div", { class: "pace-stats" });
    const stat = (value, note) => el("div", { class: "pace-stat" }, [
      el("div", { class: "kpi-value", text: value }),
      el("div", { class: "kpi-note", text: note }),
    ]);
    stats.appendChild(stat(`${pace.observed_weekly_hours}h`, "Observed per week"));
    stats.appendChild(stat(`${pace.planned_weekly_hours}h`, "Planned per week"));
    stats.appendChild(stat(`${pace.days_observed}`, "Days tracked"));
    if (pace.projected_weeks_remaining != null) {
      stats.appendChild(stat(
        `${pace.projected_weeks_remaining}`,
        `Weeks left at your real rate (plan says ${pace.planned_weeks_remaining})`,
      ));
    }
    card.appendChild(stats);
  }

  if (pace.suggested_weekly_hours != null) {
    card.appendChild(el("button", {
      class: "btn btn-primary", type: "button",
      text: `Re-plan around ${pace.suggested_weekly_hours}h a week`,
      attrs: { type: "button" },
      on: { click: replanToPace },
    }));
  }
}

async function replanToPace() {
  try {
    const result = await api(`/api/learners/${state.learnerId}/replan`, { method: "POST" });
    state.profile = await api(`/api/learners/${state.learnerId}/profile`);
    if (result.changed) state.path = await api(`/api/learners/${state.learnerId}/path`);
    renderPath();
    renderProfileTab();
    await loadDashboard();
    toast(result.message, 5000);
  } catch (err) { toast(err.message); }
}

function renderDashboard(d) {
  const kpis = $("#dashKpis");
  kpis.innerHTML = "";

  const ringCard = el("div", { class: "kpi" });
  const ringWrap = el("div", { class: "ring-wrap" });
  ringWrap.appendChild(donut(d.overall_percent));
  ringWrap.appendChild(el("div", {}, [
    el("div", { class: "kpi-label", text: "Overall progress" }),
    el("div", { class: "kpi-note", text: `${d.items_completed} of ${d.items_total} items` }),
    el("div", { class: "kpi-note", text: d.streak_note }),
  ]));
  ringCard.appendChild(ringWrap);
  kpis.appendChild(ringCard);

  const tile = (label, value, note) => {
    const node = el("div", { class: "kpi" });
    node.appendChild(el("div", { class: "kpi-label", text: label }));
    node.appendChild(el("div", { class: "kpi-value", text: value }));
    if (note) node.appendChild(el("div", { class: "kpi-note", text: note }));
    return node;
  };
  kpis.appendChild(tile("Skills mastered", `${d.skills_mastered}/${d.skills_total}`, "Toward " + d.goal_title));
  kpis.appendChild(tile("Hours remaining", `${d.hours_remaining}`, `${d.hours_completed}h of ${d.hours_total}h done`));
  kpis.appendChild(tile("Weeks remaining", `${d.weeks_remaining}`, "At your current weekly hours"));

  renderPace(d.pace);
  renderDashboardPaths(d);

  // milestones timeline
  const tl = $("#dashMilestones");
  tl.innerHTML = "";
  d.milestones.forEach((m) => {
    const row = el("div", { class: "tl-row" });
    row.appendChild(el("div", { class: `tl-dot ${m.status}`, text: m.status === "completed" ? "✓" : String(m.order) }));
    const mid = el("div");
    mid.appendChild(el("div", { class: "tl-name", text: m.title }));
    const bar = el("div", { class: "tl-bar" });
    bar.appendChild(el("span", { attrs: { style: `width:${m.percent}%` } }));
    mid.appendChild(bar);
    row.appendChild(mid);
    row.appendChild(el("div", { class: "tl-pct", text: `${m.completed_items}/${m.total_items} · ~${m.est_weeks}w` }));
    tl.appendChild(row);
  });

  // next actions
  const next = $("#dashNext");
  next.innerHTML = "";
  if (!d.next_actions.length) {
    next.appendChild(el("div", { class: "empty", text: "Nothing left — the path is complete." }));
  }
  d.next_actions.forEach((a) => {
    const node = el("div", { class: "next-item" });
    node.appendChild(el("div", { class: "t", text: a.title }));
    node.appendChild(el("div", { class: "m", text: `${a.type} · ${a.hours}h · ${a.milestone_title}` }));
    node.appendChild(el("div", { class: "m", text: a.why }));
    next.appendChild(node);
  });

  // domain stacked bars
  const domains = $("#dashDomains");
  domains.innerHTML = "";
  domains.appendChild(el("div", { class: "legend" }, [
    el("span", {}, [el("i", { class: "dot dot-mastered", attrs: { style: "width:.6rem;height:.6rem;border-radius:50%;display:inline-block" } }), " mastered"]),
    el("span", {}, [el("i", { class: "dot dot-in_progress", attrs: { style: "width:.6rem;height:.6rem;border-radius:50%;display:inline-block" } }), " in progress"]),
    el("span", {}, [el("i", { class: "dot dot-planned", attrs: { style: "width:.6rem;height:.6rem;border-radius:50%;display:inline-block" } }), " planned"]),
  ]));
  Object.entries(d.domain_breakdown).forEach(([domain, counts]) => {
    const total = counts.mastered + counts.in_progress + counts.planned || 1;
    const row = el("div", { class: "domain-row" });
    row.appendChild(el("div", { text: domain }));
    const stack = el("div", { class: "stack" });
    ["mastered", "in_progress", "planned"].forEach((k) => {
      if (!counts[k]) return;
      stack.appendChild(el("span", { class: `seg-${k}`, attrs: { style: `width:${(counts[k] / total) * 100}%` } }));
    });
    row.appendChild(stack);
    row.appendChild(el("div", { class: "tl-pct", text: `${counts.mastered}/${total}` }));
    domains.appendChild(row);
  });

  // skill chips
  const skills = $("#dashSkills");
  skills.innerHTML = "";
  d.skill_progress.forEach((s) => {
    skills.appendChild(el("div", { class: "skill-chip", attrs: { title: `${s.domain} · level ${s.level} · ${s.state}` } }, [
      el("span", { class: `dot dot-${s.state}` }),
      el("span", { text: s.name }),
    ]));
  });
}

function donut(percent) {
  const svgNS = "http://www.w3.org/2000/svg";
  const size = 84, stroke = 9, r = (size - stroke) / 2, c = 2 * Math.PI * r;
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("width", size); svg.setAttribute("height", size);
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
  svg.setAttribute("class", "ring");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${Math.round(percent)} percent complete`);

  const mk = (cls, dash) => {
    const circle = document.createElementNS(svgNS, "circle");
    circle.setAttribute("cx", size / 2); circle.setAttribute("cy", size / 2);
    circle.setAttribute("r", r); circle.setAttribute("fill", "none");
    circle.setAttribute("stroke-width", stroke); circle.setAttribute("class", cls);
    if (dash) {
      circle.setAttribute("stroke-dasharray", dash);
      circle.setAttribute("transform", `rotate(-90 ${size / 2} ${size / 2})`);
    }
    return circle;
  };
  svg.appendChild(mk("ring-track"));
  svg.appendChild(mk("ring-fill", `${(percent / 100) * c} ${c}`));

  const text = document.createElementNS(svgNS, "text");
  text.setAttribute("x", size / 2); text.setAttribute("y", size / 2 + 5);
  text.setAttribute("text-anchor", "middle"); text.setAttribute("class", "ring-text");
  text.textContent = pct(percent);
  svg.appendChild(text);
  return svg;
}

/* ---------------------------------------------------------- recommendations */
async function loadRecommendations() {
  const list = $("#recList");
  list.innerHTML = "";
  if (!state.profile || !state.profile.goal_id) {
    list.appendChild(el("div", { class: "empty", text: "Set a goal in the Chat tab first." }));
    return;
  }
  const type = $("#recType").value;
  const ready = $("#recReady").checked;
  const query = new URLSearchParams({ limit: "12", ready_only: String(ready) });
  if (type) query.set("type", type);

  let recs = [];
  try {
    recs = await api(`/api/learners/${state.learnerId}/recommendations?${query}`);
  } catch (err) { toast(err.message); return; }

  if (!recs.length) {
    list.appendChild(el("div", { class: "empty", text: "Nothing matches those filters." }));
    return;
  }

  // Rank is information, so show it. Anything you can start now that closes a
  // real gap leads; things you cannot start yet are separated rather than
  // scattered through the list looking equally available.
  const startable = recs.filter((r) => r.prerequisites_met);
  const blocked = recs.filter((r) => !r.prerequisites_met);

  if (startable.length) {
    list.appendChild(sectionHeading(
      "Best for your path right now",
      "Closes a gap you have, and every prerequisite is already met.",
    ));
    startable.forEach((r, i) => list.appendChild(renderRecommendation(r, i)));
  }
  if (blocked.length) {
    list.appendChild(sectionHeading(
      "Worth knowing about, later",
      "Strong matches that need a prerequisite you have not covered yet.",
    ));
    blocked.forEach((r, i) => list.appendChild(renderRecommendation(r, startable.length + i)));
  }
}

function sectionHeading(title, note) {
  return el("div", { class: "rec-heading" }, [
    el("h3", { text: title }),
    el("p", { class: "hint", text: note }),
  ]);
}

$("#recRefresh").addEventListener("click", loadRecommendations);
$("#recType").addEventListener("change", loadRecommendations);
$("#recReady").addEventListener("change", loadRecommendations);

/* Tier by score so the eye lands on the right card first: the leader is
   marked, strong matches are tinted, the rest are plain. */
function recommendationTier(r, index) {
  if (!r.prerequisites_met) return { rank: "later", label: "" };
  if (index === 0) return { rank: "top", label: "Best match" };
  if (r.score >= 0.75) return { rank: "strong", label: "Strong match" };
  if (r.closes_gap_skills.length) return { rank: "useful", label: "Closes a gap" };
  return { rank: "plain", label: "" };
}

function renderRecommendation(r, index = 0) {
  const tier = recommendationTier(r, index);
  const card = el("div", {
    class: `rec rec-${tier.rank}`,
    attrs: { style: `animation-delay:${Math.min(index, 8) * 45}ms` },
  });

  if (tier.label) {
    card.appendChild(el("span", { class: `rec-flag rec-flag-${tier.rank}`, text: tier.label }));
  }
  const onPath = (state.path?.milestones || [])
    .some((m) => m.items.some((i) => i.item_id === r.item_id));
  if (onPath) {
    card.appendChild(el("span", { class: "rec-flag rec-flag-on", text: "On your path" }));
  }

  const head = el("div", { class: "rec-head" });
  head.appendChild(el("div", {}, [
    el("div", { class: "item-title" }, [r.title, itemLink(r), videoLink(r)]),
    el("div", { class: "item-sub", text:
      `${r.provider} · ${r.type} · ${r.hours}h · level ${r.level} · ${r.format} · ${r.cost} · ${r.rating}/5` }),
  ]));
  head.appendChild(el("div", { class: "rec-score", text: r.score.toFixed(3), attrs: { title: "Weighted match score" } }));
  card.appendChild(head);
  card.appendChild(el("div", { class: "item-sub", text: r.description }));

  const reasons = el("ul", { class: "rec-reasons" });
  r.reasons.forEach((reason) => reasons.appendChild(el("li", { text: reason })));
  card.appendChild(reasons);

  if (!r.prerequisites_met) {
    card.appendChild(el("div", { class: "pill pill-missing", text:
      "Prerequisites first: " + r.missing_prerequisites.join(", ") }));
  }

  const bd = el("div", { class: "breakdown" });
  Object.entries(r.breakdown).forEach(([key, value]) => {
    const cell = el("div");
    const row = el("div", { class: "bd-row" });
    row.appendChild(el("span", { class: "bd-name", text: SCORE_LABELS[key] || key }));
    row.appendChild(el("span", { text: value.toFixed(2) }));
    cell.appendChild(row);
    const bar = el("div", { class: "bd-bar" });
    bar.appendChild(el("span", { attrs: { style: `width:${value * 100}%` } }));
    cell.appendChild(bar);
    bd.appendChild(cell);
  });
  card.appendChild(bd);

  const actions = el("div", { class: "row" });
  actions.appendChild(explainButton(r.item_id));
  if (!onPath) {
    actions.appendChild(el("button", {
      class: "btn btn-mini btn-accent", text: "+ Add to my path",
      attrs: { type: "button", title: "Schedule this in the earliest stage that allows it" },
      on: { click: () => addItemToPath(r.item_id, r.title).then(loadRecommendations) },
    }));
  }
  card.appendChild(actions);
  return card;
}

/* ---------------------------------------------------------------- profile */
function renderProfileTab() {
  if (!state.profile) return;
  const p = state.profile;
  $("#pfName").value = p.name || "";
  $("#pfLevel").value = p.experience_level;
  $("#pfHours").value = p.weekly_hours;
  $("#pfCost").value = p.cost_preference;

  const goalSelect = $("#pfGoal");
  goalSelect.innerHTML = "";
  goalSelect.appendChild(el("option", { text: "— choose a goal —", attrs: { value: "" } }));
  state.goals.forEach((g) => {
    const opt = el("option", { text: `${g.title} (${g.domain})`, attrs: { value: g.id } });
    if (g.id === p.goal_id) opt.selected = true;
    goalSelect.appendChild(opt);
  });

  const formats = $("#pfFormats");
  formats.innerHTML = "";
  ["video", "interactive", "reading", "project"].forEach((f) => {
    const input = el("input", { attrs: { type: "checkbox", value: f } });
    input.checked = (p.preferred_formats || []).includes(f);
    formats.appendChild(el("label", { class: "chip" }, [input, f]));
  });

  const picker = $("#pfSkills");
  picker.innerHTML = "";
  const byDomain = {};
  state.skills.forEach((s) => { (byDomain[s.domain] = byDomain[s.domain] || []).push(s); });
  Object.entries(byDomain).forEach(([domain, skills]) => {
    picker.appendChild(el("div", { class: "picker-group", text: domain }));
    skills.forEach((s) => {
      const input = el("input", { attrs: { type: "checkbox", value: s.id } });
      input.checked = (p.declared_skills || []).includes(s.id);
      picker.appendChild(el("label", { attrs: { title: s.description } }, [input, s.name]));
    });
  });

  renderHistoryPicker();
  loadProfileSummary();
}

/* Prior learning history: courses finished before this app ever saw them.
   Selections live in state, not in the DOM, so filtering the list cannot
   silently drop a tick the learner has already made. */
function renderHistoryPicker(filter = "") {
  const host = $("#pfHistory");
  if (!host) return;
  const chosen = state.history;
  host.innerHTML = "";
  const needle = filter.trim().toLowerCase();
  const courses = state.items
    .filter((i) => i.type === "course")
    .filter((i) => !needle
      || i.title.toLowerCase().includes(needle)
      || i.provider.toLowerCase().includes(needle));

  if (!courses.length) {
    host.appendChild(el("div", { class: "hint", text: "Nothing matches that filter." }));
    return;
  }
  const byDomain = {};
  courses.forEach((i) => { (byDomain[i.domain] = byDomain[i.domain] || []).push(i); });
  Object.entries(byDomain).forEach(([domain, list]) => {
    host.appendChild(el("div", { class: "picker-group", text: domain }));
    list.forEach((item) => {
      const input = el("input", {
        attrs: { type: "checkbox", value: item.id },
        on: { change: (e) => {
          if (e.target.checked) chosen.add(item.id); else chosen.delete(item.id);
        } },
      });
      input.checked = chosen.has(item.id);
      host.appendChild(el("label", { attrs: { title: item.description } }, [
        input, `${item.title} · ${item.provider}`,
      ]));
    });
  });
}

$("#pfHistorySearch").addEventListener("input", (event) => {
  renderHistoryPicker(event.target.value);
});

async function loadProfileSummary() {
  const box = $("#profileSummary");
  box.innerHTML = "";
  try {
    const s = await api(`/api/learners/${state.learnerId}/profile/summary`);
    box.appendChild(el("p", { text: s.summary }));
    if (s.direct_skill_names.length) {
      box.appendChild(el("div", { class: "hint", text: "Demonstrated: " + s.direct_skill_names.join(", ") }));
    }
    if (s.implied_skill_names.length) {
      box.appendChild(el("div", { class: "hint", text:
        "Implied by prerequisites (you are not asked to relearn these): " + s.implied_skill_names.join(", ") }));
    }
    box.appendChild(el("div", { class: "hint", text:
      `Difficulty target after feedback: level ${s.target_level}.` }));
  } catch (err) {
    box.appendChild(el("div", { class: "hint", text: "Set a goal to see profiling output." }));
  }
}

$("#profileForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    name: $("#pfName").value.trim() || "Learner",
    experience_level: $("#pfLevel").value,
    weekly_hours: Number($("#pfHours").value) || 8,
    cost_preference: $("#pfCost").value,
    preferred_formats: $$("#pfFormats input:checked").map((i) => i.value),
    declared_skills: $$("#pfSkills input:checked").map((i) => i.value),
    completed_item_ids: Array.from(state.history),
  };
  const goal = $("#pfGoal").value;
  if (goal) payload.goal_id = goal;

  try {
    state.profile = await api(`/api/learners/${state.learnerId}/profile`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    state.history = new Set(state.profile.completed_item_ids || []);
    if (state.profile.goal_id) {
      state.path = await api(`/api/learners/${state.learnerId}/path`);
      renderPath();
    }
    const learners = await api("/api/learners");
    renderLearnerSelect(learners, state.learnerId);
    loadProfileSummary();
    toast("Profile saved and path rebuilt.");
  } catch (err) { toast(err.message); }
});

boot();
