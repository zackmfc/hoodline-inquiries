(() => {
  const FIELD_LABELS = {
    t: "Title",
    md: "Meta Description",
    mt: "Meta Title",
    ex: "Excerpt",
    b: "Body (HTML fragment)",
    fia: "Featured Image Attribution",
  };

  const FIELD_ORDER = ["t", "mt", "md", "ex", "b", "fia"];

  const steps = {
    email: document.getElementById("step-email"),
    discord: document.getElementById("step-discord"),
    cms: document.getElementById("step-cms"),
    result: document.getElementById("step-result"),
  };

  const state = {
    email: null,
    assess: null,
    discord: null,
    cms: null,
    generate: null,
    gmail_message_id: "",
  };

  const WIZARD_STATUS_LABELS = {
    new: { text: "New", cls: "" },
    assessed: { text: "Assessed — gate passed", cls: "status-active" },
    gate_failed: { text: "Gate failed", cls: "status-late" },
    image_only: { text: "Image-only", cls: "status-late" },
    located: { text: "Article located", cls: "status-active" },
    completed: { text: "Completed", cls: "status-done" },
    corrected: { text: "Corrected", cls: "status-done" },
    triaged_pending: { text: "Triaged · Pending", cls: "status-active" },
    triaged_rejected: { text: "Triaged · Rejected", cls: "status-late" },
  };

  function setStepState(stepEl, newState) {
    stepEl.dataset.state = newState;
    const pill = stepEl.querySelector(".step-state");
    if (!pill) return;

    const map = {
      locked: { text: "Locked", cls: "" },
      active: { text: "In progress", cls: "status-active" },
      done: { text: "Done", cls: "status-done" },
      blocked: { text: "Blocked", cls: "status-late" },
    };
    pill.classList.remove("status-active", "status-done", "status-late");
    const conf = map[newState] || map.locked;
    pill.textContent = conf.text;
    if (conf.cls) pill.classList.add(conf.cls);

    stepEl.classList.toggle("is-locked", newState === "locked");
  }

  function setFieldDisabled(stepEl, disabled) {
    stepEl.querySelectorAll("input, textarea, button, a.btn").forEach((el) => {
      if (disabled) {
        el.setAttribute("disabled", "disabled");
        if (el.tagName === "A") {
          el.setAttribute("aria-disabled", "true");
          el.classList.add("btn-disabled");
        }
      } else {
        el.removeAttribute("disabled");
        if (el.tagName === "A") {
          el.removeAttribute("aria-disabled");
          el.classList.remove("btn-disabled");
        }
      }
    });
  }

  function unlockStep(stepEl) {
    setStepState(stepEl, "active");
    setFieldDisabled(stepEl, false);
  }

  function lockStep(stepEl) {
    setStepState(stepEl, "locked");
    setFieldDisabled(stepEl, true);
  }

  // Initial lock state
  [steps.discord, steps.cms, steps.result].forEach(lockStep);
  setStepState(steps.email, "active");

  async function postJSON(url, data) {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const text = await resp.text();
    let payload;
    try {
      payload = text ? JSON.parse(text) : {};
    } catch (err) {
      payload = { detail: text };
    }
    if (!resp.ok) {
      const detail = payload && payload.detail ? payload.detail : `HTTP ${resp.status}`;
      throw new Error(detail);
    }
    return payload;
  }

  function formToObject(form) {
    const fd = new FormData(form);
    const result = {};
    fd.forEach((value, key) => {
      result[key] = typeof value === "string" ? value : "";
    });
    return result;
  }

  function setStatus(el, text, tone) {
    if (!el) return;
    el.textContent = text || "";
    el.classList.remove("status-late", "status-active", "status-done");
    if (tone === "error") el.classList.add("status-late");
    if (tone === "info") el.classList.add("status-active");
    if (tone === "ok") el.classList.add("status-done");
  }

  async function copyToClipboard(text, btn) {
    try {
      await navigator.clipboard.writeText(text);
      if (btn) {
        const original = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(() => (btn.textContent = original), 1200);
      }
    } catch (err) {
      console.error("Copy failed", err);
    }
  }

  // ── Step 1a: Inbox picker ───────────────────────────────────────
  const inboxFetchBtn = document.getElementById("inbox-fetch-btn");
  const inboxClearCacheBtn = document.getElementById("inbox-clear-cache-btn");
  const inboxLimitSel = document.getElementById("inbox-limit");
  const inboxIncludeProcessed = document.getElementById("inbox-include-processed");
  const inboxStatus = document.getElementById("inbox-status");
  const inboxList = document.getElementById("inbox-list");
  const inboxItemTemplate = document.getElementById("inbox-item-template");

  function formatReceivedAt(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return "";
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    } catch (err) {
      return "";
    }
  }

  function applyTriageVisual(itemRoot, decision) {
    if (!itemRoot) return;
    itemRoot.classList.remove("is-triaged-pending", "is-triaged-rejected");
    if (decision === "pending") {
      itemRoot.classList.add("is-triaged-pending");
      itemRoot.style.borderLeft = "3px solid #2f6b46";
      itemRoot.style.opacity = "0.85";
    } else if (decision === "rejected") {
      itemRoot.classList.add("is-triaged-rejected");
      itemRoot.style.borderLeft = "3px solid #a3321f";
      itemRoot.style.opacity = "0.6";
    }
  }

  function attachTriageHandlers(itemRoot, msg, refs) {
    const { pendingBtn, rejectBtn, statusEl, wizardStatusEl } = refs;
    if (!pendingBtn || !rejectBtn) return;

    // Reflect existing triage state on initial render.
    const existing = msg.wizard_status;
    if (existing === "triaged_pending") applyTriageVisual(itemRoot, "pending");
    if (existing === "triaged_rejected") applyTriageVisual(itemRoot, "rejected");

    async function submitTriage(decision, triggeringBtn) {
      if (!msg.gmail_message_id) {
        statusEl.textContent = "No Gmail message id — cannot triage.";
        return;
      }
      pendingBtn.disabled = true;
      rejectBtn.disabled = true;
      statusEl.textContent = decision === "pending" ? "Marking pending…" : "Rejecting…";
      try {
        const resp = await fetch("/api/corrections/triage", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            gmail_message_id: msg.gmail_message_id,
            decision,
          }),
        });
        const text = await resp.text();
        let payload;
        try { payload = text ? JSON.parse(text) : {}; } catch (_) { payload = { detail: text }; }
        if (!resp.ok) {
          throw new Error(payload.detail || `HTTP ${resp.status}`);
        }
        applyTriageVisual(itemRoot, decision);
        const label = decision === "pending" ? "Pending" : "Rejected";
        statusEl.textContent = `Marked as ${label}.`;
        statusEl.classList.add("status-done");

        if (wizardStatusEl) {
          wizardStatusEl.innerHTML = "";
          const conf = WIZARD_STATUS_LABELS[payload.status] || WIZARD_STATUS_LABELS.new;
          const pill = document.createElement("span");
          pill.className = "pill " + (conf.cls || "");
          pill.textContent = conf.text;
          wizardStatusEl.appendChild(pill);
        }
        msg.wizard_status = payload.status;
      } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
        statusEl.classList.add("status-late");
        pendingBtn.disabled = false;
        rejectBtn.disabled = false;
      }
    }

    pendingBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      submitTriage("pending", pendingBtn);
    });
    rejectBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      submitTriage("rejected", rejectBtn);
    });
  }

  if (inboxClearCacheBtn) {
    inboxClearCacheBtn.addEventListener("click", async () => {
      const ok = window.confirm(
        "Wipe all Corrections-wizard state? Every email returns to 'new' and you'll lose any in-progress wizard runs. Gmail, Discord cache, and published corrections are NOT affected."
      );
      if (!ok) return;

      inboxClearCacheBtn.disabled = true;
      inboxFetchBtn.disabled = true;
      setStatus(inboxStatus, "Clearing wizard cache…", "info");

      try {
        const resp = await fetch("/api/corrections/wizard/clear", {
          method: "POST",
        });
        const text = await resp.text();
        let payload;
        try { payload = text ? JSON.parse(text) : {}; } catch (_) { payload = { detail: text }; }
        if (!resp.ok) {
          throw new Error(payload.detail || `HTTP ${resp.status}`);
        }

        // Reset in-flight wizard state so the currently-open email doesn't
        // think it's still mid-flow.
        state.gmail_message_id = "";
        resetWizardUI();
        emailForm.reset();
        inboxList.innerHTML = "";
        inboxList.hidden = true;

        const cleared = typeof payload.cleared === "number" ? payload.cleared : 0;
        setStatus(
          inboxStatus,
          `Cleared ${cleared} wizard record${cleared === 1 ? "" : "s"}. Fetch the queue to start over.`,
          "ok"
        );
      } catch (err) {
        setStatus(inboxStatus, `Error: ${err.message}`, "error");
      } finally {
        inboxClearCacheBtn.disabled = false;
        inboxFetchBtn.disabled = false;
      }
    });
  }

  inboxFetchBtn.addEventListener("click", async () => {
    const limit = parseInt(inboxLimitSel.value, 10) || 5;
    const includeProcessed = inboxIncludeProcessed && inboxIncludeProcessed.checked;
    inboxStatus.textContent = "Loading queue from Gmail…";
    inboxStatus.classList.remove("status-late", "status-done");
    inboxStatus.classList.add("status-active");
    inboxList.hidden = true;
    inboxList.innerHTML = "";

    try {
      const url =
        `/api/corrections/inbox?limit=${limit}` +
        (includeProcessed ? `&include_processed=true` : ``);
      const resp = await fetch(url);
      const text = await resp.text();
      let payload;
      try { payload = JSON.parse(text); } catch (_) { payload = { detail: text }; }
      if (!resp.ok) {
        throw new Error(payload.detail || `HTTP ${resp.status}`);
      }

      const messages = payload.messages || [];
      const filtered = payload.filtered_count || 0;
      if (messages.length === 0) {
        const msg = filtered > 0
          ? `No unprocessed messages — hid ${filtered} already-processed. Tick "Include already-processed" to show them.`
          : "Queue is empty — no unread correction-candidate emails.";
        inboxStatus.textContent = msg;
        inboxStatus.classList.remove("status-active", "status-late");
        inboxStatus.classList.add("status-done");
        return;
      }

      messages.forEach((msg) => {
        const frag = inboxItemTemplate.content.cloneNode(true);
        const itemRoot = frag.querySelector(".inbox-item");
        const btn = frag.querySelector(".inbox-item-btn");
        frag.querySelector(".inbox-item-subject").textContent =
          msg.subject || "(no subject)";
        frag.querySelector(".inbox-item-date").textContent =
          formatReceivedAt(msg.received_at);
        frag.querySelector(".inbox-item-sender").textContent =
          msg.sender_raw || msg.sender_email || "(unknown sender)";
        frag.querySelector(".inbox-item-snippet").textContent =
          msg.snippet || "";

        const statusEl = frag.querySelector(".inbox-item-status");
        const statusKey = msg.wizard_status || "new";
        const conf = WIZARD_STATUS_LABELS[statusKey] || WIZARD_STATUS_LABELS.new;
        if (statusKey !== "new") {
          const pill = document.createElement("span");
          pill.className = "pill " + (conf.cls || "");
          pill.textContent = conf.text;
          statusEl.appendChild(pill);

          if (msg.wizard_updated_at) {
            const meta = document.createElement("span");
            meta.className = "muted inbox-item-status-meta";
            const who = msg.wizard_touched_by ? ` by ${msg.wizard_touched_by}` : "";
            meta.textContent = `  ${formatReceivedAt(msg.wizard_updated_at)}${who}`;
            statusEl.appendChild(meta);
          }
          if (itemRoot && conf.cls) itemRoot.classList.add("has-wizard-status");
        }

        btn.addEventListener("click", () => selectInboxMessage(msg));

        const triagePendingBtn = frag.querySelector(".inbox-item-triage-pending");
        const triageRejectBtn = frag.querySelector(".inbox-item-triage-reject");
        const triageStatusEl = frag.querySelector(".inbox-item-triage-status");
        attachTriageHandlers(itemRoot, msg, {
          pendingBtn: triagePendingBtn,
          rejectBtn: triageRejectBtn,
          statusEl: triageStatusEl,
          wizardStatusEl: statusEl,
        });

        inboxList.appendChild(frag);
      });

      inboxList.hidden = false;
      const filteredNote = filtered > 0 && !includeProcessed
        ? ` Hid ${filtered} already-processed.`
        : "";
      inboxStatus.textContent =
        `Loaded ${messages.length} message${messages.length === 1 ? "" : "s"}.${filteredNote} Click one to prefill the form.`;
      inboxStatus.classList.remove("status-active", "status-late");
      inboxStatus.classList.add("status-done");
    } catch (err) {
      inboxStatus.textContent = `Error: ${err.message}`;
      inboxStatus.classList.remove("status-active", "status-done");
      inboxStatus.classList.add("status-late");
    }
  });

  function selectInboxMessage(msg) {
    resetWizardUI();

    state.gmail_message_id = msg.gmail_message_id || "";

    emailForm.querySelector('[name="sender_name"]').value = msg.sender_name || "";
    emailForm.querySelector('[name="sender_email"]').value = msg.sender_email || "";
    emailForm.querySelector('[name="subject"]').value = msg.subject || "";
    emailForm.querySelector('[name="body"]').value = msg.body || msg.snippet || "";

    state.email = {
      sender_name: msg.sender_name || "",
      sender_email: msg.sender_email || "",
      subject: msg.subject || "",
      body: msg.body || msg.snippet || "",
    };

    inboxList.querySelectorAll(".inbox-item").forEach((el) => {
      el.classList.remove("is-selected");
    });
    const items = Array.from(inboxList.querySelectorAll(".inbox-item"));
    const idx = items.findIndex((el) => {
      const subjEl = el.querySelector(".inbox-item-subject");
      return subjEl && subjEl.textContent === (msg.subject || "(no subject)");
    });
    if (idx >= 0) items[idx].classList.add("is-selected");

    restoreWizardState(msg.wizard_state || {}, msg.wizard_status || "new");

    emailForm.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function resetWizardUI() {
    state.assess = null;
    state.discord = null;
    state.cms = null;
    state.generate = null;

    assessResult.hidden = true;
    gateVerdict.classList.remove("gate-pass", "gate-fail", "gate-image-only");
    gateVerdict.innerHTML = "";
    setStatus(assessStatus, "", "");
    setStatus(discordStatus, "", "");
    setStatus(locatorStatus, "", "");
    setStatus(generateStatus, "", "");
    discordResult.hidden = true;
    generateFields.hidden = true;
    generateSummary.hidden = true;
    generateRawWrap.hidden = true;
    if (locatorTrace) {
      locatorTrace.hidden = true;
      locatorTrace.innerHTML = "";
    }
    clearLocatorBanners();

    setStepState(steps.email, "active");
    lockStep(steps.discord);
    lockStep(steps.cms);
    lockStep(steps.result);
  }

  function restoreWizardState(stored, status) {
    if (!stored || typeof stored !== "object") return;

    const assessEntry = stored.assess;
    if (assessEntry && assessEntry.response) {
      const res = assessEntry.response;
      state.assess = res;
      renderAssessResult(res);

      if (status === "image_only" || res.image_only) {
        setStepState(steps.email, "done");
      } else if (res.gate_passed) {
        setStepState(steps.email, "done");
        unlockStep(steps.discord);
      } else {
        setStepState(steps.email, "blocked");
      }
    }

    const discordEntry = stored.discord;
    if (discordEntry && discordEntry.result && discordEntry.result.found) {
      const r = discordEntry.result;
      setDiscordResolved({
        article_id: r.article_id,
        cms_edit_url: r.cms_edit_url,
      });
      // Restore banners only for auto-located results (manual paste has no metadata).
      if (discordEntry.source === "auto_locate") {
        renderLocatorMatchSource(r);
        renderLocatorGoogleWarning(r);
      }
    }

    const generateEntry = stored.generate;
    if (generateEntry && generateEntry.response) {
      unlockStep(steps.cms);
      const cms = generateEntry.cms_inputs || {};
      Object.entries(cms).forEach(([name, value]) => {
        const field = cmsForm.querySelector(`[name="${name}"]`);
        if (field && typeof value === "string") field.value = value;
      });
      state.cms = cms;
      state.generate = generateEntry.response;
      unlockStep(steps.result);
      renderGenerateResult(generateEntry.response);
      setStepState(steps.cms, "done");
      setStepState(steps.result, "done");
      showMarkCorrectedIfEligible(status);
    }
  }

  function renderAssessResult(res) {
    scoreCRVS.textContent = res.CRVS;
    scoreSAS.textContent = res.SAS;
    meterCRVS.style.width = `${(res.CRVS / 10) * 100}%`;
    meterSAS.style.width = `${(res.SAS / 10) * 100}%`;
    reasonCRVS.textContent = res.crvs_reasoning || "";
    reasonSAS.textContent = res.sas_reasoning || "";
    assessResult.hidden = false;

    gateVerdict.classList.remove("gate-pass", "gate-fail", "gate-image-only");
    if (res.gate_passed && res.image_only) {
      gateVerdict.classList.add("gate-image-only");
      const summary = (res.image_request_summary || "").trim();
      gateVerdict.innerHTML =
        `<p class="gate-verdict-heading"><strong>Image-only request — stop here.</strong></p>` +
        `<p>This request is only about the article's image, so no Discord lookup or Claude web-search run is needed. Route it to whoever handles image corrections.</p>` +
        (summary
          ? `<p class="gate-verdict-detail"><strong>What the sender said about the image:</strong> ${escapeHtml(summary)}</p>`
          : "");
    } else if (res.gate_passed) {
      gateVerdict.classList.add("gate-pass");
      const hintBits = [];
      if (res.article_url_hint) hintBits.push(`URL: ${res.article_url_hint}`);
      if (res.article_title_hint) hintBits.push(`Title: "${res.article_title_hint}"`);
      gateVerdict.textContent =
        "Both scores above 4 — proceed to step 2." +
        (hintBits.length ? `  (${hintBits.join(" · ")})` : "");
    } else {
      gateVerdict.classList.add("gate-fail");
      gateVerdict.textContent =
        "At least one score is 4 or lower — skipping the remaining steps. Re-assess or handle manually.";
    }
  }

  // ── Step 1: Email → Assess ──────────────────────────────────────
  const emailForm = document.getElementById("email-form");
  const assessStatus = document.getElementById("assess-status");
  const assessResult = document.getElementById("assess-result");
  const scoreCRVS = document.getElementById("score-crvs");
  const scoreSAS = document.getElementById("score-sas");
  const meterCRVS = document.getElementById("meter-crvs");
  const meterSAS = document.getElementById("meter-sas");
  const reasonCRVS = document.getElementById("reason-crvs");
  const reasonSAS = document.getElementById("reason-sas");
  const gateVerdict = document.getElementById("gate-verdict");

  emailForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = formToObject(emailForm);
    if (!data.subject.trim() && !data.body.trim()) {
      setStatus(assessStatus, "Enter at least a subject or body.", "error");
      return;
    }

    setStatus(assessStatus, "Assessing with Claude…", "info");
    assessResult.hidden = true;
    lockStep(steps.discord);
    lockStep(steps.cms);
    lockStep(steps.result);

    try {
      const res = await postJSON("/api/corrections/assess", {
        ...data,
        gmail_message_id: state.gmail_message_id || "",
      });
      state.email = data;
      state.assess = res;

      renderAssessResult(res);

      if (res.gate_passed && res.image_only) {
        setStepState(steps.email, "done");
        lockStep(steps.discord);
        lockStep(steps.cms);
        lockStep(steps.result);
        setStatus(assessStatus, "Image-only — no further steps required.", "ok");
      } else if (res.gate_passed) {
        setStepState(steps.email, "done");
        unlockStep(steps.discord);
        setStatus(assessStatus, "", "ok");
      } else {
        setStepState(steps.email, "blocked");
        setStatus(assessStatus, "Gate not passed.", "error");
      }
    } catch (err) {
      setStatus(assessStatus, `Error: ${err.message}`, "error");
    }
  });

  // ── Step 2: Locate via cascade, or manual paste ────────────────
  const discordForm = document.getElementById("discord-form");
  const discordStatus = document.getElementById("discord-status");
  const discordResult = document.getElementById("discord-result");
  const articleIdEl = document.getElementById("article-id");
  const editUrlLink = document.getElementById("edit-url-link");
  const cmsEditBtn = document.getElementById("cms-edit-btn");

  const locatorRunBtn = document.getElementById("locator-run-btn");
  const locatorStatus = document.getElementById("locator-status");
  const locatorTrace = document.getElementById("locator-trace");
  const locatorWarning = document.getElementById("locator-warning");
  const locatorMatchSource = document.getElementById("locator-match-source");

  function clearLocatorBanners() {
    if (locatorWarning) {
      locatorWarning.hidden = true;
      locatorWarning.innerHTML = "";
    }
    if (locatorMatchSource) {
      locatorMatchSource.hidden = true;
      locatorMatchSource.innerHTML = "";
    }
  }

  function renderLocatorMatchSource(res) {
    if (!locatorMatchSource) return;
    if (!res || !res.found) {
      locatorMatchSource.hidden = true;
      locatorMatchSource.innerHTML = "";
      return;
    }
    const label = res.match_source_label || res.match_source || "";
    const n = res.match_word_count;
    const words = Array.isArray(res.match_words) ? res.match_words.join(" ") : "";
    const bits = [];
    if (label) bits.push(`<strong>How we found it:</strong> ${escapeHtml(label)}`);
    if (n && words) {
      bits.push(
        `<span class="muted">first ${n} words — <code>${escapeHtml(words)}</code></span>`
      );
    }
    locatorMatchSource.innerHTML = bits.join("  ");
    locatorMatchSource.hidden = bits.length === 0;
  }

  function renderLocatorGoogleWarning(res) {
    if (!locatorWarning) return;
    if (!res || !res.found || !res.google_search_warning) {
      locatorWarning.hidden = true;
      locatorWarning.innerHTML = "";
      return;
    }
    const query = res.google_query || "";
    const queryLine = query
      ? `<div class="locator-warning-detail">Google query: <code>${escapeHtml(query)}</code></div>`
      : "";
    locatorWarning.innerHTML = `
      <div class="locator-warning-head">
        <strong>⚠ This match came from a Google search — verify before proceeding.</strong>
      </div>
      <div class="locator-warning-detail">
        We couldn't match the sender-quoted title or the hoodline.com URL in
        the email directly, so we asked Claude for a search query and picked
        from the top Google results. Double-check the CMS edit link actually
        corresponds to the article the sender is referring to.
      </div>
      ${queryLine}
    `;
    locatorWarning.hidden = false;
  }

  function setDiscordResolved({ article_id, cms_edit_url }) {
    articleIdEl.textContent = article_id ?? "—";
    editUrlLink.textContent = cms_edit_url;
    editUrlLink.href = cms_edit_url;
    cmsEditBtn.href = cms_edit_url;
    cmsEditBtn.removeAttribute("aria-disabled");
    cmsEditBtn.classList.remove("btn-disabled");
    discordResult.hidden = false;
    state.discord = { found: true, article_id, cms_edit_url };
    setStepState(steps.discord, "done");
    unlockStep(steps.cms);
  }

  function renderLocatorTrace(trace) {
    locatorTrace.innerHTML = "";
    if (!trace || trace.length === 0) {
      locatorTrace.hidden = true;
      return;
    }
    trace.forEach((t) => {
      const li = document.createElement("li");
      li.className = "locator-trace-item" + (t.matched ? " is-hit" : "");
      const head = document.createElement("div");
      head.className = "locator-trace-head";
      const stepSpan = document.createElement("span");
      stepSpan.className = "locator-trace-step";
      stepSpan.textContent = `${t.step} · ${t.action}`;
      head.appendChild(stepSpan);
      if (t.matched) {
        const badge = document.createElement("span");
        badge.className = "pill status-done";
        badge.textContent = "matched";
        head.appendChild(badge);
      }
      li.appendChild(head);
      if (t.detail) {
        const detail = document.createElement("div");
        detail.className = "locator-trace-detail";
        detail.textContent = t.detail;
        li.appendChild(detail);
      }
      locatorTrace.appendChild(li);
    });
    locatorTrace.hidden = false;
  }

  async function refreshDiscordCacheIncremental() {
    try {
      const resp = await fetch("/api/discord/refresh-incremental", {
        method: "POST",
      });
      const text = await resp.text();
      let payload;
      try { payload = text ? JSON.parse(text) : {}; } catch (_) { payload = {}; }
      if (!resp.ok) {
        return { ok: false, detail: payload.detail || `HTTP ${resp.status}` };
      }
      return { ok: true, payload };
    } catch (err) {
      return { ok: false, detail: err && err.message ? err.message : String(err) };
    }
  }

  locatorRunBtn.addEventListener("click", async () => {
    if (!state.email) {
      setStatus(locatorStatus, "Run step 1 first.", "error");
      return;
    }

    locatorRunBtn.disabled = true;
    locatorTrace.hidden = true;
    locatorTrace.innerHTML = "";
    clearLocatorBanners();

    // Fire an incremental Discord-cache refresh before the locate so a
    // freshly-posted editor message lands in editorial_posts in time.
    setStatus(locatorStatus, "Refreshing Discord cache (new messages only)…", "info");
    const refresh = await refreshDiscordCacheIncremental();
    if (refresh.ok && refresh.payload) {
      const cached = refresh.payload.articles_cached ?? 0;
      const scanned = refresh.payload.messages_scanned ?? 0;
      console.info(
        `discord refresh: ${cached} new article(s) cached, ${scanned} message(s) scanned`
      );
    } else if (!refresh.ok) {
      console.warn("Discord refresh failed, continuing with locate:", refresh.detail);
    }

    setStatus(locatorStatus, "Locating article… (may scrape via Decodo)", "info");

    const payload = {
      subject: state.email.subject || "",
      body: state.email.body || "",
      article_url_hint: (state.assess && state.assess.article_url_hint) || "",
      article_title_hint: (state.assess && state.assess.article_title_hint) || "",
      gmail_message_id: state.gmail_message_id || "",
    };

    try {
      const res = await postJSON("/api/corrections/locate-discord", payload);
      renderLocatorTrace(res.trace);

      if (res.found && res.cms_edit_url) {
        const sourceLabel = res.match_source_label || res.match_source || "";
        const sourceSuffix = sourceLabel ? ` — ${sourceLabel}` : "";
        setStatus(
          locatorStatus,
          `Found article #${res.article_id} via "${res.authoritative_title}"${sourceSuffix}.`,
          "ok"
        );
        renderLocatorMatchSource(res);
        renderLocatorGoogleWarning(res);
        setDiscordResolved({
          article_id: res.article_id,
          cms_edit_url: res.cms_edit_url,
        });
      } else {
        setStatus(
          locatorStatus,
          "Couldn't auto-locate the Discord message. Paste it manually below.",
          "error"
        );
      }
    } catch (err) {
      setStatus(locatorStatus, `Error: ${err.message}`, "error");
    } finally {
      locatorRunBtn.disabled = false;
    }
  });

  discordForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = formToObject(discordForm);
    if (!data.message.trim()) {
      setStatus(discordStatus, "Paste the Discord message first.", "error");
      return;
    }

    setStatus(discordStatus, "Extracting edit URL…", "info");
    try {
      const res = await postJSON("/api/corrections/parse-discord", {
        ...data,
        gmail_message_id: state.gmail_message_id || "",
      });
      state.discord = res;

      if (!res.found) {
        discordResult.hidden = true;
        setStatus(
          discordStatus,
          "No hoodline.impress3.com/articles/{id}/edit URL found. Check the message.",
          "error"
        );
        lockStep(steps.cms);
        lockStep(steps.result);
        return;
      }

      articleIdEl.textContent = res.article_id;
      editUrlLink.textContent = res.cms_edit_url;
      editUrlLink.href = res.cms_edit_url;
      cmsEditBtn.href = res.cms_edit_url;
      cmsEditBtn.removeAttribute("aria-disabled");
      cmsEditBtn.classList.remove("btn-disabled");
      discordResult.hidden = false;

      setStepState(steps.discord, "done");
      setStatus(discordStatus, "", "ok");
      unlockStep(steps.cms);
    } catch (err) {
      setStatus(discordStatus, `Error: ${err.message}`, "error");
    }
  });

  // ── Step 3: CMS fields → generate correction ────────────────────
  const cmsForm = document.getElementById("cms-form");
  const generateStatus = document.getElementById("generate-status");
  const generateFields = document.getElementById("generate-fields");
  const generateSummary = document.getElementById("generate-summary");
  const generateRawWrap = document.getElementById("generate-raw-wrap");
  const generateRaw = document.getElementById("generate-raw");
  const copyRawBtn = document.getElementById("copy-raw-btn");
  const fieldTemplate = document.getElementById("field-card-template");

  cmsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const cmsData = formToObject(cmsForm);
    if (!cmsData.title.trim() && !cmsData.article_body.trim()) {
      setStatus(
        generateStatus,
        "At least Title or Body is required to run the correction.",
        "error"
      );
      return;
    }

    const payload = {
      ...state.email,
      ...cmsData,
      gmail_message_id: state.gmail_message_id || "",
    };

    setStatus(
      generateStatus,
      "Scraping outbound article links via Decodo, then calling Claude with web search… this may take 60-180s.",
      "info"
    );
    generateFields.hidden = true;
    generateSummary.hidden = true;
    generateRawWrap.hidden = true;

    try {
      const res = await postJSON("/api/corrections/generate", payload);
      state.cms = cmsData;
      state.generate = res;

      renderGenerateResult(res);
      setStepState(steps.cms, "done");
      unlockStep(steps.result);
      setStepState(steps.result, "done");
      setStatus(generateStatus, "", "ok");
      showMarkCorrectedIfEligible("completed");
    } catch (err) {
      setStatus(generateStatus, `Error: ${err.message}`, "error");
    }
  });

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderGenerateResult(res) {
    const correction = res.correction || {};
    const changes = res.changes || {};
    const overallSummary = (res.summary || "").trim();

    // Summary row
    const bits = [];
    if (typeof correction.CRVS2 === "number") {
      bits.push(
        `<span class="pill"><strong>CRVS2:</strong> ${correction.CRVS2} / 10</span>`
      );
    }
    if (typeof correction.if === "boolean") {
      bits.push(
        `<span class="pill ${correction.if ? "status-late" : "status-done"}">Image flag: ${
          correction.if ? "investigate" : "ok"
        }</span>`
      );
    }
    if (typeof res.web_search_requests === "number") {
      bits.push(
        `<span class="pill">Web searches: ${res.web_search_requests}</span>`
      );
    }
    const snapshots = Array.isArray(res.outbound_snapshots) ? res.outbound_snapshots : [];
    if (snapshots.length > 0 || typeof res.outbound_links_found === "number") {
      const redirected = snapshots.filter((s) => s && s.redirected).length;
      const errored = snapshots.filter((s) => s && s.error).length;
      const total = snapshots.length || res.outbound_links_found || 0;
      const pillClass = redirected > 0 ? "status-late" : "";
      let label = `Outbound sources: ${total}`;
      if (redirected > 0) label += ` · ${redirected} redirected`;
      if (errored > 0) label += ` · ${errored} failed`;
      bits.push(`<span class="pill ${pillClass}">${label}</span>`);
    }
    if (res.model) {
      bits.push(`<span class="pill mono">${res.model}</span>`);
    }

    const proposedFields = FIELD_ORDER.filter(
      (k) => typeof correction[k] === "string" && correction[k].trim().length > 0
    );
    const changeKeys = Object.keys(changes);
    const imageFlagFlipped = typeof correction.if === "boolean" && correction.if;

    const verdict =
      proposedFields.length === 0 && !imageFlagFlipped
        ? "No field edits proposed."
        : `Proposed edits: ${proposedFields.map((k) => FIELD_LABELS[k]).join(", ") || "image only"}.`;

    let changeListHtml = "";
    if (overallSummary || changeKeys.length > 0) {
      const items = [];
      if (overallSummary) {
        items.push(`<li><strong>Summary:</strong> ${escapeHtml(overallSummary)}</li>`);
      }
      changeKeys.forEach((k) => {
        const label = FIELD_LABELS[k] || (k === "if" ? "Image" : k);
        items.push(
          `<li><strong>${escapeHtml(label)}:</strong> ${escapeHtml(changes[k])}</li>`
        );
      });
      changeListHtml = `<ul class="changes-list">${items.join("")}</ul>`;
    }

    let snapshotListHtml = "";
    if (snapshots.length > 0) {
      const items = snapshots.map((s) => {
        const req = escapeHtml(s.requested_url || "");
        const fin = escapeHtml(s.final_url || s.requested_url || "");
        const redirectedBadge = s.redirected
          ? ` <span class="pill status-late">redirected</span>`
          : "";
        const errorBadge = s.error
          ? ` <span class="pill status-late">fetch failed</span>`
          : "";
        const finalLine =
          s.redirected && s.final_url && s.final_url !== s.requested_url
            ? `<div class="snapshot-final">→ <a href="${fin}" target="_blank" rel="noreferrer">${fin}</a></div>`
            : "";
        const titleLine = s.title
          ? `<div class="snapshot-title">${escapeHtml(s.title)}</div>`
          : "";
        return (
          `<li class="snapshot-item${s.redirected ? " is-redirected" : ""}">` +
          `<div class="snapshot-head"><a href="${req}" target="_blank" rel="noreferrer" class="mono">${req}</a>${redirectedBadge}${errorBadge}</div>` +
          finalLine +
          titleLine +
          `</li>`
        );
      });
      snapshotListHtml =
        `<details class="snapshot-wrap"><summary>Outbound sources sent to Claude (${snapshots.length})</summary>` +
        `<ul class="snapshot-list">${items.join("")}</ul></details>`;
    }

    generateSummary.innerHTML = `
      <p class="summary-line"><strong>${escapeHtml(verdict)}</strong></p>
      <div class="summary-pills">${bits.join("")}</div>
      ${changeListHtml}
      ${snapshotListHtml}
    `;
    generateSummary.hidden = false;

    // Field cards
    generateFields.innerHTML = "";
    if (proposedFields.length === 0) {
      generateFields.innerHTML = `<p class="muted">Claude did not recommend changes to any text field.</p>`;
    } else {
      proposedFields.forEach((key) => {
        const frag = fieldTemplate.content.cloneNode(true);
        const card = frag.querySelector(".field-card");
        card.querySelector(".field-card-title").textContent =
          `${FIELD_LABELS[key]}  (${key})`;

        const changeNote = changes[key];
        if (changeNote) {
          const note = document.createElement("p");
          note.className = "field-card-change";
          note.innerHTML = `<strong>What changed:</strong> ${escapeHtml(changeNote)}`;
          // Insert after the header
          const header = card.querySelector("header");
          header.insertAdjacentElement("afterend", note);
        }

        const valueEl = card.querySelector(".field-card-value");
        valueEl.textContent = correction[key];
        const btn = card.querySelector(".field-card-copy");
        btn.addEventListener("click", () => copyToClipboard(correction[key], btn));
        generateFields.appendChild(frag);
      });
    }
    generateFields.hidden = false;

    generateRaw.textContent = JSON.stringify(correction, null, 2);
    generateRawWrap.hidden = false;
  }

  copyRawBtn.addEventListener("click", () => {
    copyToClipboard(generateRaw.textContent, copyRawBtn);
  });

  // ── Mark corrected + deep-link restore ─────────────────────────
  const markCorrectedWrap = document.getElementById("mark-corrected-wrap");
  const markCorrectedBtn = document.getElementById("mark-corrected-btn");
  const markCorrectedStatus = document.getElementById("mark-corrected-status");

  function showMarkCorrectedIfEligible(wizardStatus) {
    if (!markCorrectedWrap) return;
    const canMark =
      !!state.gmail_message_id &&
      wizardStatus !== "corrected" &&
      (wizardStatus === "completed" || !!state.generate);
    markCorrectedWrap.hidden = !canMark;
    if (wizardStatus === "corrected") {
      setStatus(markCorrectedStatus, "Already marked as corrected.", "ok");
      markCorrectedBtn.disabled = true;
    } else {
      setStatus(markCorrectedStatus, "", "");
      markCorrectedBtn.disabled = false;
    }
  }

  if (markCorrectedBtn) {
    markCorrectedBtn.addEventListener("click", async () => {
      if (!state.gmail_message_id) {
        setStatus(markCorrectedStatus, "No Gmail message associated.", "error");
        return;
      }
      markCorrectedBtn.disabled = true;
      setStatus(markCorrectedStatus, "Marking as corrected…", "info");
      try {
        const resp = await fetch(
          `/api/corrections/wizard/${encodeURIComponent(state.gmail_message_id)}/mark-corrected`,
          { method: "POST" }
        );
        const text = await resp.text();
        let payload;
        try { payload = JSON.parse(text); } catch (_) { payload = { detail: text }; }
        if (!resp.ok) throw new Error(payload.detail || `HTTP ${resp.status}`);
        setStatus(markCorrectedStatus, "Marked as corrected.", "ok");
      } catch (err) {
        markCorrectedBtn.disabled = false;
        setStatus(markCorrectedStatus, `Error: ${err.message}`, "error");
      }
    });
  }

  async function autoRestoreFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const mid = params.get("gmail_message_id");
    if (!mid) return;

    try {
      const resp = await fetch(
        `/api/corrections/wizard/${encodeURIComponent(mid)}`
      );
      if (!resp.ok) return;
      const record = await resp.json();

      state.gmail_message_id = record.gmail_message_id || mid;

      emailForm.querySelector('[name="sender_name"]').value = record.sender_name || "";
      emailForm.querySelector('[name="sender_email"]').value = record.sender_email || "";
      emailForm.querySelector('[name="subject"]').value = record.subject || "";
      emailForm.querySelector('[name="body"]').value = record.body || record.snippet || "";

      state.email = {
        sender_name: record.sender_name || "",
        sender_email: record.sender_email || "",
        subject: record.subject || "",
        body: record.body || record.snippet || "",
      };

      restoreWizardState(record.state || {}, record.status || "new");
      showMarkCorrectedIfEligible(record.status);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (err) {
      console.error("Deep-link restore failed", err);
    }
  }

  autoRestoreFromUrl();
})();
