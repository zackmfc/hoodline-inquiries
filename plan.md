# Hoodline Correction Automation — Plan

## 1. Goal

Automate the intake, triage, verification, and staging of reader correction requests sent to `contact@hoodline.com`, so Eddie (or the on-call editor) only spends time on the final review-and-publish step.

**Explicitly out of scope for v1:** auto-publishing. Every edit is staged as a draft in the Hoodline CMS and requires human approval before going live.

---

## 2. System overview

```
Gmail inbox (contact@hoodline.com)
    │
    ▼ [Gmail filter: keywords → forward/label]
Intake service (webhook or Gmail API pull)
    │
    ▼
Classifier (LLM) — is this actually a correction request?
    │
    ▼
Article resolver — find the Hoodline article in question
    ├─ Direct link in email?  → fetch URL
    ├─ Title mentioned?        → Discord search → edit URL
    └─ Vague reference?        → LLM generates search query → Discord search
    │
    ▼
Article fetcher (headless browser, authenticated)
    │   Collect: title, meta, synopsis, body, publish date, byline, existing notes
    ▼
Verification agent
    ├─ Re-open every outbound link in the article (has the source issued its own correction / editor's note?)
    ├─ Web search grounding (corroborate the claim)
    └─ Output: confidence 0–10, recommended action, proposed edit
    │
    ▼
Remediation classifier — decides one of:
    1. Silent correction (no notice)
    2. Update note: "Update ({today's date}): …"
    3. Editor's Note at bottom
    4. Editor's Note at top
    │
    ▼
Draft stager — prepares the diff in Hoodline CMS as an unpublished draft
    │
    ▼
Review dashboard (Eddie / editor logs in)
    │
    ▼ [human approves or rejects]
Publish
```

---

## 3. Component details

### 3.1 Gmail intake

- Create a Gmail filter on `contact@hoodline.com` matching: `correction`, `error`, `mistake`, `inaccurate`, `wrong`, `incorrect`, `request`, `demand`, `typo`, `update`, `editor's note`, `retract`.
- Filter applies a label (e.g. `auto/correction-candidate`) and optionally forwards to the intake service.
- Service polls Gmail API for unread messages with that label (preferred over forwarding — richer metadata, threading, attachments).
- Every processed email is re-labeled `auto/processed` and tagged with the resulting case ID.
- 70%+ false-positive rate from the filter is acceptable — the classifier is the real gate.

### 3.2 Classifier

- Single LLM call with the email body + subject + sender.
- Output schema:
  - `is_correction_request` (bool)
  - `request_type` (factual_error | outdated_info | missing_context | opinion_disagreement | pr_pitch | other)
  - `specific_claim` (what the sender says is wrong)
  - `proposed_correction` (what they say it should be)
  - `referenced_article_hint` (URL if present, else title/topic/slug fragment)
  - `sender_authority_signal` (first_party | expert | reader | anonymous | unknown)
- Anything that isn't `is_correction_request = true` with `request_type` in {factual_error, outdated_info, missing_context} drops out of the pipeline and is logged for Eddie's weekly review.

### 3.3 Article resolver

Priority order:

1. **Direct URL** in the email body pointing to hoodline.com → strip query/fragment, canonicalize, done.
2. **Title or near-title** quoted in the email → Discord search in the editorial server for the first ~40 chars of the title; expect to land on the editor's post that contains the title and the edit URL in the same (or adjacent) message.
3. **Vague reference** ("your article last week about the Mission taqueria that closed") → LLM generates 2–3 candidate search queries; run each against Discord; rank candidates by recency and title similarity; if top candidate isn't ≥0.8 similarity, kick to human triage.

The Discord integration uses a bot account with read access to the editorial channel. Cache the last N days of editor posts in a Postgres-backed index (`pgvector` embedding column) so we aren't hammering Discord search on every request.

### 3.4 Article fetcher

