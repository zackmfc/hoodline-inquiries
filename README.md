# Hoodline Corrections

Internal FastAPI application for triaging reader correction requests, locating the referenced Hoodline article, generating a correction proposal, and publishing approved field updates to the Hoodline CMS API.

The repository contains two related workflows:

- **Corrections Wizard / Correction Queue**: the primary operator workflow. It pulls Gmail messages, scores request validity and sender authority, locates the article, loads CMS fields, asks Claude to propose edits, and lets an admin publish selected fields through the CMS API.
- **Pipeline Tester / Review Dashboard**: an earlier end-to-end pipeline harness. It runs the planned Gmail -> classifier -> resolver -> fetcher -> verification -> remediation -> stager -> review flow, stores audit events, and is useful for smoke testing individual pipeline components.

The app never auto-publishes without an authenticated admin action. The wizard's publish endpoint applies selected fields to an existing CMS article via `PATCH /api/articles/:id`.

## Repository Layout

```text
.
|-- Dockerfile
|-- docker-compose.yml
|-- requirements.txt
|-- plan.md
|-- .env.example
|-- app/
|   |-- main.py                 # FastAPI app, routes, page rendering, job workers
|   |-- storage.py              # Postgres schema and persistence methods
|   |-- auth.py                 # Scrypt password hashing and verification
|   |-- gmail_client.py         # Gmail API domain-delegated intake
|   |-- corrections.py          # Corrections Wizard Claude prompts and parsing
|   |-- article_locator.py      # CMS/Discord/Decodo article-location cascade
|   |-- cms_client.py           # Hoodline CMS API client
|   |-- decodo_client.py        # Decodo Google/page scraping client
|   |-- discord_cache.py        # Discord guild scan into editorial_posts
|   |-- classifier.py           # Pipeline correction-intent classifier
|   |-- resolver.py             # Pipeline editorial-cache resolver
|   |-- fetcher.py              # Public article HTML fetcher
|   |-- verification.py         # Pipeline verification agent
|   |-- remediation.py          # Pipeline note/action classifier
|   |-- stager.py               # Pipeline draft staging/diff helper
|   |-- keyword_analysis.py     # Pending-vs-rejected keyword analysis
|   |-- templates/              # Jinja pages
|   `-- static/                 # Browser JS and CSS
|-- logs/
|   |-- pipeline.log
|   `-- runs/
`-- secrets/                    # Mounted read-only in Docker; ignored by git
```

The parent directory also contains `hoodline-cms-api/`, which documents the external CMS API contract used by `app/cms_client.py`.

## Tech Stack

- Python 3.12 in Docker
- FastAPI + Uvicorn
- Jinja2 templates and plain JavaScript
- PostgreSQL 16 via `psycopg`
- Gmail API via Google service account domain-wide delegation
- Anthropic Messages API for scoring and correction generation
- Decodo scraper API for Google search and page snapshots
- Discord REST API for editorial-post cache population
- `requests` and BeautifulSoup for HTTP/page parsing

## Quick Start

1. Create your local environment file:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` with real credentials where needed. At minimum, set a strong `APP_SESSION_SECRET`. For full functionality, configure CMS, Anthropic, Gmail, Discord, and Decodo credentials.

3. Start the app and database:

   ```bash
   docker compose up --build
   ```

4. Open the app:

   ```text
   http://localhost:8080
   ```

5. Sign in with the configured default superuser from `DEFAULT_SUPERUSER_USERNAME` and `DEFAULT_SUPERUSER_PASSWORD`, then rotate that password from `/users`.

The app creates tables and bootstraps the default superuser during FastAPI startup.

## Local Development Without Docker

Docker is the supported path because `docker-compose.yml` provisions Postgres and the correct service hostnames. If running manually:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql://hoodline:hoodline@localhost:5432/hoodline'
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

You must provide a reachable Postgres database yourself.

## Configuration

All runtime configuration is environment-variable driven. See `.env.example` for the full list.

