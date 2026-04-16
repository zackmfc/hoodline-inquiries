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
