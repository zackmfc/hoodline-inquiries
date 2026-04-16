const STORAGE_KEY = "hoodline_schedule_checks_v1";

const TASKS = [
  { id: "foundation", title: "Foundation: Docker + Postgres + migrations + service skeleton", start: "2026-04-16", end: "2026-04-17", min: 10, max: 12 },
  { id: "gmail_intake", title: "3.1 Gmail intake", start: "2026-04-20", end: "2026-04-20", min: 6, max: 8 },
  { id: "classifier", title: "3.2 Classifier", start: "2026-04-21", end: "2026-04-21", min: 4, max: 6 },
  { id: "resolver", title: "3.3 Article resolver", start: "2026-04-22", end: "2026-04-23", min: 10, max: 14 },
  { id: "fetcher", title: "3.4 Article fetcher", start: "2026-04-24", end: "2026-04-24", min: 8, max: 10 },
  { id: "verification", title: "3.5 Verification agent", start: "2026-04-27", end: "2026-04-29", min: 18, max: 24 },
  { id: "remediation", title: "3.6 Remediation classifier + note writer", start: "2026-04-30", end: "2026-04-30", min: 5, max: 7 },
  { id: "stager", title: "3.7 Draft stager", start: "2026-05-01", end: "2026-05-04", min: 12, max: 16 },
  { id: "review_dashboard", title: "3.8 Review dashboard", start: "2026-05-05", end: "2026-05-07", min: 14, max: 18 },
  { id: "integration", title: "End-to-end integration, smoke tests, docs, runbooks", start: "2026-05-08", end: "2026-05-08", min: 6, max: 8 }
];

const state = {
  checks: loadChecks(),
  hoursPerDay: 6,
};

const els = {
  currentTime: document.getElementById("current-time"),
  taskList: document.getElementById("task-list"),
  taskTemplate: document.getElementById("task-template"),
  taskCount: document.getElementById("task-count"),
  doneCount: document.getElementById("done-count"),
  meterFill: document.getElementById("meter-fill"),
  remainingHours: document.getElementById("remaining-hours"),
  projectedFinish: document.getElementById("projected-finish"),
  timelineHealth: document.getElementById("timeline-health"),
  hoursPerDay: document.getElementById("hours-per-day"),
  resetBtn: document.getElementById("reset-btn"),
};

init();

function init() {
  renderTasks();
  renderSummary();
  updateClock();
  setInterval(updateClock, 1000);
  setInterval(renderSummary, 60000);

  els.hoursPerDay.addEventListener("input", () => {
    const value = Number.parseInt(els.hoursPerDay.value, 10);
    state.hoursPerDay = Number.isFinite(value) && value > 0 ? value : 6;
    renderSummary();
  });

  els.resetBtn.addEventListener("click", () => {
    state.checks = {};
    saveChecks(state.checks);
    renderTasks();
    renderSummary();
  });
}

function renderTasks() {
  const now = new Date();
  els.taskList.innerHTML = "";

  TASKS.forEach((task) => {
    const node = els.taskTemplate.content.firstElementChild.cloneNode(true);
    const checkbox = node.querySelector(".task-check");
    const title = node.querySelector(".task-title");
    const dates = node.querySelector(".task-dates");
    const hours = node.querySelector(".task-hours");
    const statusEl = node.querySelector(".task-status");

    const checked = Boolean(state.checks[task.id]);
    checkbox.checked = checked;
    checkbox.addEventListener("change", () => {
      state.checks[task.id] = checkbox.checked;
      saveChecks(state.checks);
      renderTasks();
      renderSummary();
    });

    title.textContent = task.title;
    dates.textContent = `${fmtDate(task.start)} to ${fmtDate(task.end)}`;
    hours.textContent = `${task.min}-${task.max}h`;

    const status = getTaskStatus(task, now, checked);
    statusEl.textContent = status.label;
    statusEl.classList.add(status.className);

    els.taskList.appendChild(node);
  });
}

function renderSummary() {
  const now = new Date();
  const done = TASKS.filter((t) => state.checks[t.id]).length;
  const total = TASKS.length;
  const progress = total === 0 ? 0 : Math.round((done / total) * 100);

  const remaining = TASKS.filter((t) => !state.checks[t.id]);
  const remainingMin = sum(remaining.map((t) => t.min));
  const remainingMax = sum(remaining.map((t) => t.max));
  const remainingMid = remaining.length === 0 ? 0 : (remainingMin + remainingMax) / 2;

  els.taskCount.textContent = String(total);
  els.doneCount.textContent = String(done);
  els.meterFill.style.width = `${progress}%`;
  els.remainingHours.textContent = remaining.length ? `${remainingMin}-${remainingMax}` : "0";

  if (remaining.length === 0) {
    els.projectedFinish.textContent = "All scheduled steps complete";
    els.timelineHealth.textContent = "Execution complete.";
    return;
  }

  const hoursPerDay = Math.max(1, state.hoursPerDay || 6);
  const durationMs = (remainingMid / hoursPerDay) * 24 * 60 * 60 * 1000;
  const projected = new Date(now.getTime() + durationMs);

  const overallEnd = parseDate("2026-05-08", true);
  const daysDiff = (projected.getTime() - overallEnd.getTime()) / (24 * 60 * 60 * 1000);

  els.projectedFinish.textContent = `${projected.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  })} (${hoursPerDay}h/day)`;

  if (daysDiff > 0.5) {
    els.timelineHealth.textContent = `Behind schedule by about ${Math.ceil(daysDiff)} day(s).`;
  } else if (daysDiff < -0.5) {
    els.timelineHealth.textContent = `Ahead of schedule by about ${Math.ceil(Math.abs(daysDiff))} day(s).`;
  } else {
    els.timelineHealth.textContent = "On track with target window.";
  }

  renderTasks();
}

function getTaskStatus(task, now, checked) {
  if (checked) {
    return { label: "Done", className: "status-done" };
  }

  const start = parseDate(task.start, false);
  const end = parseDate(task.end, true);

  if (now < start) {
    const daysUntil = Math.max(1, Math.ceil((start.getTime() - now.getTime()) / (24 * 60 * 60 * 1000)));
    return { label: `Upcoming (${daysUntil} day${daysUntil > 1 ? "s" : ""})`, className: "status-upcoming" };
  }

  if (now > end) {
    const lateDays = Math.max(1, Math.ceil((now.getTime() - end.getTime()) / (24 * 60 * 60 * 1000)));
    return { label: `Late (${lateDays} day${lateDays > 1 ? "s" : ""})`, className: "status-late" };
  }

  return { label: "In window", className: "status-active" };
}

function updateClock() {
  const now = new Date();
  els.currentTime.textContent = now.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDate(dateStr) {
  return parseDate(dateStr, false).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function parseDate(dateStr, endOfDay) {
  const [year, month, day] = dateStr.split("-").map(Number);
  if (endOfDay) {
    return new Date(year, month - 1, day, 23, 59, 59, 999);
  }
  return new Date(year, month - 1, day, 0, 0, 0, 0);
}

function loadChecks() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveChecks(value) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
}

function sum(values) {
  return values.reduce((acc, n) => acc + n, 0);
}
