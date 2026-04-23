from __future__ import annotations

import base64
import json
import os
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailClient:
    def __init__(self) -> None:
        self.service_account_file = os.getenv("GMAIL_SERVICE_ACCOUNT_FILE", "")
        self.service_account_json = os.getenv("GMAIL_SERVICE_ACCOUNT_JSON", "")
        self.delegated_user = os.getenv("GMAIL_DELEGATED_USER", "")
        self.default_query = os.getenv("GMAIL_DEFAULT_QUERY", "label:auto/correction-candidate is:unread")

    @property
    def configured(self) -> bool:
        return bool(self.delegated_user and (self.service_account_file or self.service_account_json))

    def fetch_intake_message(
        self,
        *,
        message_id: str | None,
        query: str | None,
        label_ids: list[str] | None,
        max_results: int,
    ) -> dict[str, Any]:
        results = self.fetch_intake_messages(
            message_id=message_id,
            query=query,
            label_ids=label_ids,
            max_results=1,
        )
        if not results:
            raise ValueError("No Gmail messages found")
        return results[0]

    def fetch_intake_messages(
        self,
        *,
        message_id: str | None = None,
        query: str | None = None,
        label_ids: list[str] | None = None,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Fetch up to max_results emails from Gmail. Returns a list of normalized messages."""
        if not self.configured:
            raise ValueError(
                "Gmail API is not configured. Set GMAIL_DELEGATED_USER plus GMAIL_SERVICE_ACCOUNT_FILE or GMAIL_SERVICE_ACCOUNT_JSON."
            )

        service = self._build_service()

        if message_id:
            msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
            return [self._normalize_message(msg)]

        q = query or self.default_query
        request = service.users().messages().list(
            userId="me",
            q=q,
            labelIds=label_ids or None,
            maxResults=max(1, min(max_results, 100)),
        )
        listing = request.execute()
        messages = listing.get("messages", [])
        if not messages:
            raise ValueError(f"No Gmail messages matched query: {q}")

        results: list[dict[str, Any]] = []
        for entry in messages:
            msg = service.users().messages().get(userId="me", id=entry["id"], format="full").execute()
            results.append(self._normalize_message(msg))

        return results

    def _build_service(self):
        credentials = self._load_credentials()
        delegated = credentials.with_subject(self.delegated_user)
        return build("gmail", "v1", credentials=delegated, cache_discovery=False)

    def _load_credentials(self):
        if self.service_account_json:
            info = json.loads(self.service_account_json)
            return service_account.Credentials.from_service_account_info(info, scopes=GMAIL_SCOPES)

        if self.service_account_file:
            return service_account.Credentials.from_service_account_file(self.service_account_file, scopes=GMAIL_SCOPES)

        raise ValueError("No service account credential source configured")

    def _normalize_message(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = message.get("payload", {})
        headers = payload.get("headers", [])

        sender = self._header_value(headers, "From")
        subject = self._header_value(headers, "Subject")
        body = self._extract_body(payload)

        if not body:
            body = message.get("snippet", "")

        return {
            "gmail_message_id": message.get("id"),
            "gmail_thread_id": message.get("threadId"),
            "sender": sender,
            "subject": subject,
            "body": body,
            "snippet": message.get("snippet", ""),
            "internal_date_ms": message.get("internalDate"),
        }

    def _header_value(self, headers: list[dict[str, Any]], name: str) -> str:
        lowered = name.lower()
        for header in headers:
            if header.get("name", "").lower() == lowered:
                return header.get("value", "")
        return ""

    def _extract_body(self, payload: dict[str, Any]) -> str:
        direct = payload.get("body", {}).get("data")
        if direct and payload.get("mimeType") == "text/plain":
            return self._decode_base64_urlsafe(direct)

        parts = payload.get("parts", [])
        for part in parts:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            if mime == "text/plain" and data:
                return self._decode_base64_urlsafe(data)

            nested_parts = part.get("parts", [])
            if nested_parts:
                nested_text = self._extract_body(part)
                if nested_text:
                    return nested_text

        fallback = payload.get("body", {}).get("data")
        if fallback:
            return self._decode_base64_urlsafe(fallback)

        return ""

    def _decode_base64_urlsafe(self, value: str) -> str:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
        return decoded.decode("utf-8", errors="replace")
