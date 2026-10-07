"use strict";

const ROUTES = new Set(["plan", "overview", "sources", "runs", "connections"]);
const i18n = window.SourceLedgerI18n;
const recentMessages = new Map();
function t(key, params = {}) {
  const value = i18n.t(`app.${key}`, params);
  recentMessages.set(value, { key, params });
  if (recentMessages.size > 512) recentMessages.delete(recentMessages.keys().next().value);
  return value;
}
const routeTitle = (route) => t(`route.${route}`);
function statusText(value) {
  const code = String(value || "unknown").toLowerCase();
  return i18n.t(`app.status.${code}`) === `app.status.${code}` ? titleCase(value) : t(`status.${code}`);
}
function operationText(value) {
  const code = String(value || "unknown").toLowerCase();
  return i18n.t(`app.operation.${code}`) === `app.operation.${code}` ? titleCase(value) : t(`operation.${code}`);
}
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
  recommendationSelectPending: new Set(),
  sourceActionPending: new Set(),
  resumePending: new Set(),
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
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat(i18n.getLocale(), {
    dateStyle: "medium", timeStyle: "short",
  }).format(date);
}

function jobs() {
  const value = state.bootstrap?.jobs;
  const ordinary = Array.isArray(value) ? value : Array.isArray(value?.jobs) ? value.jobs : [];
  const plans = state.bootstrap?.plan_jobs;
  const planJobs = Array.isArray(plans) ? plans : Array.isArray(plans?.jobs) ? plans.jobs : [];
  const seen = new Set(ordinary.map(job => job.id));
  return [...ordinary, ...planJobs.filter(job => !seen.has(job.id))].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
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
  if (!modelId) return t("providerDefault");
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
  return node("span", { className: `badge ${kind}`, text: statusText(value) });
}

const knownApiErrors = Object.freeze({
  "Internal server error": "error.internal", "API endpoint not found": "error.endpointNotFound",
  "File not found": "error.fileNotFound", "Method not allowed": "error.methodNotAllowed",
  "Request body timed out": "error.requestTimeout", "Request body must be valid UTF-8 JSON": "error.invalidJson",
  "Connection directory must stay inside the workspace": "error.connectionDirectory",
  "Generated connection snippet is outside the workspace": "error.snippetDirectory",
  "MCP is not installed. Run setup.cmd --mcp or bash setup.sh --mcp first": "error.mcpNotInstalled",
});

function localizeKnownError(value) {
  const key = knownApiErrors[String(value)];
  return key ? t(key) : String(value);
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
  if (!response.ok) {
    const rawMessage = payload?.error || null;
    const error = new Error(rawMessage ? localizeKnownError(rawMessage) : t("requestFailed", { status: response.status }));
    error.status = response.status;
    error.rawMessage = rawMessage;
    throw error;
  }
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
      if (error.status !== 404) throw error;
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
    showGlobalError(error.rawMessage || error.message, quiet);
    byId("global-message").hidden = false;
  } finally {
    if (quiet) state.pollInFlight = false;
  }
}

function currentRoute() {
  const hash = location.hash.slice(1).split("?")[0];
  if (hash === "main-content") return state.route;
  return ROUTES.has(hash) ? hash : "plan";
}

