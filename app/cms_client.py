"""Hoodline CMS API client.

Reads and writes article data via the Hoodline CMS API.
The CMS API endpoint is external to this application and must be
configured via CMS_API_BASE_URL.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger("hoodline.cms_client")

ARTICLE_FIELDS = [
    "title",
    "meta_title",
    "meta",
    "excerpt",
    "social_media_excerpt",
    "body",
    "primary_tag",
    "metros",
    "image_url",
    "featured_image_attribution",
    "article_slug",
    "editor",
    "writer",
]


class CMSClient:
    def __init__(self) -> None:
        self.api_base_url = os.getenv(
            "CMS_API_BASE_URL",
            os.getenv("CMS_BASE_URL", "https://hoodline.impress3.com"),
        ).rstrip("/")
        self.api_key = os.getenv("CMS_API_KEY", "").strip()
        self.timeout = float(os.getenv("CMS_API_TIMEOUT_SECONDS", "30"))

    @property
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "HoodlineCorrectionsBot/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def is_configured(self) -> bool:
        return bool(self.api_base_url)

    def read_article(self, article_id: int) -> dict[str, Any]:
        """Fetch article fields from the CMS API.

        Returns a dict with keys matching ARTICLE_FIELDS plus 'id'.
        """
        url = f"{self.api_base_url}/api/articles/{article_id}"
        resp = requests.get(url, headers=self._headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()

        article = {"id": article_id}
        for field in ARTICLE_FIELDS:
            article[field] = data.get(field)
        return article

    def write_article(self, article_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        """Write updated fields to an article via the CMS API.

        Only fields present in `updates` are sent. Fields not in
        ARTICLE_FIELDS are ignored.
        """
        payload = {k: v for k, v in updates.items() if k in ARTICLE_FIELDS}
        if not payload:
            raise ValueError("No valid article fields to update")

        url = f"{self.api_base_url}/api/articles/{article_id}"
        resp = requests.patch(url, headers=self._headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
