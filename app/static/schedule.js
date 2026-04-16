const STORAGE_KEY = "hoodline_schedule_checks_v2";
const TASKS = Array.isArray(window.SCHEDULE_TASKS) ? window.SCHEDULE_TASKS : [];

const state = {
  checks: { ...defaultChecks(), ...loadChecks() },
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
  if (!els.taskList) return;

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
    state.checks = defaultChecks();
    saveChecks(state.checks);
    renderSummary();
  });
}

function renderSummary() {
  const now = new Date();
  const done = TASKS.filter((t) => state.checks[t.id]).length;
  const total = TASKS.length;

  const remaining = TASKS.filter((t) => !state.checks[t.id]);
  const remainingMin = sum(remaining.map((t) => t.min));
  const remainingMax = sum(remaining.map((t) => t.max));
  const remainingMid = remaining.length ? (remainingMin + remainingMax) / 2 : 0;

  const progress = total ? Math.round((done / total) * 100) : 0;
  els.taskCount.textContent = String(total);
  els.doneCount.textContent = String(done);
  els.meterFill.style.width = `${progress}%`;
  els.remainingHours.textContent = remaining.length ? `${remainingMin}-${remainingMax}` : "0";

  if (!remaining.length) {
    els.projectedFinish.textContent = "All tasks completed";
    els.timelineHealth.textContent = "Execution complete.";
    renderTasks(now);
    return;
  }

  const daily = Math.max(1, state.hoursPerDay || 6);
  const projected = new Date(now.getTime() + ((remainingMid / daily) * 24 * 60 * 60 * 1000));
  const scheduleEnd = parseDate("2026-05-08", true);
  const daysOffset = (projected.getTime() - scheduleEnd.getTime()) / (24 * 60 * 60 * 1000);

  els.projectedFinish.textContent = `${projected.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  })} (${daily}h/day)`;

  if (daysOffset > 0.5) {
    els.timelineHealth.textContent = `Behind schedule by about ${Math.ceil(daysOffset)} day(s).`;
  } else if (daysOffset < -0.5) {
    els.timelineHealth.textContent = `Ahead of schedule by about ${Math.ceil(Math.abs(daysOffset))} day(s).`;
  } else {
    els.timelineHealth.textContent = "On target date window.";
  }

  renderTasks(now);
}

function renderTasks(now) {
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

function getTaskStatus(task, now, checked) {
  if (checked) {
    if (task.completed_at) {
      return { label: `Done (${task.completed_at})`, className: "status-done" };
    }
    return { label: "Done", className: "status-done" };
  }

  const start = parseDate(task.start, false);
  const end = parseDate(task.end, true);

  if (now < start) {
    const days = Math.max(1, Math.ceil((start.getTime() - now.getTime()) / 86400000));
    return { label: `Upcoming (${days} day${days > 1 ? "s" : ""})`, className: "status-upcoming" };
  }

  if (now > end) {
    const days = Math.max(1, Math.ceil((now.getTime() - end.getTime()) / 86400000));
    return { label: `Late (${days} day${days > 1 ? "s" : ""})`, className: "status-late" };
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

function fmtDate(value) {
  return parseDate(value, false).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function parseDate(value, endOfDay) {
  const [year, month, day] = value.split("-").map(Number);
  return endOfDay
    ? new Date(year, month - 1, day, 23, 59, 59, 999)
    : new Date(year, month - 1, day, 0, 0, 0, 0);
}

function loadChecks() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function defaultChecks() {
  const defaults = {};
  for (const task of TASKS) {
    if (task.completed === true) {
      defaults[task.id] = true;
    }
  }
  return defaults;
}

function saveChecks(value) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
}

function sum(list) {
  return list.reduce((acc, value) => acc + value, 0);
}