function route({ moveFocus = false } = {}) {
  state.route = currentRoute();
  document.querySelectorAll("[data-route]").forEach((link) => {
    const active = link.dataset.route === state.route;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  document.querySelectorAll("[data-view]").forEach((view) => { view.hidden = view.dataset.view !== state.route; });
  byId("page-title").textContent = routeTitle(state.route);
  document.title = `${routeTitle(state.route)} · SourceLedger`;
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
  window.SourceLedgerPlanUI?.render(state.bootstrap);
}

function renderChrome() {
  const root = state.bootstrap?.workspace_root || t("localWorkspace");
  const worker = state.bootstrap?.worker || {};
  const workerStatus = worker.status || "stopped";
  byId("sidebar-workspace").textContent = root;
  byId("sidebar-workspace").title = root;
  byId("sidebar-worker").textContent = t("workerStatus", { status: statusText(workerStatus).toLowerCase() });
  byId("worker-dot").className = `status-dot ${workerStatus}`;
  byId("version-label").textContent = state.bootstrap?.version ? `v${state.bootstrap.version}` : "";
  const button = byId("worker-toggle");
  const active = ["running", "idle", "starting", "stopping"].includes(workerStatus);
  button.textContent = active ? (workerStatus === "stopping" ? t("workerStopping") : t("stopWorker")) : t("startWorker");
  button.dataset.action = active ? "stop" : "start";
  button.disabled = ["starting", "stopping"].includes(workerStatus) || button.dataset.pending === "true";
}

function renderPendingChrome() {
  byId("sidebar-workspace").textContent = t("loading");
  byId("sidebar-worker").textContent = t("checkingWorker");
  byId("worker-toggle").textContent = t("worker");
  byId("worker-toggle").disabled = true;
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
  byId("workspace-heading").textContent = product().name || t("researchWorkspace");
  byId("workspace-subtitle").textContent = [ws.industry, ws.market].filter(Boolean).join(" · ") || t("configuredWorkspace");
  byId("source-count").textContent = String(sources().length);
  byId("job-count").textContent = String(jobs().length);
  byId("identifier-count").textContent = String(Object.keys(identifiers).length);

  const readiness = byId("readiness-list");
  clear(readiness);
  const items = [
    { done: true, title: t("subject"), note: t("subjectNote"), href: "#overview" },
    { done: Object.keys(identifiers).length > 0, title: t("identity"), note: Object.keys(identifiers).length ? t("identifierCount", { count: Object.keys(identifiers).length }) : t("addIdentifier"), href: "#sources" },
    { done: sources().length > 0, title: t("candidates"), note: sources().length ? t("registeredCount", { count: sources().length }) : t("registerUrl"), href: "#sources" },
    { done: jobs().length > 0, title: t("evidenceRun"), note: jobs().length ? t("inspectResults") : t("queueResearch"), href: "#runs" },
  ];
  for (const item of items) {
    const link = node("a", { text: item.done ? t("review") : t("continue"), attrs: { href: item.href } });
    readiness.append(node("div", { className: `journey-item ${item.done ? "done" : ""}` }, [
      node("span", { className: "journey-check", text: item.done ? "✓" : "·", attrs: { "aria-hidden": "true" } }),
      node("div", {}, [node("strong", { text: item.title }), node("small", { text: item.note })]), link,
    ]));
  }
  const recent = byId("overview-jobs");
  clear(recent);
  if (!jobs().length) {
    recent.append(node("div", { className: "empty-inline" }, [node("strong", { text: t("noJobsYet") }), node("p", { text: t("queuedRunHere") })]));
  } else {
    for (const job of jobs().slice(0, 4)) recent.append(node("div", { className: "mini-job" }, [
      node("div", {}, [node("strong", { text: operationText(job.operation) }), node("small", { text: formatDate(job.created_at) })]), badge(job.status),
    ]));
  }
}

function emptySetup(target, context) {
  clear(target);
  target.append(node("div", { className: "empty-mark", text: "00" }), node("h3", { text: t("createWorkspaceFirst") }),
    node("p", { text: t("setupContext", { context }) }),
    node("a", { className: "button button-primary", text: t("goSetup"), attrs: { href: "#overview" } }));
}

function renderSources({ preserveForm = false } = {}) {
  const empty = byId("sources-setup-empty");
  const content = byId("sources-content");
  empty.hidden = isConfigured();
  content.hidden = !isConfigured();
  if (!isConfigured()) { emptySetup(empty, t("sourceRegistration")); return; }
  if (!state.identityHydrated) {
    hydrateKeyValues("identifier-rows", product().identifiers || {}, t("modelPlaceholder"), t("exactValue"));
    hydrateKeyValues("spec-rows", product().required_specs || {}, t("specPlaceholder"), t("requiredValue"));
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

function addKeyValueRow(target, key = "", value = "", keyPlaceholder = t("fieldPlaceholder"), valuePlaceholder = t("valuePlaceholder")) {
  const keyInput = node("input", { value: key, placeholder: keyPlaceholder, attrs: { "aria-label": t("fieldName") } });
  const valueInput = node("input", { value, placeholder: valuePlaceholder, attrs: { "aria-label": t("fieldValue") } });
  const remove = node("button", { className: "icon-button", text: "×", type: "button", title: t("removeRow"), attrs: { "aria-label": t("removeRow") } });
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
    if (!key || !fieldValue) throw new Error(t("completeBoth"));
    if (Object.hasOwn(value, key)) throw new Error(t("duplicateField", { name: key }));
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
  byId("source-selection-summary").textContent = t("sourceSelection", { selected, total: sources().length, filter: filter ? t("sourceFilterInfo", { shown: filtered.length }) : "" });
  if (!force && fingerprint === state.sourceFingerprint) return;
  state.sourceFingerprint = fingerprint;
  const focused = document.activeElement?.dataset?.runSourceId;
  clear(target);
  if (!filtered.length) {
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: sources().length ? t("noMatchingSources") : t("noSourceCandidates") }), node("p", { text: sources().length ? t("clearFilter") : t("registerSourceHint") })]));
    return;
  }
  for (const source of filtered) {
    const title = node("h4", { text: source.name || source.id || t("source") });
    const safeUrl = safeWebUrl(source.location);
    const url = safeUrl ? node("a", { className: "source-url", text: source.location, attrs: { href: safeUrl, target: "_blank", rel: "noopener noreferrer" } }) : node("span", { className: "source-url", text: source.location || "—" });
    const discover = node("button", { className: "button button-secondary", type: "button", text: t("discoverLinks"), disabled: state.sourceActionPending.has(`discover:${source.id}`), attrs: { "data-source-action": "discover", "data-source-id": source.id } });
    const propose = node("button", { className: "button button-secondary", type: "button", text: t("proposeRecipe"), disabled: state.sourceActionPending.has(`propose:${source.id}`), attrs: { "data-source-action": "propose", "data-source-id": source.id } });
    const include = node("input", { type: "checkbox", value: String(source.id), attrs: { "data-run-source-id": String(source.id), "aria-label": t("includeSourceAria", { name: source.name || source.location || source.id }) } });
    include.checked = state.sourceSelection.get(String(source.id)) !== false;
    target.append(node("article", { className: "source-item" }, [
      node("div", { className: "source-top" }, [node("div", {}, [title, url]), badge(source.status || "candidate")]),
      node("div", { className: "source-meta" }, [badge(source.scope || "public", "info"), node("span", { className: "badge", text: source.kind === "web" || !source.kind ? t("web") : source.kind })]),
      node("label", { className: "checkbox-label source-include" }, [include, t("includeSource")]),
      node("div", { className: "source-actions" }, [discover, propose]),
    ]));
  }
  if (focused) [...target.querySelectorAll("[data-run-source-id]")].find((item) => item.dataset.runSourceId === focused)?.focus({ preventScroll: true });
}

function candidateKey(requestId, candidateId) { return `${requestId}:${candidateId}`; }

