from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from app.classifier import MessageClassifier
from app.gmail_client import GmailClient
from app.storage import PipelineRunRecord, Storage

BASE_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = BASE_DIR / "logs"
RUN_LOG_DIR = LOG_DIR / "runs"
LOG_FILE = LOG_DIR / "pipeline.log"

LOG_DIR.mkdir(parents=True, exist_ok=True)
RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Hoodline Inquiries Dashboard")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("APP_SESSION_SECRET", "dev-only-secret-change-me"),
    same_site="lax",
    https_only=False,
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

admin_user = os.getenv("APP_ADMIN_USER", "admin")
admin_password = os.getenv("APP_ADMIN_PASSWORD", "changeme")
cms_base_url = os.getenv("CMS_BASE_URL", "https://hoodline.impress3.com")
database_url = os.getenv("DATABASE_URL", "postgresql://hoodline:hoodline@postgres:5432/hoodline")

storage = Storage(database_url)
gmail_client = GmailClient()
message_classifier = MessageClassifier()

logger = logging.getLogger("hoodline.pipeline")
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = logging.FileHandler(LOG_FILE)
    formatter = logging.Formatter("%(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

SCHEDULE_TASKS: list[dict[str, Any]] = [
    {
        "id": "foundation",
        "title": "Foundation: Docker + Postgres + migrations + service skeleton",
        "start": "2026-04-16",
        "end": "2026-04-17",
        "min": 10,
        "max": 12,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "gmail_intake",
        "title": "3.1 Gmail intake",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 6,
        "max": 8,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "classifier",
        "title": "3.2 Classifier",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 4,
        "max": 6,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "resolver",
        "title": "3.3 Article resolver",
        "start": "2026-04-20",
        "end": "2026-04-21",
        "min": 10,
        "max": 14,
    },
    {"id": "fetcher", "title": "3.4 Article fetcher", "start": "2026-04-22", "end": "2026-04-22", "min": 8, "max": 10},
    {
        "id": "verification",
        "title": "3.5 Verification agent",
        "start": "2026-04-23",
        "end": "2026-04-27",
        "min": 18,
        "max": 24,
    },
    {
        "id": "remediation",
        "title": "3.6 Remediation classifier + note writer",
        "start": "2026-04-28",
        "end": "2026-04-28",
        "min": 5,
        "max": 7,
    },
    {"id": "stager", "title": "3.7 Draft stager", "start": "2026-04-29", "end": "2026-05-01", "min": 12, "max": 16},
    {
        "id": "review_dashboard",
        "title": "3.8 Review dashboard",
        "start": "2026-05-04",
        "end": "2026-05-05",
        "min": 14,
        "max": 18,
    },
    {
        "id": "integration",
        "title": "End-to-end integration, smoke tests, docs, runbooks",
        "start": "2026-05-06",
        "end": "2026-05-06",
        "min": 6,
        "max": 8,
    },
]

PIPELINE_STEPS: list[dict[str, Any]] = [
    {
        "id": "gmail_intake",
        "label": "3.1 Gmail intake",
        "description": "Fetch from Gmail API (delegated service account) or submit manual test input.",
        "inputs": [
            {"key": "mode", "label": "Mode: manual or gmail_api", "type": "text", "required": False},
            {"key": "sender", "label": "Sender email (manual mode)", "type": "text", "required": False},
            {"key": "subject", "label": "Email subject (manual mode)", "type": "text", "required": False},
            {"key": "body", "label": "Email body (manual mode)", "type": "textarea", "required": False},
            {"key": "gmail_message_id", "label": "Gmail message ID (gmail_api mode, optional)", "type": "text", "required": False},
            {"key": "gmail_query", "label": "Gmail search query (gmail_api mode)", "type": "text", "required": False},
            {"key": "gmail_label_ids", "label": "Gmail label IDs, comma-separated", "type": "text", "required": False},
            {"key": "max_results", "label": "Max results when querying", "type": "text", "required": False},
        ],
    },
    {
        "id": "classifier",
        "label": "3.2 Classifier",
        "description": "Classify correction intent with Claude (or rules fallback) and return structured fields.",
        "inputs": [
            {"key": "backend", "label": "Backend override: auto | claude | rules", "type": "text", "required": False},
            {"key": "sender", "label": "Sender override (optional)", "type": "text", "required": False},
            {"key": "subject", "label": "Subject override (optional)", "type": "text", "required": False},
            {"key": "body", "label": "Body override (optional)", "type": "textarea", "required": False},
        ],
    },
    {
        "id": "resolver",
        "label": "3.3 Article resolver",
        "description": "Resolve article URL and CMS edit URL from provided hints.",
        "inputs": [
            {"key": "article_hint", "label": "Article hint or URL", "type": "text", "required": True},
        ],
    },
    {
        "id": "fetcher",
        "label": "3.4 Article fetcher",
        "description": "Fetch and summarize article metadata from URL and CMS edit URL.",
        "inputs": [
            {"key": "article_url", "label": "Article URL", "type": "text", "required": True},
        ],
    },
    {
        "id": "verification",
        "label": "3.5 Verification agent",
        "description": "Evaluate evidence and return confidence + recommended action.",
        "inputs": [
            {"key": "specific_claim", "label": "Specific claim to verify", "type": "textarea", "required": True},
        ],
    },
    {
        "id": "remediation",
        "label": "3.6 Remediation classifier + note writer",
        "description": "Choose correction note type and draft neutral note text.",
        "inputs": [
            {"key": "recommended_action", "label": "Recommended action override", "type": "text", "required": False},
        ],
    },
    {
        "id": "stager",
        "label": "3.7 Draft stager",
        "description": "Prepare staged draft output, preview URL, and before/after summary.",
        "inputs": [
            {"key": "target_field", "label": "Target field", "type": "text", "required": True},
            {"key": "new_value", "label": "New value", "type": "textarea", "required": True},
        ],
    },
    {
        "id": "review_dashboard",
        "label": "3.8 Review dashboard",
        "description": "Finalize case packet for editor review and publish decision.",
        "inputs": [
            {"key": "reviewer", "label": "Reviewer name", "type": "text", "required": True},
        ],
    },
]

STEP_BY_ID = {step["id"]: step for step in PIPELINE_STEPS}


class StepExecutionRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


@app.on_event("startup")
def startup() -> None:
    storage.init_schema()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_run_log(run_id: str, step_id: str, message: str, payload: dict[str, Any] | None = None) -> None:
    record: dict[str, Any] = {
        "timestamp": now_iso(),
        "run_id": run_id,
        "step_id": step_id,
        "message": message,
    }
    if payload is not None:
        record["payload"] = payload

    line = json.dumps(record, ensure_ascii=True)
    logger.info(line)

    run_log_file = RUN_LOG_DIR / f"{run_id}.log"
    with run_log_file.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def ensure_admin(request: Request) -> None:
    if not request.session.get("is_admin"):
        raise HTTPException(status_code=401, detail="Admin login required")


def parse_article_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s]+", text)
    if not match:
        return None

    url = match.group(0).strip().rstrip(".,)")
    if "hoodline.com" not in url:
        return None

    return url


