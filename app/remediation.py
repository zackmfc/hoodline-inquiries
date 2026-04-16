from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import requests

REMEDIATION_ACTIONS = {
    "silent_correction",
    "update_stamp",
    "editors_note_bottom",
    "editors_note_top",
}

ERROR_CATEGORIES = {
    "date_or_sequence",
    "attribution",
    "figure",
    "quotation",
    "identity",
    "other",
}

FORBIDDEN_NOTE_TERMS = [
    r"\bai\b",
    r"\bhallucinat\w*\b",
    r"\bautomated system\w*\b",
    r"\bmodel error\w*\b",
    r"\bgenerat\w*\b",
    r"\bllm\b",
    r"\bmachine learning\b",
]

SYSTEM_PROMPT = """
You are a newsroom remediation classifier and note writer.
Return ONLY valid JSON with this schema:
{
  "selected_action": "silent_correction" | "update_stamp" | "editors_note_bottom" | "editors_note_top",
  "error_category": "date_or_sequence" | "attribution" | "figure" | "quotation" | "identity" | "other",
  "suggested_note_text": string
}
Rules:
- Notes must be neutral, factual, and concise (one or two sentences).
- Never mention AI, models, automation, hallucinations, or generation.
- Never speculate about intent, apologize broadly, or mention staff names.
- Prefer "A previous version of this article ..." framing for editor notes.
""".strip()


