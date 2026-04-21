const ACTIVE_RUN_KEY = "hoodline_active_pipeline_run";
const steps = Array.isArray(window.PIPELINE_STEPS) ? window.PIPELINE_STEPS : [];

const state = {
  runId: null,
  runData: null,
  pollTimer: null,
  autoRunning: false,
};

const els = {
  newRunBtn: document.getElementById("new-run-btn"),
  runAllBtn: document.getElementById("run-all-btn"),
  runBanner: document.getElementById("run-banner"),
  stepsContainer: document.getElementById("steps-container"),
  stepTemplate: document.getElementById("step-template"),
  logsView: document.getElementById("logs-view"),
  outputView: document.getElementById("output-view"),
};

init();

function init() {
  if (!els.stepsContainer) return;

  renderStepCards();
  els.newRunBtn.addEventListener("click", createRun);
  if (els.runAllBtn) {
    els.runAllBtn.addEventListener("click", runAllSteps);
  }

  const stored = localStorage.getItem(ACTIVE_RUN_KEY);
  if (stored) {
    state.runId = stored;
    refreshRun().then(() => {
      loadLogs();
      startPolling();
    }).catch(() => {
      state.runId = null;
      localStorage.removeItem(ACTIVE_RUN_KEY);
      renderRunBanner();
      updateStepStates();
    });
  } else {
    renderRunBanner();
    updateStepStates();
  }
}

function renderStepCards() {
  els.stepsContainer.innerHTML = "";
  steps.forEach((step) => {
    const node = els.stepTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.stepId = step.id;

    node.querySelector(".step-label").textContent = step.label;
    node.querySelector(".step-desc").textContent = step.description;

    const form = node.querySelector(".step-form");

    if (step.id === "gmail_intake") {
      renderGmailIntakeForm(form, step);
    } else {
      for (const field of step.inputs) {
        const label = document.createElement("label");
        label.textContent = field.label;

        const el = field.type === "textarea" ? document.createElement("textarea") : document.createElement("input");
        if (field.type !== "textarea") {
          el.type = field.type || "text";
        }
        el.name = field.key;
        if (field.required) {
          el.required = true;
        }
        if (field.default) {
          el.value = field.default;
        }
        label.appendChild(el);
        form.appendChild(label);
      }
    }

    const button = node.querySelector(".run-step-btn");
    button.addEventListener("click", () => runStep(step.id));

    els.stepsContainer.appendChild(node);
  });
}

function renderGmailIntakeForm(form, step) {
  // Status banner
  const statusDiv = document.createElement("div");
  statusDiv.className = "intake-status";
  statusDiv.innerHTML = `
    <p><span class="pill status-done">Gmail API configured</span>
    contact@hoodline.com &mdash; label:auto/correction-candidate is:unread</p>
  `;
  form.appendChild(statusDiv);

  // Hidden mode input (defaults to gmail_api)
  const modeInput = document.createElement("input");
  modeInput.type = "hidden";
  modeInput.name = "mode";
  modeInput.value = "gmail_api";
  modeInput.dataset.alwaysSend = "true";
  form.appendChild(modeInput);

  // Max results
  const maxLabel = document.createElement("label");
  maxLabel.textContent = "Number of emails to fetch (1-10)";
  const maxInput = document.createElement("input");
  maxInput.type = "number";
  maxInput.name = "max_results";
  maxInput.value = "5";
  maxInput.min = "1";
  maxInput.max = "10";
  maxInput.dataset.alwaysSend = "true";
  maxLabel.appendChild(maxInput);
  form.appendChild(maxLabel);

  // Manual mode toggle
  const toggleLabel = document.createElement("label");
  toggleLabel.className = "toggle-label";
  const toggleCheck = document.createElement("input");
  toggleCheck.type = "checkbox";
  toggleCheck.className = "manual-toggle";
  toggleCheck.dataset.skipSend = "true";
  toggleLabel.appendChild(toggleCheck);
  toggleLabel.appendChild(document.createTextNode(" Switch to manual mode"));
  form.appendChild(toggleLabel);

  // Manual fields (hidden by default)
  const manualFields = document.createElement("div");
  manualFields.className = "manual-fields";
  manualFields.style.display = "none";

  const manualInputs = [
    { key: "sender", label: "Sender email", type: "text" },
    { key: "subject", label: "Email subject", type: "text" },
    { key: "body", label: "Email body", type: "textarea" },
  ];
  for (const field of manualInputs) {
    const label = document.createElement("label");
    label.textContent = field.label;
    const el = field.type === "textarea" ? document.createElement("textarea") : document.createElement("input");
    if (field.type !== "textarea") el.type = "text";
    el.name = field.key;
    label.appendChild(el);
    manualFields.appendChild(label);
  }
  form.appendChild(manualFields);

  toggleCheck.addEventListener("change", () => {
    if (toggleCheck.checked) {
      modeInput.value = "manual";
      manualFields.style.display = "block";
      maxLabel.style.display = "none";
      statusDiv.style.display = "none";
    } else {
      modeInput.value = "gmail_api";
      manualFields.style.display = "none";
      maxLabel.style.display = "block";
      statusDiv.style.display = "block";
    }
  });
}