function recommendationHandoff(request) {
  const topic = request.topic || {};
  const subject = [topic.industry, topic.product?.name, topic.market].filter(Boolean).join(" · ");
  return `${t("handoffId", { id: request.id })}\n${t("handoffKind", { kind: request.kind })}\n${t("handoffQuery", { query: request.query })}${subject ? `\n${t("handoffTopic", { subject })}` : ""}\n${t("handoffInstructions")}`;
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
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: t("noRecommendationRequests") }), node("p", { text: t("recommendationPrompt") })]));
    return;
  }
  for (const request of requests) {
    const requestId = String(request.id);
    const card = node("article", { className: "recommendation-request", attrs: { "data-request-id": requestId } });
    card.append(node("div", { className: "recommendation-head" }, [
      node("div", {}, [node("span", { className: "eyebrow", text: request.kind === "company" ? t("companyLookup") : t("keywordSearch") }), node("h4", { text: request.query || t("untitledRequest"), attrs: { tabindex: "-1" } }), node("small", { text: t("requestDate", { id: requestId, date: formatDate(request.created_at) }) })]), badge(request.status || "pending"),
    ]));
    const job = recommendationJobs.find((item) => String(item.arguments?.request_id) === requestId)
      || state.recommendationReceipts.get(requestId);
    const runActive = state.recommendationRunPending.has(requestId) || ["queued", "running"].includes(job?.status);
    const settings = aiSettings();
    const provider = aiProvider(settings.provider);
    const available = provider?.available === true;
    const run = node("div", { className: "web-recommendation" }, [
      node("div", {}, [
        node("strong", { text: t("runWeb") }),
        node("p", { className: "hint", text: `${provider?.label || titleCase(settings.provider)} · ${aiModelLabel(settings.provider, settings.model)} · ${t("seconds", { count: settings.timeout_seconds })}` }),
        node("a", { text: t("changeDefault"), attrs: { href: "#connections" } }),
      ]),
      node("button", { className: "button button-primary", type: "button", text: runActive ? t("recommendationRunning") : job?.status === "failed" || job?.status === "interrupted" ? t("retryWeb") : t("runWeb"), disabled: runActive || !available, attrs: { "data-run-recommendation": requestId } }),
    ]);
    if (!available) run.append(node("p", { className: "hint web-run-message", text: t("cliUnavailable") }));
    if (state.recommendationRunErrors.has(requestId)) run.append(node("p", { className: "form-error", attrs: { role: "alert" } }, errorText(state.recommendationRunErrors.get(requestId))));
    if (job) {
      const requested = job.arguments || settings;
      run.append(node("p", { className: "request-status", attrs: { role: "status" } }, [
        badge(job.status), ` ${titleCase(requested.provider)} · ${aiModelLabel(requested.provider, requested.model)}${job.status === "succeeded" ? ` · ${t("reviewSuggestions")}` : ""}`,
      ]));
      if (!runActive && !state.recommendationRunErrors.has(requestId) && ["failed", "interrupted"].includes(job.status)) {
        const detail = job.error?.message || (typeof job.error === "string" ? job.error : "");
        run.append(node("p", { className: "form-error", attrs: { role: "alert" } }, detail ? errorText(detail) : t("recommendationStopped")));
      }
    } else if (runActive) run.append(node("p", { className: "request-status", attrs: { role: "status" } }, t("queueingRecommendation")));
    card.append(run);
    const handoff = node("div", { className: "handoff" }, [
      node("strong", { text: t("useAiApp") }),
      node("p", { text: t("aiAppInstructions") }),
      node("button", { className: "button button-secondary", type: "button", text: t("copyRequest"), attrs: { "data-copy-request": requestId } }),
      node("details", {}, [node("summary", { text: t("viewRequest") }), node("pre", { text: recommendationHandoff(request) })]),
    ]);
    if (opened.has(requestId)) handoff.querySelector("details").open = true;
    card.append(handoff);
    if (request.note) card.append(node("p", { className: "hint", text: request.note }));
    const candidates = Array.isArray(request.candidates) ? request.candidates : [];
    if (request.status === "pending" && !candidates.length) card.append(node("p", { className: "request-status", text: t("noSuggestionsYet") }));
    else if (!candidates.length) card.append(node("p", { className: "request-status", text: t("noSuggestionsReturned") }));
    else {
      const list = node("div", { className: "candidate-list" });
      for (const candidate of candidates) {
        const id = String(candidate.id);
        const key = candidateKey(requestId, id);
        const checkbox = node("input", { type: "checkbox", value: id, attrs: { "data-candidate-id": id, "data-candidate-request": requestId, "aria-label": t("selectCandidateAria", { name: candidate.name || candidate.url || id }) } });
        checkbox.checked = state.candidateSelection.get(key) === true;
        checkbox.disabled = Boolean(candidate.source_id);
        const link = (label, value) => {
          const safe = safeWebUrl(value);
          return safe ? node("a", { text: `${label}: ${value}`, attrs: { href: safe, target: "_blank", rel: "noopener noreferrer" } }) : node("span", { text: `${label}: ${value || "—"}` });
        };
        list.append(node("article", { className: "candidate-item" }, [
          node("label", { className: "checkbox-label candidate-check" }, [checkbox, candidate.source_id ? t("addedTargets") : t("selectSuggestion")]),
          node("strong", { text: candidate.name || t("unnamedSuggestion") }), link(t("site"), candidate.url),
          node("p", { text: candidate.reason || t("noReason") }), link(t("sourceReference"), candidate.evidence_url),
          node("small", { text: t("unverified") }),
        ]));
      }
      card.append(list);
      const selectedCount = candidates.filter((candidate) => !candidate.source_id && state.candidateSelection.get(candidateKey(requestId, String(candidate.id))) === true).length;
      card.append(node("div", { className: "recommendation-actions" }, [
        node("span", { text: t("selectedCount", { count: selectedCount }) }),
        node("button", { className: "button button-primary", type: "button", text: t("addSelected"), disabled: selectedCount === 0 || state.recommendationSelectPending.has(requestId), attrs: { "data-add-request": requestId } }),
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
  if (!Object.keys(product().identifiers || {}).length) blockers.push(t("blockIdentifier"));
  if (!sources().length) blockers.push(t("blockCandidate"));
  else if (!selectedSourceIds().length) blockers.push(t("blockOneSource"));
  if (selectedSourceIds().length > 50) blockers.push(t("blockMaxSources"));
  return blockers;
}

function renderRuns() {
  const empty = byId("runs-setup-empty");
  const content = byId("runs-content");
  const configured = isConfigured();
  const hasJobs = jobs().length > 0;
  empty.hidden = configured || hasJobs;
  content.hidden = !configured && !hasJobs;
  byId("legacy-guided-run").hidden = !configured;
  byId("advanced-form").closest("details").hidden = !configured;
  if (!configured && !hasJobs) {
    clear(empty);
    empty.append(node("h3", { text: i18n.t("plan.noRunsTitle") }),
      node("p", { text: i18n.t("plan.noRunsHint") }),
      node("a", { className: "button button-primary", text: i18n.t("plan.goPlan"), attrs: { href: "#plan" } }));
    return;
  }
  if (!configured) { renderJobList(); return; }
  const blockers = runBlockers();
  byId("run-source-summary").textContent = t("runSourceSummary", { selected: selectedSourceIds().length, total: sources().length });
  const blockerTarget = byId("run-blockers");
  clear(blockerTarget);
  if (!blockers.length) blockerTarget.append(badge("ready", "success"));
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
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: t("noJobHistory") }), node("p", { text: t("queueJobHint") })]));
    if (!state.selectedJobId) renderJobDetail();
    return;
  }
  for (const job of jobs()) {
    const button = node("button", { className: `job-button ${job.id === state.selectedJobId ? "active" : ""}`, type: "button", attrs: { "data-job-id": job.id, "aria-label": t("jobAria", { operation: operationText(job.operation), status: statusText(job.status) }) } }, [
      node("span", {}, [node("strong", { text: operationText(job.operation) }), node("small", { text: formatDate(job.created_at) })]), badge(job.status),
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
    state.selectedJob = { id, loadError: error.rawMessage || error.message };
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
    if (state.selectedJobId === requestedId && state.observationOffset === requestedOffset) state.observations = { rows: [], total: 0, offset: requestedOffset, limit: OBSERVATION_PAGE_SIZE, error: error.rawMessage || error.message };
  }
}

function fact(label, value) {
  return node("div", {}, [node("dt", { text: label }), node("dd", { text: displayValue(value) })]);
}

function renderJobDetail() {
  const target = byId("job-detail");
  clear(target);
  if (state.loadingJob) {
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: t("loadingRun") }), node("p", { text: t("readingReceipt") })]));
    return;
  }
  const job = state.selectedJob;
  if (!job || job.id !== state.selectedJobId) {
    target.append(node("div", { className: "empty-inline" }, [node("strong", { text: t("selectRun") }), node("p", { text: t("selectRunHint") })]));
    return;
  }
  if (job.loadError) {
    target.append(node("div", { className: "detail-error", text: errorText(job.loadError) }));
    return;
  }
  const result = job.result || {};
  const actions = node("div", { className: "detail-actions" });
  const evidence = result.evidence_status || result.result?.status;
  const resumable = !["recommend", "plan_preview", "research_plan"].includes(job.operation) && (job.status === "interrupted" || (job.operation === "agent" && evidence === "paused"));
  if (resumable) actions.append(node("button", { className: "button button-secondary", type: "button", text: t("resume"), disabled: state.resumePending.has(String(job.id)), attrs: { "data-resume-job": job.id } }));
  if (result.report_path) actions.append(node("a", { className: "button button-primary", text: t("downloadXlsx"), attrs: { href: `/api/jobs/${encodeURIComponent(job.id)}/report` } }));
  const executionStatus = result.execution_status || job.status;
  target.append(node("div", { className: "detail-head" }, [
    node("div", {}, [node("span", { className: "eyebrow", text: t("jobDetail") }), node("h3", { text: operationText(job.operation) }), node("span", { className: "detail-id", text: job.id })]), actions,
  ]));
  target.append(node("dl", { className: "detail-facts" }, [
    fact(t("jobState"), statusText(job.status)), fact(t("execution"), statusText(executionStatus)), fact(t("evidence"), evidence ? statusText(evidence) : t("notAvailable")),
    fact(t("attempt"), job.attempt ?? 0), fact(t("created"), formatDate(job.created_at)), fact(t("updated"), formatDate(job.updated_at)),
  ]));
  if (job.operation === "recommend") target.append(node("dl", { className: "detail-facts" }, [
    fact(t("requestedProvider"), job.arguments?.provider), fact(t("requestedModel"), aiModelLabel(job.arguments?.provider, job.arguments?.model)),
    fact(t("actualModel"), result.result?.actual_model || t("notReported")),
  ]));
  if (job.operation === "research_plan") {
    const coverage = Array.isArray(result.result?.coverage) ? result.result.coverage : Array.isArray(result.coverage) ? result.coverage : [];
    const visited = coverage.filter(item => ["visited", "no_data"].includes(item.status));
    const collected = coverage.filter(item => item.status === "visited" && Number(item.products || 0) > 0);
    target.append(node("dl", { className: "detail-facts" }, [
      fact(i18n.t("plan.coverage"), `${visited.length} / ${coverage.length}`),
      fact(i18n.t("plan.collectedPages"), collected.length),
      fact(i18n.t("plan.observationCount"), state.observations?.total ?? t("notAvailable")),
    ]));
    const scopeLabels = { matched: "scopeMatched", unknown: "scopeUnknown", excluded: "scopeExcluded", not_checked: "scopeNotChecked" };
    const scopeCount = (item, key) => Number.isSafeInteger(item.scope_counts?.[key]) && item.scope_counts[key] >= 0 ? item.scope_counts[key] : 0;
    const conditionText = condition => condition && typeof condition === "object"
      ? `${i18n.t(`plan.field.${condition.field}`)} ${i18n.t(`plan.operator.${condition.operator}`)} ${displayValue(condition.value)}` : "";
    if (coverage.some(item => item.scope_counts)) {
      target.append(node("h4", { text: i18n.t("plan.conditionChecks") }));
      target.append(node("dl", { className: "detail-facts" }, Object.entries(scopeLabels).map(([key, label]) =>
        fact(i18n.t(`plan.${label}`), coverage.reduce((sum, item) => sum + scopeCount(item, key), 0)))));
    }
    if (result.result?.scope_note) target.append(node("p", { className: "plan-scope-notice", text: i18n.t("plan.scopeNote") }));
    if (result.result?.scope) target.append(node("p", { className: "hint", text: i18n.t("plan.scopeBound") }));
    const scope = result.result?.plan_scope;
    if (scope) {
      const details = node("details", { className: "plan-scope" });
      details.append(node("summary", { text: i18n.t("plan.scopeDetails") }));
      details.append(node("p", { text: displayValue(scope.request_text) }));
      details.append(node("dl", { className: "detail-facts" }, [
        fact(i18n.t("ui.industry"), scope.topic?.industry), fact(i18n.t("ui.product"), scope.topic?.product),
        fact(i18n.t("ui.market"), scope.topic?.market), fact(i18n.t("plan.categories"), (scope.categories || []).join(" · ")),
        fact(i18n.t("plan.include"), (scope.include_terms || []).join(" · ")), fact(i18n.t("plan.exclude"), (scope.exclude_terms || []).join(" · ")),
      ]));
      if (Array.isArray(scope.conditions) && scope.conditions.length) {
        details.append(node("p", { text: scope.conditions.map(conditionText).join("; ") }));
      }
      target.append(details);
    }
    if (coverage.length) {
      const list = node("div", { className: "plan-coverage" });
      for (const item of coverage) {
        const safe = safeWebUrl(item.url);
        list.append(node("div", { className: "plan-coverage-row" }, [
          safe ? node("a", { text: item.url, attrs: { href: safe, target: "_blank", rel: "noopener noreferrer" } }) : node("span", { text: displayValue(item.url) }),
          badge(item.status),
        ]));
        if (Array.isArray(item.scope_assessments) && item.scope_assessments.length) {
          const checks = node("details", { className: "plan-scope" });
          checks.append(node("summary", { text: i18n.t("plan.conditionChecks") }));
          for (const assessment of item.scope_assessments) {
            const label = scopeLabels[assessment.status] || "scopeUnknown";
            checks.append(node("p", { text: `${displayValue(assessment.name)} · ${i18n.t(`plan.${label}`)}` }));
            for (const check of Array.isArray(assessment.checks) ? assessment.checks : []) {
              const checkLabel = scopeLabels[check.status] || "scopeUnknown";
              checks.append(node("p", { text: `${conditionText(check.condition)} · ${i18n.t(`plan.${checkLabel}`)}${check.reason ? ` — ${check.reason}` : ""}` }));
              for (const proof of Array.isArray(check.evidence) ? check.evidence : []) {
                checks.append(node("p", { text: `${displayValue(proof.raw).slice(0, 500)} (${displayValue(proof.location).slice(0, 300)})` }));
              }
            }
            if (assessment.reason) checks.append(node("p", { text: assessment.reason }));
          }
          if (item.scope_assessments_truncated > 0) checks.append(node("p", { text: i18n.t("plan.checksTruncated", { count: item.scope_assessments_truncated }) }));
          list.append(checks);
        } else if (["visited", "no_data"].includes(item.status)) {
          list.append(node("p", { className: "hint", text: i18n.t("plan.sourceOnly") }));
        }
      }
      target.append(node("h4", { text: i18n.t("plan.coverageDetails") }), list);
    }
  }
  if (job.error) target.append(node("div", { className: "detail-error", text: errorText(typeof job.error === "string" ? job.error : displayValue(job.error)) }));
  if (result.execution_status === "succeeded") target.append(node("p", { className: "hint", text: t("executionCaution") }));
  renderObservations(target);
}

