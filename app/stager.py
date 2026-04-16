from __future__ import annotations

import difflib
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests


class DraftStager:
    def __init__(self, *, cms_base_url: str) -> None:
        self.cms_base_url = cms_base_url.rstrip("/")
        self.mode = os.getenv("STAGER_MODE", "dry_run").strip().lower() or "dry_run"
        self.timeout_seconds = float(os.getenv("STAGER_TIMEOUT_SECONDS", "30"))
        self.user_agent = os.getenv(
            "STAGER_USER_AGENT",
            "HoodlineDraftStager/1.0 (+https://hoodline.com)",
        )
        self.enable_remote_writes = os.getenv("STAGER_ENABLE_REMOTE_WRITES", "false").strip().lower() == "true"
        self.cms_username = os.getenv("CMS_STAGER_USERNAME", "").strip()
        self.cms_app_password = os.getenv("CMS_STAGER_APP_PASSWORD", "").strip()

    def stage(
        self,
        *,
        article_cms_id: int | None,
        article_url: str,
        article_edit_url: str | None,
        target_field: str,
        new_value: str,
        current_value: str,
        suggested_note_text: str | None,
        selected_action: str | None,
    ) -> dict[str, Any]:
        target = (target_field or "").strip().lower()
        if target not in {"title", "body", "excerpt", "meta_description"}:
            raise ValueError("target_field must be one of: title, body, excerpt, meta_description")

        before_text = (current_value or "").strip()
        after_text = (new_value or "").strip()
        if not after_text:
            raise ValueError("new_value is required")

        note_text = (suggested_note_text or "").strip()
        selected = (selected_action or "").strip().lower()
        note_placement = "none"
        note_applied = False

        if target == "body" and note_text and note_text.lower() != "no reader-facing note required for this change.":
            if selected == "editors_note_top":
                after_text = f"{note_text}\n\n{after_text}"
                note_placement = "top"
                note_applied = True
            elif selected in {"editors_note_bottom", "update_stamp"}:
                after_text = f"{after_text}\n\n{note_text}"
                note_placement = "bottom"
                note_applied = True

        diff_patch = self._build_diff(before_text, after_text)
        diff_summary = self._build_diff_summary(before_text, after_text, target)

        remote_applied, remote_status, remote_reference = self._attempt_remote_stage(
            article_cms_id=article_cms_id,
            target_field=target,
            value=after_text,
        )

        preview_url = self._build_preview_url(article_url=article_url, article_cms_id=article_cms_id)
        if not preview_url and article_edit_url:
            preview_url = article_edit_url

        return {
            "staged": True,
            "stager_mode": self.mode,
            "remote_applied": remote_applied,
            "remote_status": remote_status,
            "remote_reference": remote_reference,
            "target_field": target,
            "before_value": before_text,
            "new_value": after_text,
            "note_applied": note_applied,
            "note_placement": note_placement,
            "preview_url": preview_url,
            "diff_summary": diff_summary,
            "diff_patch": diff_patch,
            "last_edited_at": datetime.now(timezone.utc).isoformat(),
        }

    def _attempt_remote_stage(
        self,
        *,
        article_cms_id: int | None,
        target_field: str,
        value: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        if self.mode != "wp_rest":
            return False, "dry_run_only", None
        if not self.enable_remote_writes:
            return False, "remote_writes_disabled", None
        if article_cms_id is None:
            return False, "missing_article_cms_id", None
        if not self.cms_username or not self.cms_app_password:
            return False, "missing_cms_credentials", None

        autosave_url = f"{self.cms_base_url}/wp-json/wp/v2/posts/{article_cms_id}/autosaves"
        payload = self._payload_for_target(target_field, value)
        headers = {"User-Agent": self.user_agent}
        try:
            response = requests.post(
                autosave_url,
                auth=(self.cms_username, self.cms_app_password),
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
            if response.status_code >= 400:
                snippet = response.text[:200].replace("\n", " ")
                return False, f"autosave_failed_{response.status_code}: {snippet}", None

            data = response.json()
            return True, "autosave_created", {"autosave_id": data.get("id"), "post_id": article_cms_id}
        except requests.RequestException as exc:
            return False, f"autosave_request_failed: {exc}", None

    def _payload_for_target(self, target_field: str, value: str) -> dict[str, Any]:
        if target_field in {"body", "meta_description"}:
            return {"content": value}
        if target_field == "title":
            return {"title": value}
        return {"excerpt": value}

    def _build_preview_url(self, *, article_url: str, article_cms_id: int | None) -> str:
        if article_cms_id is not None:
            return f"{self.cms_base_url}/?p={article_cms_id}&preview=true"

        parsed = urlparse(article_url)
        if parsed.scheme and parsed.netloc:
            separator = "&" if parsed.query else "?"
            return f"{article_url}{separator}preview=1"
        return ""

    def _build_diff(self, before_text: str, after_text: str) -> str:
        before_lines = before_text.splitlines() or [""]
        after_lines = after_text.splitlines() or [""]
        diff = difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
            n=2,
        )
        return "\n".join(diff)[:8000]

    def _build_diff_summary(self, before_text: str, after_text: str, target_field: str) -> str:
        before_words = len(before_text.split())
        after_words = len(after_text.split())
        delta_words = after_words - before_words
        if delta_words > 0:
            direction = f"+{delta_words} words"
        elif delta_words < 0:
            direction = f"{delta_words} words"
        else:
            direction = "no word-count change"
        return f"Staged {target_field} update ({direction})."
