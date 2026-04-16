const ACTIVE_RUN_KEY = "hoodline_active_pipeline_run";
const steps = Array.isArray(window.PIPELINE_STEPS) ? window.PIPELINE_STEPS : [];

const state = {
  runId: null,
  runData: null,
  pollTimer: null,
};

const els = {
  newRunBtn: document.getElementById("new-run-btn"),
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

  const stored = localStorage.getItem(ACTIVE_RUN_KEY);
  if (stored) {
    state.runId = stored;
    refreshRun().then(() => {
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
      label.appendChild(el);
      form.appendChild(label);
    }

    const button = node.querySelector(".run-step-btn");
    button.addEventListener("click", () => runStep(step.id));

    els.stepsContainer.appendChild(node);
  });
}

async function createRun() {
  try {
    const result = await requestJson("/api/pipeline/runs", { method: "POST" });
    state.runId = result.run_id;
    state.runData = result;
    localStorage.setItem(ACTIVE_RUN_KEY, state.runId);

    els.outputView.textContent = "Run created. Ready for first step.";
    renderRunBanner();
    updateStepStates();
    await loadLogs();
    startPolling();
  } catch (err) {
    showError(err);
  }
}

async function refreshRun() {
  if (!state.runId) return;

  const result = await requestJson(`/api/pipeline/runs/${state.runId}`);
  state.runData = result;
  renderRunBanner();
  updateStepStates();
  renderOutputs();
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
    payload.inputs[input.name] = input.value;
  }

  const button = card.querySelector(".run-step-btn");
  button.disabled = true;

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

    els.outputView.textContent = JSON.stringify(result.last_output, null, 2);
    renderRunBanner();
    updateStepStates();
    await loadLogs();
  } catch (err) {
    showError(err);
  } finally {
    button.disabled = false;
  }
}

function renderRunBanner() {
  if (!state.runId || !state.runData) {
    els.runBanner.textContent = "No active run";
    return;
  }

  if (state.runData.completed) {
    els.runBanner.textContent = `Run ${state.runId} complete.`;
    return;
  }

  els.runBanner.textContent = `Run ${state.runId} | Next step: ${state.runData.next_step}`;
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
      stepState.textContent = "Ready";
      stepState.className = "pill status-active";
      button.disabled = false;
      formElements.forEach((el) => {
        el.disabled = false;
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

function renderOutputs() {
  if (!state.runData || !Array.isArray(state.runData.outputs) || !state.runData.outputs.length) {
    els.outputView.textContent = "No step output yet.";
    return;
  }

  els.outputView.textContent = JSON.stringify(state.runData.outputs, null, 2);
}

function startPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
  }

  state.pollTimer = setInterval(async () => {
    if (!state.runId) return;
    try {
      await refreshRun();
      await loadLogs();
    } catch {
      // Keep polling lightweight; explicit user action will surface errors.
    }
  }, 3000);
}

async function loadLogs() {
  if (!state.runId) {
    els.logsView.textContent = "Start a run to stream logs.";
    return;
  }

  const result = await requestJson(`/api/pipeline/runs/${state.runId}/logs?lines=200`);
  if (!Array.isArray(result.logs) || !result.logs.length) {
    els.logsView.textContent = "No logs for this run yet.";
    return;
  }

  const lines = result.logs.map((entry) => {
    const ts = entry.timestamp || "";
    const step = entry.step_id || "system";
    const msg = entry.message || "";
    const payload = entry.payload ? `\n${JSON.stringify(entry.payload, null, 2)}` : "";
    return `${ts} [${step}] ${msg}${payload}`;
  });

  els.logsView.textContent = lines.join("\n\n");
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
  els.outputView.textContent = `Error: ${err.message}`;
}