function renderObservations(target) {
  const payload = state.observations;
  if (!payload) return;
  const section = node("section", { className: "observation-section" });
  section.append(node("h4", { text: t("observations") }), node("p", { className: "hint", text: t("observationsHint") }));
  if (payload.error) {
    section.append(node("div", { className: "detail-error", text: errorText(payload.error) })); target.append(section); return;
  }
  const rows = Array.isArray(payload.rows) ? payload.rows : [];
  if (payload.result_count !== undefined) {
    section.append(node("p", { text: i18n.t("plan.resultCounts", { results: payload.result_count, linked: payload.linked_evidence_count, unlinked: payload.unlinked_evidence_count }) }));
  }
  if (Array.isArray(payload.evidence_submissions) && payload.evidence_submissions.length) {
    const pending = node("details");
    pending.append(node("summary", { text: i18n.t("plan.unlinkedEvidence") }));
    for (const evidence of payload.evidence_submissions) {
      pending.append(node("p", { text: `${displayValue(evidence.raw_fields?.name)} · ${displayValue(evidence.raw_fields)} · ${displayValue(evidence.locator)}` }));
    }
    section.append(pending);
  }
  if (!rows.length) {
    section.append(node("div", { className: "empty-inline" }, [node("strong", { text: t("noObservations") }), node("p", { text: t("noLedger") })]));
    target.append(section); return;
  }
  const table = node("table");
  const header = node("tr");
  for (const label of [i18n.t("plan.productName"), i18n.t("plan.optionsAttributes"), t("source"), t("observedAmount"), t("estimatedAmount"), t("currency"), t("valueOrigin"), t("evidenceStatus"), i18n.t("plan.evidenceLocation"), t("collected")]) header.append(node("th", { text: label, attrs: { scope: "col" } }));
  table.append(node("thead", {}, header));
  const body = node("tbody");
  for (const row of rows) {
    const sourceCell = node("td");
    const safeUrl = safeWebUrl(row.source_url);
    if (safeUrl) sourceCell.append(node("a", { text: row.source_name || row.source_id || safeUrl, attrs: { href: safeUrl, target: "_blank", rel: "noopener noreferrer" } }));
    else sourceCell.textContent = displayValue(row.source_name || row.source_id);
    const observed = row.value_origin === "observed" ? row.amount : null;
    const estimated = row.value_origin === "calculator_estimate" ? (row.derived_amount ?? row.derived_values?.estimated_price ?? null) : null;
    const evidenceStatus = row.result_status === "conflict" ? i18n.t("plan.resultConflict") : row.verification_level || row.status || "unknown";
    const fields = row.raw_fields && typeof row.raw_fields === "object" ? row.raw_fields : {};
    const productName = fields.name || row.product_name || row.product_id;
    const attributes = Object.entries(fields).filter(([key, value]) => key !== "name" && key !== "price" && key !== "currency" && value !== null && value !== "").map(([key, value]) => `${key}: ${displayValue(value)}`).join(" · ");
    const attributeCell = node("td");
    const keyFields = row.price_profile === "rental" ? ["term_months", "deposit_amount", "annual_mileage_km"] : ["unit", "pack_quantity", "price_basis"];
    for (const key of keyFields) {
      if (fields[key] != null) attributeCell.append(node("p", { text: `${i18n.t(`plan.field.${key}`)}: ${displayValue(fields[key])}` }));
    }
    const allFields = node("details");
    allFields.append(node("summary", { text: i18n.t("plan.optionsAttributes") }), node("p", { text: attributes || "—" }));
    attributeCell.append(allFields);
    const detailCell = node("td");
    detailCell.append(node("p", { text: displayValue(row.locator || row.evidence_path) }));
    if (row.missing_conditions?.length) detailCell.append(node("p", { text: `${i18n.t("plan.resultMissing")}: ${row.missing_conditions.map(key => i18n.t(`plan.field.${key}`)).join(", ")}` }));
    if (row.field_status) {
      const details = node("details");
      details.append(node("summary", { text: i18n.t("plan.resultFields") }));
      for (const [key, field] of Object.entries(row.field_status)) {
        const label = field.status === "missing" ? "resultMissing" : field.status === "conflict" ? "resultConflict" : "resultRecorded";
        details.append(node("p", { text: `${i18n.t(`plan.field.${key}`)}: ${field.value == null ? "—" : displayValue(field.value)} · ${i18n.t(`plan.${label}`)}` }));
      }
      detailCell.append(details);
    }
    if (row.supporting_evidence?.length) {
      const proof = node("details");
      proof.append(node("summary", { text: i18n.t("plan.resultEvidence", { count: row.supporting_evidence.length }) }), node("p", { text: i18n.t("plan.evidencePending") }));
      for (const evidence of row.supporting_evidence) {
        proof.append(node("p", { text: `${displayValue(evidence.raw_fields)} · ${displayValue(evidence.locator)} · ${formatDate(evidence.collected_at)}` }));
        for (const conflict of evidence.conflicts || []) proof.append(node("p", { text: `${i18n.t("plan.resultConflict")}: ${conflict.field} · ${conflict.observed} / ${conflict.submitted}` }));
      }
      detailCell.append(proof);
    }
    const displayPrice = observed == null ? "—" : String(observed).replace(/^(-?\d+)(\.\d+)?$/, (_, integer, fraction = "") => integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + fraction);
    body.append(node("tr", {}, [node("td", { text: displayValue(productName) }), attributeCell, sourceCell, node("td", { text: displayPrice }), node("td", { text: displayValue(estimated) }), node("td", { text: displayValue(row.currency) }), node("td", { text: statusText(row.value_origin) }), node("td", {}, badge(evidenceStatus)), detailCell, node("td", { text: formatDate(row.collected_at) })]));
  }
  table.append(body);
  section.append(node("div", { className: "table-scroll" }, table));
  const total = Number(payload.total || 0);
  const offset = Number(payload.offset || 0);
  const previous = node("button", { className: "button button-secondary", type: "button", text: t("previous"), disabled: offset <= 0, attrs: { "data-page": "previous" } });
  const next = node("button", { className: "button button-secondary", type: "button", text: t("next"), disabled: offset + rows.length >= total, attrs: { "data-page": "next" } });
  section.append(node("div", { className: "pagination" }, [previous, node("span", { text: t("pagination", { start: offset + 1, end: offset + rows.length, total }) }), next]));
  target.append(section);
}

