"use strict";

const ROUTES = new Set(["overview", "sources", "runs", "connections"]);
const ROUTE_TITLES = { overview: "Overview", sources: "Sources", runs: "Runs", connections: "Connections" };
const OBSERVATION_PAGE_SIZE = 50;

const state = {
  bootstrap: null,
  route: "overview",
  selectedJobId: null,
  selectedJob: null,
  observations: null,
  observationOffset: 0,
  loadingJob: false,
  identityHydrated: false,
  polling: null,
  pollInFlight: false,
  sourceFingerprint: null,
  recommendationFingerprint: null,
  sourceSelection: new Map(),
  candidateSelection: new Map(),
  sourceSelectionWorkspace: null,
  jobFingerprint: null,
  overviewFingerprint: null,
  aiSettingsWorkspace: null,
  aiSettingsSavedFingerprint: null,
  aiProvidersFingerprint: null,
  aiSettingsDirty: false,
  recommendationRunPending: new Set(),
  recommendationReceipts: new Map(),
  recommendationRunErrors: new Map(),
};

const byId = (id) => document.getElementById(id);

function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.type) element.type = options.type;
  if (options.name) element.name = options.name;
  if (options.value !== undefined) element.value = String(options.value);
  if (options.placeholder) element.placeholder = options.placeholder;
  if (options.disabled) element.disabled = true;
  if (options.title) element.title = options.title;
  if (options.attrs) {
    for (const [key, value] of Object.entries(options.attrs)) element.setAttribute(key, String(value));
  }
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function displayValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function titleCase(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function safeWebUrl(value) {
  try {
    const url = new URL(String(value));
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch (_error) {
    return null;
  }
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium", timeStyle: "short",
  }).format(date);
}

function jobs() {
  const value = state.bootstrap?.jobs;
  return Array.isArray(value) ? value : Array.isArray(value?.jobs) ? value.jobs : [];
}

function workspace() {
  return state.bootstrap?.workspace || state.bootstrap?.research?.workspace || null;
}

function product() {
  return workspace()?.product || {};
}

function sources() {
  const full = workspace()?.sources;
  if (Array.isArray(full)) return full;
  return Array.isArray(state.bootstrap?.research?.sources) ? state.bootstrap.research.sources : [];
}

function recommendationRequests() {
  const requests = state.bootstrap?.recommendations?.requests;
  return Array.isArray(requests) ? requests : [];
}

function aiSettings() {
  return state.bootstrap?.ai?.settings || { provider: "codex", model: "", timeout_seconds: 180 };
}

function aiProviders() {
  return Array.isArray(state.bootstrap?.ai?.providers) ? state.bootstrap.ai.providers : [];
}

function aiProvider(id) {
  return aiProviders().find((provider) => provider.id === id);
}

function aiModelLabel(providerId, modelId) {
  if (!modelId) return "Provider default";
  return aiProvider(providerId)?.models?.find((model) => model.id === modelId)?.label || modelId;
}

function selectedSourceIds() {
  return sources().filter((source) => state.sourceSelection.get(String(source.id)) !== false).map((source) => String(source.id));
}

function syncSourceSelection() {
  const workspaceKey = String(state.bootstrap?.workspace_root || "local");
  if (state.sourceSelectionWorkspace !== workspaceKey) {
    state.sourceSelectionWorkspace = workspaceKey;
    state.sourceSelection = new Map();
    try {
      const saved = JSON.parse(sessionStorage.getItem(`sourceledger:selected-sources:${workspaceKey}`) || "[]");
      if (Array.isArray(saved)) for (const [id, selected] of saved) {
        if (typeof id === "string" && typeof selected === "boolean") state.sourceSelection.set(id, selected);
      }
    } catch (_error) { /* A blocked or stale storage entry should not prevent research. */ }
  }
  const current = new Set();
  for (const source of sources()) {
    const id = String(source.id);
    current.add(id);
    if (!state.sourceSelection.has(id)) state.sourceSelection.set(id, true);
  }
  for (const id of state.sourceSelection.keys()) if (!current.has(id)) state.sourceSelection.delete(id);
}

function saveSourceSelection() {
  try { sessionStorage.setItem(`sourceledger:selected-sources:${state.sourceSelectionWorkspace}`, JSON.stringify([...state.sourceSelection])); }
  catch (_error) { /* Session storage is optional. */ }
}

function isConfigured() {
  return state.bootstrap?.status === "configured";
}

function badge(value, kindOverride) {
  const normalized = String(value || "unknown").toLowerCase();
  let kind = kindOverride || "";
  if (!kind) {
    if (["succeeded", "verified", "eligible", "completed", "idle", "running"].includes(normalized)) kind = "success";
    else if (["failed", "ineligible", "interrupted", "blocked", "policy_denied"].includes(normalized)) kind = "danger";
    else if (["queued", "starting", "stopping", "paused", "review", "needs_review", "partial", "candidate"].includes(normalized)) kind = "warning";
    else kind = "info";
  }
  return node("span", { className: `badge ${kind}`, text: titleCase(value) });
}

async function api(path, options = {}) {
  const request = { method: options.method || "GET", headers: { Accept: "application/json" } };
  if (options.body !== undefined) {
    request.headers["Content-Type"] = "application/json";
    const token = state.bootstrap?.csrf_token;
    if (token) request.headers["X-SourceLedger-Token"] = token;
    request.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, request);
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(payload?.error || `Request failed (${response.status})`);
  return payload;
}

async function loadBootstrap({ quiet = false } = {}) {
  if (quiet && state.pollInFlight) return;
  if (quiet) state.pollInFlight = true;
  try {
    let payload;
    try {
      payload = await api("/api/bootstrap");
    } catch (error) {
      if (!String(error.message).includes("404")) throw error;
      payload = await api("/api/state");
    }
    const previousStatus = state.bootstrap?.status;
    state.bootstrap = payload;
    if (previousStatus !== payload.status) state.identityHydrated = false;
    byId("global-message").hidden = true;
    byId("loading-view").hidden = true;
    render({ polling: quiet });
    if (quiet && state.selectedJobId) await refreshSelectedJob();
  } catch (error) {
    byId("loading-view").hidden = true;
    byId("global-message-text").textContent = quiet ? `Connection refresh failed: ${error.message}. Displayed data may be stale.` : error.message;
    byId("global-message").hidden = false;
  } finally {
    if (quiet) state.pollInFlight = false;
  }
}

function currentRoute() {
  const hash = location.hash.slice(1).split("?")[0];
  if (hash === "main-content") return state.route;
  return ROUTES.has(hash) ? hash : "overview";
}