| Area | Variables | Purpose |
| --- | --- | --- |
| App/session | `APP_SESSION_SECRET`, `STATIC_ASSET_VERSION` | Session signing and static cache busting. |
| Database | `DATABASE_URL` | Postgres connection string. |
| Bootstrap user | `DEFAULT_SUPERUSER_USERNAME`, `DEFAULT_SUPERUSER_PASSWORD` | First admin account created on startup if missing. |
| Background jobs | `BACKGROUND_JOBS_ENABLED`, `BACKGROUND_JOB_WORKER_CONCURRENCY`, `BACKGROUND_JOB_POLL_SECONDS`, `BACKGROUND_JOB_STALE_SECONDS` | In-process worker threads that claim jobs from Postgres. |
| CMS | `CMS_BASE_URL`, `CMS_API_BASE_URL`, `CMS_API_EMAIL`, `CMS_API_PASSWORD`, `CMS_API_KEY`, `CMS_API_TIMEOUT_SECONDS` | Read/write access to Hoodline CMS API. Email/password login is preferred; static bearer token is legacy fallback. |
| Gmail | `GMAIL_DELEGATED_USER`, `GMAIL_DEFAULT_QUERY`, `GMAIL_SERVICE_ACCOUNT_FILE`, `GMAIL_SERVICE_ACCOUNT_JSON`, `CORRECTIONS_INBOX_QUERY` | Gmail queue access for `contact@hoodline.com`. |
| Anthropic | `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `CLASSIFIER_MODEL`, `VERIFICATION_MODEL`, `REMEDIATION_MODEL`, `CORRECTIONS_ASSESS_MODEL`, `CORRECTIONS_GENERATE_MODEL` | Claude-backed classification, verification, and wizard generation. |
| Discord | `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CACHE_DAYS` | Guild-wide scan for editorial messages containing CMS edit URLs. |
| Decodo | `DECODO_BASIC_AUTH_TOKEN`, `DECODO_USERNAME`, `DECODO_PASSWORD`, `DECODO_BASE_URL`, `DECODO_TIMEOUT_SECONDS` | Google search and page scraping for article location and outbound snapshots. |
| Fetch/stage | `FETCHER_TIMEOUT_SECONDS`, `FETCHER_USER_AGENT`, `CMS_FETCHER_SESSION_COOKIE`, `STAGER_MODE`, `STAGER_ENABLE_REMOTE_WRITES` | Older pipeline fetcher/stager settings. |

Do not commit `.env`, service-account JSON files, or anything in `secrets/`.

## User Interface

| Page | Route | Access | Purpose |
| --- | --- | --- | --- |
| Home | `/` | any signed-in user | Entry point for primary tools. |
| Corrections Wizard | `/corrections` | admin/superuser | Main workflow for processing a single correction request. |
| Correction Queue | `/requests` | admin/superuser | Bucketed list of unread, rejected, pending, and completed wizard emails. |
| Pipeline Tester | `/pipeline` | admin/superuser | Step-by-step execution of the older full pipeline. |
| Review | `/review` | admin/superuser | Review packets from pipeline runs that reached the review step. |
| Setup Guide | `/setup` | admin/superuser | Runtime dependency checks and setup notes. |
| Dev Schedule | `/schedule` | signed-in user | Static completion schedule from `SCHEDULE_TASKS`. |
| Users | `/users` | admin/superuser | Local user management. |
| Login/logout | `/login`, `/logout` | public | Session auth. |

Roles are `superuser`, `admin`, and `user`. Admin routes accept `superuser` and `admin`. A regular `user` can only access non-admin pages such as home and schedule.

## Primary Workflow: Corrections Wizard

The Corrections Wizard is implemented by `app/corrections.py`, `app/article_locator.py`, `app/cms_client.py`, `app/decodo_client.py`, `app/gmail_client.py`, `app/discord_cache.py`, and the wizard routes in `app/main.py`.

1. **Fetch Gmail queue**  
   `/api/corrections/inbox` enqueues a `corrections_inbox` job. The job reads messages matching `CORRECTIONS_INBOX_QUERY`, stores them in `correction_wizard_emails`, and returns their stored wizard status.

2. **Manual triage**  
   `/api/corrections/triage` marks a cached Gmail message as `triaged_pending` or `triaged_rejected`. This is used to improve the Gmail filter and drive keyword analysis.

3. **Assess validity and authority**  
   `/api/corrections/assess` enqueues `corrections_assess`. Claude returns:
   - `CRVS`: Correction Request Validity Score, 0-10
   - `SAS`: Sender's Authority Score, 0-10
   - article URL/title hints
   - image-only classification

   The gate passes only when both `CRVS > 4` and `SAS > 4`. Image-only requests are stored separately as `image_only`.

4. **Locate article/CMS edit record**  
   `/api/corrections/locate-discord` enqueues `corrections_locate_discord`. Despite the route name, `ArticleLocator` tries several sources in order:
   - CMS direct edit URL found in the email
   - CMS article slug lookup from a Hoodline URL
   - CMS title or query lookup from assessed title/subject
   - cached Discord editorial posts using the sender-quoted title
   - Decodo scrape of a Hoodline URL's `<h1>` and `<title>`
   - Claude-generated Google query through Decodo, then Discord search from scraped Hoodline result titles

   The returned trace explains each attempted source and whether it matched.

5. **Load article fields**  
   `/api/cms/articles/{article_id}/wizard-fields` enqueues `cms_wizard_fields`. `CMSClient.fetch_article_fields_for_wizard()` normalizes CMS article JSON into wizard fields:
   `title`, `meta_title`, `meta_description`, `excerpt`, `social_media_excerpt`, `article_body`, `featured_image_attribution`, and `image_url`.

6. **Generate correction proposal**  
   `/api/corrections/generate` enqueues `corrections_generate`. Claude receives the email, current CMS fields, live outbound-link snapshots from Decodo, and Anthropic's web-search tool. It returns a compact correction object:
   - `t`: title
   - `md`: meta description
   - `mt`: meta title
   - `ex`: excerpt
   - `b`: body HTML
   - `fia`: featured image attribution
   - `if`: image flag
   - `CRVS2`: post-research validity score
   - `changes`: per-field explanation
   - `summary`: one-line summary

7. **Publish selected fields**  
   `/api/corrections/wizard/{gmail_message_id}/publish` enqueues `wizard_publish`. It maps wizard field names to the CMS API's `assignment` envelope and calls `PATCH /api/articles/:id`. On success, the wizard email status becomes `corrected`.

8. **Mark corrected manually**  
   `/api/corrections/wizard/{gmail_message_id}/mark-corrected` is available when the operator corrected the article outside the wizard and wants the queue updated.

## Pipeline Tester Workflow

The Pipeline Tester uses `PIPELINE_STEPS` in `app/main.py`. It persists step outputs in `pipeline_runs` and `pipeline_outputs`, and writes audit events to per-component tables.

| Step | Module | Output |
| --- | --- | --- |
| `gmail_intake` | `GmailClient` / manual input | Correction-shaped cases with matched keywords. |
| `classifier` | `MessageClassifier` | Correction intent, request type, specific claim, proposed correction, article hint, authority signal. |
| `resolver` | `ArticleResolver` | Article URL, CMS ID/edit URL, confidence, strategy. |
| `fetcher` | `ArticleFetcher` | Public article snapshot, metadata, outbound links, existing notes. |
| `verification` | `VerificationAgent` | Evidence, contradicting evidence, confidence, recommended action/edit. |
| `remediation` | `RemediationEngine` | Selected note type, error category, suggested note text. |
| `stager` | `DraftStager` | Diff, preview URL, remote staging status. |
| `review_dashboard` | `main.py` | Review packet marked ready for editor approval. |

This workflow can run with manual input and rule-based fallbacks, but the best results require Anthropic, Gmail, CMS, Discord, and Decodo credentials.

## Background Jobs

Long-running wizard work is handled by Postgres-backed in-process workers. On startup, `start_background_job_workers()` starts `BACKGROUND_JOB_WORKER_CONCURRENCY` daemon threads unless `BACKGROUND_JOBS_ENABLED=false`.

Jobs are stored in `background_jobs` and claimed with `FOR UPDATE SKIP LOCKED`. Stale running jobs are requeued when `locked_at` is older than `BACKGROUND_JOB_STALE_SECONDS` and attempts remain.

Current job types:

- `corrections_inbox`
- `corrections_assess`
- `corrections_parse_discord`
- `corrections_locate_discord`
- `corrections_generate`
- `cms_wizard_fields`
- `discord_refresh_incremental`
- `wizard_publish`
- `corrections_keyword_analysis`

Clients poll `/api/jobs/{job_id}` until the job is `succeeded` or `failed`.

## Database

`Storage.init_schema()` creates all tables at app startup. There is no separate migration framework in this repository.

| Table | Purpose |
| --- | --- |
| `users` | Local auth accounts with scrypt password hashes and roles. |
| `pipeline_runs` | Pipeline Tester run state and shared context JSON. |
| `pipeline_outputs` | Ordered step outputs for each pipeline run. |
| `gmail_intake_events` | Audit log for pipeline Gmail/manual intake. |
| `classifier_events` | Audit log for pipeline classifier output. |
| `editorial_posts` | Cached Discord article posts with title, public URL, CMS edit URL, and message metadata. |
| `resolver_events` | Audit log for pipeline resolver output. |
| `fetcher_events` | Audit log for public article fetching. |
| `verification_events` | Audit log for pipeline verification output. |
| `remediation_events` | Audit log for note/action classification. |
| `stager_events` | Audit log for pipeline staging attempts. |
| `review_decisions` | Human decisions from `/review`. |
| `correction_wizard_emails` | Primary wizard queue, status, and per-step state JSON keyed by Gmail message ID. |
| `background_jobs` | Postgres-backed async job queue. |

Because schema creation is additive, changing existing columns or constraints requires a manual migration or a new migration mechanism.

## API Reference

All JSON APIs except `/health` require a signed-in session. Most operational APIs require admin/superuser.

### Session and Users

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Liveness check; returns `{"status": "ok"}`. |
| `GET` | `/login` | Render login page. |
| `POST` | `/login` | Create session from local username/password. |
| `GET` | `/logout` | Clear session. |
| `GET` | `/users` | Manage local users. |
| `POST` | `/users/create` | Create user. Admins can only create `user`; superusers can create any role. |
| `POST` | `/users/{target_username}/update` | Update role, active status, or password. |

### Corrections Wizard and Queue

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/corrections/inbox` | Enqueue Gmail queue fetch. Query: `limit`, `include_processed`. |
| `GET` | `/api/corrections/wizard/{gmail_message_id}` | Read stored wizard state for one Gmail message. |
| `GET` | `/api/corrections/requests` | List queue bucket. Query: `bucket=unread|rejected|pending|completed`, `limit`. |
| `POST` | `/api/corrections/wizard/{gmail_message_id}/mark-corrected` | Mark cached message corrected without CMS publish. |
| `POST` | `/api/corrections/triage` | Set manual triage decision. |
| `GET` | `/api/corrections/keyword-analysis` | Enqueue distinctive keyword analysis for pending vs rejected buckets. |
| `POST` | `/api/corrections/wizard/clear` | Delete all wizard email state. Does not touch Gmail or Discord cache. |
| `POST` | `/api/corrections/assess` | Enqueue CRVS/SAS assessment. |
| `POST` | `/api/corrections/parse-discord` | Parse a pasted Discord CMS edit link. |
| `POST` | `/api/corrections/locate-discord` | Enqueue article locator cascade. |
| `POST` | `/api/corrections/generate` | Enqueue Claude correction generation. |
| `POST` | `/api/corrections/wizard/{gmail_message_id}/publish` | Enqueue CMS PATCH for selected wizard fields and mark corrected. |