function renderConnections() {
  const root = state.bootstrap?.workspace_root;
  byId("connection-workspace").textContent = root ? t("boundTo", { root }) : t("boundLocal");
  const install = byId("mcp-install-note");
  install.classList.toggle("notice-error", state.bootstrap?.mcp_available === false);
  install.title = state.bootstrap?.mcp_available === false ? t("mcpMissing") : t("mcpAvailable");
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
    ? t("providerInstalled", { provider: provider.label || titleCase(provider.id) })
    : `${t("providerUnavailable", { provider: provider?.label || titleCase(providerInput.value) })} ${t("installCli")}`;
}

function renderAiModelChoices(savedModel = "") {
  const providerId = byId("ai-provider").value;
  const select = byId("ai-model-choice");
  clear(select);
  select.append(node("option", { value: "", text: t("providerDefaultChoice") }));
  for (const model of aiProvider(providerId)?.models || []) select.append(node("option", { value: model.id, text: model.label || model.id }));
  select.append(node("option", { value: "__custom__", text: t("customModel") }));
  const known = [...select.options].some((option) => option.value === savedModel);
  select.value = savedModel && !known ? "__custom__" : savedModel;
  byId("ai-custom-model").value = savedModel && !known ? savedModel : "";
  byId("ai-custom-model-label").hidden = select.value !== "__custom__";
}

