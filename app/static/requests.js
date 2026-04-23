(() => {
  const BUCKETS = ["unread", "rejected", "pending", "completed"];

  const STATUS_LABELS = {
    new: { text: "New", cls: "" },
    assessed: { text: "Assessed", cls: "status-active" },
    located: { text: "Located", cls: "status-active" },
    image_only: { text: "Image-only", cls: "status-late" },
    gate_failed: { text: "Gate failed", cls: "status-late" },
    completed: { text: "Generated", cls: "status-active" },
    corrected: { text: "Corrected", cls: "status-done" },
    triaged_pending: { text: "Triaged · Pending", cls: "status-active" },
    triaged_rejected: { text: "Triaged · Rejected", cls: "status-late" },
  };

  const tabs = Array.from(document.querySelectorAll(".requests-tab"));
  const list = document.getElementById("requests-list");
  const empty = document.getElementById("requests-empty");
  const status = document.getElementById("requests-status");
  const refreshBtn = document.getElementById("requests-refresh-btn");
  const itemTemplate = document.getElementById("request-item-template");

  let activeBucket = "unread";
  const countEls = {};
  BUCKETS.forEach((b) => {
    countEls[b] = document.querySelector(`[data-bucket-count="${b}"]`);
  });

  function setStatus(text, tone) {
    status.textContent = text || "";
    status.classList.remove("status-late", "status-active", "status-done");
    if (tone === "error") status.classList.add("status-late");
    if (tone === "info") status.classList.add("status-active");
    if (tone === "ok") status.classList.add("status-done");
  }

  function formatDate(iso) {
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
    } catch (_) {
      return "";
    }
  }

  async function fetchBucket(bucket) {
    const resp = await fetch(
      `/api/corrections/requests?bucket=${encodeURIComponent(bucket)}&limit=100`
    );
    const text = await resp.text();
    let payload;
    try { payload = JSON.parse(text); } catch (_) { payload = { detail: text }; }
    if (!resp.ok) {
      throw new Error(payload.detail || `HTTP ${resp.status}`);
    }
    return payload.items || [];
  }

  async function markCorrected(gmailMessageId, itemEl) {
    if (!gmailMessageId) return;
    const resp = await fetch(
      `/api/corrections/wizard/${encodeURIComponent(gmailMessageId)}/mark-corrected`,
      { method: "POST" }
    );
    const text = await resp.text();
    let payload;
    try { payload = JSON.parse(text); } catch (_) { payload = { detail: text }; }
    if (!resp.ok) {
      throw new Error(payload.detail || `HTTP ${resp.status}`);
    }
    if (itemEl) itemEl.remove();
    setStatus("Marked as corrected.", "ok");
    refreshCounts();
  }

  function renderItem(item) {
    const frag = itemTemplate.content.cloneNode(true);
    const root = frag.querySelector(".request-item");
    const link = frag.querySelector(".request-item-link");
    const subjectEl = frag.querySelector(".request-item-subject");
    const dateEl = frag.querySelector(".request-item-date");
    const senderEl = frag.querySelector(".request-item-sender");
    const snippetEl = frag.querySelector(".request-item-snippet");
    const metaEl = frag.querySelector(".request-item-meta");
    const actionsEl = frag.querySelector(".request-item-actions");
    const markBtn = frag.querySelector(".request-item-mark-corrected");

    subjectEl.textContent = item.subject || "(no subject)";
    dateEl.textContent = formatDate(item.received_at || item.updated_at);
    senderEl.textContent = item.sender_raw || item.sender_email || "(unknown sender)";
    snippetEl.textContent = item.snippet || "";

    const statusKey = item.status || "new";
    const conf = STATUS_LABELS[statusKey] || STATUS_LABELS.new;

    const pills = [];
    const statusPill = `<span class="pill ${conf.cls}">${conf.text}</span>`;
    pills.push(statusPill);

    if (item.assess && typeof item.assess.CRVS === "number") {
      pills.push(
        `<span class="pill mono">CRVS ${item.assess.CRVS} · SAS ${item.assess.SAS ?? "–"}</span>`
      );
    }
    if (item.generate_crvs2 !== undefined && item.generate_crvs2 !== null) {
      pills.push(`<span class="pill mono">CRVS2 ${item.generate_crvs2}</span>`);
    }
    if (item.article && item.article.article_id) {
      pills.push(`<span class="pill mono">#${item.article.article_id}</span>`);
    }
    if (item.last_touched_by) {
      pills.push(`<span class="pill muted">${item.last_touched_by}</span>`);
    }
    metaEl.innerHTML = pills.join(" ");

    const href = item.gmail_message_id
      ? `/corrections?gmail_message_id=${encodeURIComponent(item.gmail_message_id)}`
      : "/corrections";
    link.href = href;

    if (statusKey === "completed" && item.gmail_message_id) {
      actionsEl.hidden = false;
      markBtn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        markBtn.disabled = true;
        markBtn.textContent = "Marking…";
        try {
          await markCorrected(item.gmail_message_id, root);
        } catch (err) {
          markBtn.disabled = false;
          markBtn.textContent = "Mark as corrected";
          setStatus(`Error: ${err.message}`, "error");
        }
      });
    }

    return frag;
  }

  async function loadBucket(bucket) {
    setStatus("Loading…", "info");
    list.innerHTML = "";
    empty.hidden = true;

    try {
      const items = await fetchBucket(bucket);
      if (countEls[bucket]) countEls[bucket].textContent = String(items.length);

      if (items.length === 0) {
        empty.hidden = false;
        setStatus("", "");
        return;
      }

      items.forEach((item) => list.appendChild(renderItem(item)));
      setStatus(
        `Loaded ${items.length} request${items.length === 1 ? "" : "s"}.`,
        "ok"
      );
    } catch (err) {
      setStatus(`Error: ${err.message}`, "error");
    }
  }

  async function refreshCounts() {
    await Promise.all(
      BUCKETS.map(async (b) => {
        try {
          const items = await fetchBucket(b);
          if (countEls[b]) countEls[b].textContent = String(items.length);
        } catch (_) {
          if (countEls[b]) countEls[b].textContent = "–";
        }
      })
    );
  }

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const bucket = tab.dataset.bucket;
      if (!bucket || bucket === activeBucket) return;
      activeBucket = bucket;
      tabs.forEach((t) => {
        const isActive = t === tab;
        t.classList.toggle("is-active", isActive);
        t.setAttribute("aria-selected", isActive ? "true" : "false");
      });
      loadBucket(bucket);
    });
  });

  refreshBtn.addEventListener("click", () => {
    refreshCounts();
    loadBucket(activeBucket);
  });

  refreshCounts();
  loadBucket(activeBucket);

  // --- Keyword analysis ---------------------------------------------------

  const kwBtn = document.getElementById("keyword-analysis-run-btn");
  const kwStatus = document.getElementById("keyword-analysis-status");
  const kwResults = document.getElementById("keyword-analysis-results");
  const kwPendingCount = document.getElementById("keyword-pending-count");
  const kwRejectedCount = document.getElementById("keyword-rejected-count");
  const kwRowTemplate = document.getElementById("keyword-row-template");

  const NGRAM_KEYS = ["unigrams", "bigrams", "trigrams"];

  function setKwStatus(text, tone) {
    if (!kwStatus) return;
    kwStatus.textContent = text || "";
    kwStatus.classList.remove("status-late", "status-active", "status-done");
    if (tone === "error") kwStatus.classList.add("status-late");
    if (tone === "info") kwStatus.classList.add("status-active");
    if (tone === "ok") kwStatus.classList.add("status-done");
  }

  function copyValueFor(entry) {
    // Multi-word phrases are more useful with quotes for Gmail filters.
    const term = entry.term || "";
    return (entry.n && entry.n > 1) ? `"${term}"` : term;
  }

  function renderKeywordRow(entry) {
    const frag = kwRowTemplate.content.cloneNode(true);
    const termEl = frag.querySelector(".keyword-term");
    const metaEl = frag.querySelector(".keyword-meta");
    const copyBtn = frag.querySelector(".keyword-copy");

    termEl.textContent = entry.term;
    const primaryPct = Math.round((entry.primary_rate || 0) * 100);
    const contrastPct = Math.round((entry.contrast_rate || 0) * 100);
    metaEl.textContent =
      `${entry.primary_count} here (${primaryPct}%) · ${entry.contrast_count} there (${contrastPct}%)`;

    const copyValue = copyValueFor(entry);
    copyBtn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(copyValue);
        copyBtn.textContent = "Copied";
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 1200);
      } catch (_) {
        copyBtn.textContent = "Error";
      }
    });

    return frag;
  }

  function renderKeywordBucket(bucketPrefix, groups, countEl, totalDocs) {
    if (countEl) countEl.textContent = `${totalDocs} email${totalDocs === 1 ? "" : "s"}`;
    NGRAM_KEYS.forEach((ngramKey) => {
      const listEl = document.querySelector(
        `[data-keyword-list="${bucketPrefix}-${ngramKey}"]`
      );
      const emptyEl = document.querySelector(
        `[data-keyword-empty="${bucketPrefix}-${ngramKey}"]`
      );
      if (!listEl || !emptyEl) return;

      const entries = (groups && groups[ngramKey]) || [];
      listEl.innerHTML = "";
      if (entries.length === 0) {
        emptyEl.hidden = false;
        return;
      }
      emptyEl.hidden = true;
      entries.forEach((e) => listEl.appendChild(renderKeywordRow(e)));
    });
  }

  async function runKeywordAnalysis() {
    setKwStatus("Analyzing…", "info");
    kwBtn.disabled = true;
    try {
      const resp = await fetch("/api/corrections/keyword-analysis?top_n=20&min_count=2");
      const text = await resp.text();
      let payload;
      try { payload = JSON.parse(text); } catch (_) { payload = { detail: text }; }
      if (!resp.ok) {
        throw new Error(payload.detail || `HTTP ${resp.status}`);
      }
      kwResults.hidden = false;
      renderKeywordBucket(
        "pending",
        payload.pending_distinctive || {},
        kwPendingCount,
        payload.pending_count || 0,
      );
      renderKeywordBucket(
        "rejected",
        payload.rejected_distinctive || {},
        kwRejectedCount,
        payload.rejected_count || 0,
      );
      setKwStatus(
        `Analyzed ${payload.pending_count || 0} pending vs ${payload.rejected_count || 0} rejected.`,
        "ok",
      );
    } catch (err) {
      setKwStatus(`Error: ${err.message}`, "error");
    } finally {
      kwBtn.disabled = false;
    }
  }

  if (kwBtn) {
    kwBtn.addEventListener("click", runKeywordAnalysis);
  }
})();
