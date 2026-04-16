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

from app.auth import hash_password, verify_password
from app.classifier import MessageClassifier
from app.fetcher import ArticleFetcher
from app.gmail_client import GmailClient
from app.remediation import RemediationEngine
from app.resolver import ArticleResolver, ResolverConfig
from app.stager import DraftStager
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

cms_base_url = os.getenv("CMS_BASE_URL", "https://hoodline.impress3.com")
database_url = os.getenv("DATABASE_URL", "postgresql://hoodline:hoodline@postgres:5432/hoodline")
default_superuser_username = os.getenv("DEFAULT_SUPERUSER_USERNAME", "zack@impress3.com").strip().lower()
default_superuser_password = os.getenv("DEFAULT_SUPERUSER_PASSWORD", "billGAtes1!1")

storage = Storage(database_url)
gmail_client = GmailClient()
message_classifier = MessageClassifier()
resolver_min_similarity = float(os.getenv("RESOLVER_MIN_SIMILARITY", "0.65"))
resolver_max_candidates = int(os.getenv("RESOLVER_MAX_CANDIDATES", "250"))
article_resolver = ArticleResolver(
    storage=storage,
    cms_base_url=cms_base_url,
    config=ResolverConfig(
        min_similarity=resolver_min_similarity,
        max_candidates=resolver_max_candidates,
    ),
)
article_fetcher = ArticleFetcher()
remediation_engine = RemediationEngine()
draft_stager = DraftStager(cms_base_url=cms_base_url)