function renderClientTarget() {
  const targets = {
    codex: "CODEX_HOME/config.toml",
    "claude-code": t("projectConfig"),
    "claude-desktop": t("desktopConfig"),
  };
  byId("client-config-target").textContent = targets[byId("connection-client").value];
}

function errorText(message) {
  const value = String(message);
  const known = recentMessages.get(value);
  if (known) return t(known.key, known.params);
  const apiKey = knownApiErrors[value];
  return apiKey ? t(apiKey) : `${t("error.generic")}: ${value}`;
}

function setMessage(target, message, { error = false } = {}) {
  const value = String(message || "");
  const known = recentMessages.get(value);
  if (known) {
    target.dataset.messageKey = known.key;
    target.dataset.messageParams = JSON.stringify(known.params);
    delete target.dataset.errorRaw;
    target.textContent = t(known.key, known.params);
  } else {
    target.textContent = error && value ? errorText(value) : value;
    delete target.dataset.messageKey;
    delete target.dataset.messageParams;
    if (error && value) target.dataset.errorRaw = value;
    else delete target.dataset.errorRaw;
  }
}

function refreshMessages() {
  document.querySelectorAll("[data-message-key]").forEach((target) => {
    let params = {};
    try { params = JSON.parse(target.dataset.messageParams || "{}"); } catch (_error) { /* Ignore stale metadata. */ }
    target.textContent = t(target.dataset.messageKey, params);
  });
  document.querySelectorAll("[data-error-raw]").forEach((target) => { target.textContent = errorText(target.dataset.errorRaw); });
}

function showGlobalError(message, quiet) {
  const target = byId("global-message-text");
  target.dataset.rawError = String(message);
  target.dataset.quietError = quiet ? "true" : "false";
  setMessage(target, quiet ? t("refreshFailed", { error: errorText(message) }) : message, { error: !quiet });
}

function showFormError(form, message) {
  const target = form.querySelector("[data-form-error]");
  if (!target) return;
  setMessage(target, message, { error: true });
  target.hidden = !message;
}

function setPending(form, pending) {
  form.querySelectorAll("button, input, select").forEach((control) => { control.disabled = pending; });
  form.dataset.pending = pending ? "true" : "false";
}

function toast(message, error = false) {
  const item = node("div", { className: "toast", text: message });
  setMessage(item, message, { error });
  byId("toast-region").append(item);
  while (byId("toast-region").childElementCount > 2) byId("toast-region").firstElementChild.remove();
  window.setTimeout(() => item.remove(), 4200);
}

function focusPath(element) {
  if (!element || element === document.body) return null;
  const path = [];
  let current = element;
  while (current && current !== document.body && !current.id) {
    const parent = current.parentElement;
    if (!parent) return null;
    path.unshift([...parent.children].indexOf(current));
    current = parent;
  }
  return current?.id ? { id: current.id, path } : null;
}

function restoreFocus(snapshot, original) {
  if (original?.isConnected) { original.focus({ preventScroll: true }); return; }
  if (!snapshot) return;
  let current = byId(snapshot.id);
  for (const index of snapshot.path) current = current?.children[index];
  current?.focus?.({ preventScroll: true });
}

function refreshKeyValueLabels() {
  for (const [id, keyPlaceholder, valuePlaceholder] of [
    ["identifier-rows", "modelPlaceholder", "exactValue"], ["spec-rows", "specPlaceholder", "requiredValue"],
  ]) {
    byId(id)?.querySelectorAll(".key-value-row").forEach((row) => {
      const [key, value] = row.querySelectorAll("input");
      const remove = row.querySelector("button");
      key.placeholder = t(keyPlaceholder);
      key.setAttribute("aria-label", t("fieldName"));
      value.placeholder = t(valuePlaceholder);
      value.setAttribute("aria-label", t("fieldValue"));
      remove.title = t("removeRow");
      remove.setAttribute("aria-label", t("removeRow"));
    });
  }
}

