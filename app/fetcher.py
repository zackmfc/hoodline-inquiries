from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


class ArticleFetcher:
    def __init__(self) -> None:
        self.timeout_seconds = float(os.getenv("FETCHER_TIMEOUT_SECONDS", "30"))
        self.user_agent = os.getenv(
            "FETCHER_USER_AGENT",
            "HoodlineCorrectionsBot/1.0 (+https://hoodline.com)",
        )
        self.cms_session_cookie = os.getenv("CMS_FETCHER_SESSION_COOKIE", "")

    def fetch(
        self,
        *,
        article_url: str,
        article_edit_url: str | None,
    ) -> dict[str, Any]:
        normalized_article_url = self._normalize_url(article_url)
        if not normalized_article_url:
            raise ValueError("article_url must be a valid http(s) URL")

        headers = {"User-Agent": self.user_agent}
        response = requests.get(normalized_article_url, headers=headers, timeout=self.timeout_seconds)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        headline = self._extract_headline(soup)
        meta_description = self._extract_meta_description(soup)
        byline = self._extract_byline(soup)
        publish_date = self._extract_publish_date(soup)
        outbound_links = self._extract_outbound_links(soup, normalized_article_url)
        existing_notes = self._extract_existing_notes(soup)

        cms_fetch_status = "not_requested"
        cms_http_status = None
        if article_edit_url:
            cms_fetch_status, cms_http_status = self._probe_cms_edit_url(article_edit_url, headers)

        return {
            "article_url": normalized_article_url,
            "article_edit_url": article_edit_url,
            "headline": headline,
            "meta_description": meta_description,
            "byline": byline,
            "publish_date": publish_date,
            "outbound_links_count": len(outbound_links),
            "outbound_links": outbound_links[:30],
            "existing_notes": existing_notes,
            "snapshot_status": "captured",
            "public_http_status": response.status_code,
            "cms_fetch_status": cms_fetch_status,
            "cms_http_status": cms_http_status,
        }

    def _normalize_url(self, raw: str) -> str | None:
        text = (raw or "").strip()
        if not text:
            return None
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return text

    def _extract_headline(self, soup: BeautifulSoup) -> str:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title["content"].strip()

        h1 = soup.find("h1")
        if h1:
            return h1.get_text(" ", strip=True)

        if soup.title:
            return soup.title.get_text(" ", strip=True)

        return ""

    def _extract_meta_description(self, soup: BeautifulSoup) -> str:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            return meta["content"].strip()

        og_desc = soup.find("meta", property="og:description")
        if og_desc and og_desc.get("content"):
            return og_desc["content"].strip()

        return ""

    def _extract_byline(self, soup: BeautifulSoup) -> str:
        selectors = [
            "[rel='author']",
            "[itemprop='author']",
            ".byline",
            ".author",
            ".article-byline",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                value = node.get_text(" ", strip=True)
                if value:
                    return value
        return ""

    def _extract_publish_date(self, soup: BeautifulSoup) -> str:
        candidates = [
            ("meta", {"property": "article:published_time"}, "content"),
            ("meta", {"name": "pubdate"}, "content"),
            ("time", {"datetime": True}, "datetime"),
        ]
        for tag_name, attrs, field in candidates:
            node = soup.find(tag_name, attrs=attrs)
            if node and node.get(field):
                return str(node.get(field)).strip()

        time_node = soup.find("time")
        if time_node:
            text = time_node.get_text(" ", strip=True)
            if text:
                return text
        return ""

    def _extract_outbound_links(self, soup: BeautifulSoup, article_url: str) -> list[str]:
        parsed = urlparse(article_url)
        hostname = parsed.netloc.lower()
        links: list[str] = []
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if not href.startswith("http"):
                continue
            href_host = urlparse(href).netloc.lower()
            if not href_host or href_host.endswith(hostname):
                continue
            if href in seen:
                continue
            seen.add(href)
            links.append(href)
        return links

    def _extract_existing_notes(self, soup: BeautifulSoup) -> list[str]:
        notes: list[str] = []
        for tag in soup.find_all(["p", "div", "span"]):
            text = tag.get_text(" ", strip=True)
            if not text:
                continue
            lower = text.lower()
            if lower.startswith("editor's note") or lower.startswith("update ("):
                notes.append(text)
            elif re.search(r"\b(editor'?s note|updated?\s+\(|correction:)\b", lower):
                notes.append(text)
            if len(notes) >= 12:
                break
        return notes

    def _probe_cms_edit_url(self, url: str, headers: dict[str, str]) -> tuple[str, int | None]:
        try:
            cms_headers = dict(headers)
            if self.cms_session_cookie:
                cms_headers["Cookie"] = self.cms_session_cookie
            response = requests.get(url, headers=cms_headers, timeout=self.timeout_seconds, allow_redirects=True)
            if response.status_code in {401, 403}:
                return "auth_required", response.status_code
            if "wp-login.php" in response.url:
                return "auth_required", response.status_code
            return "fetched", response.status_code
        except requests.RequestException:
            return "request_failed", None