- Headless browser (Playwright) with a dedicated bot service account logged into the Hoodline CMS.
- Session cookies stored in a secrets manager, refreshed on auth failure.
- Fetch from both the public URL (to get the rendered reader view) and the CMS edit URL (to get the source fields).
- Capture: headline, dek/subtitle, meta description, body (as structured blocks if CMS exposes them, else HTML), byline, publish date, last-updated date, any existing editor's notes or update stamps, all outbound links in the body.

### 3.5 Verification agent

This is the most important and highest-risk component. Budget the most engineering effort here.

Three parallel checks:

1. **Outbound-link check.** For every link in the original Hoodline article, fetch the current page and ask the LLM: "Has this source issued a correction, editor's note, or update that would affect the claim in the Hoodline article?" If yes, capture the exact note text — it will inform our own wording.
2. **Web search grounding.** Based on the `specific_claim` from the classifier, run 2–4 web searches for corroborating or contradicting evidence. Prefer primary sources (official sites, government records, the subject's own channels) over aggregators.
3. **Internal consistency check.** Does the correction request contradict something else in the article that the sender didn't flag? If so, surface it — Eddie needs to know.

Output:

- `confidence` (0–10 integer) — how confident we are the correction is warranted
- `evidence` (list of {source_url, quote, weight})
- `contradicting_evidence` (list, same shape)
- `recommended_action` (one of the four remediation types below, or `reject` or `needs_human`)
- `recommended_edit` (structured diff: field, old_value, new_value)
- `suggested_note_text` (if any notice is warranted — see §3.6)

**Confidence thresholds:**

- 0–4: recommend reject or flag for Eddie as ambiguous; no draft staged
- 5–7: stage draft, flag as "low confidence — verify evidence carefully"
- 8–10: stage draft, flag as "high confidence — standard review"

Never auto-publish regardless of score in v1.

### 3.6 Remediation classifier & note writer

Given a verified correction, pick exactly one of:

| Option | When to use |
|---|---|
| **1. Silent correction** | Typos, formatting, obviously-wrong minor details (misspelled name on first reference that's correct elsewhere, broken link). No reader-facing notice. |
| **2. Update stamp** `Update ({today's date}): …` | New information has emerged since publication; the original wasn't wrong when published but is now incomplete. Appended at top or bottom depending on house style. |
| **3. Editor's Note at bottom** | Factual error of modest significance — wrong date, wrong number, wrong attribution — where the correction doesn't change the thrust of the article. |
| **4. Editor's Note at top** | Material factual error that affects how a reader would interpret the whole piece; corrections involving named individuals' reputations; any correction where a reader who only reads the top of the article needs to know. |

**Note-writing rules (strict):**

- Notes must accurately identify *the category* of error (date, sequence, attribution, figure, quotation, identity) without being self-flagellating or drawing more attention to the publication's process than to the fact.
- Example of the rewrite the user requested: an article that pulled outdated info from a year ago should **not** say *"A previous version of this article inaccurately pulled information from a year ago."* It should say *"A previous version of this article misstated the sequence of events or dates,"* provided that framing accurately describes the error.
- **Editor's notes must never mention AI, hallucination, automated systems, model error, generation, or any phrasing that implies the content was produced by AI.** This is a hard rule.
- Notes must never speculate about intent ("we mistakenly believed…"), never apologize in a way that implies broader unreliability ("we regret the frequent errors…"), and never name individual staff.
- Default tense: past ("A previous version… misstated…"). Default voice: neutral and brief. One or two sentences max.
- The note writer is a separate LLM call with these rules as a system prompt and a few-shot set of good/bad examples. Its output is reviewed by the same human who approves the edit.

### 3.7 Draft stager

- Uses the same Playwright session as the fetcher.
- Applies the structured diff to the CMS draft: replaces the corrected field(s), prepends/appends the note if required, updates the "last edited" metadata.
- Does **not** click publish.
- Generates a preview URL and a side-by-side diff (before / after) and posts both to the review dashboard.

### 3.8 Review dashboard

A simple internal web app. For each pending case, show:

- Original email (collapsible)
- Detected article (title + link + preview)
- Specific claim + proposed correction from the sender
- Verification agent's evidence and contradicting evidence, each as a clickable source card
- Confidence score and recommended action
- Side-by-side diff of the staged CMS change
- Proposed editor's note text (editable inline)
- Actions: `Approve & publish`, `Edit note then publish`, `Reject`, `Send back for re-verification`, `Escalate to Eddie`

Every action is logged with the reviewer's identity and timestamp.

---

## 4. Data & state

One case record per incoming correction request, persisted from intake through resolution:

```
case_id
created_at
gmail_message_id, gmail_thread_id
classifier_output (JSON)
article_url, article_cms_id (nullable until resolver succeeds)
fetched_article_snapshot (JSON, stored for audit)
verification_output (JSON)
recommended_action
staged_diff (JSON)
staged_note_text
reviewer_user_id (nullable)
reviewer_action, reviewer_notes
final_published_at
```

PostgreSQL is the system of record from v1 (cases, job state, audit logs, and Discord search cache/index).

---

## 5. Tech stack (proposed)

- **Runtime:** Python 3.11+ for the pipeline, Node for the dashboard (or keep it all Python with FastAPI + HTMX — simpler).
- **LLM calls:** Claude (Sonnet for classifier/note-writer, Opus for verification agent where stakes are higher). Structured outputs via JSON mode.
- **Browser automation:** Playwright (Python).
- **Gmail:** Gmail API via Google service account with domain-wide delegation.
- **Database:** PostgreSQL 16+ with Alembic migrations; `pgvector` enabled for fuzzy title lookup on cached Discord editorial posts.
- **Discord:** discord.py bot with read permission on the editorial channel; cached post metadata and embeddings stored in Postgres.
- **Queue:** Simple Redis + RQ, or just a DB-backed job table if volume is low.
- **Dashboard:** FastAPI + HTMX + Tailwind. Authentication via Google SSO restricted to the Hoodline domain.
- **Hosting:** Single small VM is enough. Secrets in a managed secret store.
- **Containerization:** Docker for local dev and deployment parity; use `docker compose` to run the pipeline, dashboard, queue, and dependencies.

---

## 6. Phased rollout

**Execution rule for every phase/step:** build and run changes in Docker, then commit and `git push` before starting the next phase.

**Phase 0 — Instrumentation (week 1).** Set up the Gmail filter. Log every matching email for 1–2 weeks to get a real volume estimate and a corpus for prompt tuning. No automation yet. Run and validate using Docker; then commit and `git push`.

**Phase 1 — Classifier + article resolver + dashboard read-only (weeks 2–3).** Build intake through the resolver. Dashboard shows "here's what we found" but no edits are staged. Eddie manually corrects in the CMS; we measure how often our resolver matched the right article. Run and validate using Docker; then commit and `git push`.

**Phase 2 — Verification agent (weeks 4–5).** Add verification, confidence scoring, and the suggested note text. Dashboard now shows a recommended edit, but still no CMS writes. Measure: on cases Eddie approves, did we recommend the right action. Run and validate using Docker; then commit and `git push`.

**Phase 3 — Staged drafts (weeks 6–7).** Enable the draft stager. Eddie reviews in the CMS's native draft UI and clicks publish. Measure: time-to-publish per correction. Run and validate using Docker; then commit and `git push`.

**Phase 4 — Iteration.** Tune thresholds, expand keyword coverage, add new note templates as new error categories appear. Only consider auto-publishing for the narrowest, most boring category (link fixes, clear typos) and only after months of clean phase-3 data. Run and validate using Docker; then commit and `git push`.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Bad-faith correction requests (someone tries to get a true fact "corrected" to a false one) | Verification agent must find corroborating evidence; reputational/legal-sensitive corrections always escalate to human regardless of score |
| CMS session expiry or schema change breaks the stager | Automated health check that loads a known article's edit page daily; fail loudly |
| Discord-based article matching fails silently | Log match confidence; anything below threshold routes to human. Also store match-success rate as a dashboard metric |
| LLM hallucinates a "correction" the sender didn't ask for | Classifier output must quote the sender verbatim; verification agent is only asked to verify that specific quoted claim |
| Editor's note accidentally reveals AI involvement | Hard-coded deny-list in the note-writer system prompt; automated post-generation regex scan for forbidden terms; rejected notes regenerate |
| Legal exposure on defamation-adjacent corrections | Any correction involving allegations about a named person routes straight to Eddie; never auto-staged |
| Silent pipeline failure | Every case must reach a terminal state (published / rejected / escalated) within 48 hours or it alerts |

---

## 8. Open questions for you

1. Roughly how many correction-shaped emails per week does `contact@` receive? This sets whether we need a queue at all.
2. Does the Hoodline CMS have an API, or is headless-browser scripting the only option? An API is dramatically more reliable.
3. Who besides Eddie should have dashboard access? (Affects auth and audit design.)
4. For the outbound-link check: do you want us to fetch archived versions (Wayback) if the live link has changed, or just take the current state?
5. Is there an existing house style guide for editor's notes we should encode, beyond the rules you gave me?
6. Should the dashboard also surface *non-correction* emails the classifier rejected, in case it's wrong?

---

## 9. Dev schedule (Claude Code auto mode estimate)

Start date: **Thursday, April 16, 2026**

Assumption for estimates below: Claude Code runs in auto mode with repo access, Docker running, and required credentials/secrets already provisioned. Estimates are active build time.

Progress update as of **Thursday, April 16, 2026**: `foundation`, `3.1 gmail intake`, `3.2 classifier`, `3.3 article resolver`, `3.4 article fetcher`, `3.5 verification agent`, `3.6 remediation classifier + note writer`, `3.7 draft stager`, and `3.8 review dashboard` are complete.
Timing note: commit/log timestamps show `3.3` ran from about **12:33 PM to 12:40 PM PT** on April 16, 2026 (about **7 minutes** active build window), so estimates were tightened.
`3.8` completed in approximately **14 minutes** of active build time (22:24–22:38 UTC on April 16, 2026), well under the 1.5–3.0 hour estimate.
`3.5` completed in approximately **5 minutes** of active build time (22:41–22:46 UTC on April 16, 2026), well under the 3.0–6.0 hour estimate. Includes three parallel verification checks (outbound link, web search grounding, internal consistency), Claude integration with rules fallback, DuckDuckGo search, and verification_events audit table.

| System step | Status | Dates | Claude Code auto-mode estimate |
|---|---|---|---|
| Foundation: Docker + Postgres + migrations + service skeleton | Complete | Apr 16, 2026 | 0.5-0.75 hours |
| 3.1 Gmail intake | Complete | Apr 16, 2026 | 0.5-0.75 hours |
| 3.2 Classifier | Complete | Apr 16, 2026 | 0.2-0.3 hours |
| 3.3 Article resolver | Complete | Apr 16, 2026 | 0.1-0.2 hours |
| 3.4 Article fetcher (Playwright + CMS auth/session handling) | Complete | Apr 16, 2026 | 0.5-1.0 hours |
| 3.5 Verification agent (links + web grounding + confidence) | Complete | Apr 16, 2026 | ~0.08 hours (5 min actual) |
| 3.6 Remediation classifier + note writer | Complete | Apr 16, 2026 | 0.4-0.8 hours |
| 3.7 Draft stager (CMS draft write, no publish) | Complete | Apr 16, 2026 | 0.6-1.2 hours |
| 3.8 Review dashboard | Complete | Apr 16, 2026 | ~0.17 hours (10 min actual) |
| End-to-end integration, smoke tests, docs, runbooks | Pending | Apr 19, 2026 | 0.75-1.5 hours |

Estimated completed active build time: **~3.1-5.3 hours** (9 of 10 steps done).
Estimated remaining active build time: **0.75-1.5 hours** (integration only).
Estimated total active build time (revised): **3.85-6.8 hours**.
Updated projected completion window: **April 16, 2026** (only integration remains).

Execution expectation per step: run in Docker, validate, commit, and `git push` before moving to the next step.
