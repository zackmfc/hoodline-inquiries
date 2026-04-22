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
  const inboxLimitSel = document.getElementById("inbox-limit");
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

  inboxFetchBtn.addEventListener("click", async () => {
    const limit = parseInt(inboxLimitSel.value, 10) || 5;
    inboxStatus.textContent = "Loading queue from Gmail…";
    inboxStatus.classList.remove("status-late", "status-done");
    inboxStatus.classList.add("status-active");
    inboxList.hidden = true;
    inboxList.innerHTML = "";

    try {
      const resp = await fetch(`/api/corrections/inbox?limit=${limit}`);
      const text = await resp.text();
      let payload;
      try { payload = JSON.parse(text); } catch (_) { payload = { detail: text }; }
      if (!resp.ok) {
        throw new Error(payload.detail || `HTTP ${resp.status}`);
      }

      const messages = payload.messages || [];
      if (messages.length === 0) {
        inboxStatus.textContent = "Queue is empty — no unread correction-candidate emails.";
        inboxStatus.classList.remove("status-active", "status-late");
        inboxStatus.classList.add("status-done");
        return;
      }

      messages.forEach((msg) => {
        const frag = inboxItemTemplate.content.cloneNode(true);
        const btn = frag.querySelector(".inbox-item-btn");
        frag.querySelector(".inbox-item-subject").textContent =
          msg.subject || "(no subject)";
        frag.querySelector(".inbox-item-date").textContent =
          formatReceivedAt(msg.received_at);
        frag.querySelector(".inbox-item-sender").textContent =
          msg.sender_raw || msg.sender_email || "(unknown sender)";
        frag.querySelector(".inbox-item-snippet").textContent =
          msg.snippet || "";
        btn.addEventListener("click", () => prefillFromMessage(msg));
        inboxList.appendChild(frag);
      });

      inboxList.hidden = false;
      inboxStatus.textContent =
        `Loaded ${messages.length} message${messages.length === 1 ? "" : "s"}. Click one to prefill the form.`;
      inboxStatus.classList.remove("status-active", "status-late");
      inboxStatus.classList.add("status-done");
    } catch (err) {
      inboxStatus.textContent = `Error: ${err.message}`;
      inboxStatus.classList.remove("status-active", "status-done");
      inboxStatus.classList.add("status-late");
    }
  });

  function prefillFromMessage(msg) {
    emailForm.querySelector('[name="sender_name"]').value = msg.sender_name || "";
    emailForm.querySelector('[name="sender_email"]').value = msg.sender_email || "";
    emailForm.querySelector('[name="subject"]').value = msg.subject || "";
    emailForm.querySelector('[name="body"]').value = msg.body || msg.snippet || "";

    // Highlight the selected item
    inboxList.querySelectorAll(".inbox-item").forEach((el) => {
      el.classList.remove("is-selected");
    });
    const items = Array.from(inboxList.querySelectorAll(".inbox-item"));
    const idx = items.findIndex((el) => {
      const subjEl = el.querySelector(".inbox-item-subject");
      return subjEl && subjEl.textContent === (msg.subject || "(no subject)");
    });
    if (idx >= 0) items[idx].classList.add("is-selected");

    emailForm.scrollIntoView({ behavior: "smooth", block: "nearest" });
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
      const res = await postJSON("/api/corrections/assess", data);
      state.email = data;
      state.assess = res;

      scoreCRVS.textContent = res.CRVS;
      scoreSAS.textContent = res.SAS;
      meterCRVS.style.width = `${(res.CRVS / 10) * 100}%`;
      meterSAS.style.width = `${(res.SAS / 10) * 100}%`;
      reasonCRVS.textContent = res.crvs_reasoning || "";
      reasonSAS.textContent = res.sas_reasoning || "";
      assessResult.hidden = false;

      gateVerdict.classList.remove("gate-pass", "gate-fail");
      if (res.gate_passed) {
        gateVerdict.classList.add("gate-pass");
        const hintBits = [];
        if (res.article_url_hint) hintBits.push(`URL: ${res.article_url_hint}`);
        if (res.article_title_hint) hintBits.push(`Title: "${res.article_title_hint}"`);
        gateVerdict.textContent =
          "Both scores above 4 — proceed to step 2." +
          (hintBits.length ? `  (${hintBits.join(" · ")})` : "");
        setStepState(steps.email, "done");
        unlockStep(steps.discord);
        setStatus(assessStatus, "", "ok");
      } else {
        gateVerdict.classList.add("gate-fail");
        gateVerdict.textContent =
          "At least one score is 4 or lower — skipping the remaining steps. Re-assess or handle manually.";
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

  function setDiscordResolved({ article_id, cms_edit_url }) {
    articleIdEl.textContent = article_id ?? "—";
    editUrlLink.textContent = cms_edit_url;
    editUrlLink.href = cms_edit_url;
    cmsEditBtn.href = cms_edit_url;
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

  locatorRunBtn.addEventListener("click", async () => {
    if (!state.email) {
      setStatus(locatorStatus, "Run step 1 first.", "error");
      return;
    }

    setStatus(locatorStatus, "Locating article… (may scrape via Decodo)", "info");
    locatorTrace.hidden = true;
    locatorTrace.innerHTML = "";

    const payload = {
      subject: state.email.subject || "",
      body: state.email.body || "",
      article_url_hint: (state.assess && state.assess.article_url_hint) || "",
      article_title_hint: (state.assess && state.assess.article_title_hint) || "",
    };

    try {
      const res = await postJSON("/api/corrections/locate-discord", payload);
      renderLocatorTrace(res.trace);

      if (res.found && res.cms_edit_url) {
        setStatus(
          locatorStatus,
          `Found article #${res.article_id} via "${res.authoritative_title}".`,
          "ok"
        );
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
      const res = await postJSON("/api/corrections/parse-discord", data);
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
})();