function route({ moveFocus = false } = {}) {
  state.route = currentRoute();
  document.querySelectorAll("[data-route]").forEach((link) => {
    const active = link.dataset.route === state.route;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-view]").forEach((view) => { view.hidden = view.dataset.view !== state.route; });
  byId("page-title").textContent = ROUTE_TITLES[state.route];
  document.title = `${ROUTE_TITLES[state.route]} · SourceLedger`;
  if (state.bootstrap) render();
  if (moveFocus && location.hash !== "#main-content") {
    window.scrollTo({ top: 0, behavior: "auto" });
    byId("page-title").focus({ preventScroll: true });
  }
}

function render({ polling = false } = {}) {
  renderChrome();
  renderOverview();
  renderSources({ preserveForm: polling });
  renderRuns();
  renderConnections();
}

function renderChrome() {
  const root = state.bootstrap?.workspace_root || "Local workspace";
  const worker = state.bootstrap?.worker || {};
  const workerStatus = worker.status || "stopped";
  byId("sidebar-workspace").textContent = root;
  byId("sidebar-workspace").title = root;
  byId("sidebar-worker").textContent = `Worker ${titleCase(workerStatus).toLowerCase()}`;
  byId("worker-dot").className = `status-dot ${workerStatus}`;
  byId("version-label").textContent = state.bootstrap?.version ? `v${state.bootstrap.version}` : "";
  const button = byId("worker-toggle");
  const active = ["running", "idle", "starting", "stopping"].includes(workerStatus);
  button.textContent = active ? (workerStatus === "stopping" ? "Stopping…" : "Stop worker") : "Start worker";
  button.dataset.action = active ? "stop" : "start";
  button.disabled = ["starting", "stopping"].includes(workerStatus) || button.dataset.pending === "true";
}

function renderOverview() {
  const setup = byId("setup-panel");
  const dashboard = byId("dashboard-panel");
  setup.hidden = isConfigured();
  dashboard.hidden = !isConfigured();
  if (!isConfigured()) return;

  const ws = workspace() || {};
  const identifiers = product().identifiers || {};
  const fingerprint = JSON.stringify([ws, jobs()]);
  if (fingerprint === state.overviewFingerprint) return;
  state.overviewFingerprint = fingerprint;
  byId("workspace-heading").textContent = product().name || "Research workspace";
  byId("workspace-subtitle").textContent = [ws.industry, ws.market].filter(Boolean).join(" · ") || "Configured local research workspace";
  byId("source-count").textContent = String(sources().length);
  byId("job-count").textContent = String(jobs().length);
  byId("identifier-count").textContent = String(Object.keys(identifiers).length);

  const readiness = byId("readiness-list");
  clear(readiness);
  const items = [
    { done: true, title: "Research subject", note: "Industry, product, and market defined", href: "#overview" },
    { done: Object.keys(identifiers).length > 0, title: "Exact product identity", note: Object.keys(identifiers).length ? `${Object.keys(identifiers).length} identifier field(s)` : "Add a model, SKU, or catalog ID", href: "#sources" },
    { done: sources().length > 0, title: "Source candidates", note: sources().length ? `${sources().length} registered candidate(s)` : "Register a public or authorized internal URL", href: "#sources" },
    { done: jobs().length > 0, title: "Evidence run", note: jobs().length ? "Inspect execution and evidence results" : "Queue bounded research when ready", href: "#runs" },
  ];
  for (const item of items) {
    const link = node("a", { text: item.done ? "Review" : "Continue", attrs: { href: item.href } });
    readiness.append(node("div", { className: `journey-item ${item.done ? "done" : ""}` }, [
      node("span", { className: "journey-check", text: item.done ? "✓" : "·", attrs: { "aria-hidden": "true" } }),
      node("div", {}, [node("strong", { text: item.title }), node("small", { text: item.note })]), link,
    ]));
  }
  const recent = byId("overview-jobs");
  clear(recent);
  if (!jobs().length) {
    recent.append(node("div", { className: "empty-inline" }, [node("strong", { text: "No jobs yet" }), node("p", { text: "A queued run will appear here with its durable status." })]));
  } else {
    for (const job of jobs().slice(0, 4)) recent.append(node("div", { className: "mini-job" }, [
      node("div", {}, [node("strong", { text: titleCase(job.operation) }), node("small", { text: formatDate(job.created_at) })]), badge(job.status),
    ]));
  }
}

function emptySetup(target, context) {
  clear(target);
  target.append(node("div", { className: "empty-mark", text: "00" }), node("h3", { text: "Create the workspace first" }),
    node("p", { text: `${context} becomes available after you define an industry, product, and market.` }),
    node("a", { className: "button button-primary", text: "Go to first-run setup", attrs: { href: "#overview" } }));
}

function renderSources({ preserveForm = false } = {}) {
  const empty = byId("sources-setup-empty");
  const content = byId("sources-content");
  empty.hidden = isConfigured();
  content.hidden = !isConfigured();
  if (!isConfigured()) { emptySetup(empty, "Source registration"); return; }
  if (!state.identityHydrated) {
    hydrateKeyValues("identifier-rows", product().identifiers || {}, "model", "Exact value");
    hydrateKeyValues("spec-rows", product().required_specs || {}, "specification", "Required value");
    state.identityHydrated = true;
  }
  renderSourceList();
  renderRecommendations();
}

function hydrateKeyValues(id, values, keyPlaceholder, valuePlaceholder) {
  const target = byId(id);
  clear(target);
  const entries = Object.entries(values);
  if (!entries.length) addKeyValueRow(target, "", "", keyPlaceholder, valuePlaceholder);
  else for (const [key, value] of entries) addKeyValueRow(target, key, value, keyPlaceholder, valuePlaceholder);
}

function addKeyValueRow(target, key = "", value = "", keyPlaceholder = "field", valuePlaceholder = "value") {
  const keyInput = node("input", { value: key, placeholder: keyPlaceholder, attrs: { "aria-label": "Field name" } });
  const valueInput = node("input", { value, placeholder: valuePlaceholder, attrs: { "aria-label": "Field value" } });
  const remove = node("button", { className: "icon-button", text: "×", type: "button", title: "Remove row", attrs: { "aria-label": "Remove row" } });
  const row = node("div", { className: "key-value-row" }, [keyInput, valueInput, remove]);
  remove.addEventListener("click", () => {
    if (target.children.length === 1) { keyInput.value = ""; valueInput.value = ""; keyInput.focus(); }
    else row.remove();
  });
  target.append(row);
}

function keyValueObject(id) {
  const value = Object.create(null);
  for (const row of byId(id).querySelectorAll(".key-value-row")) {
    const inputs = row.querySelectorAll("input");
    const key = inputs[0].value.trim();
    const fieldValue = inputs[1].value.trim();
    if (!key && !fieldValue) continue;
    if (!key || !fieldValue) throw new Error("Complete both fields in each row, or remove the row.");
    if (Object.hasOwn(value, key)) throw new Error(`Duplicate field: ${key}`);
    value[key] = fieldValue;
  }
  return value;
}

function renderSourceList(force = false) {
  syncSourceSelection();
  const target = byId("source-list");
  const filter = byId("source-filter").value.trim().toLowerCase();
  const filtered = sources().filter((source) => `${source.name || ""} ${source.location || ""}`.toLowerCase().includes(filter));
  const fingerprint = JSON.stringify([filter, filtered, [...state.sourceSelection]]);
  const selected = selectedSourceIds().length;
  byId("source-selection-summary").textContent = `${selected} of ${sources().length} registered sources selected for the next guided run${filter ? ` · ${filtered.length} shown by filter; hidden selections remain selected` : ""}.`;
  if (!force && fingerprint === state.sourceFingerprint) return;
  state.sourceFingerprint = fingerprint;
  const focused = document.activeElement?.dataset?.runSourceId;
  clear(target);
  if (!filtered.length) {
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: sources().length ? "No matching sources" : "No source candidates" }), node("p", { text: sources().length ? "Clear the filter to see all candidates." : "Register a URL to build the source register." })]));
    return;
  }
  for (const source of filtered) {
    const title = node("h4", { text: source.name || source.id || "Source" });
    const safeUrl = safeWebUrl(source.location);
    const url = safeUrl ? node("a", { className: "source-url", text: source.location, attrs: { href: safeUrl, target: "_blank", rel: "noopener noreferrer" } }) : node("span", { className: "source-url", text: source.location || "—" });
    const discover = node("button", { className: "button button-secondary", type: "button", text: "Discover links", attrs: { "data-source-action": "discover", "data-source-id": source.id } });
    const propose = node("button", { className: "button button-secondary", type: "button", text: "Propose recipe", attrs: { "data-source-action": "propose", "data-source-id": source.id } });
    const include = node("input", { type: "checkbox", value: String(source.id), attrs: { "data-run-source-id": String(source.id), "aria-label": `Include ${source.name || source.location || source.id} in next guided run` } });
    include.checked = state.sourceSelection.get(String(source.id)) !== false;
    target.append(node("article", { className: "source-item" }, [
      node("div", { className: "source-top" }, [node("div", {}, [title, url]), badge(source.status || "candidate")]),
      node("div", { className: "source-meta" }, [badge(source.scope || "public", "info"), node("span", { className: "badge", text: source.kind || "web" })]),
      node("label", { className: "checkbox-label source-include" }, [include, "Include in next guided run"]),
      node("div", { className: "source-actions" }, [discover, propose]),
    ]));
  }
  if (focused) [...target.querySelectorAll("[data-run-source-id]")].find((item) => item.dataset.runSourceId === focused)?.focus({ preventScroll: true });
}