def parse_label_ids(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def run_to_response(run: PipelineRunRecord) -> dict[str, Any]:
    next_step = PIPELINE_STEPS[run.current_index]["id"] if run.current_index < len(PIPELINE_STEPS) else None
    return {
        "run_id": run.run_id,
        "created_at": run.created_at.isoformat(),
        "current_index": run.current_index,
        "next_step": next_step,
        "outputs": run.outputs,
        "completed": run.current_index >= len(PIPELINE_STEPS),
    }


def get_run_or_404(run_id: str) -> PipelineRunRecord:
    run = storage.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def execute_step(context: dict[str, Any], step_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
    if step_id == "gmail_intake":
        mode = (inputs.get("mode") or "manual").strip().lower()
        if mode not in {"manual", "gmail_api"}:
            raise HTTPException(status_code=400, detail="mode must be 'manual' or 'gmail_api'")

        sender = ""
        subject = ""
        body = ""
        gmail_message_id = None
        gmail_thread_id = None
        source = mode

        if mode == "gmail_api":
            max_results_raw = (inputs.get("max_results") or "1").strip()
            try:
                max_results = int(max_results_raw)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="max_results must be an integer") from exc

            label_ids = parse_label_ids(inputs.get("gmail_label_ids", ""))
            try:
                fetched = gmail_client.fetch_intake_message(
                    message_id=(inputs.get("gmail_message_id") or "").strip() or None,
                    query=(inputs.get("gmail_query") or "").strip() or None,
                    label_ids=label_ids,
                    max_results=max_results,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            sender = fetched.get("sender", "")
            subject = fetched.get("subject", "")
            body = fetched.get("body", "")
            gmail_message_id = fetched.get("gmail_message_id")
            gmail_thread_id = fetched.get("gmail_thread_id")
        else:
            sender = (inputs.get("sender") or "").strip()
            subject = (inputs.get("subject") or "").strip()
            body = (inputs.get("body") or "").strip()
            if not sender or not subject or not body:
                raise HTTPException(
                    status_code=400,
                    detail="manual mode requires sender, subject, and body",
                )

        text = f"{subject}\n{body}".lower()
        keywords = [
            "correction",
            "error",
            "mistake",
            "inaccurate",
            "wrong",
            "incorrect",
            "typo",
            "update",
            "editor's note",
            "retract",
        ]
        matched = [keyword for keyword in keywords if keyword in text]
        case_id = f"case-{uuid4().hex[:8]}"
        output = {
            "case_id": case_id,
            "source": source,
            "is_candidate": bool(matched),
            "matched_keywords": matched,
            "labels_applied": ["auto/correction-candidate", "auto/processed"],
            "gmail_message_id": gmail_message_id,
            "gmail_thread_id": gmail_thread_id,
            "sender": sender,
            "subject": subject,
            "body": body,
        }
        context.update(output)
        return output

    if step_id == "classifier":
        sender = (inputs.get("sender") or context.get("sender") or "").strip()
        subject = inputs.get("subject") or context.get("subject", "")
        body = inputs.get("body") or context.get("body", "")
        backend_override = (inputs.get("backend") or "").strip().lower() or None
        try:
            output = message_classifier.classify(
                sender=sender,
                subject=subject,
                body=body,
                backend_override=backend_override,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        output["sender"] = sender
        output["subject"] = subject
        output["body"] = body
        context.update(output)
        return output

    if step_id == "resolver":
        hint = inputs.get("article_hint") or context.get("referenced_article_hint") or ""
        article_url = parse_article_url(hint)
        if not article_url:
            slug = "-".join(re.findall(r"[a-z0-9]+", hint.lower())[:8]) or "resolved-article"
            article_url = f"https://hoodline.com/{slug}/"

        edit_id = abs(hash(article_url)) % 100000
        output = {
            "article_url": article_url,
            "article_cms_id": edit_id,
            "article_edit_url": f"{cms_base_url.rstrip('/')}/wp-admin/post.php?post={edit_id}&action=edit",
            "resolver_confidence": 0.86,
        }
        context.update(output)
        return output

    if step_id == "fetcher":
        article_url = inputs.get("article_url") or context.get("article_url")
        if not article_url:
            raise HTTPException(status_code=400, detail="article_url is required")

        output = {
            "article_url": article_url,
            "headline": "Sample fetched article headline",
            "byline": "Hoodline Staff",
            "publish_date": "2026-03-12",
            "outbound_links_count": 5,
            "existing_notes": [],
            "snapshot_status": "captured",
        }
        context.update(output)
        return output

    if step_id == "verification":
        claim = inputs.get("specific_claim") or context.get("specific_claim") or ""
        lowered = claim.lower()
        confidence = 6
        if any(term in lowered for term in ["wrong", "incorrect", "inaccurate"]):
            confidence = 8
        if any(term in lowered for term in ["might", "maybe", "unclear"]):
            confidence = 5

        if confidence >= 8:
            action = "editors_note_bottom"
        elif confidence >= 6:
            action = "update_stamp"
        else:
            action = "needs_human"

        output = {
            "confidence": confidence,
            "recommended_action": action,
            "evidence": [
                {
                    "source_url": "https://example.gov/source-record",
                    "quote": "Primary source indicates the corrected date.",
                    "weight": 0.7,
                }
            ],
            "contradicting_evidence": [],
            "recommended_edit": {
                "field": "body",
                "old_value": "Old statement",
                "new_value": "Corrected statement based on source",
            },
        }
        context.update(output)
        return output

    if step_id == "remediation":
        action = inputs.get("recommended_action") or context.get("recommended_action", "needs_human")
        note_text = {
            "silent_correction": "No reader-facing note required for this change.",
            "update_stamp": "Update (April 16, 2026): A previous version omitted newer confirmed details.",
            "editors_note_bottom": "Editor's Note: A previous version misstated a factual detail and has been corrected.",
            "editors_note_top": "Editor's Note: A previous version included a material factual error; this article has been corrected.",
        }.get(action, "Escalate to editor for manual note drafting.")

        output = {
            "selected_action": action,
            "suggested_note_text": note_text,
            "note_writer_guardrails_applied": True,
        }
        context.update(output)
        return output

    if step_id == "stager":
        target_field = inputs.get("target_field")
        new_value = inputs.get("new_value")
        preview_id = uuid4().hex[:10]
        output = {
            "staged": True,
            "target_field": target_field,
            "new_value": new_value,
            "preview_url": f"{cms_base_url.rstrip('/')}/?preview={preview_id}",
            "diff_summary": f"Updated {target_field} with proposed correction text.",
        }
        context.update(output)
        return output

    if step_id == "review_dashboard":
        reviewer = inputs.get("reviewer")
        output = {
            "reviewer": reviewer,
            "status": "ready_for_editor_approval",
            "actions_available": [
                "Approve & publish",
                "Edit note then publish",
                "Reject",
                "Send back for re-verification",
                "Escalate to Eddie",
            ],
            "case_packet_ready": True,
        }
        context.update(output)
        return output

    raise HTTPException(status_code=404, detail=f"Unknown step: {step_id}")


def validate_required_inputs(run: PipelineRunRecord, step: dict[str, Any], inputs: dict[str, Any]) -> None:
    # gmail_intake has conditional required fields, validated in execute_step.
    if step["id"] == "gmail_intake":
        return
    if step["id"] == "classifier":
        subject = (inputs.get("subject") or run.context.get("subject") or "").strip()
        body = (inputs.get("body") or run.context.get("body") or "").strip()
        if not subject and not body:
            raise HTTPException(
                status_code=400,
                detail="classifier requires subject and/or body from prior step or overrides",
            )
        return

    missing: list[str] = []
    for field_def in step["inputs"]:
        if not field_def.get("required"):
            continue

        key = field_def["key"]
        from_request = inputs.get(key)
        from_context = run.context.get(key)
        if (from_request is None or str(from_request).strip() == "") and (from_context is None or str(from_context).strip() == ""):
            missing.append(key)

    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required inputs: {', '.join(missing)}")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "is_admin": bool(request.session.get("is_admin")),
            "cms_base_url": cms_base_url,
        },
    )


@app.get("/schedule", response_class=HTMLResponse)
async def schedule(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "schedule.html",
        {
            "request": request,
            "is_admin": bool(request.session.get("is_admin")),
            "schedule_tasks_json": json.dumps(SCHEDULE_TASKS),
            "cms_base_url": cms_base_url,
        },
    )


@app.get("/pipeline", response_class=HTMLResponse)
async def pipeline(request: Request) -> HTMLResponse:
    if not request.session.get("is_admin"):
        return RedirectResponse(url="/login?next=/pipeline", status_code=302)

    return templates.TemplateResponse(
        "pipeline.html",
        {
            "request": request,
            "is_admin": True,
            "pipeline_steps_json": json.dumps(PIPELINE_STEPS),
            "cms_base_url": cms_base_url,
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/pipeline") -> HTMLResponse:
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "next": next,
            "error": None,
            "is_admin": bool(request.session.get("is_admin")),
            "cms_base_url": cms_base_url,
        },
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/pipeline"),
) -> HTMLResponse:
    if username == admin_user and password == admin_password:
        request.session["is_admin"] = True
        request.session["username"] = username
        return RedirectResponse(url=next, status_code=302)

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "next": next,
            "error": "Invalid credentials.",
            "is_admin": False,
            "cms_base_url": cms_base_url,
        },
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/pipeline/steps")
async def api_steps(request: Request) -> JSONResponse:
    ensure_admin(request)
    return JSONResponse({"steps": PIPELINE_STEPS})


