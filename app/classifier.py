from __future__ import annotations

import json
import os
import re
from typing import Any

import requests

REQUEST_TYPES = {
    "factual_error",
    "outdated_info",
    "missing_context",
    "opinion_disagreement",
    "pr_pitch",
    "other",
}

AUTHORITY_SIGNALS = {
    "first_party",
    "expert",
    "reader",
    "anonymous",
    "unknown",
}

SYSTEM_PROMPT = """
You are classifying reader emails for a newsroom correction intake pipeline.
Return ONLY valid JSON with this schema:
{
  "is_correction_request": boolean,
  "request_type": "factual_error" | "outdated_info" | "missing_context" | "opinion_disagreement" | "pr_pitch" | "other",
  "specific_claim": string,
  "proposed_correction": string,
  "referenced_article_hint": string,
  "sender_authority_signal": "first_party" | "expert" | "reader" | "anonymous" | "unknown"
}
No markdown fences. No additional keys.
""".strip()


class MessageClassifier:
    def __init__(self) -> None:
        self.default_backend = os.getenv("CLASSIFIER_BACKEND", "auto").strip().lower() or "auto"
        self.model = os.getenv("CLASSIFIER_MODEL", "claude-3-5-sonnet-latest")
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        self.timeout_seconds = float(os.getenv("CLASSIFIER_TIMEOUT_SECONDS", "45"))

    def classify(
        self,
        *,
        sender: str,
        subject: str,
        body: str,
        backend_override: str | None = None,
    ) -> dict[str, Any]:
        backend = (backend_override or self.default_backend or "auto").strip().lower()
        if backend not in {"auto", "rules", "claude"}:
            backend = "auto"

        if backend == "claude" and not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when classifier backend is 'claude'")

        if backend in {"auto", "claude"} and self.api_key:
            try:
                payload = self._classify_with_claude(sender=sender, subject=subject, body=body)
                payload["classifier_backend"] = "claude"
                payload["classifier_model"] = self.model
                return payload
            except Exception as exc:
                if backend == "claude":
                    raise ValueError(f"Claude classification failed: {exc}") from exc

                fallback = self._classify_with_rules(sender=sender, subject=subject, body=body)
                fallback["classifier_backend"] = "rules_fallback"
                fallback["classifier_model"] = "rules"
                fallback["classifier_warning"] = f"Claude unavailable: {exc}"
                return fallback

        payload = self._classify_with_rules(sender=sender, subject=subject, body=body)
        payload["classifier_backend"] = "rules"
        payload["classifier_model"] = "rules"
        return payload

    def _classify_with_claude(self, *, sender: str, subject: str, body: str) -> dict[str, Any]:
        url = f"{self.base_url}/v1/messages"
        user_prompt = (
            f"Sender: {sender}\n"
            f"Subject: {subject}\n"
            f"Body:\n{body}\n\n"
            "Classify this message according to the required JSON schema."
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
        parsed = self._extract_json(raw_text)
        return self._normalize_payload(parsed, sender=sender, subject=subject, body=body)

    def _classify_with_rules(self, *, sender: str, subject: str, body: str) -> dict[str, Any]:
        content = f"{subject}\n{body}".lower()

        if any(token in content for token in ["wrong", "incorrect", "inaccurate", "error", "false"]):
            request_type = "factual_error"
        elif any(token in content for token in ["outdated", "old", "last year", "no longer", "updated"]):
            request_type = "outdated_info"
        elif any(token in content for token in ["missing context", "left out", "omits", "missing"]):
            request_type = "missing_context"
        elif any(token in content for token in ["i disagree", "opinion", "biased", "spin"]):
            request_type = "opinion_disagreement"
        elif any(token in content for token in ["press release", "pitch", "sponsored", "partner content"]):
            request_type = "pr_pitch"
        else:
            request_type = "other"

        is_correction = request_type in {"factual_error", "outdated_info", "missing_context"}
        specific_claim = body.split(".")[0].strip() if body.strip() else "No specific claim extracted"
        referenced = self._extract_reference_hint(subject, body)
        authority = self._infer_authority(sender, body)

        return {
            "is_correction_request": is_correction,
            "request_type": request_type,
            "specific_claim": specific_claim,
            "proposed_correction": "Extracted from sender narrative",
            "referenced_article_hint": referenced,
            "sender_authority_signal": authority,
        }

    def _extract_reference_hint(self, subject: str, body: str) -> str:
        url_match = re.search(r"https?://[^\s]+", f"{subject}\n{body}")
        if url_match:
            return url_match.group(0).rstrip(".,)")

        lines = [line.strip() for line in body.splitlines() if line.strip()]
        for line in lines:
            if len(line) >= 18 and any(word in line.lower() for word in ["article", "story", "headline", "hoodline"]):
                return line[:240]

        return ""

    def _infer_authority(self, sender: str, body: str) -> str:
        sender_lower = sender.lower()
        body_lower = body.lower()

        if any(token in sender_lower for token in [".gov", ".edu"]):
            return "expert"
        if any(token in body_lower for token in ["i am the owner", "i am the subject", "this is my business", "my company"]):
            return "first_party"
        if not sender.strip():
            return "anonymous"
        return "reader"

    def _extract_json(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ValueError("Empty classifier response")

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise ValueError(f"No JSON object in classifier response: {text[:200]}")

        return json.loads(match.group(0))

    def _normalize_payload(self, payload: dict[str, Any], *, sender: str, subject: str, body: str) -> dict[str, Any]:
        request_type = str(payload.get("request_type", "other")).strip().lower()
        if request_type not in REQUEST_TYPES:
            request_type = "other"

        authority = str(payload.get("sender_authority_signal", "unknown")).strip().lower()
        if authority not in AUTHORITY_SIGNALS:
            authority = "unknown"

        specific_claim = str(payload.get("specific_claim", "")).strip()
        if not specific_claim:
            specific_claim = body.split(".")[0].strip() or "No specific claim extracted"

        referenced_hint = str(payload.get("referenced_article_hint", "")).strip()
        if not referenced_hint:
            referenced_hint = self._extract_reference_hint(subject, body)

        proposed = str(payload.get("proposed_correction", "")).strip()
        if not proposed:
            proposed = "Extracted from sender narrative"

        is_correction_raw = payload.get("is_correction_request")
        if isinstance(is_correction_raw, bool):
            is_correction = is_correction_raw
        else:
            is_correction = request_type in {"factual_error", "outdated_info", "missing_context"}

        return {
            "is_correction_request": is_correction,
            "request_type": request_type,
            "specific_claim": specific_claim,
            "proposed_correction": proposed,
            "referenced_article_hint": referenced_hint,
            "sender_authority_signal": authority,
        }