function onLanguageChange() {
  const originalFocus = document.activeElement;
  const focus = focusPath(originalFocus);
  const advancedValues = new Map([...byId("advanced-fields").querySelectorAll("input")].map((input) => [input.name, input.value]));
  const modelChoice = byId("ai-model-choice").value;
  const customModel = byId("ai-custom-model").value;
  state.overviewFingerprint = null;
  state.sourceFingerprint = null;
  state.recommendationFingerprint = null;
  state.jobFingerprint = null;
  byId("page-title").textContent = routeTitle(state.route);
  document.title = `${routeTitle(state.route)} · SourceLedger`;
  if (state.bootstrap) {
    render({ polling: true });
    renderJobDetail();
  } else renderPendingChrome();
  refreshKeyValueLabels();
  const choice = byId("ai-model-choice");
  if (choice.options.length) {
    choice.options[0].textContent = t("providerDefaultChoice");
    choice.options[choice.options.length - 1].textContent = t("customModel");
    choice.value = modelChoice;
    byId("ai-custom-model").value = customModel;
    byId("ai-custom-model-label").hidden = modelChoice !== "__custom__";
  }
  advancedFields();
  for (const input of byId("advanced-fields").querySelectorAll("input")) if (advancedValues.has(input.name)) input.value = advancedValues.get(input.name);
  const sourceForm = byId("source-form");
  sourceForm.querySelector('button[type="submit"]').textContent = sourceForm.elements.namedItem("name").value.trim() && !sourceForm.elements.namedItem("url").value.trim() ? t("createCompanyRequest") : t("registerSource");
  const global = byId("global-message-text");
  if (!byId("global-message").hidden && global.dataset.rawError) showGlobalError(global.dataset.rawError, global.dataset.quietError === "true");
  refreshMessages();
  i18n.translateStatic(document);
  window.SourceLedgerPlanUI?.languageChanged();
  restoreFocus(focus, originalFocus);
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
    target.append(field(t("urls"), "urls", "https://example.com/a, https://example.com/b", true), field(t("maxPages"), "max_pages", "5", false, "number"));
  } else if (operation === "verify") {
    target.append(field(t("configPath"), "config_path", "configs/source.json", true), field(t("samplesPath"), "samples_path", t("optional")));
  } else if (operation === "run") {
    target.append(field(t("configPath"), "config_path", "configs/source.json", true), field(t("maxTasks"), "max_tasks", t("optional"), false, "number"));
  } else {
    target.append(field(t("configPath"), "config_path", "configs/source.json", true), field(t("runId"), "run_id", t("existingRunId"), true));
  }
  if (byId("advanced-form").dataset.pending === "true") target.querySelectorAll("input").forEach((input) => { input.disabled = true; });
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
    if (!urls.length) throw new Error(t("needUrl"));
    args.urls = urls;
    args.max_pages = Number(data.get("max_pages") || 5);
    args.max_seconds = 120;
    args.incremental = true;
  } else {
    const configPath = String(data.get("config_path") || "").trim();
    if (!configPath) throw new Error(t("needConfigPath"));
    args.config_path = configPath;
    if (operation === "verify") {
      const samples = String(data.get("samples_path") || "").trim(); if (samples) args.samples_path = samples;
    } else if (operation === "run") {
      const maximum = String(data.get("max_tasks") || "").trim(); if (maximum) args.max_tasks = Number(maximum);
    } else {
      const runId = String(data.get("run_id") || "").trim(); if (!runId) throw new Error(t("needRunId")); args.run_id = runId;
    }
  }
  return { operation, arguments: args };
}