function candidateKey(requestId, candidateId) { return `${requestId}:${candidateId}`; }

function recommendationHandoff(request) {
  const topic = request.topic || {};
  const subject = [topic.industry, topic.product?.name, topic.market].filter(Boolean).join(" · ");
  return `SourceLedger recommendation request ID: ${request.id}\nKind: ${request.kind}\nQuery: ${request.query}${subject ? `\nResearch topic: ${subject}` : ""}\nUse the connected SourceLedger MCP tools list_source_recommendation_requests and submit_source_recommendations. Search for real sites and include a real evidence URL for each suggestion. Do not invent a URL, price, currency, identifier, or commercial condition. Submit candidates for this request ID; the operator will choose which sources to add. If search tools are unavailable or no real sites are found, submit an empty result with a clear note explaining why.`;
}

function renderRecommendations(force = false) {
  const requests = recommendationRequests();
  byId("recommendation-register").hidden = !isConfigured() || !requests.length;
  byId("recommendation-empty").hidden = !isConfigured() || Boolean(requests.length);
  const recommendationJobs = [...(state.bootstrap?.recommendation_jobs || []), ...jobs()]
    .filter((job) => job.operation === "recommend");
  const fingerprint = JSON.stringify([requests, recommendationJobs, aiSettings(), aiProviders(), [...state.recommendationRunPending]]);
  if (!force && fingerprint === state.recommendationFingerprint) return;
  state.recommendationFingerprint = fingerprint;
  const target = byId("recommendation-list");
  const active = document.activeElement?.closest?.("[data-candidate-id]");
  const focusedCandidate = active ? [active.dataset.candidateRequest, active.dataset.candidateId] : null;
  const opened = new Set([...target.querySelectorAll(".recommendation-request")].filter((card) => card.querySelector(".handoff details")?.open).map((card) => card.dataset.requestId));
  clear(target);
  if (!requests.length) {
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: "No recommendation requests" }), node("p", { text: "Enter a company name or keyword to prepare a connected assistant search." })]));
    return;
  }
  for (const request of requests) {
    const requestId = String(request.id);
    const card = node("article", { className: "recommendation-request", attrs: { "data-request-id": requestId } });
    card.append(node("div", { className: "recommendation-head" }, [
      node("div", {}, [node("span", { className: "eyebrow", text: request.kind === "company" ? "Company lookup" : "Keyword search" }), node("h4", { text: request.query || "Untitled request", attrs: { tabindex: "-1" } }), node("small", { text: `Request ${requestId} · ${formatDate(request.created_at)}` })]), badge(request.status || "pending"),
    ]));
    const job = recommendationJobs.find((item) => String(item.arguments?.request_id) === requestId)
      || state.recommendationReceipts.get(requestId);
    const runActive = state.recommendationRunPending.has(requestId) || ["queued", "running"].includes(job?.status);
    const settings = aiSettings();
    const provider = aiProvider(settings.provider);
    const available = provider?.available === true;
    const run = node("div", { className: "web-recommendation" }, [
      node("div", {}, [
        node("strong", { text: "Run in web UI" }),
        node("p", { className: "hint", text: `${provider?.label || titleCase(settings.provider)} · ${aiModelLabel(settings.provider, settings.model)} · ${settings.timeout_seconds} seconds` }),
        node("a", { text: "Change workspace default", attrs: { href: "#connections" } }),
      ]),
      node("button", { className: "button button-primary", type: "button", text: runActive ? "Recommendation running…" : job?.status === "failed" || job?.status === "interrupted" ? "Retry in web UI" : "Run in web UI", disabled: runActive || !available, attrs: { "data-run-recommendation": requestId } }),
    ]);
    if (!available) run.append(node("p", { className: "hint web-run-message", text: "Install and sign in to the selected CLI to run here. You can still use an AI app below." }));
    if (state.recommendationRunErrors.has(requestId)) run.append(node("p", { className: "form-error", attrs: { role: "alert" } }, state.recommendationRunErrors.get(requestId)));
    if (job) {
      const requested = job.arguments || settings;
      run.append(node("p", { className: "request-status", attrs: { role: "status" } }, [
        badge(job.status), ` ${titleCase(requested.provider)} · ${aiModelLabel(requested.provider, requested.model)}${job.status === "succeeded" ? " · Review suggestions below." : ""}`,
      ]));
      if (!runActive && !state.recommendationRunErrors.has(requestId) && ["failed", "interrupted"].includes(job.status)) run.append(node("p", { className: "form-error", attrs: { role: "alert" } }, job.error?.message || (typeof job.error === "string" ? job.error : "The recommendation run stopped. Check the CLI setup, then retry.")));
    } else if (runActive) run.append(node("p", { className: "request-status", attrs: { role: "status" } }, "Queueing recommendation…"));
    card.append(run);
    const handoff = node("div", { className: "handoff" }, [
      node("strong", { text: "Use an AI app" }),
      node("p", { text: "Copy this request into your connected Claude or Codex app. That app controls its own model and submits source references through MCP." }),
      node("button", { className: "button button-secondary", type: "button", text: "Copy request", attrs: { "data-copy-request": requestId } }),
      node("details", {}, [node("summary", { text: "View request text" }), node("pre", { text: recommendationHandoff(request) })]),
    ]);
    if (opened.has(requestId)) handoff.querySelector("details").open = true;
    card.append(handoff);
    if (request.note) card.append(node("p", { className: "hint", text: request.note }));
    const candidates = Array.isArray(request.candidates) ? request.candidates : [];
    if (request.status === "pending" && !candidates.length) card.append(node("p", { className: "request-status", text: "No suggestions yet. Run in web UI or copy the request to an AI app." }));
    else if (!candidates.length) card.append(node("p", { className: "request-status", text: "No suggestions were returned. You can create another request or ask the assistant to search again." }));
    else {
      const list = node("div", { className: "candidate-list" });
      for (const candidate of candidates) {
        const id = String(candidate.id);
        const key = candidateKey(requestId, id);
        const checkbox = node("input", { type: "checkbox", value: id, attrs: { "data-candidate-id": id, "data-candidate-request": requestId, "aria-label": `Select ${candidate.name || candidate.url || id} for research` } });
        checkbox.checked = state.candidateSelection.get(key) === true;
        checkbox.disabled = Boolean(candidate.source_id);
        const link = (label, value) => {
          const safe = safeWebUrl(value);
          return safe ? node("a", { text: `${label}: ${value}`, attrs: { href: safe, target: "_blank", rel: "noopener noreferrer" } }) : node("span", { text: `${label}: ${value || "—"}` });
        };
        list.append(node("article", { className: "candidate-item" }, [
          node("label", { className: "checkbox-label candidate-check" }, [checkbox, candidate.source_id ? "Added to research targets" : "Select this suggestion"]),
          node("strong", { text: candidate.name || "Unnamed suggestion" }), link("Site", candidate.url),
          node("p", { text: candidate.reason || "No reason supplied." }), link("Source reference", candidate.evidence_url),
          node("small", { text: "Unverified recommendation · no price evidence" }),
        ]));
      }
      card.append(list);
      const selectedCount = candidates.filter((candidate) => !candidate.source_id && state.candidateSelection.get(candidateKey(requestId, String(candidate.id))) === true).length;
      card.append(node("div", { className: "recommendation-actions" }, [
        node("span", { text: `${selectedCount} selected` }),
        node("button", { className: "button button-primary", type: "button", text: "Add selected to research list", disabled: selectedCount === 0, attrs: { "data-add-request": requestId } }),
      ]));
    }
    target.append(card);
  }
  if (focusedCandidate) {
    const box = [...target.querySelectorAll("[data-candidate-id]")].find((item) => item.dataset.candidateRequest === focusedCandidate[0] && item.dataset.candidateId === focusedCandidate[1]);
    box?.focus({ preventScroll: true });
  }
}

