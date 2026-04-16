const state = {
  cases: [],
  filter: "pending",
};

const els = {
  container: document.getElementById("cases-container"),
  loading: document.getElementById("cases-loading"),
  template: document.getElementById("case-template"),
};

init();

function init() {
  document.querySelectorAll(".filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".filter-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.filter = btn.dataset.filter;
      loadCases();
    });
  });

  loadCases();
}

async function loadCases() {
  els.loading.style.display = "block";
  els.loading.textContent = "Loading cases...";

  const existingCards = els.container.querySelectorAll(".case-card");
  existingCards.forEach((c) => c.remove());

  try {
    const url = state.filter
      ? `/api/review/cases?status=${state.filter}`
      : "/api/review/cases";
    const data = await requestJson(url);
    state.cases = data.cases || [];

    if (!state.cases.length) {
      els.loading.textContent = state.filter === "pending"
        ? "No pending cases. Run a pipeline to generate review cases."
        : "No cases found.";
      return;
    }

    els.loading.style.display = "none";
    state.cases.forEach(renderCase);
  } catch (err) {
    els.loading.textContent = `Error loading cases: ${err.message}`;
  }
}

function renderCase(caseData) {
  const node = els.template.content.firstElementChild.cloneNode(true);
  node.dataset.runId = caseData.run_id;

  const headline = caseData.article?.headline || "Untitled Article";
  const caseId = caseData.case_id || caseData.run_id.slice(0, 12);
  node.querySelector(".case-title").textContent = `${headline}`;
  node.querySelector(".case-meta").textContent =
    `Case ${caseId} | Run ${caseData.run_id.slice(0, 12)}... | ${formatDate(caseData.created_at)}`;

  const confidence = caseData.verification?.confidence;
  const confEl = node.querySelector(".case-confidence");
  if (confidence != null) {
    confEl.textContent = `Confidence: ${confidence}/10`;
    confEl.classList.add(confidence >= 8 ? "status-done" : confidence >= 5 ? "status-active" : "status-late");
  } else {
    confEl.style.display = "none";
  }

  const recAction = caseData.verification?.recommended_action || "";
  const recEl = node.querySelector(".case-action-rec");
  recEl.textContent = recAction.replace(/_/g, " ");

  const statusEl = node.querySelector(".case-status");
  const latestAction = caseData.latest_review_action;
  if (latestAction) {
    statusEl.textContent = latestAction.replace(/_/g, " ");
    statusEl.classList.add(
      latestAction === "approve_publish" ? "status-done" :
      latestAction === "reject" ? "status-late" : "status-active"
    );
  } else {
    statusEl.textContent = "Pending review";
    statusEl.classList.add("status-active");
  }

  const email = caseData.email || {};
  node.querySelector(".email-sender").textContent = email.sender || "Unknown";
  node.querySelector(".email-subject").textContent = email.subject || "No subject";
  node.querySelector(".email-body").textContent = email.body || "(empty)";

  const article = caseData.article || {};
  node.querySelector(".article-headline").textContent = article.headline || "Unknown article";
  const articleLink = node.querySelector(".article-link");
  if (article.article_url) {
    articleLink.href = article.article_url;
    articleLink.textContent = article.article_url;
  } else {
    articleLink.textContent = "No URL";
  }
  const previewSpan = node.querySelector(".article-preview-link");
  if (article.preview_url) {
    previewSpan.innerHTML = ` | <a href="${escapeHtml(article.preview_url)}" target="_blank">Preview staged draft</a>`;
  }

  const claim = caseData.claim || {};
  node.querySelector(".claim-text").textContent = claim.specific_claim || "(not extracted)";
  node.querySelector(".claim-proposed").textContent = claim.proposed_correction || "(none)";
  node.querySelector(".claim-type").textContent = claim.request_type || "unknown";

  const verification = caseData.verification || {};
  const evidenceList = node.querySelector(".evidence-list");
  renderEvidenceCards(evidenceList, verification.evidence || []);

  const contradictingList = node.querySelector(".contradicting-list");
  const contradicting = verification.contradicting_evidence || [];
  if (contradicting.length) {
    node.querySelector(".contradicting-header").style.display = "block";
    renderEvidenceCards(contradictingList, contradicting);
  }

  const diff = caseData.staged_diff || {};
  node.querySelector(".diff-field").textContent = `Field: ${diff.target_field || "N/A"} | Remote applied: ${diff.remote_applied ? "Yes" : "No"}`;
  node.querySelector(".diff-before-text").textContent = diff.before || "(empty)";
  node.querySelector(".diff-after-text").textContent = diff.after || "(empty)";

  const noteText = caseData.remediation?.suggested_note_text || "";
  node.querySelector(".editor-note-input").value = noteText;

  node.querySelectorAll(".action-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      submitAction(caseData.run_id, btn.dataset.action, node);
    });
  });

  if (latestAction) {
    loadDecisionHistory(caseData.run_id, node);
  }

  els.container.appendChild(node);
}

function renderEvidenceCards(container, items) {
  if (!items.length) {
    container.innerHTML = '<p class="muted">No evidence collected.</p>';
    return;
  }

  items.forEach((ev) => {
    const card = document.createElement("div");
    card.className = "evidence-card";
    card.innerHTML = `
      <p class="evidence-quote">"${escapeHtml(ev.quote || "")}"</p>
      <p class="muted evidence-source">
        ${ev.source_url ? `<a href="${escapeHtml(ev.source_url)}" target="_blank">${escapeHtml(ev.source_url)}</a>` : "No source URL"}
        ${ev.weight != null ? ` | weight: ${ev.weight}` : ""}
      </p>
    `;
    container.appendChild(card);
  });
}

async function submitAction(runId, action, cardNode) {
  const editorNote = cardNode.querySelector(".editor-note-input").value.trim();
  const reviewerNotes = cardNode.querySelector(".reviewer-notes-input").value.trim();

  const buttons = cardNode.querySelectorAll(".action-btn");
  buttons.forEach((b) => (b.disabled = true));

  try {
    const result = await requestJson(`/api/review/cases/${runId}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: action,
        editor_note: editorNote,
        reviewer_notes: reviewerNotes,
      }),
    });

    const statusEl = cardNode.querySelector(".case-status");
    statusEl.textContent = action.replace(/_/g, " ");
    statusEl.className = "pill " + (
      action === "approve_publish" ? "status-done" :
      action === "reject" ? "status-late" : "status-active"
    );

    loadDecisionHistory(runId, cardNode);
  } catch (err) {
    alert(`Action failed: ${err.message}`);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

async function loadDecisionHistory(runId, cardNode) {
  try {
    const data = await requestJson(`/api/review/cases/${runId}`);
    const decisions = data.decisions || [];
    if (!decisions.length) return;

    const logSection = cardNode.querySelector(".case-decision-log");
    logSection.style.display = "block";

    const entries = logSection.querySelector(".decision-entries");
    entries.innerHTML = decisions.map((d) => `
      <div class="decision-entry">
        <span class="pill ${
          d.action === "approve_publish" ? "status-done" :
          d.action === "reject" ? "status-late" : "status-active"
        }">${d.action.replace(/_/g, " ")}</span>
        <strong>${escapeHtml(d.reviewer_username)}</strong>
        at ${formatDate(d.created_at)}
        ${d.reviewer_notes ? `<span class="muted"> — ${escapeHtml(d.reviewer_notes)}</span>` : ""}
      </div>
    `).join("");
  } catch {
    // Decision history is supplementary; don't block on failure.
  }
}

function formatDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
  });

  if (response.status === 401) {
    window.location.href = "/login?next=/review";
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