### Jobs, CMS, Discord

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/jobs/{job_id}` | Poll a background job. |
| `POST` | `/api/discord/refresh` | Full Discord guild scan into `editorial_posts`. Runs synchronously. |
| `POST` | `/api/discord/refresh-incremental` | Enqueue incremental Discord scan since the newest cached post. |
| `GET` | `/api/cms/articles/{article_id}` | Proxy CMS article read. |
| `GET` | `/api/cms/articles/{article_id}/wizard-fields` | Enqueue CMS read normalized for wizard fields. |
| `PATCH` | `/api/cms/articles/{article_id}` | Proxy CMS assignment update. |

### Pipeline Tester and Review

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/pipeline/steps` | Return `PIPELINE_STEPS`. |
| `POST` | `/api/pipeline/runs` | Create a pipeline run. |
| `GET` | `/api/pipeline/runs/{run_id}` | Fetch pipeline run state. |
| `POST` | `/api/pipeline/runs/{run_id}/steps/{step_id}` | Execute the next expected pipeline step. |
| `GET` | `/api/pipeline/runs/{run_id}/logs` | Tail JSONL logs for one pipeline run. |
| `GET` | `/api/review/cases` | List pipeline runs that reached review. Query: `status=pending|reviewed`. |
| `GET` | `/api/review/cases/{run_id}` | Fetch review decision history. |
| `POST` | `/api/review/cases/{run_id}/action` | Save `approve_publish`, `edit_publish`, `reject`, `send_back`, or `escalate`. |
| `GET` | `/api/schedule/tasks` | Return static dev schedule. |