class RemediationEngine:
    def __init__(self) -> None:
        self.default_backend = os.getenv("REMEDIATION_BACKEND", "auto").strip().lower() or "auto"
        self.model = os.getenv("REMEDIATION_MODEL", "claude-3-5-sonnet-latest")
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        self.timeout_seconds = float(os.getenv("REMEDIATION_TIMEOUT_SECONDS", "45"))

    def classify_and_write(
        self,
        *,
        specific_claim: str,
        request_type: str,
        confidence: int | None,
        recommended_action: str | None = None,
    ) -> dict[str, Any]:
        backend = self.default_backend
        if backend not in {"auto", "rules", "claude"}:
            backend = "auto"

        if backend == "claude" and not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when remediation backend is 'claude'")

        if backend in {"auto", "claude"} and self.api_key:
            try:
                payload = self._classify_with_claude(
                    specific_claim=specific_claim,
                    request_type=request_type,
                    confidence=confidence,
                    recommended_action=recommended_action,
                )
                normalized = self._normalize_payload(
                    payload=payload,
                    specific_claim=specific_claim,
                    request_type=request_type,
                    confidence=confidence,
                    recommended_action=recommended_action,
                )
                normalized["note_writer_backend"] = "claude"
                normalized["note_writer_model"] = self.model
                return normalized
            except Exception as exc:
                if backend == "claude":
                    raise ValueError(f"Claude remediation failed: {exc}") from exc

                fallback = self._classify_with_rules(
                    specific_claim=specific_claim,
                    request_type=request_type,
                    confidence=confidence,
                    recommended_action=recommended_action,
                )
                fallback["note_writer_backend"] = "rules_fallback"
                fallback["note_writer_model"] = "rules"
                fallback["note_writer_warning"] = f"Claude unavailable: {exc}"
                return fallback

        payload = self._classify_with_rules(
            specific_claim=specific_claim,
            request_type=request_type,
            confidence=confidence,
            recommended_action=recommended_action,
        )
        payload["note_writer_backend"] = "rules"
        payload["note_writer_model"] = "rules"
        return payload

    def _classify_with_claude(
        self,
        *,
        specific_claim: str,
        request_type: str,
        confidence: int | None,
        recommended_action: str | None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/v1/messages"
        user_prompt = (
            f"request_type: {request_type}\n"
            f"confidence: {confidence if confidence is not None else 'unknown'}\n"
            f"recommended_action: {recommended_action or 'none'}\n"
            f"specific_claim: {specific_claim}\n\n"
            "Choose exactly one remediation action and write a compliant note."
        )
        request_payload = {
            "model": self.model,
            "max_tokens": 700,
            "temperature": 0,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        response = requests.post(
            url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=request_payload,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise ValueError(f"Anthropic API error {response.status_code}: {response.text[:300]}")

        data = response.json()
        content_blocks = data.get("content", [])
        text_parts = [block.get("text", "") for block in content_blocks if block.get("type") == "text"]
        raw_text = "\n".join(part for part in text_parts if part)
        return self._extract_json(raw_text)

    def _classify_with_rules(
        self,
        *,
        specific_claim: str,
        request_type: str,
        confidence: int | None,
        recommended_action: str | None,
    ) -> dict[str, Any]:
        category = self._infer_error_category(specific_claim)
        action = self._infer_action(
            specific_claim=specific_claim,
            request_type=request_type,
            confidence=confidence,
            recommended_action=recommended_action,
        )
        note_text = self._build_note_text(action=action, category=category)

        return {
            "selected_action": action,
            "error_category": category,
            "suggested_note_text": note_text,
            "note_writer_guardrails_applied": True,
        }

    def _normalize_payload(
        self,
        *,
        payload: dict[str, Any],
        specific_claim: str,
        request_type: str,
        confidence: int | None,
        recommended_action: str | None,
    ) -> dict[str, Any]:
        category = str(payload.get("error_category", "")).strip().lower()
        if category not in ERROR_CATEGORIES:
            category = self._infer_error_category(specific_claim)

        action = str(payload.get("selected_action", "")).strip().lower()
        if action not in REMEDIATION_ACTIONS:
            action = self._infer_action(
                specific_claim=specific_claim,
                request_type=request_type,
                confidence=confidence,
                recommended_action=recommended_action,
            )

        generated = str(payload.get("suggested_note_text", "")).strip()
        if not generated:
            generated = self._build_note_text(action=action, category=category)

        clean = self._sanitize_note(generated)
        return {
            "selected_action": action,
            "error_category": category,
            "suggested_note_text": clean,
            "note_writer_guardrails_applied": True,
        }

    def _extract_json(self, text: str) -> dict[str, Any]:
        clean = (text or "").strip()
        if not clean:
            raise ValueError("Empty remediation response")

        try:
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", clean)
        if not match:
            raise ValueError(f"No JSON object in remediation response: {clean[:200]}")

        return json.loads(match.group(0))

    def _infer_action(
        self,
        *,
        specific_claim: str,
        request_type: str,
        confidence: int | None,
        recommended_action: str | None,
    ) -> str:
        if recommended_action and recommended_action in REMEDIATION_ACTIONS:
            return recommended_action

        claim_lower = specific_claim.lower()
        if any(token in claim_lower for token in ["typo", "spelling", "misspelled", "broken link", "formatting"]):
            return "silent_correction"

        if request_type == "outdated_info":
            return "update_stamp"

        score = confidence if isinstance(confidence, int) else 6
        if score >= 9:
            return "editors_note_top"
        if score >= 6:
            return "editors_note_bottom"
        return "update_stamp"

    def _infer_error_category(self, specific_claim: str) -> str:
        text = specific_claim.lower()
        if any(term in text for term in ["date", "year", "month", "timeline", "sequence", "before", "after"]):
            return "date_or_sequence"
        if any(term in text for term in ["attribution", "attributed", "credited", "according to", "said by"]):
            return "attribution"
        if any(term in text for term in ["quote", "quoted", "quotation"]):
            return "quotation"
        if any(term in text for term in ["name", "identity", "person", "organization", "company", "business"]):
            return "identity"
        if any(term in text for term in ["number", "count", "total", "percent", "amount"]) or re.search(r"\d", text):
            return "figure"
        return "other"

    def _build_note_text(self, *, action: str, category: str) -> str:
        today = datetime.now(timezone.utc).strftime("%B %-d, %Y")
        if action == "silent_correction":
            return "No reader-facing note required for this change."
        if action == "update_stamp":
            return f"Update ({today}): This article has been updated to reflect newly confirmed information."
        if action == "editors_note_top":
            return "Editor's Note: A previous version of this article included a material factual error and has been corrected."

        category_phrases = {
            "date_or_sequence": "misstated the sequence of events or dates",
            "attribution": "misattributed information",
            "figure": "reported an incorrect figure",
            "quotation": "misquoted a source",
            "identity": "misidentified a person or organization",
            "other": "included a factual error",
        }
        phrase = category_phrases.get(category, category_phrases["other"])
        return f"Editor's Note: A previous version of this article {phrase} and has been corrected."

    def _sanitize_note(self, note_text: str) -> str:
        stripped = re.sub(r"\s+", " ", note_text).strip()
        if not stripped:
            return "Escalate to editor for manual note drafting."

        if self._contains_forbidden_term(stripped):
            return "Escalate to editor for manual note drafting."

        # Keep generated copy to at most two sentences.
        sentences = re.split(r"(?<=[.!?])\s+", stripped)
        clipped = " ".join(sentence.strip() for sentence in sentences[:2] if sentence.strip())
        if self._contains_forbidden_term(clipped):
            return "Escalate to editor for manual note drafting."
        return clipped

    def _contains_forbidden_term(self, text: str) -> bool:
        lowered = text.lower()
        return any(re.search(pattern, lowered) for pattern in FORBIDDEN_NOTE_TERMS)
