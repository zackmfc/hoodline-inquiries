"""Hoodline CMS API client.

Implements the contract described in /hoodline-cms-api/API_ENDPOINT_GUIDE.md:

  POST /api/auth/login        — email/password → short-lived JWT + exp
  POST /api/articles          — create + submit an article on the default
                                website (server picks the site via
                                API_DEFAULT_WEBSITE_ID)
  PATCH /api/articles/:id     — update an existing article using the
                                internal `assignment` envelope
  POST /api/images            — stage a cover image and get back an
                                image_guid usable on POST /api/articles
  GET  /api/articles[/:id]    — list/show articles visible to the API
  GET  /api/users[/:id]       — lookup users (writer/editor IDs)
  GET  /api/tags[/:id]        — lookup tags (use category_type=primary)
  GET  /api/metro_areas[/:id] — lookup metro areas
  GET  /api/websites          — lookup websites

Auth flow:
  - Caller supplies CMS_API_EMAIL / CMS_API_PASSWORD; the client logs in
    on demand, caches the JWT until ~2 minutes before it expires, and
    auto-retries once on 401.
  - For backwards compat, CMS_API_KEY is honored as a long-lived bearer
    token that bypasses login. Used by ops/admin tooling that does not
    have a real API account.

"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import requests

logger = logging.getLogger("hoodline.cms_client")


WIZARD_FIELD_TO_ASSIGNMENT_KEY = {
    # wizard field name           # PATCH assignment key
    "title": "title",
    "meta_title": "meta_title",
    "meta_description": "meta_description",
    "excerpt": "excerpt",
    "social_media_excerpt": "social_media_excerpt",
    "article_body": "text",  # PATCH uses "text", not "body"
    "featured_image_attribution": "featured_image_attribution",
}


class CMSAPIError(RuntimeError):
    """Raised when the CMS API returns an error response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class CMSAuthError(CMSAPIError):
    """Raised when login fails or no usable credentials are configured."""