logger = logging.getLogger("hoodline.pipeline")
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = logging.FileHandler(LOG_FILE)
    formatter = logging.Formatter("%(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

ALLOWED_ROLES = {"superuser", "admin", "user"}
ADMIN_ROLES = {"superuser", "admin"}

SCHEDULE_TASKS: list[dict[str, Any]] = [
    {
        "id": "foundation",
        "title": "Foundation: Docker + Postgres + migrations + service skeleton",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 0.5,
        "max": 0.75,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "gmail_intake",
        "title": "3.1 Gmail intake",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 0.5,
        "max": 0.75,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "classifier",
        "title": "3.2 Classifier",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 0.2,
        "max": 0.3,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "resolver",
        "title": "3.3 Article resolver",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 0.1,
        "max": 0.2,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "fetcher",
        "title": "3.4 Article fetcher",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 0.5,
        "max": 1.0,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "verification",
        "title": "3.5 Verification agent",
        "start": "2026-04-17",
        "end": "2026-04-18",
        "min": 3.0,
        "max": 6.0,
    },
    {
        "id": "remediation",
        "title": "3.6 Remediation classifier + note writer",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 0.4,
        "max": 0.8,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "stager",
        "title": "3.7 Draft stager",
        "start": "2026-04-16",
        "end": "2026-04-16",
        "min": 0.6,
        "max": 1.2,
        "completed": True,
        "completed_at": "2026-04-16",
    },
    {
        "id": "review_dashboard",
        "title": "3.8 Review dashboard",
        "start": "2026-04-18",
        "end": "2026-04-19",
        "min": 1.5,
        "max": 3.0,
    },
    {
        "id": "integration",
        "title": "End-to-end integration, smoke tests, docs, runbooks",
        "start": "2026-04-19",
        "end": "2026-04-19",
        "min": 0.75,
        "max": 1.5,
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
        "description": "Resolve article URL from direct link or cached editorial posts (with optional seed input).",
        "inputs": [
            {"key": "article_hint", "label": "Article hint or URL", "type": "text", "required": True},
            {"key": "seed_title", "label": "Seed editorial title (optional)", "type": "text", "required": False},
            {"key": "seed_article_url", "label": "Seed editorial article URL (optional)", "type": "text", "required": False},
            {"key": "seed_cms_edit_url", "label": "Seed editorial CMS edit URL (optional)", "type": "text", "required": False},
            {"key": "seed_content", "label": "Seed editorial message content (optional)", "type": "textarea", "required": False},
            {"key": "seed_channel", "label": "Seed editorial channel (optional)", "type": "text", "required": False},
            {"key": "seed_message_id", "label": "Seed editorial message ID (optional)", "type": "text", "required": False},
        ],
    },
    {
        "id": "fetcher",
        "label": "3.4 Article fetcher",
        "description": "Fetch and parse public article metadata, outbound links, and existing correction notes.",
        "inputs": [
            {"key": "article_url", "label": "Article URL", "type": "text", "required": True},
            {"key": "article_edit_url", "label": "CMS edit URL override (optional)", "type": "text", "required": False},
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
            {"key": "request_type", "label": "Request type override", "type": "text", "required": False},
            {"key": "specific_claim", "label": "Specific claim override", "type": "textarea", "required": False},
            {"key": "confidence", "label": "Confidence override (0-10)", "type": "text", "required": False},
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
    bootstrap_superuser()


def bootstrap_superuser() -> None:
    existing = storage.get_user(default_superuser_username)
    if existing is not None:
        return

    storage.create_user(
        username=default_superuser_username,
        password_hash=hash_password(default_superuser_password),
        role="superuser",
        is_active=True,
    )
    logger.info(
        json.dumps(
            {
                "timestamp": now_iso(),
                "event": "bootstrap_superuser_created",
                "username": default_superuser_username,
            },
            ensure_ascii=True,
        )
    )


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


def session_user(request: Request) -> dict[str, Any] | None:
    username = request.session.get("username")
    role = request.session.get("role")
    if not username or not role:
        return None
    return {
        "username": username,
        "role": role,
        "is_admin": role in ADMIN_ROLES,
        "is_superuser": role == "superuser",
    }


def ensure_authenticated_api(request: Request) -> dict[str, Any]:
    user = session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Login required")
    return user


def ensure_admin_api(request: Request) -> dict[str, Any]:
    user = ensure_authenticated_api(request)
    if user["role"] not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def page_user_or_redirect(request: Request, next_path: str) -> dict[str, Any] | RedirectResponse:
    user = session_user(request)
    if user is None:
        return RedirectResponse(url=f"/login?next={next_path}", status_code=302)
    return user


def render_context(request: Request, **kwargs: Any) -> dict[str, Any]:
    user = session_user(request)
    ctx = {
        "request": request,
        "cms_base_url": cms_base_url,
        "current_user": user,
        "is_admin": bool(user and user["is_admin"]),
        "is_superuser": bool(user and user["is_superuser"]),
    }
    ctx.update(kwargs)
    return ctx


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
        seed_payload = {
            "title": inputs.get("seed_title"),
            "article_url": inputs.get("seed_article_url"),
            "cms_edit_url": inputs.get("seed_cms_edit_url"),
            "content": inputs.get("seed_content"),
            "channel": inputs.get("seed_channel"),
            "message_id": inputs.get("seed_message_id"),
        }
        try:
            output = article_resolver.resolve(
                article_hint=hint,
                classifier_hint=context.get("referenced_article_hint"),
                seed_editorial_post=seed_payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        context.update(output)
        return output

    if step_id == "fetcher":
        article_url = inputs.get("article_url") or context.get("article_url")
        if not article_url:
            raise HTTPException(status_code=400, detail="article_url is required")
        article_edit_url = (inputs.get("article_edit_url") or context.get("article_edit_url") or "").strip() or None
        try:
            output = article_fetcher.fetch(article_url=article_url, article_edit_url=article_edit_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Fetcher failed: {exc}") from exc
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
        specific_claim = str(inputs.get("specific_claim") or context.get("specific_claim") or "").strip()
        request_type = str(inputs.get("request_type") or context.get("request_type") or "other").strip().lower()
        recommended_action = str(inputs.get("recommended_action") or context.get("recommended_action") or "").strip().lower() or None

        confidence_raw = inputs.get("confidence", context.get("confidence"))
        confidence_value: int | None = None
        if confidence_raw is not None and str(confidence_raw).strip():
            try:
                confidence_value = int(str(confidence_raw).strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="confidence must be an integer") from exc
            if confidence_value < 0 or confidence_value > 10:
                raise HTTPException(status_code=400, detail="confidence must be between 0 and 10")

        try:
            output = remediation_engine.classify_and_write(
                specific_claim=specific_claim,
                request_type=request_type,
                confidence=confidence_value,
                recommended_action=recommended_action,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Remediation failed: {exc}") from exc

        context.update(output)
        return output

    if step_id == "stager":
        target_field = str(inputs.get("target_field") or "").strip().lower()
        new_value = str(inputs.get("new_value") or "").strip()
        if not target_field:
            raise HTTPException(status_code=400, detail="target_field is required")
        if not new_value:
            raise HTTPException(status_code=400, detail="new_value is required")

        recommended_edit = context.get("recommended_edit") or {}
        current_value = ""
        if isinstance(recommended_edit, dict) and target_field == str(recommended_edit.get("field", "")).strip().lower():
            current_value = str(recommended_edit.get("old_value") or "").strip()
        elif target_field == "title":
            current_value = str(context.get("headline") or "").strip()
        elif target_field in {"body", "meta_description"}:
            current_value = str(context.get("meta_description") or "").strip()

        article_cms_id = context.get("article_cms_id")
        if article_cms_id is not None:
            try:
                article_cms_id = int(article_cms_id)
            except (TypeError, ValueError):
                article_cms_id = None

        try:
            output = draft_stager.stage(
                article_cms_id=article_cms_id,
                article_url=str(context.get("article_url") or ""),
                article_edit_url=str(context.get("article_edit_url") or "").strip() or None,
                target_field=target_field,
                new_value=new_value,
                current_value=current_value,
                suggested_note_text=str(context.get("suggested_note_text") or "").strip() or None,
                selected_action=str(context.get("selected_action") or "").strip() or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Stager failed: {exc}") from exc

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
    gate = page_user_or_redirect(request, "/")
    if isinstance(gate, RedirectResponse):
        return gate
    return templates.TemplateResponse("home.html", render_context(request))


@app.get("/schedule", response_class=HTMLResponse)
async def schedule(request: Request) -> HTMLResponse:
    gate = page_user_or_redirect(request, "/schedule")
    if isinstance(gate, RedirectResponse):
        return gate
    return templates.TemplateResponse(
        "schedule.html",
        render_context(request, schedule_tasks_json=json.dumps(SCHEDULE_TASKS)),
    )


@app.get("/pipeline", response_class=HTMLResponse)
async def pipeline(request: Request) -> HTMLResponse:
    user = page_user_or_redirect(request, "/pipeline")
    if isinstance(user, RedirectResponse):
        return user
    if user["role"] not in ADMIN_ROLES:
        return RedirectResponse(url="/", status_code=302)

    return templates.TemplateResponse(
        "pipeline.html",
        render_context(request, pipeline_steps_json=json.dumps(PIPELINE_STEPS)),
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    if session_user(request) is not None:
        return RedirectResponse(url=next or "/", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        render_context(request, next=next, error=None),
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> HTMLResponse:
    account = storage.get_user(username.strip().lower())
    if account and account.get("is_active") and verify_password(password, account.get("password_hash", "")):
        request.session["username"] = account["username"]
        request.session["role"] = account["role"]
        return RedirectResponse(url=next, status_code=302)

    return templates.TemplateResponse(
        "login.html",
        render_context(request, next=next, error="Invalid credentials."),
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request) -> HTMLResponse:
    user = page_user_or_redirect(request, "/users")
    if isinstance(user, RedirectResponse):
        return user
    if user["role"] not in ADMIN_ROLES:
        return RedirectResponse(url="/", status_code=302)

    users = storage.list_users()
    return templates.TemplateResponse(
        "users.html",
        render_context(request, users=users, error=None, success=None),
    )


@app.post("/users/create", response_class=HTMLResponse)
async def users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
) -> HTMLResponse:
    actor = page_user_or_redirect(request, "/users")
    if isinstance(actor, RedirectResponse):
        return actor
    if actor["role"] not in ADMIN_ROLES:
        return RedirectResponse(url="/", status_code=302)

    normalized_username = username.strip().lower()
    requested_role = role.strip().lower()
    if requested_role not in ALLOWED_ROLES:
        requested_role = "user"

    if actor["role"] == "admin" and requested_role != "user":
        requested_role = "user"

    if not normalized_username or len(password) < 8:
        users = storage.list_users()
        return templates.TemplateResponse(
            "users.html",
            render_context(request, users=users, error="Username required and password must be at least 8 characters.", success=None),
            status_code=400,
        )

    if storage.get_user(normalized_username) is not None:
        users = storage.list_users()
        return templates.TemplateResponse(
            "users.html",
            render_context(request, users=users, error="User already exists.", success=None),
            status_code=400,
        )

    storage.create_user(
        username=normalized_username,
        password_hash=hash_password(password),
        role=requested_role,
        is_active=True,
    )
    users = storage.list_users()
    return templates.TemplateResponse(
        "users.html",
        render_context(request, users=users, error=None, success=f"Created user {normalized_username} ({requested_role})."),
    )


@app.post("/users/{target_username}/update", response_class=HTMLResponse)
async def users_update(
    target_username: str,
    request: Request,
    role: str = Form(""),
    password: str = Form(""),
    is_active: str = Form("true"),
) -> HTMLResponse:
    actor = page_user_or_redirect(request, "/users")
    if isinstance(actor, RedirectResponse):
        return actor
    if actor["role"] not in ADMIN_ROLES:
        return RedirectResponse(url="/", status_code=302)

    normalized_target = target_username.strip().lower()
    account = storage.get_user(normalized_target)
    if account is None:
        users = storage.list_users()
        return templates.TemplateResponse(
            "users.html",
            render_context(request, users=users, error="Target user not found.", success=None),
            status_code=404,
        )

    if actor["role"] == "admin":
        if account["role"] == "superuser":
            users = storage.list_users()
            return templates.TemplateResponse(
                "users.html",
                render_context(request, users=users, error="Admin cannot edit superuser accounts.", success=None),
                status_code=403,
            )
        if role and role.strip().lower() != "user":
            role = "user"

    requested_role = role.strip().lower() if role else account["role"]
    if requested_role not in ALLOWED_ROLES:
        requested_role = account["role"]

    if actor["role"] == "admin" and requested_role != "user":
        requested_role = "user"

    if account["role"] == "superuser" and actor["role"] != "superuser":
        users = storage.list_users()
        return templates.TemplateResponse(
            "users.html",
            render_context(request, users=users, error="Only superuser can edit superuser accounts.", success=None),
            status_code=403,
        )

    password_hash = None
    password_clean = password.strip()
    if password_clean:
        if len(password_clean) < 8:
            users = storage.list_users()
            return templates.TemplateResponse(
                "users.html",
                render_context(request, users=users, error="Password must be at least 8 characters.", success=None),
                status_code=400,
            )
        password_hash = hash_password(password_clean)

    active_bool = is_active.strip().lower() == "true"
    try:
        storage.update_user(
            username=normalized_target,
            role=requested_role,
            password_hash=password_hash,
            is_active=active_bool,
        )
    except ValueError:
        users = storage.list_users()
        return templates.TemplateResponse(
            "users.html",
            render_context(request, users=users, error="Target user not found.", success=None),
            status_code=404,
        )

    users = storage.list_users()
    return templates.TemplateResponse(
        "users.html",
        render_context(request, users=users, error=None, success=f"Updated user {normalized_target}."),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/pipeline/steps")
async def api_steps(request: Request) -> JSONResponse:
    ensure_admin_api(request)
    return JSONResponse({"steps": PIPELINE_STEPS})


@app.post("/api/pipeline/runs")
async def api_create_run(request: Request) -> JSONResponse:
    user = ensure_admin_api(request)
    run_id = uuid4().hex
    created_by = user["username"]

    storage.create_run(run_id, created_by)
    append_run_log(run_id, "system", "Run created", {"current_step": PIPELINE_STEPS[0]["id"]})

    run = get_run_or_404(run_id)
    response = run_to_response(run)
    return JSONResponse(response)


@app.get("/api/pipeline/runs/{run_id}")
async def api_get_run(run_id: str, request: Request) -> JSONResponse:
    ensure_admin_api(request)
    run = get_run_or_404(run_id)
    return JSONResponse(run_to_response(run))


@app.post("/api/pipeline/runs/{run_id}/steps/{step_id}")
async def api_run_step(run_id: str, step_id: str, payload: StepExecutionRequest, request: Request) -> JSONResponse:
    ensure_admin_api(request)
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
    if step_id == "resolver":
        storage.save_resolver_event(
            run_id=run_id,
            case_id=context.get("case_id"),
            article_hint=payload.inputs.get("article_hint", ""),
            strategy=output.get("resolver_strategy", "unknown"),
            confidence=output.get("resolver_confidence"),
            needs_human=bool(output.get("needs_human")),
            output=output,
        )
    if step_id == "fetcher":
        storage.save_fetcher_event(
            run_id=run_id,
            case_id=context.get("case_id"),
            article_url=output.get("article_url", ""),
            article_edit_url=output.get("article_edit_url"),
            fetch_status=output.get("snapshot_status", "unknown"),
            http_status=output.get("public_http_status"),
            output=output,
        )
    if step_id == "remediation":
        storage.save_remediation_event(
            run_id=run_id,
            case_id=context.get("case_id"),
            selected_action=output.get("selected_action", "unknown"),
            error_category=output.get("error_category", "other"),
            note_text=output.get("suggested_note_text", ""),
            backend=output.get("note_writer_backend", "unknown"),
            model=output.get("note_writer_model"),
            output=output,
        )
    if step_id == "stager":
        article_cms_id = context.get("article_cms_id")
        if article_cms_id is not None:
            try:
                article_cms_id = int(article_cms_id)
            except (TypeError, ValueError):
                article_cms_id = None
        storage.save_stager_event(
            run_id=run_id,
            case_id=context.get("case_id"),
            article_cms_id=article_cms_id,
            target_field=output.get("target_field", ""),
            remote_applied=bool(output.get("remote_applied")),
            remote_status=output.get("remote_status", "unknown"),
            preview_url=output.get("preview_url", ""),
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
    ensure_admin_api(request)

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
async def api_schedule_tasks(request: Request) -> JSONResponse:
    ensure_authenticated_api(request)
    return JSONResponse({"tasks": SCHEDULE_TASKS})