## CMS API Integration

`app/cms_client.py` follows the contract in `../hoodline-cms-api/API_ENDPOINT_GUIDE.md`.

Supported CMS operations:

- `POST /api/auth/login`
- `GET /api/articles`
- `GET /api/articles/:id`
- `PATCH /api/articles/:id`
- `POST /api/articles`
- `POST /api/images`
- lookup endpoints for users, tags, metro areas, and websites

Wizard publish accepts user-facing field names and maps them as follows:

| Wizard field | CMS assignment key |
| --- | --- |
| `title` | `title` |
| `meta_title` | `meta_title` |
| `meta_description` | `meta_description` |
| `excerpt` | `excerpt` |
| `social_media_excerpt` | `social_media_excerpt` |
| `article_body` | `text` |
| `featured_image_attribution` | `featured_image_attribution` |

## External Service Notes

### Gmail

`GmailClient` uses a Google service account with domain-wide delegation and the readonly scope `https://www.googleapis.com/auth/gmail.readonly`. Configure either `GMAIL_SERVICE_ACCOUNT_FILE` or `GMAIL_SERVICE_ACCOUNT_JSON`, plus `GMAIL_DELEGATED_USER`.

The app only reads Gmail. It does not currently mark messages read or apply Gmail labels.

### Discord

