"""Decodo scraper API client.

Used by the Corrections Wizard's article-locator cascade to:
- run Google searches (target: google_search) when we need to discover the
  hoodline.com article from a fuzzy reference,
- fetch arbitrary pages (target: universal) so we can extract the
  authoritative <title> and meta fields from a hoodline.com article when
  the inbound email only gave us a link.

Configure via environment:
- DECODO_BASIC_AUTH_TOKEN   (preferred; exactly the token that goes in
  "Authorization: Basic <token>")
- DECODO_USERNAME + DECODO_PASSWORD  (alternate — we base64 them for you)
- DECODO_BASE_URL          (default https://scraper-api.decodo.com/v2)
- DECODO_TIMEOUT_SECONDS   (default 60)
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("hoodline.decodo")

DEFAULT_BASE_URL = "https://scraper-api.decodo.com/v2"


class DecodoClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("DECODO_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(os.getenv("DECODO_TIMEOUT_SECONDS", "60"))
        self._auth_token = self._build_auth_token()

    def _build_auth_token(self) -> str:
        explicit = os.getenv("DECODO_BASIC_AUTH_TOKEN", "").strip()
        if explicit:
            return explicit

        user = os.getenv("DECODO_USERNAME", "").strip()
        pwd = os.getenv("DECODO_PASSWORD", "").strip()
        if user and pwd:
            raw = f"{user}:{pwd}".encode("utf-8")
            return base64.b64encode(raw).decode("ascii")

        return ""

    def is_configured(self) -> bool:
        return bool(self._auth_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Basic {self._auth_token}",
            "Content-Type": "application/json",
        }

    def _post_scrape(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured():
            raise ValueError("Decodo is not configured (DECODO_BASIC_AUTH_TOKEN missing)")

        url = f"{self.base_url}/scrape"
        response = requests.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise ValueError(
                f"Decodo API error {response.status_code}: {response.text[:400]}"
            )
        return response.json()

    def _first_result_content(self, data: dict[str, Any]) -> Any:
        results = data.get("results") or []
        if not results:
            return None
        return results[0].get("content")

    def search_google(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Run a Google search via Decodo.

        Returns a list of {title, url, snippet} dicts (best effort).
        Tries the structured google_search target first, then falls back
        to scraping the raw Google results page.
        """
        query = (query or "").strip()
        if not query:
            return []

        # Structured SERP first
        try:
            data = self._post_scrape(
                {
                    "target": "google_search",
                    "query": query,
                    "parse": True,
                    "page_count": 1,
                }
            )
            content = self._first_result_content(data)
            parsed = _parse_structured_google(content)
            if parsed:
                return parsed[:max_results]
        except Exception as exc:
            logger.warning("Decodo google_search failed, will fall back: %s", exc)

        # Fallback: ask Decodo to fetch the Google results HTML and parse links
        try:
            data = self._post_scrape(
                {
                    "target": "google",
                    "url": f"https://www.google.com/search?q={requests.utils.quote(query)}",
                    "headless": "html",
                    "page_count": 1,
                }
            )
            html = self._first_result_content(data)
            if isinstance(html, str) and html:
                return _parse_google_html(html)[:max_results]
        except Exception as exc:
            logger.warning("Decodo google html fallback failed: %s", exc)

        return []

    def scrape_page(self, url: str) -> dict[str, Any]:
        """Scrape a page via Decodo and extract title + meta fields."""
        url = (url or "").strip()
        if not url:
            raise ValueError("url is required")

        data = self._post_scrape(
            {
                "target": "universal",
                "url": url,
                "headless": "html",
                "page_count": 1,
            }
        )
        html = self._first_result_content(data)
        if not isinstance(html, str) or not html:
            return {"title": "", "meta_title": "", "meta_description": "", "html": ""}

        return _extract_page_fields(html, url)


def _parse_structured_google(content: Any) -> list[dict[str, Any]]:
    """Pull organic results out of Decodo's structured google_search content."""
    if not isinstance(content, dict):
        return []

    # Common shapes Decodo has used historically.
    candidates: list[dict[str, Any]] = []
    for path in (
        ("results", "organic"),
        ("organic",),
        ("results",),
    ):
        node: Any = content
        ok = True
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and isinstance(node, list):
            candidates = node
            break

    parsed: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link") or ""
        title = item.get("title") or ""
        snippet = item.get("desc") or item.get("description") or item.get("snippet") or ""
        if url:
            parsed.append({"url": url, "title": title, "snippet": snippet})
    return parsed


def _parse_google_html(html: str) -> list[dict[str, Any]]:
    """Fallback parser for raw Google results HTML."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if not href.startswith("http"):
            continue
        if "google.com" in href:
            continue
        if href in seen:
            continue
        seen.add(href)

        h3 = anchor.find("h3")
        title = h3.get_text(" ", strip=True) if h3 else anchor.get_text(" ", strip=True)
        if not title:
            continue

        results.append({"url": href, "title": title, "snippet": ""})

        if len(results) >= 20:
            break

    return results


def _extract_page_fields(html: str, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.find("title")
    page_title = title_el.get_text(strip=True) if title_el else ""
    page_title_stripped = _strip_site_suffix(page_title)

    og_title_el = soup.find("meta", attrs={"property": "og:title"})
    og_title = og_title_el.get("content", "").strip() if og_title_el else ""

    h1_el = soup.find("h1")
    h1_text = h1_el.get_text(" ", strip=True) if h1_el else ""

    meta_name_title_el = soup.find("meta", attrs={"name": "title"})
    meta_title = meta_name_title_el.get("content", "").strip() if meta_name_title_el else ""
    if not meta_title:
        meta_title = og_title or page_title_stripped

    meta_desc_el = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    meta_description = meta_desc_el.get("content", "").strip() if meta_desc_el else ""

    # Some sites (Hoodline included) have an og:title that lags behind
    # edits to the article. The h1 is typically the live headline, so
    # prefer h1 > stripped <title> > og:title, and expose all three as
    # candidates so the locator can try each one.
    candidates_ordered = [h1_text, page_title_stripped, og_title]
    dedup_candidates: list[str] = []
    seen: set[str] = set()
    for cand in candidates_ordered:
        c = (cand or "").strip()
        if not c:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        dedup_candidates.append(c)

    article_title = dedup_candidates[0] if dedup_candidates else ""

    return {
        "title": article_title,
        "title_candidates": dedup_candidates,
        "meta_title": meta_title,
        "meta_description": meta_description,
        "og_title": og_title,
        "h1": h1_text,
        "page_title": page_title,
        "source_url": source_url,
        "html_length": len(html),
    }


def _strip_site_suffix(title: str) -> str:
    for sep in (" | ", " — ", " - ", " • "):
        if sep in title:
            head, _, _tail = title.partition(sep)
            return head.strip()
    return title.strip()