class CMSClient:
    """Thin client for the Hoodline CMS API."""

    def __init__(self) -> None:
        self.api_base_url = os.getenv(
            "CMS_API_BASE_URL",
            os.getenv("CMS_BASE_URL", "https://hoodline.impress3.com"),
        ).rstrip("/")
        self.email = os.getenv("CMS_API_EMAIL", "").strip()
        self.password = os.getenv("CMS_API_PASSWORD", "").strip()
        self.timeout = float(os.getenv("CMS_API_TIMEOUT_SECONDS", "30"))
        self.user_agent = os.getenv(
            "CMS_API_USER_AGENT",
            "HoodlineCorrectionsBot/1.0",
        )

        # Optional long-lived bearer token used as a fallback when
        # email/password aren't configured. Mostly here so existing
        # deployments keep working.
        self._static_token: str | None = os.getenv("CMS_API_KEY", "").strip() or None

        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._lock = Lock()

    # ── configuration / health ──────────────────────────────────────

    def is_configured(self) -> bool:
        if not self.api_base_url:
            return False
        if self._static_token:
            return True
        return bool(self.email) and bool(self.password)

    def configuration_summary(self) -> dict[str, Any]:
        return {
            "api_base_url": self.api_base_url,
            "auth_mode": (
                "static_token"
                if self._static_token
                else ("login" if self.email and self.password else "unconfigured")
            ),
            "email": self.email or None,
        }

    # ── auth ────────────────────────────────────────────────────────

    def login(self, *, timeout: float | None = None) -> str:
        """Force a fresh login, returning the new JWT."""
        if self._static_token:
            return self._static_token

        if not (self.email and self.password):
            raise CMSAuthError(
                "CMS_API_EMAIL and CMS_API_PASSWORD must be set to log in"
            )

        url = f"{self.api_base_url}/api/auth/login"
        try:
            response = requests.post(
                url,
                json={
                    "credentials": {
                        "email": self.email,
                        "password": self.password,
                    }
                },
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": self.user_agent,
                },
                timeout=timeout if timeout is not None else self.timeout,
            )
        except requests.RequestException as exc:
            raise CMSAuthError(f"Login request failed: {exc}") from exc

        if response.status_code >= 400:
            raise CMSAuthError(
                f"Login failed: {response.status_code} {response.text[:300]}",
                status_code=response.status_code,
                payload=_safe_json(response),
            )

        data = _safe_json(response) or {}
        token = str(data.get("token") or "").strip()
        if not token:
            raise CMSAuthError(
                "Login response did not contain a token", payload=data
            )

        self._token = token
        self._token_expires_at = _parse_expiry(data.get("exp"))
        return token

    def _ensure_token(self, *, timeout: float | None = None) -> str:
        if self._static_token:
            return self._static_token

        with self._lock:
            now = datetime.now(timezone.utc)
            if (
                self._token
                and self._token_expires_at
                and now < self._token_expires_at - timedelta(minutes=2)
            ):
                return self._token
            return self.login(timeout=timeout)

    # ── HTTP plumbing ───────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        retry_on_401: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any] | list[Any]:
        if not self.api_base_url:
            raise CMSAPIError("CMS_API_BASE_URL is not configured")

        url = f"{self.api_base_url}{path}"
        token = self._ensure_token(timeout=timeout)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=timeout if timeout is not None else self.timeout,
            )
        except requests.RequestException as exc:
            raise CMSAPIError(f"{method} {path} request failed: {exc}") from exc

        # One-shot retry: token rejected → log in fresh and try again.
        if response.status_code == 401 and retry_on_401 and not self._static_token:
            with self._lock:
                self._token = None
                self._token_expires_at = None
            return self._request(
                method,
                path,
                json_body=json_body,
                params=params,
                retry_on_401=False,
                timeout=timeout,
            )

        if response.status_code >= 400:
            payload = _safe_json(response)
            raise CMSAPIError(
                _format_error(method, path, response, payload),
                status_code=response.status_code,
                payload=payload,
            )

        return _safe_json(response) or {}

    # ── articles ────────────────────────────────────────────────────

    def get_article(self, article_id: int, *, timeout: float | None = None) -> dict[str, Any]:
        result = self._request("GET", f"/api/articles/{article_id}", timeout=timeout)
        if not isinstance(result, dict):
            return {}
        article = result.get("article")
        return article if isinstance(article, dict) else result

    def list_articles(
        self,
        *,
        article_id: int | None = None,
        website_id: int | None = None,
        remote_id: str | None = None,
        article_slug: str | None = None,
        status: str | None = None,
        workflow: str | None = None,
        page_type: str | None = "article",
        title: str | None = None,
        q: str | None = None,
        page: int | None = 1,
        per_page: int | None = 10,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if article_id is not None:
            params["id"] = article_id
        if website_id is not None:
            params["website_id"] = website_id
        if remote_id:
            params["remote_id"] = remote_id
        if article_slug:
            params["article_slug"] = article_slug
        if status:
            params["status"] = status
        if workflow:
            params["workflow"] = workflow
        if page_type:
            params["page_type"] = page_type
        if title:
            params["title"] = title
        if q:
            params["q"] = q
        if page is not None:
            params["page"] = max(1, int(page))
        if per_page is not None:
            params["per_page"] = max(1, min(int(per_page), 100))

        result = self._request("GET", "/api/articles", params=params or None, timeout=timeout)
        if isinstance(result, dict):
            articles = result.get("articles")
            pagination = result.get("pagination")
            return {
                "articles": articles if isinstance(articles, list) else [],
                "pagination": pagination if isinstance(pagination, dict) else {},
                "raw": result,
            }
        if isinstance(result, list):
            return {"articles": result, "pagination": {}, "raw": result}
        return {"articles": [], "pagination": {}, "raw": result}

    def create_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/api/articles", json_body=payload)
        return result if isinstance(result, dict) else {}

    def update_article(
        self, article_id: int, assignment: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /api/articles/:id with the assignment envelope."""
        clean = {k: v for k, v in assignment.items() if v is not None}
        if not clean:
            raise ValueError("assignment must contain at least one field to update")
        result = self._request(
            "PATCH",
            f"/api/articles/{article_id}",
            json_body={"assignment": clean},
        )
        return result if isinstance(result, dict) else {}

    def update_article_from_wizard_fields(
        self, article_id: int, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """Translate wizard-named fields → assignment envelope and PATCH.

        Accepts keys from WIZARD_FIELD_TO_ASSIGNMENT_KEY. Unknown keys are
        ignored. Empty strings are kept as intentional clears; only None is
        dropped.
        """
        assignment: dict[str, Any] = {}
        for wizard_key, value in fields.items():
            api_key = WIZARD_FIELD_TO_ASSIGNMENT_KEY.get(wizard_key)
            if not api_key:
                continue
            if value is None:
                continue
            assignment[api_key] = value
        if not assignment:
            raise ValueError("No recognized wizard fields supplied")
        return self.update_article(article_id, assignment)

    def fetch_article_fields_for_wizard(self, article_id: int) -> dict[str, Any]:
        """Best-effort GET that maps to the /corrections wizard step-3 form.

        Returns the wizard-form shape so the JS can drop straight into the
        existing inputs. The `available` flag tells the UI whether to show
        a "fields prefilled from CMS" affordance vs. ask the user to paste
        manually.
        """
        try:
            data = self.get_article(article_id)
        except CMSAPIError as exc:
            return {
                "available": False,
                "error": str(exc),
                "status_code": exc.status_code,
            }

        if not isinstance(data, dict) or not data:
            return {"available": False, "error": "empty_response"}

        # Normalize whichever field names the GET ends up using. The create
        # contract uses `meta` for SEO description while the PATCH envelope
        # uses `meta_description` — accept either on read.
        article_body = (
            data.get("body") or data.get("text") or data.get("article_body") or ""
        )
        image_url = (
            data.get("cover_image_url")
            or data.get("image_url")
            or data.get("featured_image_url")
            or ""
        )

        fields = {
            "title": str(data.get("title") or ""),
            "meta_title": str(data.get("meta_title") or ""),
            "meta_description": str(
                data.get("meta_description") or data.get("meta") or ""
            ),
            "excerpt": str(data.get("excerpt") or ""),
            "social_media_excerpt": str(data.get("social_media_excerpt") or ""),
            "article_body": str(article_body or ""),
            "featured_image_attribution": str(
                data.get("featured_image_attribution") or ""
            ),
            "image_url": str(image_url or ""),
        }

        # If every field is empty the GET responded with a useless shape
        # (the documented current caveat); surface that to the UI.
        non_empty = any(v.strip() for v in fields.values())
        return {
            "available": non_empty,
            "fields": fields,
            "raw": data,
        }

    # ── images ──────────────────────────────────────────────────────

    def stage_image(self, image_url: str) -> dict[str, Any]:
        result = self._request(
            "POST", "/api/images", json_body={"image_url": image_url}
        )
        return result if isinstance(result, dict) else {}

    # ── lookups ─────────────────────────────────────────────────────

    def list_users(self, *, q: str | None = None) -> list[dict[str, Any]]:
        params = {"q": q} if q else None
        return _as_list(self._request("GET", "/api/users", params=params))

    def get_user(self, user_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/api/users/{user_id}")
        return result if isinstance(result, dict) else {}

    def list_tags(self) -> list[dict[str, Any]]:
        return _as_list(self._request("GET", "/api/tags"))

    def get_tag(self, tag_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/api/tags/{tag_id}")
        return result if isinstance(result, dict) else {}

    def list_metro_areas(
        self,
        *,
        parent_id: int | None = None,
        ai: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if parent_id is not None:
            params["parent_id"] = parent_id
        if ai is not None:
            params["ai"] = "true" if ai else "false"
        return _as_list(self._request("GET", "/api/metro_areas", params=params or None))

    def get_metro_area(self, metro_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/api/metro_areas/{metro_id}")
        return result if isinstance(result, dict) else {}

    def list_websites(self, *, name: str | None = None) -> list[dict[str, Any]]:
        params = {"name": name} if name else None
        return _as_list(self._request("GET", "/api/websites", params=params))


# ── helpers ────────────────────────────────────────────────────────


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("data", "items", "results"):
            inner = value.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    return []


def _format_error(
    method: str,
    path: str,
    response: requests.Response,
    payload: Any,
) -> str:
    base = f"CMS API {method} {path} returned {response.status_code}"
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return f"{base}: {value.strip()}"
        if payload:
            return f"{base}: {payload}"
    snippet = (response.text or "").strip()[:240]
    return f"{base}: {snippet}" if snippet else base


def _parse_expiry(raw: Any) -> datetime | None:
    """Parse the API's `exp` field — documented as 'MM-DD-YYYY HH:MM'.

    Falls back to None on unrecognized formats; callers treat that as
    "expires unknown" and force a re-login on the next 401.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    formats = (
        "%m-%d-%Y %H:%M",
        "%m-%d-%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue
    logger.warning("Unrecognized CMS token expiry format: %r", raw)
    return None