`DiscordCachePopulator` reads every text channel in the configured guild and looks for messages matching:

```text
https://hoodline.impress3.com/articles/{id}/edit
```

It stores the text before the URL as the article title. The locator searches this cache with `ILIKE` term matching.

### Decodo

Decodo is used for:

- structured or HTML Google search in `ArticleLocator`
- scraping Hoodline pages to extract `<h1>`, `<title>`, meta description, and visible text
- fetching outbound source snapshots before correction generation

Without Decodo, direct CMS/Discord matches still work, but fuzzy article location and outbound-source context are degraded.

### Anthropic

The Wizard assessment and generation paths require `ANTHROPIC_API_KEY`. The older pipeline classifier, verification, and remediation modules have rules-based fallbacks when Claude is unavailable, unless the backend is explicitly forced to `claude`.

Correction-note prompts include guardrails prohibiting references to AI, hallucinations, automated systems, model error, or generation issues.

## Logs and Debugging

- Shared pipeline/application logs: `logs/pipeline.log`
- Per-pipeline-run logs: `logs/runs/{run_id}.log`
- Background job result/error: `background_jobs.result_json` and `background_jobs.error`
- Wizard per-email state: `correction_wizard_emails.state_json`
- Setup checks: `/setup`

Useful database inspection examples:

```bash
docker exec hoodline_postgres psql -U hoodline -d hoodline -c "select job_id, job_type, status, error from background_jobs order by created_at desc limit 10;"
docker exec hoodline_postgres psql -U hoodline -d hoodline -c "select gmail_message_id, status, updated_at from correction_wizard_emails order by updated_at desc limit 10;"
docker exec hoodline_postgres psql -U hoodline -d hoodline -c "select count(*) from editorial_posts;"
```

## Safety and Operational Guardrails

- Wizard publishing requires an authenticated admin/superuser session.
- The CMS publish path only accepts known wizard fields.
- The app records who queued/published wizard work via `created_by`, `last_touched_by`, and CMS publish state.
- The Pipeline Stager defaults to dry-run behavior unless remote staging flags are explicitly enabled.
- Generated editor notes are constrained to concise, neutral language and sanitized for forbidden AI-related terms.
- `.env`, service-account JSON files, `secrets/`, and logs are git-ignored.

## Known Caveats

- There is no test suite in this repository.
- There is no formal migration system; `Storage.init_schema()` only creates missing tables/indexes.
- Background jobs run inside the web process. Multiple app containers can safely claim jobs via row locks, but process restarts interrupt active jobs until they are marked stale and requeued.
- Gmail intake is readonly. Processed labels/read state are represented in local Postgres, not written back to Gmail.
- The older Pipeline Tester and the newer Corrections Wizard overlap conceptually but use different code paths and database records.
- `DraftStager` is mostly for the older pipeline. The primary wizard publish path uses `CMSClient.update_article_from_wizard_fields()`.
- If external credentials are missing, some UI paths will be unavailable rather than degraded. `/setup` is the fastest way to see what is configured.