function installEvents() {
  window.addEventListener("sourceledger-language-change", onLanguageChange);
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
    try { await api("/api/worker", { method: "POST", body: { action: button.dataset.action } }); toast(t(button.dataset.action === "start" ? "workerStartRequested" : "workerStopRequested")); await loadBootstrap({ quiet: true }); }
    catch (error) { toast(error.message, true); }
    finally { button.dataset.pending = "false"; renderChrome(); }
  });
  byId("setup-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    submit(form, () => api("/api/workspace", { method: "POST", body: { industry: String(data.get("industry")).trim(), product: String(data.get("product")).trim(), market: String(data.get("market")).trim(), locale: "en" } }), t("workspaceCreated"));
  });
  byId("source-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const name = String(data.get("name") || "").trim(); const url = String(data.get("url") || "").trim();
    if (!name && !url) { showFormError(form, t("needCompanyUrl")); return; }
    if (!url && data.get("scope") === "internal") { showFormError(form, t("needInternalUrl")); return; }
    if (url && !safeWebUrl(url)) { showFormError(form, t("needHttpUrl")); return; }
    submit(form, async () => {
      let result;
      if (url) result = await api("/api/sources", { method: "POST", body: { url, scope: String(data.get("scope")), ...(name ? { name } : {}) } });
      else result = await api("/api/recommendations", { method: "POST", body: { query: name, kind: "company" } });
      form.reset();
      form.querySelector('button[type="submit"]').textContent = t("registerSource");
      return result;
    }, url ? t("sourceRegistered") : t("companyRequestCreated"), url ? null : focusCreatedRequest);
  });
  byId("source-form").addEventListener("input", () => {
    const form = byId("source-form");
    const name = form.elements.namedItem("name").value.trim();
    const url = form.elements.namedItem("url").value.trim();
    form.querySelector('button[type="submit"]').textContent = name && !url ? t("createCompanyRequest") : t("registerSource");
  });
  byId("recommendation-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget; const query = String(new FormData(form).get("query") || "").trim();
    if (!query) { showFormError(form, t("needKeyword")); return; }
    submit(form, async () => { const result = await api("/api/recommendations", { method: "POST", body: { query, kind: "keyword" } }); form.reset(); return result; }, t("recommendationCreated"), focusCreatedRequest);
  });
  byId("recommendation-list").addEventListener("change", (event) => {
    const box = event.target.closest("[data-candidate-id]");
    if (!box) return;
    state.candidateSelection.set(candidateKey(box.dataset.candidateRequest, box.dataset.candidateId), box.checked);
    const card = box.closest(".recommendation-request");
    const count = [...card.querySelectorAll("[data-candidate-id]")].filter((item) => item.checked && !item.disabled).length;
    card.querySelector(".recommendation-actions span").textContent = t("selectedCount", { count });
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
        toast(t("recommendationQueued"));
        await loadBootstrap({ quiet: true });
      } catch (error) {
        state.recommendationRunErrors.set(requestId, error.rawMessage || error.message);
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
      try { await navigator.clipboard.writeText(recommendationHandoff(request)); toast(t("requestCopied")); }
      catch (_error) { toast(t("copyRequestUnavailable")); }
      return;
    }
    const button = event.target.closest("[data-add-request]");
    if (!button) return;
    const request = recommendationRequests().find((item) => String(item.id) === button.dataset.addRequest);
    if (!request) return;
    const requestId = String(request.id);
    if (state.recommendationSelectPending.has(requestId)) return;
    const ids = (request.candidates || []).filter((candidate) => !candidate.source_id && state.candidateSelection.get(candidateKey(String(request.id), String(candidate.id))) === true).map((candidate) => String(candidate.id));
    if (!ids.length) return;
    state.recommendationSelectPending.add(requestId);
    button.disabled = true;
    try {
      const result = await api("/api/recommendations/select", { method: "POST", body: { request_id: String(request.id), candidate_ids: ids } });
      for (const id of ids) state.candidateSelection.delete(candidateKey(String(request.id), id));
      toast(t("sourcesAdded", { count: result.added_count ?? ids.length }));
      await loadBootstrap({ quiet: true });
    } catch (error) { toast(error.message, true); }
    finally { state.recommendationSelectPending.delete(requestId); renderRecommendations(true); }
  });
  byId("identity-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget;
    submit(form, () => { const identifiers = keyValueObject("identifier-rows"); if (!Object.keys(identifiers).length) throw new Error(t("needIdentifier")); return api("/api/product", { method: "POST", body: { identifiers, required_specs: keyValueObject("spec-rows") } }); }, t("identitySaved"));
  });
  document.querySelectorAll("[data-add-row]").forEach((button) => button.addEventListener("click", () => {
    const target = byId(button.dataset.addRow); addKeyValueRow(target, "", "", target.id === "identifier-rows" ? t("modelPlaceholder") : t("specPlaceholder"), target.id === "identifier-rows" ? t("exactValue") : t("requiredValue")); target.lastElementChild.querySelector("input").focus();
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
    byId("source-selection-summary").textContent = t("sourceSelection", { selected: selectedSourceIds().length, total: sources().length, filter: byId("source-filter").value.trim() ? t("hiddenSelected") : "" });
    renderRuns();
  });
  byId("source-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-source-action]"); if (!button) return;
    const operation = button.dataset.sourceAction;
    const actionKey = `${operation}:${button.dataset.sourceId}`;
    if (state.sourceActionPending.has(actionKey)) return;
    state.sourceActionPending.add(actionKey);
    button.disabled = true;
    api("/api/jobs", { method: "POST", body: { operation, arguments: operation === "discover" ? { source_id: button.dataset.sourceId, limit: 100 } : { source_id: button.dataset.sourceId, timeout_seconds: 30, max_model_calls: 0 } } })
      .then(() => { toast(t(operation === "discover" ? "discoverQueued" : "proposeQueued")); return loadBootstrap({ quiet: true }); }).catch((error) => toast(error.message, true)).finally(() => { state.sourceActionPending.delete(actionKey); renderSourceList(true); });
  });
  byId("agent-form").addEventListener("submit", (event) => { event.preventDefault(); const form = event.currentTarget; const sourceIds = selectedSourceIds(); if (!sourceIds.length || sourceIds.length > 50) { showFormError(form, t("needSourceRange")); return; } submit(form, () => api("/api/jobs", { method: "POST", body: { operation: "agent", arguments: { source_ids: sourceIds, max_sources: sourceIds.length, max_seconds: 120, max_model_calls: 0 } } }), t("researchQueued")); });
  byId("advanced-operation").addEventListener("change", advancedFields);
  byId("advanced-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const body = advancedArguments(form);
      submit(form, () => api("/api/jobs", { method: "POST", body }), t("advancedQueued"));
    } catch (error) {
      showFormError(form, error.message);
    }
  });
  byId("job-list").addEventListener("click", (event) => { const button = event.target.closest("[data-job-id]"); if (button) selectJob(button.dataset.jobId); });
  byId("job-detail").addEventListener("click", async (event) => {
    const resume = event.target.closest("[data-resume-job]");
    if (resume) {
      const id = resume.dataset.resumeJob;
      if (state.resumePending.has(id)) return;
      state.resumePending.add(id);
      resume.disabled = true;
      try { await api(`/api/jobs/${encodeURIComponent(id)}/resume`, { method: "POST", body: {} }); toast(t("jobRequeued")); await loadBootstrap({ quiet: true }); await selectJob(id, { keepOffset: true }); }
      catch (error) { toast(error.message, true); }
      finally { state.resumePending.delete(id); renderJobDetail(); }
      return;
    }
    const page = event.target.closest("[data-page]");
    if (page) { state.observationOffset = Math.max(0, state.observationOffset + (page.dataset.page === "next" ? OBSERVATION_PAGE_SIZE : -OBSERVATION_PAGE_SIZE)); page.disabled = true; await loadObservations(); renderJobDetail(); }
  });
  byId("connection-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    submit(form, async () => { const result = await api("/api/connections", { method: "POST", body: { client: String(data.get("client")) } }); byId("connection-snippet").textContent = String(result.snippet || ""); byId("connection-result").hidden = false; }, t("snippetGenerated"));
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
    if (data.get("model_choice") === "__custom__" && !model) { showFormError(form, t("needModel")); byId("ai-custom-model").focus(); return; }
    if (!Number.isInteger(timeout) || timeout < 30 || timeout > 600) { showFormError(form, t("needTimeout")); byId("ai-timeout").focus(); return; }
    const settings = { provider: String(data.get("provider")), model, timeout_seconds: timeout };
    submit(form, async () => {
      const result = await api("/api/ai/settings", { method: "POST", body: settings });
      state.aiSettingsDirty = false;
      return result;
    }, t("aiSettingsSaved"));
  });
  byId("connection-client").addEventListener("change", () => {
    renderClientTarget();
    byId("connection-result").hidden = true;
    byId("connection-snippet").textContent = "";
  });
  byId("copy-snippet").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(byId("connection-snippet").textContent); toast(t("snippetCopied")); }
    catch (_error) { toast(t("copySnippetUnavailable")); }
  });
  document.addEventListener("visibilitychange", updatePolling);
}

function updatePolling() {
  if (state.polling) { clearInterval(state.polling); state.polling = null; }
  if (!document.hidden) state.polling = window.setInterval(() => loadBootstrap({ quiet: true }), 3000);
}

i18n.initialize();
renderPendingChrome();
installEvents();
advancedFields();
route();
loadBootstrap();
updatePolling();