function runBlockers() {
  const blockers = [];
  if (!Object.keys(product().identifiers || {}).length) blockers.push("Exact identifier required");
  if (!sources().length) blockers.push("Source candidate required");
  else if (!selectedSourceIds().length) blockers.push("Select at least one source");
  if (selectedSourceIds().length > 50) blockers.push("Select at most 50 sources");
  return blockers;
}

function renderRuns() {
  const empty = byId("runs-setup-empty");
  const content = byId("runs-content");
  empty.hidden = isConfigured();
  content.hidden = !isConfigured();
  if (!isConfigured()) { emptySetup(empty, "Durable runs"); return; }
  const blockers = runBlockers();
  byId("run-source-summary").textContent = `${selectedSourceIds().length} of ${sources().length} registered sources selected. Up to 50 sources, 120 seconds, and no external model calls. Change the selection on the Sources page.`;
  const blockerTarget = byId("run-blockers");
  clear(blockerTarget);
  if (!blockers.length) blockerTarget.append(badge("Ready", "success"));
  else for (const value of blockers) blockerTarget.append(badge(value, "warning"));
  byId("agent-form").querySelector("button").disabled = blockers.length > 0;
  renderJobList();
}

function renderJobList() {
  const target = byId("job-list");
  const fingerprint = JSON.stringify([state.selectedJobId, jobs()]);
  if (fingerprint === state.jobFingerprint) return;
  state.jobFingerprint = fingerprint;
  clear(target);
  if (!jobs().length) {
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: "No job history" }), node("p", { text: "Queue a guided or advanced job to begin." })]));
    if (!state.selectedJobId) renderJobDetail();
    return;
  }
  for (const job of jobs()) {
    const button = node("button", { className: `job-button ${job.id === state.selectedJobId ? "active" : ""}`, type: "button", attrs: { "data-job-id": job.id, "aria-label": `${titleCase(job.operation)} job, ${titleCase(job.status)}` } }, [
      node("span", {}, [node("strong", { text: titleCase(job.operation) }), node("small", { text: formatDate(job.created_at) })]), badge(job.status),
    ]);
    target.append(button);
  }
}