@app.post("/api/pipeline/runs")
async def api_create_run(request: Request) -> JSONResponse:
    ensure_admin(request)
    run_id = uuid4().hex
    created_by = request.session.get("username")

    storage.create_run(run_id, created_by)
    append_run_log(run_id, "system", "Run created", {"current_step": PIPELINE_STEPS[0]["id"]})

    run = get_run_or_404(run_id)
    response = run_to_response(run)
    return JSONResponse(response)


@app.get("/api/pipeline/runs/{run_id}")
async def api_get_run(run_id: str, request: Request) -> JSONResponse:
    ensure_admin(request)
    run = get_run_or_404(run_id)
    return JSONResponse(run_to_response(run))


@app.post("/api/pipeline/runs/{run_id}/steps/{step_id}")
async def api_run_step(run_id: str, step_id: str, payload: StepExecutionRequest, request: Request) -> JSONResponse:
    ensure_admin(request)
    run = get_run_or_404(run_id)

    if run.current_index >= len(PIPELINE_STEPS):
        raise HTTPException(status_code=400, detail="Run is already complete")

    expected_step = PIPELINE_STEPS[run.current_index]["id"]
    if step_id != expected_step:
        raise HTTPException(status_code=400, detail=f"Expected step '{expected_step}' next")

    step = STEP_BY_ID.get(step_id)
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    validate_required_inputs(run, step, payload.inputs)

    append_run_log(run_id, step_id, "Step started", {"inputs": payload.inputs})

    context = dict(run.context)
    output = execute_step(context, step_id, payload.inputs)
    append_run_log(run_id, step_id, "Step completed", {"output": output})

    if step_id == "gmail_intake":
        storage.save_gmail_intake_event(
            run_id=run_id,
            case_id=output["case_id"],
            source=output["source"],
            gmail_message_id=output.get("gmail_message_id"),
            gmail_thread_id=output.get("gmail_thread_id"),
            sender=output.get("sender", ""),
            subject=output.get("subject", ""),
            body=output.get("body", ""),
            matched_keywords=output.get("matched_keywords", []),
            is_candidate=bool(output.get("is_candidate")),
        )
    if step_id == "classifier":
        storage.save_classifier_event(
            run_id=run_id,
            case_id=context.get("case_id"),
            backend=output.get("classifier_backend", "unknown"),
            model=output.get("classifier_model"),
            sender=output.get("sender", ""),
            subject=output.get("subject", ""),
            body=output.get("body", ""),
            output=output,
        )

    step_index = run.current_index
    storage.append_step_output(
        run_id,
        step_index=step_index,
        step_id=step_id,
        step_label=step["label"],
        inputs=payload.inputs,
        output=output,
        context=context,
        current_index=step_index + 1,
    )

    updated = get_run_or_404(run_id)
    response = run_to_response(updated)
    response["last_output"] = updated.outputs[-1] if updated.outputs else None
    return JSONResponse(response)


@app.get("/api/pipeline/runs/{run_id}/logs")
async def api_run_logs(run_id: str, request: Request, lines: int = 200) -> JSONResponse:
    ensure_admin(request)

    if not storage.run_exists(run_id):
        raise HTTPException(status_code=404, detail="Run not found")

    log_file = RUN_LOG_DIR / f"{run_id}.log"
    if not log_file.exists():
        return JSONResponse({"run_id": run_id, "logs": []})

    with log_file.open("r", encoding="utf-8") as fh:
        all_lines = fh.readlines()

    tail = all_lines[-max(1, min(lines, 1000)) :]
    parsed = []
    for raw in tail:
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError:
            parsed.append({"timestamp": now_iso(), "message": raw})

    return JSONResponse({"run_id": run_id, "logs": parsed})


@app.get("/api/schedule/tasks")
async def api_schedule_tasks() -> JSONResponse:
    return JSONResponse({"tasks": SCHEDULE_TASKS})
