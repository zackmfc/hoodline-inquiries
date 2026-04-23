from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass
class PipelineRunRecord:
    run_id: str
    created_at: datetime
    created_by: str | None
    current_index: int
    context: dict[str, Any]
    outputs: list[dict[str, Any]]


class Storage:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def init_schema(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pipeline_runs (
                        run_id TEXT PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        created_by TEXT,
                        current_index INTEGER NOT NULL DEFAULT 0,
                        context_json JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pipeline_outputs (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
                        step_index INTEGER NOT NULL,
                        step_id TEXT NOT NULL,
                        step_label TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        inputs_json JSONB NOT NULL,
                        output_json JSONB NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_outputs_run_step
                    ON pipeline_outputs(run_id, step_index)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS gmail_intake_events (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
                        case_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        gmail_message_id TEXT,
                        gmail_thread_id TEXT,
                        sender TEXT,
                        subject TEXT,
                        body TEXT,
                        matched_keywords TEXT[] NOT NULL DEFAULT '{}',
                        is_candidate BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS classifier_events (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
                        case_id TEXT,
                        backend TEXT NOT NULL,
                        model TEXT,
                        sender TEXT,
                        subject TEXT,
                        body TEXT,
                        output_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS editorial_posts (
                        id BIGSERIAL PRIMARY KEY,
                        source TEXT NOT NULL DEFAULT 'discord',
                        channel TEXT,
                        message_id TEXT,
                        title TEXT NOT NULL,
                        article_url TEXT NOT NULL,
                        cms_edit_url TEXT,
                        content TEXT,
                        posted_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        UNIQUE (source, message_id)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS resolver_events (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
                        case_id TEXT,
                        article_hint TEXT,
                        strategy TEXT NOT NULL,
                        confidence DOUBLE PRECISION,
                        needs_human BOOLEAN NOT NULL DEFAULT FALSE,
                        output_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL CHECK (role IN ('superuser', 'admin', 'user')),
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS fetcher_events (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
                        case_id TEXT,
                        article_url TEXT NOT NULL,
                        article_edit_url TEXT,
                        fetch_status TEXT NOT NULL,
                        http_status INTEGER,
                        output_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS remediation_events (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
                        case_id TEXT,
                        selected_action TEXT NOT NULL,
                        error_category TEXT NOT NULL,
                        note_text TEXT NOT NULL,
                        backend TEXT NOT NULL,
                        model TEXT,
                        output_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS stager_events (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
                        case_id TEXT,
                        article_cms_id INTEGER,
                        target_field TEXT NOT NULL,
                        remote_applied BOOLEAN NOT NULL DEFAULT FALSE,
                        remote_status TEXT NOT NULL,
                        preview_url TEXT,
                        output_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS review_decisions (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
                        case_id TEXT,
                        reviewer_username TEXT NOT NULL,
                        action TEXT NOT NULL CHECK (action IN (
                            'approve_publish', 'edit_publish', 'reject',
                            'send_back', 'escalate'
                        )),
                        editor_note TEXT NOT NULL DEFAULT '',
                        reviewer_notes TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_review_decisions_run
                    ON review_decisions(run_id)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS correction_wizard_emails (
                        gmail_message_id TEXT PRIMARY KEY,
                        gmail_thread_id TEXT,
                        sender_raw TEXT NOT NULL DEFAULT '',
                        sender_name TEXT NOT NULL DEFAULT '',
                        sender_email TEXT NOT NULL DEFAULT '',
                        subject TEXT NOT NULL DEFAULT '',
                        snippet TEXT NOT NULL DEFAULT '',
                        body TEXT NOT NULL DEFAULT '',
                        received_at TIMESTAMPTZ,
                        status TEXT NOT NULL DEFAULT 'new',
                        state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        last_touched_by TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_correction_wizard_emails_status
                    ON correction_wizard_emails(status)
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_correction_wizard_emails_updated
                    ON correction_wizard_emails(updated_at DESC)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS verification_events (
                        id BIGSERIAL PRIMARY KEY,
                        run_id TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
                        case_id TEXT,
                        confidence INTEGER,
                        recommended_action TEXT NOT NULL,
                        backend TEXT NOT NULL,
                        model TEXT,
                        link_checks INTEGER NOT NULL DEFAULT 0,
                        search_queries INTEGER NOT NULL DEFAULT 0,
                        output_json JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )

    def create_run(self, run_id: str, created_by: str | None) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_runs (run_id, created_by)
                    VALUES (%s, %s)
                    """,
                    (run_id, created_by),
                )

    def create_user(self, *, username: str, password_hash: str, role: str, is_active: bool = True) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, role, is_active)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (username, password_hash, role, is_active),
                )

    def upsert_user(
        self,
        *,
        username: str,
        password_hash: str,
        role: str,
        is_active: bool = True,
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (username, password_hash, role, is_active)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (username)
                    DO UPDATE SET
                        password_hash = EXCLUDED.password_hash,
                        role = EXCLUDED.role,
                        is_active = EXCLUDED.is_active,
                        updated_at = NOW()
                    """,
                    (username, password_hash, role, is_active),
                )

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT username, password_hash, role, is_active, created_at, updated_at
                    FROM users
                    WHERE username = %s
                    """,
                    (username,),
                )
                row = cur.fetchone()
        return dict(row) if row is not None else None

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT username, role, is_active, created_at, updated_at
                    FROM users
                    ORDER BY username ASC
                    """
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def update_user(
        self,
        *,
        username: str,
        role: str | None = None,
        password_hash: str | None = None,
        is_active: bool | None = None,
    ) -> None:
        updates: list[str] = []
        params: list[Any] = []
        if role is not None:
            updates.append("role = %s")
            params.append(role)
        if password_hash is not None:
            updates.append("password_hash = %s")
            params.append(password_hash)
        if is_active is not None:
            updates.append("is_active = %s")
            params.append(is_active)

        if not updates:
            return

        updates.append("updated_at = NOW()")
        params.append(username)

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    UPDATE users
                    SET {", ".join(updates)}
                    WHERE username = %s
                    """,
                    params,
                )
                if cur.rowcount == 0:
                    raise ValueError("User not found")

    def run_exists(self, run_id: str) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pipeline_runs WHERE run_id = %s", (run_id,))
                row = cur.fetchone()
                return row is not None

    def get_run(self, run_id: str) -> PipelineRunRecord | None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT run_id, created_at, created_by, current_index, context_json
                    FROM pipeline_runs
                    WHERE run_id = %s
                    """,
                    (run_id,),
                )
                run_row = cur.fetchone()
                if run_row is None:
                    return None

                cur.execute(
                    """
                    SELECT step_index, step_id, step_label, created_at, inputs_json, output_json
                    FROM pipeline_outputs
                    WHERE run_id = %s
                    ORDER BY step_index ASC
                    """,
                    (run_id,),
                )
                output_rows = cur.fetchall()

        outputs: list[dict[str, Any]] = []
        for row in output_rows:
            outputs.append(
                {
                    "step_index": row["step_index"],
                    "step_id": row["step_id"],
                    "step_label": row["step_label"],
                    "timestamp": row["created_at"].isoformat(),
                    "inputs": row["inputs_json"],
                    "output": row["output_json"],
                }
            )

        return PipelineRunRecord(
            run_id=run_row["run_id"],
            created_at=run_row["created_at"],
            created_by=run_row.get("created_by"),
            current_index=run_row["current_index"],
            context=run_row["context_json"] or {},
            outputs=outputs,
        )

    def append_step_output(
        self,
        run_id: str,
        *,
        step_index: int,
        step_id: str,
        step_label: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        context: dict[str, Any],
        current_index: int,
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pipeline_outputs (
                        run_id, step_index, step_id, step_label, inputs_json, output_json
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (run_id, step_index, step_id, step_label, Jsonb(inputs), Jsonb(output)),
                )
                cur.execute(
                    """
                    UPDATE pipeline_runs
                    SET current_index = %s,
                        context_json = %s
                    WHERE run_id = %s
                    """,
                    (current_index, Jsonb(context), run_id),
                )

    def save_gmail_intake_event(
        self,
        *,
        run_id: str,
        case_id: str,
        source: str,
        gmail_message_id: str | None,
        gmail_thread_id: str | None,
        sender: str,
        subject: str,
        body: str,
        matched_keywords: list[str],
        is_candidate: bool,
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO gmail_intake_events (
                        run_id,
                        case_id,
                        source,
                        gmail_message_id,
                        gmail_thread_id,
                        sender,
                        subject,
                        body,
                        matched_keywords,
                        is_candidate
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        case_id,
                        source,
                        gmail_message_id,
                        gmail_thread_id,
                        sender,
                        subject,
                        body,
                        matched_keywords,
                        is_candidate,
                    ),
                )

    def save_classifier_event(
        self,
        *,
        run_id: str,
        case_id: str | None,
        backend: str,
        model: str | None,
        sender: str,
        subject: str,
        body: str,
        output: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO classifier_events (
                        run_id,
                        case_id,
                        backend,
                        model,
                        sender,
                        subject,
                        body,
                        output_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        case_id,
                        backend,
                        model,
                        sender,
                        subject,
                        body,
                        Jsonb(output),
                    ),
                )

    def upsert_editorial_post(
        self,
        *,
        source: str,
        channel: str | None,
        message_id: str | None,
        title: str,
        article_url: str,
        cms_edit_url: str | None,
        content: str | None,
        posted_at: datetime | None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            with conn.cursor() as cur:
                if message_id:
                    cur.execute(
                        """
                        INSERT INTO editorial_posts (
                            source, channel, message_id, title, article_url, cms_edit_url, content, posted_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (source, message_id)
                        DO UPDATE SET
                            channel = EXCLUDED.channel,
                            title = EXCLUDED.title,
                            article_url = EXCLUDED.article_url,
                            cms_edit_url = EXCLUDED.cms_edit_url,
                            content = EXCLUDED.content,
                            posted_at = EXCLUDED.posted_at,
                            updated_at = NOW()
                        RETURNING id, source, channel, message_id, title, article_url, cms_edit_url, content, posted_at, created_at, updated_at
                        """,
                        (source, channel, message_id, title, article_url, cms_edit_url, content, posted_at),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO editorial_posts (
                            source, channel, message_id, title, article_url, cms_edit_url, content, posted_at
                        )
                        VALUES (%s, %s, NULL, %s, %s, %s, %s, %s)
                        RETURNING id, source, channel, message_id, title, article_url, cms_edit_url, content, posted_at, created_at, updated_at
                        """,
                        (source, channel, title, article_url, cms_edit_url, content, posted_at),
                    )
                row = cur.fetchone()

        if row is None:
            raise RuntimeError("Failed to upsert editorial post")

        return dict(row)

    def latest_editorial_posted_at(self, *, source: str = "discord") -> datetime | None:
        """Return the newest posted_at in editorial_posts for the given source.

        Used by the incremental Discord cache refresh so we only scan Discord
        messages posted after this timestamp instead of re-walking the full
        90-day window.
        """
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MAX(posted_at) AS newest
                    FROM editorial_posts
                    WHERE source = %s
                    """,
                    (source,),
                )
                row = cur.fetchone()

        if not row:
            return None
        value = row["newest"] if isinstance(row, dict) else row[0]
        if isinstance(value, datetime):
            return value
        return None

    def list_editorial_posts(self, *, limit: int = 200) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 1000))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, source, channel, message_id, title, article_url, cms_edit_url, content, posted_at, created_at, updated_at
                    FROM editorial_posts
                    ORDER BY COALESCE(posted_at, created_at) DESC
                    LIMIT %s
                    """,
                    (bounded_limit,),
                )
                rows = cur.fetchall()

        return [dict(row) for row in rows]

    def search_editorial_posts(
        self,
        *,
        terms: list[str],
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Return editorial posts where every term appears (ILIKE) in title OR content.

        Newest first. Used by the article locator to find the Discord
        message for an article given the first N words of its title.
        """
        cleaned = [t.strip() for t in terms if t and t.strip()]
        if not cleaned:
            return []

        bounded_limit = max(1, min(limit, 200))
        conditions: list[str] = []
        params: list[Any] = []
        for term in cleaned:
            conditions.append("(title ILIKE %s OR content ILIKE %s)")
            pattern = f"%{term}%"
            params.append(pattern)
            params.append(pattern)

        params.append(bounded_limit)
        where_clause = " AND ".join(conditions)

        sql = f"""
            SELECT id, source, channel, message_id, title, article_url, cms_edit_url, content, posted_at, created_at, updated_at
            FROM editorial_posts
            WHERE {where_clause}
            ORDER BY COALESCE(posted_at, created_at) DESC
            LIMIT %s
        """

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall()

        return [dict(row) for row in rows]

    def save_resolver_event(
        self,
        *,
        run_id: str,
        case_id: str | None,
        article_hint: str,
        strategy: str,
        confidence: float | None,
        needs_human: bool,
        output: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO resolver_events (
                        run_id,
                        case_id,
                        article_hint,
                        strategy,
                        confidence,
                        needs_human,
                        output_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        case_id,
                        article_hint,
                        strategy,
                        confidence,
                        needs_human,
                        Jsonb(output),
                    ),
                )

    def save_fetcher_event(
        self,
        *,
        run_id: str,
        case_id: str | None,
        article_url: str,
        article_edit_url: str | None,
        fetch_status: str,
        http_status: int | None,
        output: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO fetcher_events (
                        run_id,
                        case_id,
                        article_url,
                        article_edit_url,
                        fetch_status,
                        http_status,
                        output_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        case_id,
                        article_url,
                        article_edit_url,
                        fetch_status,
                        http_status,
                        Jsonb(output),
                    ),
                )

    def save_remediation_event(
        self,
        *,
        run_id: str,
        case_id: str | None,
        selected_action: str,
        error_category: str,
        note_text: str,
        backend: str,
        model: str | None,
        output: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO remediation_events (
                        run_id,
                        case_id,
                        selected_action,
                        error_category,
                        note_text,
                        backend,
                        model,
                        output_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        case_id,
                        selected_action,
                        error_category,
                        note_text,
                        backend,
                        model,
                        Jsonb(output),
                    ),
                )

    def save_stager_event(
        self,
        *,
        run_id: str,
        case_id: str | None,
        article_cms_id: int | None,
        target_field: str,
        remote_applied: bool,
        remote_status: str,
        preview_url: str,
        output: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO stager_events (
                        run_id,
                        case_id,
                        article_cms_id,
                        target_field,
                        remote_applied,
                        remote_status,
                        preview_url,
                        output_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        case_id,
                        article_cms_id,
                        target_field,
                        remote_applied,
                        remote_status,
                        preview_url,
                        Jsonb(output),
                    ),
                )

    def list_review_cases(self, *, status_filter: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Return pipeline runs that have reached the review_dashboard step, enriched with step outputs."""
        bounded_limit = max(1, min(limit, 200))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        r.run_id,
                        r.created_at,
                        r.created_by,
                        r.current_index,
                        r.context_json,
                        (
                            SELECT json_agg(
                                json_build_object(
                                    'step_id', po.step_id,
                                    'step_label', po.step_label,
                                    'output', po.output_json
                                ) ORDER BY po.step_index
                            )
                            FROM pipeline_outputs po
                            WHERE po.run_id = r.run_id
                        ) AS step_outputs,
                        (
                            SELECT rd.action
                            FROM review_decisions rd
                            WHERE rd.run_id = r.run_id
                            ORDER BY rd.created_at DESC
                            LIMIT 1
                        ) AS latest_review_action
                    FROM pipeline_runs r
                    WHERE r.current_index >= 7
                    ORDER BY r.created_at DESC
                    LIMIT %s
                    """,
                    (bounded_limit,),
                )
                rows = cur.fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            latest_action = row.get("latest_review_action")
            if status_filter == "pending" and latest_action is not None:
                continue
            if status_filter == "reviewed" and latest_action is None:
                continue
            results.append(dict(row))
        return results

    def get_review_case(self, run_id: str) -> dict[str, Any] | None:
        """Get a single review case with full context and decision history."""
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        r.run_id,
                        r.created_at,
                        r.created_by,
                        r.current_index,
                        r.context_json
                    FROM pipeline_runs r
                    WHERE r.run_id = %s AND r.current_index >= 7
                    """,
                    (run_id,),
                )
                run_row = cur.fetchone()
                if run_row is None:
                    return None

                cur.execute(
                    """
                    SELECT step_index, step_id, step_label, output_json, created_at
                    FROM pipeline_outputs
                    WHERE run_id = %s
                    ORDER BY step_index ASC
                    """,
                    (run_id,),
                )
                step_rows = cur.fetchall()

                cur.execute(
                    """
                    SELECT id, reviewer_username, action, editor_note, reviewer_notes, created_at
                    FROM review_decisions
                    WHERE run_id = %s
                    ORDER BY created_at DESC
                    """,
                    (run_id,),
                )
                decision_rows = cur.fetchall()

        result = dict(run_row)
        result["step_outputs"] = [dict(r) for r in step_rows]
        result["decisions"] = [dict(r) for r in decision_rows]
        return result

    def save_review_decision(
        self,
        *,
        run_id: str,
        case_id: str | None,
        reviewer_username: str,
        action: str,
        editor_note: str = "",
        reviewer_notes: str = "",
    ) -> dict[str, Any]:
        valid_actions = {"approve_publish", "edit_publish", "reject", "send_back", "escalate"}
        if action not in valid_actions:
            raise ValueError(f"Invalid action: {action}. Must be one of {valid_actions}")

        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO review_decisions (
                        run_id, case_id, reviewer_username, action, editor_note, reviewer_notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, run_id, case_id, reviewer_username, action, editor_note, reviewer_notes, created_at
                    """,
                    (run_id, case_id, reviewer_username, action, editor_note, reviewer_notes),
                )
                row = cur.fetchone()

        if row is None:
            raise RuntimeError("Failed to save review decision")
        return dict(row)

    def upsert_wizard_email(
        self,
        *,
        gmail_message_id: str,
        gmail_thread_id: str | None,
        sender_raw: str,
        sender_name: str,
        sender_email: str,
        subject: str,
        snippet: str,
        body: str,
        received_at: datetime | None,
    ) -> None:
        """Insert or update the base email record. Leaves state/status untouched on conflict."""
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO correction_wizard_emails (
                        gmail_message_id, gmail_thread_id,
                        sender_raw, sender_name, sender_email,
                        subject, snippet, body, received_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (gmail_message_id)
                    DO UPDATE SET
                        gmail_thread_id = EXCLUDED.gmail_thread_id,
                        sender_raw = EXCLUDED.sender_raw,
                        sender_name = EXCLUDED.sender_name,
                        sender_email = EXCLUDED.sender_email,
                        subject = EXCLUDED.subject,
                        snippet = EXCLUDED.snippet,
                        body = EXCLUDED.body,
                        received_at = EXCLUDED.received_at,
                        updated_at = NOW()
                    """,
                    (
                        gmail_message_id,
                        gmail_thread_id,
                        sender_raw,
                        sender_name,
                        sender_email,
                        subject,
                        snippet,
                        body,
                        received_at,
                    ),
                )

    def save_wizard_step(
        self,
        *,
        gmail_message_id: str,
        step_key: str,
        step_data: dict[str, Any],
        status: str,
        touched_by: str | None,
    ) -> None:
        """Merge a step's output into state_json and update status + touched_by."""
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO correction_wizard_emails (
                        gmail_message_id, status, state_json, last_touched_by
                    )
                    VALUES (
                        %s, %s, jsonb_build_object(%s::text, %s::jsonb), %s
                    )
                    ON CONFLICT (gmail_message_id)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        state_json =
                            COALESCE(correction_wizard_emails.state_json, '{}'::jsonb)
                            || jsonb_build_object(%s::text, %s::jsonb),
                        last_touched_by = EXCLUDED.last_touched_by,
                        updated_at = NOW()
                    """,
                    (
                        gmail_message_id,
                        status,
                        step_key,
                        Jsonb(step_data),
                        touched_by,
                        step_key,
                        Jsonb(step_data),
                    ),
                )

    def get_wizard_email(self, gmail_message_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT gmail_message_id, gmail_thread_id,
                           sender_raw, sender_name, sender_email,
                           subject, snippet, body, received_at,
                           status, state_json, last_touched_by,
                           created_at, updated_at
                    FROM correction_wizard_emails
                    WHERE gmail_message_id = %s
                    """,
                    (gmail_message_id,),
                )
                row = cur.fetchone()
        return dict(row) if row is not None else None

    def get_wizard_emails_by_ids(
        self, message_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        if not message_ids:
            return {}
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT gmail_message_id, status, state_json,
                           last_touched_by, updated_at
                    FROM correction_wizard_emails
                    WHERE gmail_message_id = ANY(%s)
                    """,
                    (message_ids,),
                )
                rows = cur.fetchall()
        return {row["gmail_message_id"]: dict(row) for row in rows}

    def list_wizard_emails_by_statuses(
        self,
        *,
        statuses: list[str],
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not statuses:
            return []
        bounded_limit = max(1, min(limit, 500))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT gmail_message_id, gmail_thread_id,
                           sender_raw, sender_name, sender_email,
                           subject, snippet, received_at,
                           status, state_json, last_touched_by,
                           created_at, updated_at
                    FROM correction_wizard_emails
                    WHERE status = ANY(%s)
                    ORDER BY COALESCE(received_at, updated_at) DESC
                    LIMIT %s
                    """,
                    (statuses, bounded_limit),
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]

    def clear_wizard_emails(self) -> int:
        """Delete every row in correction_wizard_emails. Returns rows removed.

        Used by the "Clear wizard cache" button in the Corrections wizard so
        the operator can start over with a fresh queue — every email
        re-appears as status=new.
        """
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM correction_wizard_emails")
                return cur.rowcount or 0

    def mark_wizard_corrected(
        self,
        *,
        gmail_message_id: str,
        touched_by: str | None,
    ) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE correction_wizard_emails
                    SET status = 'corrected',
                        last_touched_by = COALESCE(%s, last_touched_by),
                        updated_at = NOW()
                    WHERE gmail_message_id = %s
                    """,
                    (touched_by, gmail_message_id),
                )
                return cur.rowcount > 0

    def set_wizard_triage(
        self,
        *,
        gmail_message_id: str,
        status: str,
        touched_by: str | None,
    ) -> bool:
        """Set a manual triage decision on a pulled email.

        status must be 'triaged_pending' or 'triaged_rejected'. The email is
        looked up by Gmail message id; if it isn't cached yet the caller
        should upsert it first via upsert_wizard_email.
        """
        if status not in {"triaged_pending", "triaged_rejected"}:
            raise ValueError(f"Invalid triage status: {status}")
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE correction_wizard_emails
                    SET status = %s,
                        last_touched_by = COALESCE(%s, last_touched_by),
                        updated_at = NOW()
                    WHERE gmail_message_id = %s
                    """,
                    (status, touched_by, gmail_message_id),
                )
                return cur.rowcount > 0

    def fetch_wizard_subject_body_by_statuses(
        self,
        *,
        statuses: list[str],
        limit: int = 2000,
    ) -> list[tuple[str, str]]:
        """Return (subject, body) pairs for emails in the given status set.

        Used by the keyword-analysis endpoint. Bounded by `limit` so a huge
        triage history can't blow out the analyzer.
        """
        if not statuses:
            return []
        bounded_limit = max(1, min(limit, 5000))
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT subject, body
                    FROM correction_wizard_emails
                    WHERE status = ANY(%s)
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (statuses, bounded_limit),
                )
                rows = cur.fetchall()
        return [(row.get("subject") or "", row.get("body") or "") for row in rows]

    def save_verification_event(
        self,
        *,
        run_id: str,
        case_id: str | None,
        confidence: int | None,
        recommended_action: str,
        backend: str,
        model: str | None,
        link_checks: int,
        search_queries: int,
        output: dict[str, Any],
    ) -> None:
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO verification_events (
                        run_id, case_id, confidence, recommended_action,
                        backend, model, link_checks, search_queries, output_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        case_id,
                        confidence,
                        recommended_action,
                        backend,
                        model,
                        link_checks,
                        search_queries,
                        Jsonb(output),
                    ),
                )