async function selectJob(id, { keepOffset = false } = {}) {
  state.selectedJobId = id;
  if (!keepOffset) state.observationOffset = 0;
  state.loadingJob = true;
  renderJobList();
  renderJobDetail();
  try {
    const selected = await api(`/api/jobs/${encodeURIComponent(id)}`);
    if (state.selectedJobId !== id) return;
    state.selectedJob = selected;
    state.observations = null;
    if (state.selectedJob?.result?.run_id && state.selectedJob?.result?.output_dir) await loadObservations();
  } catch (error) {
    if (state.selectedJobId !== id) return;
    state.selectedJob = { id, loadError: error.message };
  } finally {
    if (state.selectedJobId !== id) return;
    state.loadingJob = false;
    renderJobDetail();
  }
}

async function refreshSelectedJob() {
  const requestedId = state.selectedJobId;
  try {
    const before = JSON.stringify([state.selectedJob, state.observations]);
    const job = await api(`/api/jobs/${encodeURIComponent(requestedId)}`);
    if (state.selectedJobId !== requestedId) return;
    state.selectedJob = job;
    if (job?.result?.run_id && job?.result?.output_dir) await loadObservations();
    if (before !== JSON.stringify([state.selectedJob, state.observations])) renderJobDetail();
  } catch (_error) {
    // Keep the last durable receipt visible during a transient poll failure.
  }
}

async function loadObservations() {
  const requestedId = state.selectedJobId;
  const requestedOffset = state.observationOffset;
  try {
    const payload = await api(`/api/jobs/${encodeURIComponent(requestedId)}/observations?offset=${requestedOffset}&limit=${OBSERVATION_PAGE_SIZE}`);
    if (state.selectedJobId === requestedId && state.observationOffset === requestedOffset) state.observations = payload;
  } catch (error) {
    if (state.selectedJobId === requestedId && state.observationOffset === requestedOffset) state.observations = { rows: [], total: 0, offset: requestedOffset, limit: OBSERVATION_PAGE_SIZE, error: error.message };
  }
}

function fact(label, value) {
  return node("div", {}, [node("dt", { text: label }), node("dd", { text: displayValue(value) })]);
}

function renderJobDetail() {
  const target = byId("job-detail");
  clear(target);
  if (state.loadingJob) {
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: "Loading run…" }), node("p", { text: "Reading the durable job receipt." })]));
    return;
  }
  const job = state.selectedJob;
  if (!job || job.id !== state.selectedJobId) {
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: "Select a run" }), node("p", { text: "Execution details, evidence state, and observations will appear here." })]));
    return;
  }
  if (job.loadError) {
    target.append(node("div", { className: "detail-error", text: job.loadError }));
    return;
  }
  const result = job.result || {};
  const actions = node("div", { className: "detail-actions" });
  const evidence = result.evidence_status || result.result?.status;
  const resumable = job.operation !== "recommend" && (job.status === "interrupted" || (job.operation === "agent" && evidence === "paused"));
  if (resumable) actions.append(node("button", { className: "button button-secondary", type: "button", text: "Resume", attrs: { "data-resume-job": job.id } }));
  if (result.report_path) actions.append(node("a", { className: "button button-primary", text: "Download XLSX", attrs: { href: `/api/jobs/${encodeURIComponent(job.id)}/report` } }));
  const executionStatus = result.execution_status || job.status;
  target.append(node("div", { className: "detail-head" }, [
    node("div", {}, [node("span", { className: "eyebrow", text: "Job detail" }), node("h3", { text: titleCase(job.operation) }), node("span", { className: "detail-id", text: job.id })]), actions,
  ]));
  target.append(node("dl", { className: "detail-facts" }, [
    fact("Job state", job.status), fact("Execution", executionStatus), fact("Evidence", evidence || "Not available"),
    fact("Attempt", job.attempt ?? 0), fact("Created", formatDate(job.created_at)), fact("Updated", formatDate(job.updated_at)),
  ]));
  if (job.operation === "recommend") target.append(node("dl", { className: "detail-facts" }, [
    fact("Requested provider", job.arguments?.provider), fact("Requested model", aiModelLabel(job.arguments?.provider, job.arguments?.model)),
    fact("Actual model", result.result?.actual_model || "Not reported"),
  ]));
  if (job.error) target.append(node("div", { className: "detail-error", text: typeof job.error === "string" ? job.error : displayValue(job.error) }));
  if (result.execution_status === "succeeded") target.append(node("p", { className: "hint", text: "Execution succeeded only means the job completed. Accept evidence only after reviewing its evidence status and observations." }));
  renderObservations(target);
}

function renderObservations(target) {
  const payload = state.observations;
  if (!payload) return;
  const section = node("section", { className: "observation-section" });
  section.append(node("h4", { text: "Observations" }), node("p", { className: "hint", text: "Observed values and their origin are shown separately from evidence review state." }));
  if (payload.error) {
    section.append(node("div", { className: "detail-error", text: payload.error })); target.append(section); return;
  }
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  if (!rows.length) {
    section.append(node("div", { className: "empty-inline" }, [node("strong", { text: "No observations on this page" }), node("p", { text: "This run may not have produced a collection ledger." })]));
    target.append(section); return;
  }
  const table = node("table");
  const header = node("tr");
  for (const label of ["Source", "Observed amount", "Estimated amount", "Currency", "Value origin", "Evidence status", "Collected"]) header.append(node("th", { text: label, attrs: { scope: "col" } }));
  table.append(node("thead", {}, header));
  const body = node("tbody");
  for (const row of rows) {
    const sourceCell = node("td");
    const safeUrl = safeWebUrl(row.source_url);
    if (safeUrl) sourceCell.append(node("a", { text: row.source_name || row.source_id || safeUrl, attrs: { href: safeUrl, target: "_blank", rel: "noopener noreferrer" } }));
    else sourceCell.textContent = displayValue(row.source_name || row.source_id);
    const observed = row.value_origin === "observed" ? row.amount : null;
    const estimated = row.value_origin === "calculator_estimate" ? (row.derived_amount ?? row.derived_values?.estimated_price ?? null) : null;
    const evidenceStatus = row.verification_level || row.status || "unknown";
    body.append(node("tr", {}, [sourceCell, node("td", { text: displayValue(observed) }), node("td", { text: displayValue(estimated) }), node("td", { text: displayValue(row.currency) }), node("td", { text: displayValue(row.value_origin) }), node("td", {}, badge(evidenceStatus)), node("td", { text: formatDate(row.collected_at) })]));
  }
  table.append(body);
  section.append(node("div", { className: "table-scroll" }, table));
  const total = Number(payload.total || 0);
  const offset = Number(payload.offset || 0);
  const previous = node("button", { className: "button button-secondary", type: "button", text: "Previous", disabled: offset <= 0, attrs: { "data-page": "previous" } });
  const next = node("button", { className: "button button-secondary", type: "button", text: "Next", disabled: offset + rows.length >= total, attrs: { "data-page": "next" } });
  section.append(node("div", { className: "pagination" }, [previous, node("span", { text: `${offset + 1}–${offset + rows.length} of ${total}` }), next]));
  target.append(section);
}