async function createRun() {
  try {
    const result = await requestJson("/api/pipeline/runs", { method: "POST" });
    state.runId = result.run_id;
    state.runData = result;
    localStorage.setItem(ACTIVE_RUN_KEY, state.runId);

    renderRunBanner();
    updateStepStates();
    startPolling();

    // Load logs after a brief delay to ensure the file is flushed
    setTimeout(() => loadLogs(), 300);
  } catch (err) {
    showError(err);
  }
}

async function runAllSteps() {
  if (state.autoRunning) return;

  try {
    const result = await requestJson("/api/pipeline/runs", { method: "POST" });
    state.runId = result.run_id;
    state.runData = result;
    localStorage.setItem(ACTIVE_RUN_KEY, state.runId);
    renderRunBanner();
    updateStepStates();
    startPolling();
  } catch (err) {
    showError(err);
    return;
  }

  state.autoRunning = true;
  if (els.runAllBtn) {
    els.runAllBtn.disabled = true;
    els.runAllBtn.textContent = "Running...";
  }
  els.newRunBtn.disabled = true;
  renderRunBanner();

  try {
    for (let i = 0; i < steps.length; i++) {
      if (!state.runData || state.runData.completed) break;
      if (state.runData.current_index !== i) break;

      const stepId = steps[i].id;
      renderRunBanner();
      await runStep(stepId);

      await new Promise((resolve) => setTimeout(resolve, 800));
    }
  } catch (err) {
    showError(err);
  } finally {
    state.autoRunning = false;
    if (els.runAllBtn) {
      els.runAllBtn.disabled = false;
      els.runAllBtn.textContent = "Run All Steps";
    }
    els.newRunBtn.disabled = false;
    renderRunBanner();
    updateStepStates();
  }
}

async function refreshRun() {
  if (!state.runId) return;

  const result = await requestJson(`/api/pipeline/runs/${state.runId}`);
  state.runData = result;
  renderRunBanner();
  updateStepStates();
}

