/* Research plans are durable server documents. Form drafts remain local until explicitly saved. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const tr = (key, args = {}) => window.SourceLedgerI18n.t(`plan.${key}`, args);
  const ui = { plan: null, newRequestMode: false, dirty: false, requestDirty: false, changeDirty: false, removed: new Set(), selection: new Map(), candidateFingerprint: null, historyFingerprint: null, busy: false, stale: false, bootstrap: null, previewJob: null, queuedPreviewId: null, feedback: "" };
  const lineValues = value => String(value || "").split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  const writeLines = value => Array.isArray(value) ? value.join("\n") : "";
  const apiCall = (path, body) => api(path, { method: "POST", body });
  const safeLink = value => safeWebUrl(value);

  function error(message) {
    const target = $("plan-error");
    target.textContent = message;
    target.hidden = !message;
  }
  function requestError(message) {
    const target = $("plan-request-error");
    target.textContent = message;
    target.hidden = !message;
  }
  function status(message) { ui.feedback = message || ""; renderStatus(); }
  function ready() { return typeof ui.bootstrap?.csrf_token === "string" && ui.bootstrap.csrf_token.length > 0; }
  function syncControls() {
    const disabled = ui.busy || !ready();
    for (const id of ["plan-create", "plan-save", "plan-preview", "plan-change", "plan-start", "plan-copy", "plan-new-request", "plan-history"]) $(id).disabled = disabled;
    $("plan-add-form").querySelector('button[type="submit"]').disabled = disabled;
  }
  function setBusy(value) {
    ui.busy = value;
    syncControls();
  }
  function applyPlan(plan) {
    ui.plan = plan;
    ui.newRequestMode = false;
    ui.dirty = false;
    ui.stale = false;
    ui.feedback = "";
    ui.queuedPreviewId = null;
    ui.removed.clear();
    ui.selection = new Map((plan.candidates || []).map(candidate => [candidate.id, candidate.selected === true]));
    ui.candidateFingerprint = null;
    if (!ui.requestDirty) $("plan-request-text").value = plan.request_text || "";
    $("plan-summary").value = plan.summary || "";
    for (const field of ["industry", "product", "market"]) $(`plan-${field}`).value = plan.topic?.[field] || "";
    $("plan-categories").value = writeLines(plan.categories);
    $("plan-include").value = writeLines(plan.include_terms);
    $("plan-exclude").value = writeLines(plan.exclude_terms);
    if (!ui.changeDirty) $("plan-change-text").value = "";
    renderPlan();
  }
  function plansFromBootstrap(bootstrap) {
    const value = bootstrap?.research_plans;
    const plans = Array.isArray(value) ? value : value?.plans;
    return Array.isArray(plans) ? plans : [];
  }
  function renderHistory(plans) {
    const target = $("plan-history");
    $("plan-history-label").hidden = plans.length === 0;
    const selected = ui.newRequestMode ? "" : ui.plan?.id || "";
    const fingerprint = JSON.stringify([selected, plans.map(plan => [plan.id, plan.revision, plan.summary, plan.request_text])]);
    if (fingerprint === ui.historyFingerprint) return;
    ui.historyFingerprint = fingerprint;
    target.replaceChildren();
    const fresh = document.createElement("option");
    fresh.value = "";
    fresh.textContent = tr("newRequest");
    target.append(fresh);
    for (const plan of plans) {
      const option = document.createElement("option");
      option.value = plan.id;
      option.textContent = `${(plan.summary || plan.request_text || plan.id).slice(0, 80)} · r${plan.revision}`;
      target.append(option);
    }
    target.value = selected;
  }
  function resetForNewRequest() {
    ui.plan = null;
    ui.newRequestMode = true;
    ui.dirty = false;
    ui.requestDirty = false;
    ui.changeDirty = false;
    ui.stale = false;
    ui.feedback = "";
    ui.queuedPreviewId = null;
    ui.removed.clear();
    ui.selection.clear();
    $("plan-request-text").value = "";
    $("plan-change-text").value = "";
    $("plan-request-error").hidden = true;
    error("");
    renderHistory(plansFromBootstrap(ui.bootstrap));
    renderPlan();
    $("plan-request-text").focus();
  }
  function openPlan(plan) {
    ui.requestDirty = false;
    ui.changeDirty = false;
    error("");
    applyPlan(plan);
    renderHistory(plansFromBootstrap(ui.bootstrap));
  }
  function planJobs(bootstrap) {
    const value = bootstrap?.plan_jobs;
    return Array.isArray(value) ? value : Array.isArray(value?.jobs) ? value.jobs : [];
  }
  function currentPreviewJob() {
    const plan = ui.plan;
    return planJobs(ui.bootstrap).find(job => job.operation === "plan_preview"
      && (job.arguments?.plan_id === plan?.id || job.plan_id === plan?.id)
      && (job.status === "succeeded"
        ? job.arguments?.expected_revision + 1 === plan?.revision
        : job.arguments?.expected_revision === plan?.revision)) || null;
  }
  function completedEmptyPreview() {
    return ui.plan?.state === "draft" && !(ui.plan.candidates || []).length
      && ui.previewJob?.status === "succeeded";
  }
  function render(bootstrap) {
    ui.bootstrap = bootstrap;
    renderAiPath();
    const plans = plansFromBootstrap(bootstrap);
    if (!ui.newRequestMode) {
      const current = ui.plan ? plans.find(plan => plan.id === ui.plan.id) : plans[0];
      if (current && !ui.plan) applyPlan(current);
      else if (current && current.revision !== ui.plan.revision) {
        if (ui.dirty || ui.changeDirty || (ui.requestDirty && $("plan-request-text").value.trim() !== ui.plan.request_text)) {
          ui.stale = true;
          error(tr("stale"));
        } else applyPlan(current);
      }
    }
    renderHistory(plans);
    ui.previewJob = currentPreviewJob();
    if (ui.previewJob?.id === ui.queuedPreviewId) {
      ui.feedback = "";
      if (["succeeded", "failed", "interrupted"].includes(ui.previewJob.status)) ui.queuedPreviewId = null;
    }
    renderPlan();
    syncControls();
  }
  function el(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }
  function link(url, label) {
    const safe = safeLink(url);
    if (!safe) return el("span", "plan-url-invalid", url || "—");
    const a = el("a", "plan-url", label);
    a.href = safe;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.title = safe;
    return a;
  }
  function renderCandidates() {
    const fingerprint = JSON.stringify([ui.plan?.revision, ui.previewJob?.id, ui.previewJob?.status,
      [...ui.selection], [...ui.removed], window.SourceLedgerI18n.getLanguage()]);
    if (fingerprint === ui.candidateFingerprint) return;
    ui.candidateFingerprint = fingerprint;
    const target = $("plan-candidates");
    target.replaceChildren();
    const candidates = (ui.plan?.candidates || []).filter(candidate => !ui.removed.has(candidate.id));
    if (!candidates.length) {
      target.append(el("p", "plan-empty", tr(completedEmptyPreview() ? "emptyAfterPreview" : "empty")));
      return;
    }
    for (const candidate of candidates) {
      const card = el("article", "plan-candidate");
      card.dataset.candidateId = candidate.id;
      const top = el("div", "plan-candidate-head");
      top.append(el("strong", "", candidate.name || candidate.url), el("span", "badge info", tr(`kind.${candidate.kind || "site"}`)));
      card.append(top);
      if (candidate.reason) card.append(el("p", "plan-reason", candidate.reason));
      const refs = el("div", "plan-refs");
      refs.append(el("span", "plan-ref-label", tr("sourceLink")), link(candidate.url, candidate.url || "—"));
      if (candidate.evidence_url && candidate.evidence_url !== candidate.url) refs.append(el("span", "plan-ref-label", tr("evidence")), link(candidate.evidence_url, candidate.evidence_url));
      card.append(refs);
      const actions = el("div", "plan-candidate-actions");
      const label = el("label", "checkbox-label");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.dataset.planSelect = candidate.id;
      check.checked = ui.selection.get(candidate.id) === true;
      label.append(check, el("span", "", tr("select")));
      const remove = el("button", "text-button", tr("remove"));
      remove.type = "button";
      remove.dataset.planRemove = candidate.id;
      actions.append(label, remove);
      card.append(actions);
      target.append(card);
    }
  }
  function updateBudget() {
    if (!ui.plan) return;
    const count = [...ui.selection].filter(([id, selected]) => selected && !ui.removed.has(id)).length;
    $("plan-budget").textContent = tr("budget", { count, pages: Math.min(50, Math.max(10, count)) });
  }
  function handoffText() {
    const plan = ui.plan;
    if (!plan) return "";
    return `Use SourceLedger host tools to prepare a research preview for plan ${plan.id} at revision ${plan.revision}. First call get_research_plan(plan_id=${plan.id}) and read its current revision, request, edited topic, summary, categories, text filters, selected and manually added candidates, and excluded_urls. Respect all of those choices. Search or browse for real product and source URLs. Preserve the full research request and its detailed conditions. Distinguish reference entities from research targets: for competitors of A, exclude A's own products and prices unless explicitly requested; for compare A and its competitors, include both. State targets and exclusions in summary and note, not literal text filters. Leave unspecified markets empty; never copy unrelated workspace defaults. If the relationship is ambiguous, leave candidates empty and explain the alternatives for review. Never invent URLs, identifiers, prices, currencies, or commercial terms. Never reintroduce an excluded URL. Submit only unverified suggestions; do not start research. Call submit_research_preview with plan_id=${plan.id}, expected_revision=${plan.revision}, and preview {summary, topic:{industry,product,market}, categories, include_terms, exclude_terms, candidates:[{name,url,evidence_url,reason,kind}], note}. If the current server revision differs from ${plan.revision}, stop and ask the user to review the updated plan. The user must review and explicitly confirm before start_research_plan. Include/exclude terms are literal text filters, not proof that detailed requirements are met.\n\nResearch request:\n${plan.request_text}\n\nCurrent topic: ${JSON.stringify(plan.topic || {})}\nCurrent summary: ${plan.summary || ""}\nCurrent categories: ${JSON.stringify(plan.categories || [])}\nCurrent include text: ${JSON.stringify(plan.include_terms || [])}\nCurrent exclude text: ${JSON.stringify(plan.exclude_terms || [])}\nSelected source URLs: ${JSON.stringify((plan.candidates || []).filter(item => item.selected).map(item => item.url))}\nManually added candidates: ${JSON.stringify((plan.candidates || []).filter(item => item.origin === "user").map(item => ({ name: item.name, url: item.url, selected: item.selected })))}\nRemoved source URLs: ${JSON.stringify(plan.excluded_urls || [])}`;
  }
  function renderAiPath() {
    const local = $("plan-ai-path").value === "local";
    $("plan-local-info").hidden = !local;
    if (local) {
      const settings = aiSettings();
      const provider = aiProvider(settings.provider);
      const info = $("plan-local-info");
      info.replaceChildren(document.createTextNode(tr("localSettings", {
        provider: provider?.label || settings.provider,
        model: aiModelLabel(settings.provider, settings.model),
      }) + " "));
      const anchor = el("a", "", i18n.t("ui.connections"));
      anchor.href = "#connections";
      info.append(anchor);
    }
    $("plan-preview").textContent = tr(local ? "regenerate" : "copyForHost");
    $("plan-create").textContent = tr(local ? "create" : "prepare");
  }
  function renderStatus() {
    const plan = ui.plan;
    if (!plan) return;
    const pending = ui.previewJob && ["queued", "running", "starting"].includes(ui.previewJob.status);
    const failed = ui.previewJob && ["failed", "interrupted"].includes(ui.previewJob.status);
    const failureDetail = typeof ui.previewJob?.error?.message === "string" ? ui.previewJob.error.message : "";
    const message = ui.feedback || (pending ? tr("previewPending") : failed ? [tr("previewFailed"), failureDetail].filter(Boolean).join(" ")
      : completedEmptyPreview() ? tr("previewEmpty") : plan.state === "preview" ? tr("previewReady") : "");
    const target = $("plan-status");
    target.replaceChildren();
    if (message) target.append(document.createTextNode(message));
    if (plan.note) {
      if (message) target.append(document.createElement("br"));
      target.append(document.createTextNode(plan.note));
    }
  }
  function renderPlan() {
    const plan = ui.plan;
    $("plan-workspace").hidden = !plan;
    $("plan-new-request").hidden = !plan;
    if (!plan) return;
    $("plan-status-title").textContent = plan.summary || tr("newPlan");
    $("plan-state-badge").textContent = tr(plan.state || "draft");
    if (!ui.dirty && !ui.stale) renderStatus();
    renderCandidates();
    updateBudget();
    $("plan-copy-text").textContent = handoffText();
  }
  function changes() {
    const plan = ui.plan;
    const choices = [...document.querySelectorAll("[data-plan-select]")];
    return {
      summary: $("plan-summary").value.trim(),
      topic: Object.fromEntries(["industry", "product", "market"].map(field => [field, $(`plan-${field}`).value.trim()])),
      categories: lineValues($("plan-categories").value),
      include_terms: lineValues($("plan-include").value),
      exclude_terms: lineValues($("plan-exclude").value),
      selected_candidate_ids: choices.filter(box => box.checked).map(box => box.dataset.planSelect),
      removed_candidate_ids: [...ui.removed],
    };
  }
  async function save(extra = {}) {
    if (!ui.plan) throw new Error(tr("noRequest"));
    if (ui.stale) throw new Error(tr("stale"));
    if (!ui.dirty && !Object.keys(extra).length) return ui.plan;
    const result = await apiCall("/api/plans/edit", { plan_id: ui.plan.id, expected_revision: ui.plan.revision, changes: { ...changes(), ...extra } });
    applyPlan(result.plan);
    return result.plan;
  }
  async function saveIncludingRequest(extra = {}) {
    const request = $("plan-request-text").value.trim();
    const changes = { ...extra };
    if (ui.requestDirty && request !== ui.plan?.request_text && !Object.hasOwn(changes, "request_text")) {
      if (!request) throw new Error(tr("noRequest"));
      changes.request_text = request;
    }
    const plan = await save(changes);
    ui.requestDirty = false;
    $("plan-request-text").value = plan.request_text || "";
    return plan;
  }
  async function preview() {
    const plan = ui.plan;
    if ($("plan-ai-path").value === "host") {
      $("plan-workspace").querySelector(".plan-handoff details").open = true;
      status(tr("hostReady"));
      $("plan-copy").focus({ preventScroll: true });
      return;
    }
    const settings = aiSettings();
    if (aiProvider(settings.provider)?.available !== true) {
      status(tr("providerUnavailable"));
      return;
    }
    const result = await apiCall("/api/plans/preview", {
      plan_id: plan.id, expected_revision: plan.revision,
      provider: settings.provider, model: settings.model || "", timeout_seconds: settings.timeout_seconds || 180,
    });
    status(tr("queued"));
    ui.queuedPreviewId = result.job?.id || null;
    await loadBootstrap({ quiet: true });
  }
  async function act(task, { request = false } = {}) {
    if (ui.busy) return;
    error("");
    requestError("");
    if (!ready()) {
      if (request || !ui.plan) requestError(tr("notReady")); else error(tr("notReady"));
      return;
    }
    setBusy(true);
    try { await task(); }
    catch (failure) {
      if (failure.status === 409 || /revision|stale|plan changed|changed while/i.test(failure.rawMessage || "")) ui.stale = true;
      const message = ui.stale ? tr("stale") : failure.message;
      if (request || !ui.plan) requestError(message); else error(message);
    } finally { setBusy(false); }
  }
  function validateTopicAndTargets() {
    const topic = changes().topic;
    if (Object.values(topic).some(value => !value)) throw new Error(tr("needTopic"));
    const selected = [...document.querySelectorAll("[data-plan-select]:checked")];
    if (!selected.length) throw new Error(tr("needCandidate"));
    if (selected.length > 50) throw new Error(tr("tooManyCandidates"));
    for (const box of selected) {
      const candidate = ui.plan.candidates.find(item => item.id === box.dataset.planSelect);
      if (!candidate || !safeLink(candidate.url)) throw new Error(tr("invalidUrl"));
    }
  }
  function install() {
    $("plan-ai-path").addEventListener("change", renderAiPath);
    $("plan-new-request").addEventListener("click", resetForNewRequest);
    $("plan-history").addEventListener("change", event => {
      const id = event.target.value;
      if (!id) resetForNewRequest();
      else {
        const plan = plansFromBootstrap(ui.bootstrap).find(item => item.id === id);
        if (plan) openPlan(plan);
      }
    });
    $("plan-request-text").addEventListener("input", () => { ui.requestDirty = true; if (!ui.plan) ui.newRequestMode = true; });
    $("plan-change-text").addEventListener("input", () => { ui.changeDirty = true; });
    $("plan-request-form").addEventListener("submit", event => {
      event.preventDefault();
      const request = $("plan-request-text").value.trim();
      if (!request) { requestError(tr("noRequest")); return; }
      requestError("");
      act(async () => {
        if (ui.plan && !ui.newRequestMode) {
          await saveIncludingRequest();
        } else {
          const result = await apiCall("/api/plans", { request_text: request });
          ui.requestDirty = false;
          applyPlan(result.plan);
          await loadBootstrap({ quiet: true });
        }
        await preview();
      }, { request: true });
    });
    $("plan-workspace").addEventListener("input", event => {
      if (event.target.matches("[data-plan-edit]")) ui.dirty = true;
    });
    $("plan-candidates").addEventListener("change", event => {
      if (event.target.matches("[data-plan-select]")) { ui.selection.set(event.target.dataset.planSelect, event.target.checked); ui.dirty = true; ui.candidateFingerprint = null; updateBudget(); }
    });
    $("plan-candidates").addEventListener("click", event => {
      const button = event.target.closest("[data-plan-remove]");
      if (!button) return;
      ui.removed.add(button.dataset.planRemove);
      ui.dirty = true;
      renderCandidates();
      updateBudget();
    });
    $("plan-save").addEventListener("click", () => act(async () => { await saveIncludingRequest(); status(tr("saved")); await loadBootstrap({ quiet: true }); }));
    $("plan-preview").addEventListener("click", () => act(async () => { await saveIncludingRequest(); await preview(); }));
    $("plan-change").addEventListener("click", () => act(async () => {
      const direction = $("plan-change-text").value.trim();
      if (!direction) throw new Error(tr("needChange"));
      const baseRequest = $("plan-request-text").value.trim() || ui.plan.request_text;
      await saveIncludingRequest({ request_text: `${baseRequest}\n\nAdditional direction:\n${direction}` });
      ui.changeDirty = false;
      $("plan-change-text").value = "";
      await preview();
    }));
    $("plan-add-form").addEventListener("submit", event => {
      event.preventDefault();
      act(async () => {
        const form = event.currentTarget;
        const data = new FormData(form);
        const url = String(data.get("url") || "").trim();
        if (!safeLink(url)) throw new Error(tr("invalidUrl"));
        await saveIncludingRequest({ added_candidates: [{ name: String(data.get("name") || "").trim() || url, url, evidence_url: url, reason: String(data.get("reason") || "").trim(), kind: "site" }] });
        form.reset();
        status(tr("saved"));
        await loadBootstrap({ quiet: true });
      });
    });
    $("plan-copy").addEventListener("click", () => act(async () => {
      if ([...$("plan-add-form").querySelectorAll("input")].some(input => input.value.trim())) throw new Error(tr("unsavedChange"));
      const direction = $("plan-change-text").value.trim();
      if (direction) {
        const baseRequest = $("plan-request-text").value.trim() || ui.plan.request_text;
        await saveIncludingRequest({ request_text: `${baseRequest}\n\nAdditional direction:\n${direction}` });
        ui.changeDirty = false;
        $("plan-change-text").value = "";
      } else await saveIncludingRequest();
      $("plan-copy-text").textContent = handoffText();
      try { await navigator.clipboard.writeText(handoffText()); status(tr("copied")); }
      catch { status(tr("copyFailed")); }
      await loadBootstrap({ quiet: true });
    }));
    $("plan-start").addEventListener("click", () => act(async () => {
      if (ui.requestDirty && $("plan-request-text").value.trim() !== ui.plan.request_text) throw new Error(tr("unsavedRequest"));
      if ($("plan-change-text").value.trim() || [...$("plan-add-form").querySelectorAll("input")].some(input => input.value.trim())) throw new Error(tr("unsavedChange"));
      validateTopicAndTargets();
      let plan = await save();
      if (plan.state !== "confirmed") {
        const confirmed = await apiCall("/api/plans/confirm", { plan_id: plan.id, expected_revision: plan.revision, user_confirmed: true });
        plan = confirmed.plan;
        applyPlan(plan);
      }
      const selectedCount = plan.candidates.filter(candidate => candidate.selected).length;
      const started = await apiCall("/api/plans/start", { plan_id: plan.id, expected_revision: plan.revision, max_pages: Math.min(50, Math.max(10, selectedCount)), max_seconds: 120 });
      status(tr("started"));
      await loadBootstrap({ quiet: true });
      if (started.job?.id) {
        location.hash = "#runs";
        await selectJob(started.job.id);
      }
    }));
  }
  function languageChanged() {
    renderAiPath();
    ui.historyFingerprint = null;
    renderHistory(plansFromBootstrap(ui.bootstrap));
    renderPlan();
    // Static labels are translated by the shared i18n layer; inputs are intentionally untouched.
  }
  install();
  syncControls();
  window.SourceLedgerPlanUI = Object.freeze({ render, languageChanged });
})();