function renderConnections() {
  const root = state.bootstrap?.workspace_root;
  byId("connection-workspace").textContent = root ? `Bound to ${root}` : "The connection remains bound to this local workspace.";
  const install = byId("mcp-install-note");
  install.classList.toggle("notice-error", state.bootstrap?.mcp_available === false);
  install.title = state.bootstrap?.mcp_available === false ? "MCP support is not installed in this environment." : "MCP support is available.";
  renderClientTarget();
  renderAiSettings();
}

function renderAiSettings() {
  const workspaceKey = String(state.bootstrap?.workspace_root || "local");
  const providerInput = byId("ai-provider");
  const savedFingerprint = JSON.stringify(aiSettings());
  const providersFingerprint = JSON.stringify(aiProviders());
  if (state.aiSettingsWorkspace !== workspaceKey || (!state.aiSettingsDirty && (state.aiSettingsSavedFingerprint !== savedFingerprint || state.aiProvidersFingerprint !== providersFingerprint))) {
    state.aiSettingsWorkspace = workspaceKey;
    state.aiSettingsSavedFingerprint = savedFingerprint;
    state.aiProvidersFingerprint = providersFingerprint;
    state.aiSettingsDirty = false;
    const providers = aiProviders();
    clear(providerInput);
    for (const provider of providers) providerInput.append(node("option", { value: provider.id, text: provider.label || titleCase(provider.id) }));
    if (!providers.length) providerInput.append(node("option", { value: "codex", text: "Codex CLI" }), node("option", { value: "claude", text: "Claude Code CLI" }));
    const settings = aiSettings();
    providerInput.value = settings.provider;
    if (!providerInput.value) providerInput.selectedIndex = 0;
    byId("ai-timeout").value = String(settings.timeout_seconds ?? 180);
    renderAiModelChoices(settings.model || "");
  }
  const provider = aiProvider(providerInput.value);
  byId("ai-provider-status").textContent = provider?.available
    ? `${provider.label || titleCase(provider.id)} installed. Sign-in is checked when you run a request, together with model access.`
    : `${provider?.label || titleCase(providerInput.value)} unavailable.${provider?.message ? ` ${provider.message}` : " Install and sign in to its native CLI before running here."}`;
}

function renderAiModelChoices(savedModel = "") {
  const providerId = byId("ai-provider").value;
  const select = byId("ai-model-choice");
  clear(select);
  select.append(node("option", { value: "", text: "Provider default (CLI decides)" }));
  for (const model of aiProvider(providerId)?.models || []) select.append(node("option", { value: model.id, text: model.label || model.id }));
  select.append(node("option", { value: "__custom__", text: "Custom model ID…" }));
  const known = [...select.options].some((option) => option.value === savedModel);
  select.value = savedModel && !known ? "__custom__" : savedModel;
  byId("ai-custom-model").value = savedModel && !known ? savedModel : "";
  byId("ai-custom-model-label").hidden = select.value !== "__custom__";
}

function renderClientTarget() {
  const targets = {
    codex: "CODEX_HOME/config.toml",
    "claude-code": "Project .mcp.json",
    "claude-desktop": "APPDATA/Claude/claude_desktop_config.json (Windows) or ~/Library/Application Support/Claude/claude_desktop_config.json (macOS)",
  };
  byId("client-config-target").textContent = targets[byId("connection-client").value];
}

function showFormError(form, message) {
  const target = form.querySelector("[data-form-error]");
  if (!target) return;
  target.textContent = message || "";
  target.hidden = !message;
}

function setPending(form, pending) {
  form.querySelectorAll("button, input, select").forEach((control) => { control.disabled = pending; });
  form.dataset.pending = pending ? "true" : "false";
}

function toast(message) {
  const item = node("div", { className: "toast", text: message });
  byId("toast-region").append(item);
  while (byId("toast-region").childElementCount > 2) byId("toast-region").firstElementChild.remove();
  window.setTimeout(() => item.remove(), 4200);
}

function focusCreatedRequest(result) {
  const id = String(result?.request?.id || "");
  const card = [...byId("recommendation-list").querySelectorAll(".recommendation-request")].find((item) => item.dataset.requestId === id);
  if (!card) return;
  card.scrollIntoView({ block: "start", behavior: "smooth" });
  card.querySelector("h4")?.focus({ preventScroll: true });
}

async function submit(form, task, successMessage, onSuccess) {
  if (form.dataset.pending === "true") return;
  showFormError(form, "");
  setPending(form, true);
  try {
    const result = await task();
    if (result?.id && result?.operation) state.selectedJobId = result.id;
    toast(successMessage);
    await loadBootstrap({ quiet: true });
    if (onSuccess) onSuccess(result);
    if (result?.id && result?.operation) await selectJob(result.id);
  } catch (error) {
    showFormError(form, error.message);
  } finally {
    setPending(form, false);
    if (form === byId("agent-form")) renderRuns();
  }
}

function advancedFields() {
  const operation = byId("advanced-operation").value;
  const target = byId("advanced-fields");
  clear(target);
  if (operation === "collect_sites") {
    target.append(field("URLs", "urls", "https://example.com/a, https://example.com/b", true), field("Maximum pages", "max_pages", "5", false, "number"));
  } else if (operation === "verify") {
    target.append(field("Config path", "config_path", "configs/source.json", true), field("Samples path", "samples_path", "Optional"));
  } else if (operation === "run") {
    target.append(field("Config path", "config_path", "configs/source.json", true), field("Maximum tasks", "max_tasks", "Optional", false, "number"));
  } else {
    target.append(field("Config path", "config_path", "configs/source.json", true), field("Run ID", "run_id", "Existing run ID", true));
  }
}

