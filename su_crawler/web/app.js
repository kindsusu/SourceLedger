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
  jobFingerprint: null,
  overviewFingerprint: null,
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
  const target = byId("source-list");
  const filter = byId("source-filter").value.trim().toLowerCase();
  const filtered = sources().filter((source) => `${source.name || ""} ${source.location || ""}`.toLowerCase().includes(filter));
  const fingerprint = JSON.stringify([filter, filtered]);
  if (!force && fingerprint === state.sourceFingerprint) return;
  state.sourceFingerprint = fingerprint;
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
    target.append(node("article", { className: "source-item" }, [
      node("div", { className: "source-top" }, [node("div", {}, [title, url]), badge(source.status || "candidate")]),
      node("div", { className: "source-meta" }, [badge(source.scope || "public", "info"), node("span", { className: "badge", text: source.kind || "web" })]),
      node("div", { className: "source-actions" }, [discover, propose]),
    ]));
  }
}

function runBlockers() {
  const blockers = [];
  if (!Object.keys(product().identifiers || {}).length) blockers.push("Exact identifier required");
  if (!sources().length) blockers.push("Source candidate required");
  return blockers;
}

function renderRuns() {
  const empty = byId("runs-setup-empty");
  const content = byId("runs-content");
  empty.hidden = isConfigured();
  content.hidden = !isConfigured();
  if (!isConfigured()) { emptySetup(empty, "Durable runs"); return; }
  const blockers = runBlockers();
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
  const resumable = job.status === "interrupted" || (job.operation === "agent" && evidence === "paused");
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
  window.setTimeout(() => item.remove(), 4200);
}

async function submit(form, task, successMessage) {
  if (form.dataset.pending === "true") return;
  showFormError(form, "");
  setPending(form, true);
  try {
    const result = await task();
    if (result?.id && result?.operation) state.selectedJobId = result.id;
    toast(successMessage);
    await loadBootstrap({ quiet: true });
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
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const name = String(data.get("name") || "").trim();
    submit(form, async () => { await api("/api/sources", { method: "POST", body: { url: String(data.get("url")).trim(), scope: String(data.get("scope")), ...(name ? { name } : {}) } }); form.reset(); }, "Source registered.");
  });
  byId("identity-form").addEventListener("submit", (event) => {
    event.preventDefault(); const form = event.currentTarget;
    submit(form, () => { const identifiers = keyValueObject("identifier-rows"); if (!Object.keys(identifiers).length) throw new Error("Add at least one exact product identifier."); return api("/api/product", { method: "POST", body: { identifiers, required_specs: keyValueObject("spec-rows") } }); }, "Product identity saved.");
  });
  document.querySelectorAll("[data-add-row]").forEach((button) => button.addEventListener("click", () => {
    const target = byId(button.dataset.addRow); addKeyValueRow(target, "", "", target.id === "identifier-rows" ? "model" : "specification", target.id === "identifier-rows" ? "Exact value" : "Required value"); target.lastElementChild.querySelector("input").focus();
  }));
  byId("source-filter").addEventListener("input", () => renderSourceList(true));
  byId("source-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-source-action]"); if (!button) return;
    const operation = button.dataset.sourceAction; button.disabled = true;
    api("/api/jobs", { method: "POST", body: { operation, arguments: operation === "discover" ? { source_id: button.dataset.sourceId, limit: 100 } : { source_id: button.dataset.sourceId, timeout_seconds: 30, max_model_calls: 0 } } })
      .then(() => { toast(`${titleCase(operation)} job queued.`); return loadBootstrap({ quiet: true }); }).catch((error) => toast(error.message)).finally(() => { button.disabled = false; });
  });
  byId("agent-form").addEventListener("submit", (event) => { event.preventDefault(); const form = event.currentTarget; submit(form, () => api("/api/jobs", { method: "POST", body: { operation: "agent", arguments: { max_sources: 3, max_seconds: 120, max_model_calls: 0 } } }), "Research run queued."); });
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