async function runStep(stepId) {
  if (!state.runId) {
    showError(new Error("Start a run first."));
    return;
  }

  const card = document.querySelector(`.step-card[data-step-id="${stepId}"]`);
  if (!card) return;

  const formInputs = card.querySelectorAll("input, textarea");
  const payload = { inputs: {} };
  for (const input of formInputs) {
    // Skip checkboxes used for UI toggling
    if (input.dataset.skipSend) continue;
    // Always send inputs marked as alwaysSend, or inputs with values
    if (input.dataset.alwaysSend || input.value) {
      payload.inputs[input.name] = input.value;
    }
  }

  const button = card.querySelector(".run-step-btn");
  button.disabled = true;

  const stepState = card.querySelector(".step-state");
  stepState.textContent = "Running...";
  stepState.className = "pill status-running";

  try {
    const result = await requestJson(`/api/pipeline/runs/${state.runId}/steps/${stepId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    state.runData = {
      run_id: result.run_id,
      current_index: result.current_index,
      next_step: result.next_step,
      completed: result.completed,
      outputs: result.outputs,
    };

    const output = result.last_output;
    renderStepOutput(stepId, output);

    renderRunBanner();
    updateStepStates();
    await loadLogs();
  } catch (err) {
    showError(err);
    throw err;
  } finally {
    button.disabled = false;
  }
}

function renderStepOutput(stepId, output) {
  if (!output) {
    els.outputView.textContent = "Step completed (no output).";
    return;
  }

  // Special rendering for gmail_intake: show emails in accordion
  if (stepId === "gmail_intake") {
    const cases = output.output?.cases || output.cases || [];
    if (cases.length > 0) {
      renderEmailAccordion(cases);
      return;
    }
  }

  // Default: show formatted JSON
  const casesCount = output.output?.total || output.output?.cases?.length;
  if (casesCount) {
    els.outputView.innerHTML = "";
    els.outputView.textContent = `${stepId}: processed ${casesCount} case(s)\n\n` +
      JSON.stringify(output, null, 2);
  } else {
    els.outputView.innerHTML = "";
    els.outputView.textContent = JSON.stringify(output, null, 2);
  }
}

function renderEmailAccordion(cases) {
  els.outputView.innerHTML = "";

  const wrapper = document.createElement("div");
  wrapper.className = "email-accordion";

  const heading = document.createElement("h3");
  heading.textContent = `Fetched ${cases.length} email${cases.length !== 1 ? "s" : ""}`;
  heading.style.margin = "0 0 0.75rem 0";
  wrapper.appendChild(heading);

  cases.forEach((c, idx) => {
    const item = document.createElement("details");
    item.className = "email-item";
    if (idx === 0) item.open = true;

    const summary = document.createElement("summary");
    summary.className = "email-summary";

    const num = document.createElement("span");
    num.className = "email-num";
    num.textContent = `#${idx + 1}`;

    const subjectText = document.createElement("span");
    subjectText.className = "email-subject";
    subjectText.textContent = c.subject || "(no subject)";

    const senderText = document.createElement("span");
    senderText.className = "email-sender";
    senderText.textContent = c.sender || "unknown";

    const candidatePill = document.createElement("span");
    candidatePill.className = c.is_candidate ? "pill status-done" : "pill";
    candidatePill.textContent = c.is_candidate ? "candidate" : "not matched";

    summary.appendChild(num);
    summary.appendChild(subjectText);
    summary.appendChild(candidatePill);
    item.appendChild(summary);

    const body = document.createElement("div");
    body.className = "email-body";

    const meta = document.createElement("div");
    meta.className = "email-meta";
    meta.innerHTML = `
      <p><strong>From:</strong> ${escapeHtml(c.sender || "")}</p>
      <p><strong>Subject:</strong> ${escapeHtml(c.subject || "")}</p>
      ${c.matched_keywords?.length ? `<p><strong>Keywords:</strong> ${c.matched_keywords.map(k => `<code>${escapeHtml(k)}</code>`).join(", ")}</p>` : ""}
      ${c.case_id ? `<p><strong>Case ID:</strong> <code>${escapeHtml(c.case_id)}</code></p>` : ""}
      ${c.gmail_message_id ? `<p><strong>Gmail ID:</strong> <code>${escapeHtml(c.gmail_message_id)}</code></p>` : ""}
    `;
    body.appendChild(meta);

    const bodyContent = document.createElement("pre");
    bodyContent.className = "email-content";
    bodyContent.textContent = c.body || "(empty body)";
    body.appendChild(bodyContent);

    item.appendChild(body);
    wrapper.appendChild(item);
  });

  els.outputView.appendChild(wrapper);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function renderRunBanner() {
  if (!state.runId || !state.runData) {
    els.runBanner.textContent = "No active run";
    return;
  }

  if (state.autoRunning) {
    const idx = state.runData.current_index;
    const stepLabel = idx < steps.length ? steps[idx].label : "finishing";
    els.runBanner.textContent = `Run ${state.runId.slice(0, 8)}... | Auto-running: ${stepLabel}`;
    return;
  }

  if (state.runData.completed) {
    els.runBanner.textContent = `Run ${state.runId.slice(0, 8)}... | Complete`;
    return;
  }

  els.runBanner.textContent = `Run ${state.runId.slice(0, 8)}... | Next step: ${state.runData.next_step}`;
}

function updateStepStates() {
  const cards = document.querySelectorAll(".step-card");

  cards.forEach((card, idx) => {
    const stepState = card.querySelector(".step-state");
    const button = card.querySelector(".run-step-btn");
    const formElements = card.querySelectorAll("input, textarea");

    if (!state.runData) {
      stepState.textContent = "Pending";
      stepState.className = "pill";
      button.disabled = true;
      formElements.forEach((el) => {
        el.disabled = true;
      });
      return;
    }

    if (idx < state.runData.current_index) {
      stepState.textContent = "Done";
      stepState.className = "pill status-done";
      button.disabled = true;
      formElements.forEach((el) => {
        el.disabled = true;
      });
      return;
    }

    if (idx === state.runData.current_index && !state.runData.completed) {
      if (stepState.textContent !== "Running...") {
        stepState.textContent = "Ready";
        stepState.className = "pill status-active";
      }
      button.disabled = state.autoRunning;
      formElements.forEach((el) => {
        if (!el.dataset.skipSend) {
          el.disabled = state.autoRunning;
        }
      });
      return;
    }

    stepState.textContent = state.runData.completed ? "Done" : "Locked";
    stepState.className = state.runData.completed ? "pill status-done" : "pill";
    button.disabled = true;
    formElements.forEach((el) => {
      el.disabled = true;
    });
  });
}

function startPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
  }

  state.pollTimer = setInterval(() => {
    if (!state.runId) return;

    // Run refresh and logs independently so one failing doesn't block the other
    refreshRun().catch(() => {});
    loadLogs().catch(() => {});
  }, 3000);
}

async function loadLogs() {
  if (!state.runId) {
    els.logsView.textContent = "Start a run to stream logs.";
    return;
  }

  const result = await requestJson(`/api/pipeline/runs/${state.runId}/logs?lines=200`);
  if (!Array.isArray(result.logs) || !result.logs.length) {
    els.logsView.textContent = "Waiting for logs...";
    return;
  }

  const lines = result.logs.map((entry) => {
    const ts = entry.timestamp ? entry.timestamp.split("T")[1]?.split(".")[0] || entry.timestamp : "";
    const step = entry.step_id || "system";
    const msg = entry.message || "";
    return `${ts} [${step}] ${msg}`;
  });

  els.logsView.textContent = lines.join("\n");
  els.logsView.scrollTop = els.logsView.scrollHeight;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
  });

  if (response.status === 401) {
    window.location.href = "/login?next=/pipeline";
    throw new Error("Unauthorized");
  }

  if (!response.ok) {
    const payload = await safeJson(response);
    const message = payload?.detail || `${response.status} ${response.statusText}`;
    throw new Error(message);
  }

  return response.json();
}

async function safeJson(response) {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function showError(err) {
  els.outputView.innerHTML = "";
  els.outputView.textContent = `Error: ${err.message}`;
}