function field(labelText, name, placeholder, required = false, type = "text") {
  const input = node("input", { type, name, placeholder, attrs: required ? { required: "" } : {} });
  return node("label", {}, [labelText, input]);
}

function advancedArguments(form) {
  const data = new FormData(form);
  const operation = String(data.get("operation"));
  const args = {};
  if (operation === "collect_sites") {
    const urls = String(data.get("urls") || "").split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
    if (!urls.length) throw new Error("Add at least one explicit URL to collect.");
    args.urls = urls;
    args.max_pages = Number(data.get("max_pages") || 5);
    args.max_seconds = 120;
    args.incremental = true;
  } else {
    const configPath = String(data.get("config_path") || "").trim();
    if (!configPath) throw new Error("Config path is required.");
    args.config_path = configPath;
    if (operation === "verify") {
      const samples = String(data.get("samples_path") || "").trim(); if (samples) args.samples_path = samples;
    } else if (operation === "run") {
      const maximum = String(data.get("max_tasks") || "").trim(); if (maximum) args.max_tasks = Number(maximum);
    } else {
      const runId = String(data.get("run_id") || "").trim(); if (!runId) throw new Error("Run ID is required."); args.run_id = runId;
    }
  }
  return { operation, arguments: args };
}

function installEvents() {
  window.addEventListener("hashchange", () => route({ moveFocus: true }));
  document.querySelector("details.advanced > summary").addEventListener("click", (event) => {
    const details = event.currentTarget.parentElement;
    if (!details.open) {
      event.preventDefault();
      details.open = true;
    }
  });
  byId("retry-button").addEventListener("click", () => loadBootstrap());
  byId("worker-toggle").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (button.dataset.pending === "true") return;
    button.dataset.pending = "true"; button.disabled = true;
    try { await api("/api/worker", { method: "POST", body: { action: button.dataset.action } }); toast(`Worker ${button.dataset.action} requested.`); await loadBootstrap({ quiet: true }); }
    catch (error) { toast(error.message); }
    finally { button.dataset.pending = "false"; renderChrome(); }
  });
  byId("setup-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    submit(form, () => api("/api/workspace", { method: "POST", body: { industry: String(data.get("industry")).trim(), product: String(data.get("product")).trim(), market: String(data.get("market")).trim(), locale: "en" } }), "Workspace created.");
  });
  byId("source-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const name = String(data.get("name") || "").trim(); const url = String(data.get("url") || "").trim();
    if (!name && !url) { showFormError(form, "Enter a company name or a source URL."); return; }
    if (!url && data.get("scope") === "internal") { showFormError(form, "Enter an authorized internal URL to register an internal source."); return; }
    if (url && !safeWebUrl(url)) { showFormError(form, "Enter a full HTTP or HTTPS source URL."); return; }
    submit(form, async () => {
      let result;
      if (url) result = await api("/api/sources", { method: "POST", body: { url, scope: String(data.get("scope")), ...(name ? { name } : {}) } });
      else result = await api("/api/recommendations", { method: "POST", body: { query: name, kind: "company" } });
      form.reset();
      form.querySelector('button[type="submit"]').textContent = "Register source";
      return result;
    }, url ? "Source registered." : "Company recommendation request created.", url ? null : focusCreatedRequest);
  });
  byId("source-form").addEventListener("input", () => {
    const form = byId("source-form");
    const name = form.elements.namedItem("name").value.trim();
    const url = form.elements.namedItem("url").value.trim();
    form.querySelector('button[type="submit"]').textContent = name && !url ? "Create company request" : "Register source";
  });
  byId("recommendation-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget; const query = String(new FormData(form).get("query") || "").trim();
    if (!query) { showFormError(form, "Enter a source keyword."); return; }
    submit(form, async () => { const result = await api("/api/recommendations", { method: "POST", body: { query, kind: "keyword" } }); form.reset(); return result; }, "Recommendation request created.", focusCreatedRequest);
  });
  byId("recommendation-list").addEventListener("change", (event) => {
    const box = event.target.closest("[data-candidate-id]");
    if (!box) return;
    state.candidateSelection.set(candidateKey(box.dataset.candidateRequest, box.dataset.candidateId), box.checked);
    const card = box.closest(".recommendation-request");
    const count = [...card.querySelectorAll("[data-candidate-id]")].filter((item) => item.checked && !item.disabled).length;
    card.querySelector(".recommendation-actions span").textContent = `${count} selected`;
    card.querySelector("[data-add-request]").disabled = count === 0;
  });
  byId("recommendation-list").addEventListener("click", async (event) => {
    const run = event.target.closest("[data-run-recommendation]");
    if (run) {
      const requestId = run.dataset.runRecommendation;
      if (state.recommendationRunPending.has(requestId)) return;
      const settings = aiSettings();
      if (aiProvider(settings.provider)?.available !== true) return;
      state.recommendationRunErrors.delete(requestId);
      state.recommendationRunPending.add(requestId);
      renderRecommendations(true);
      try {
        const result = await api("/api/recommendations/run", { method: "POST", body: {
          request_id: requestId, provider: settings.provider, model: settings.model,
          timeout_seconds: settings.timeout_seconds,
        } });
        if (result?.job) state.recommendationReceipts.set(requestId, result.job);
        toast("Recommendation queued. Review its suggestions when the run succeeds.");
        await loadBootstrap({ quiet: true });
      } catch (error) {
        state.recommendationRunErrors.set(requestId, error.message);
      } finally {
        state.recommendationRunPending.delete(requestId);
        renderRecommendations(true);
      }
      return;
    }
    const copy = event.target.closest("[data-copy-request]");
    if (copy) {
      const request = recommendationRequests().find((item) => String(item.id) === copy.dataset.copyRequest);
      if (!request) return;
      try { await navigator.clipboard.writeText(recommendationHandoff(request)); toast("Request copied."); }
      catch (_error) { toast("Copy was unavailable. Open View request text to copy it manually."); }
      return;
    }
    const button = event.target.closest("[data-add-request]");
    if (!button) return;
    const request = recommendationRequests().find((item) => String(item.id) === button.dataset.addRequest);
    if (!request) return;
    const ids = (request.candidates || []).filter((candidate) => !candidate.source_id && state.candidateSelection.get(candidateKey(String(request.id), String(candidate.id))) === true).map((candidate) => String(candidate.id));
    if (!ids.length) return;
    button.disabled = true;
    try {
      const result = await api("/api/recommendations/select", { method: "POST", body: { request_id: String(request.id), candidate_ids: ids } });
      for (const id of ids) state.candidateSelection.delete(candidateKey(String(request.id), id));
      toast(`${result.added_count ?? ids.length} source(s) added to research targets.`);
      await loadBootstrap({ quiet: true });
    } catch (error) { toast(error.message); button.disabled = false; }
  });
  byId("identity-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget;
    submit(form, () => { const identifiers = keyValueObject("identifier-rows"); if (!Object.keys(identifiers).length) throw new Error("Add at least one exact product identifier."); return api("/api/product", { method: "POST", body: { identifiers, required_specs: keyValueObject("spec-rows") } }); }, "Product identity saved.");
  });
  document.querySelectorAll("[data-add-row]").forEach((button) => button.addEventListener("click", () => {
    const target = byId(button.dataset.addRow); addKeyValueRow(target, "", "", target.id === "identifier-rows" ? "model" : "specification", target.id === "identifier-rows" ? "Exact value" : "Required value"); target.lastElementChild.querySelector("input").focus();
  }));
  byId("source-filter").addEventListener("input", () => renderSourceList(true));
  byId("source-list").addEventListener("change", (event) => {
    const box = event.target.closest("[data-run-source-id]");
    if (!box) return;
    state.sourceSelection.set(box.dataset.runSourceId, box.checked);
    saveSourceSelection();
    const filter = byId("source-filter").value.trim().toLowerCase();
    const filtered = sources().filter((source) => `${source.name || ""} ${source.location || ""}`.toLowerCase().includes(filter));
    state.sourceFingerprint = JSON.stringify([filter, filtered, [...state.sourceSelection]]);
    byId("source-selection-summary").textContent = `${selectedSourceIds().length} of ${sources().length} registered sources selected for the next guided run${byId("source-filter").value.trim() ? " · Hidden selections remain selected" : ""}.`;
    renderRuns();
  });
  byId("source-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-source-action]"); if (!button) return;
    const operation = button.dataset.sourceAction; button.disabled = true;
    api("/api/jobs", { method: "POST", body: { operation, arguments: operation === "discover" ? { source_id: button.dataset.sourceId, limit: 100 } : { source_id: button.dataset.sourceId, timeout_seconds: 30, max_model_calls: 0 } } })
      .then(() => { toast(`${titleCase(operation)} job queued.`); return loadBootstrap({ quiet: true }); }).catch((error) => toast(error.message)).finally(() => { button.disabled = false; });
  });
  byId("agent-form").addEventListener("submit", (event) => { event.preventDefault(); const form = event.currentTarget; const sourceIds = selectedSourceIds(); if (!sourceIds.length || sourceIds.length > 50) { showFormError(form, "Select between 1 and 50 sources on the Sources page."); return; } submit(form, () => api("/api/jobs", { method: "POST", body: { operation: "agent", arguments: { source_ids: sourceIds, max_sources: sourceIds.length, max_seconds: 120, max_model_calls: 0 } } }), "Research run queued."); });
  byId("advanced-operation").addEventListener("change", advancedFields);
  byId("advanced-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const body = advancedArguments(form);
      submit(form, () => api("/api/jobs", { method: "POST", body }), "Advanced job queued.");
    } catch (error) {
      showFormError(form, error.message);
    }
  });
  byId("job-list").addEventListener("click", (event) => { const button = event.target.closest("[data-job-id]"); if (button) selectJob(button.dataset.jobId); });
  byId("job-detail").addEventListener("click", async (event) => {
    const resume = event.target.closest("[data-resume-job]");
    if (resume) { resume.disabled = true; try { await api(`/api/jobs/${encodeURIComponent(resume.dataset.resumeJob)}/resume`, { method: "POST", body: {} }); toast("Job requeued from its checkpoint."); await loadBootstrap({ quiet: true }); await selectJob(resume.dataset.resumeJob, { keepOffset: true }); } catch (error) { toast(error.message); } return; }
    const page = event.target.closest("[data-page]");
    if (page) { state.observationOffset = Math.max(0, state.observationOffset + (page.dataset.page === "next" ? OBSERVATION_PAGE_SIZE : -OBSERVATION_PAGE_SIZE)); page.disabled = true; await loadObservations(); renderJobDetail(); }
  });
  byId("connection-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    submit(form, async () => { const result = await api("/api/connections", { method: "POST", body: { client: String(data.get("client")) } }); byId("connection-snippet").textContent = String(result.snippet || ""); byId("connection-result").hidden = false; }, "Connection snippet generated.");
  });
  byId("ai-settings-form").addEventListener("input", () => { state.aiSettingsDirty = true; });
  byId("ai-settings-form").addEventListener("change", () => { state.aiSettingsDirty = true; });
  byId("ai-provider").addEventListener("change", () => { renderAiModelChoices(); renderAiSettings(); });
  byId("ai-model-choice").addEventListener("change", () => {
    const custom = byId("ai-model-choice").value === "__custom__";
    byId("ai-custom-model-label").hidden = !custom;
    if (custom) byId("ai-custom-model").focus();
  });
  byId("ai-settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const model = data.get("model_choice") === "__custom__"
      ? String(data.get("custom_model") || "").trim() : String(data.get("model_choice") || "");
    const timeout = Number(data.get("timeout_seconds"));
    if (data.get("model_choice") === "__custom__" && !model) { showFormError(form, "Enter an exact model ID."); byId("ai-custom-model").focus(); return; }
    if (!Number.isInteger(timeout) || timeout < 30 || timeout > 600) { showFormError(form, "Enter a timeout from 30 to 600 seconds."); byId("ai-timeout").focus(); return; }
    const settings = { provider: String(data.get("provider")), model, timeout_seconds: timeout };
    submit(form, async () => {
      const result = await api("/api/ai/settings", { method: "POST", body: settings });
      state.aiSettingsDirty = false;
      return result;
    }, "Workspace AI settings saved.");
  });
  byId("connection-client").addEventListener("change", () => {
    renderClientTarget();
    byId("connection-result").hidden = true;
    byId("connection-snippet").textContent = "";
  });
  byId("copy-snippet").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(byId("connection-snippet").textContent); toast("Snippet copied."); }
    catch (_error) { toast("Copy was unavailable. Select the snippet and copy it manually."); }
  });
  document.addEventListener("visibilitychange", updatePolling);
}

function updatePolling() {
  if (state.polling) { clearInterval(state.polling); state.polling = null; }
  if (!document.hidden) state.polling = window.setInterval(() => loadBootstrap({ quiet: true }), 3000);
}

installEvents();
advancedFields();
route();
loadBootstrap();
updatePolling();
