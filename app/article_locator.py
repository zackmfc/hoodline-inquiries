"""Article locator cascade.

Given an inbound correction email (subject + body), try to locate the
editorial Discord message that points at the CMS edit URL for the
Hoodline article the sender is referencing.

Cascade (strict order — stop as soon as Discord returns a hit):

1. Email title (from Claude's assess step, quoted by the sender):
     - Discord search on the first 5 words of the title.
     - If that misses, Discord search on the first 4 words.

2. Email URL (any hoodline.com link in the email body):
     - Decodo-scrape the URL.
     - Discord search on first 5 words of the <h1>, then 4 words.
     - Discord search on first 5 words of the <title> tag, then 4 words.

3. Claude-generated Google query (only if 1 and 2 both miss):
     - Ask Claude to propose a short search query from the email.
     - Append " hoodline" and run it through Decodo Google search.
     - For the first TWO hoodline.com results returned, scrape each page
       and run Discord search on <h1> (5w→4w) then <title> (5w→4w).
     - ANY hit found at this stage is flagged with google_search_warning
       so the operator knows it may not be reliable.

4. Otherwise: flag needs_human with the full trace.

The returned payload always identifies HOW we found the match via
match_source / match_source_label / match_word_count.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.corrections import generate_search_query
from app.decodo_client import DecodoClient
from app.storage import Storage

logger = logging.getLogger("hoodline.article_locator")

CMS_EDIT_URL_PATTERN = re.compile(
    r"https?://hoodline\.impress3\.com/articles/(\d+)/edit"
)

HOODLINE_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?hoodline\.com/[^\s)\"'>]+"
)

# Characters that frequently break Discord ILIKE search when used literally
# in a title (colon, semicolon, em dash, en dash, hyphen).
_TITLE_BREAK_RE = re.compile(r"[:;\-—–]")

# Source labels (match_source) → human-readable strings shown in the UI.
_SOURCE_LABELS: dict[str, str] = {
    "email_title": "sender-quoted title from the email",
    "email_url_h1": "<h1> of the hoodline.com URL in the email",
    "email_url_page_title": "<title> of the hoodline.com URL in the email",
    "google_1_h1": "<h1> of the #1 Google result",
    "google_1_page_title": "<title> of the #1 Google result",
    "google_2_h1": "<h1> of the #2 Google result",
    "google_2_page_title": "<title> of the #2 Google result",
}

_GOOGLE_SOURCES = {
    "google_1_h1",
    "google_1_page_title",
    "google_2_h1",
    "google_2_page_title",
}


def _strip_site_suffix(title: str) -> str:
    """Strip common " | Site Name" / " — Site Name" suffixes from a page title."""
    for sep in (" | ", " — ", " - ", " • "):
        if sep in title:
            head, _, _tail = title.partition(sep)
            return head.strip()
    return title.strip()


@dataclass
class TraceStep:
    step: str
    action: str
    detail: str = ""
    matched: bool = False
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "detail": self.detail,
            "matched": self.matched,
            "data": self.data,
        }


class ArticleLocator:
    def __init__(self, *, storage: Storage, decodo: DecodoClient | None = None) -> None:
        self.storage = storage
        self.decodo = decodo or DecodoClient()

    def locate(
        self,
        *,
        email_subject: str,
        email_body: str,
        hinted_url: str | None = None,
        hinted_title: str | None = None,
    ) -> dict[str, Any]:
        """Run the cascade and return a structured result.

        See module docstring for the cascade definition.
        """
        trace: list[TraceStep] = []
        combined = f"{email_subject or ''}\n{email_body or ''}"

        # Only trust hoodline.com / hoodline.impress3.com URLs for the
        # email-URL branch. A hinted URL from Claude's assessment may point
        # at a source the email is linking to (NYPost, PR wire, etc.) —
        # not the Hoodline article we want.
        candidate_url = (hinted_url or "").strip() or self._extract_hoodline_url(combined)
        if candidate_url and not self._is_hoodline_url(candidate_url):
            trace.append(TraceStep(
                step="extract",
                action="reject_non_hoodline_url",
                detail=f"Ignoring non-hoodline URL hint: {candidate_url[:200]}",
                data={"url": candidate_url},
            ))
            candidate_url = self._extract_hoodline_url(combined)
            if candidate_url and not self._is_hoodline_url(candidate_url):
                candidate_url = ""

        email_url = candidate_url
        email_title = (hinted_title or "").strip()

        trace.append(TraceStep(
            step="extract",
            action="parse_email",
            detail=(
                f"hoodline_url={'yes' if email_url else 'no'}; "
                f"title_mention={'yes' if email_title else 'no'}"
            ),
            data={"url": email_url, "title": email_title},
        ))

        # De-dup across all Discord searches: keyed on (lowered words joined, n).
        tried: set[tuple[str, int]] = set()

        # ── Step 1: Email title (5w → 4w) ─────────────────────────────
        if email_title:
            hit = self._try_title_at_sizes(
                email_title,
                source="email_title",
                trace=trace,
                tried=tried,
            )
            if hit:
                return self._resolved(hit, trace, article_url=email_url)

        # ── Step 2: Scrape email URL → h1 (5w→4w), page_title (5w→4w) ──
        final_article_url = ""
        if email_url:
            page = self._scrape_page(email_url, trace, label="email_url")
            final_article_url = email_url

            hit = self._try_page_titles(
                page,
                source_prefix="email_url",
                trace=trace,
                tried=tried,
            )
            if hit:
                return self._resolved(hit, trace, article_url=email_url)

        # ── Step 3: Claude-generated Google fallback ─────────────────
        query = self._build_claude_query(email_subject, email_body, trace)
        google_hoodline_urls: list[str] = []
        if query:
            google_hoodline_urls = self._google_hoodline_urls(
                query=query,
                limit=2,
                exclude_url=email_url or "",
                trace=trace,
            )

        for idx, url in enumerate(google_hoodline_urls, start=1):
            label = f"google_{idx}"
            page = self._scrape_page(url, trace, label=label)
            hit = self._try_page_titles(
                page,
                source_prefix=label,
                trace=trace,
                tried=tried,
            )
            if hit:
                if not final_article_url:
                    final_article_url = url
                return self._resolved(
                    hit,
                    trace,
                    article_url=url,
                    google_query=query,
                )

        # ── Step 4: Flag needs_human ─────────────────────────────────
        trace.append(TraceStep(
            step="flag",
            action="exhausted_cascade",
            detail="No Discord message matched any of the attempted titles.",
            data={"article_url": final_article_url},
        ))
        return self._not_found(trace, article_url=final_article_url)

    # ───────────────────────────────────────────────────────── internals ──

    def _extract_hoodline_url(self, text: str) -> str:
        match = HOODLINE_URL_PATTERN.search(text or "")
        if not match:
            return ""
        return match.group(0).rstrip(".,)\"'>")

    def _is_hoodline_url(self, url: str) -> bool:
        if not url:
            return False
        lowered = url.lower()
        return (
            "hoodline.com/" in lowered
            or "hoodline.impress3.com/" in lowered
        )

    def _words_of(self, segment: str) -> list[str]:
        cleaned = re.sub(r"[^\w\s]", " ", segment or "").strip()
        return [w for w in cleaned.split() if w]

    def _first_n_words(self, text: str, n: int) -> list[str]:
        """Pick the first n search terms for Discord cache lookup.

        Discord-cache titles are stored verbatim, so search terms that
        include punctuation tend to miss. If the title contains a break
        character (:, ;, -, —, –) and there are at least 3 words before
        it, we use the words before the break; otherwise, we use the
        words after the break.
        """
        if not text or n <= 0:
            return []

        match = _TITLE_BREAK_RE.search(text)
        if match is None:
            return self._words_of(text)[:n]

        before_words = self._words_of(text[:match.start()])
        if len(before_words) >= 3:
            return before_words[:n]

        return self._words_of(text[match.end():])[:n]

    def _lookup_discord(
        self,
        words: list[str],
        trace: list[TraceStep],
        *,
        label: str,
    ) -> dict[str, Any] | None:
        if not words:
            trace.append(TraceStep(
                step="discord_search",
                action=f"skip_{label}",
                detail="No usable search words.",
            ))
            return None

        kept = [w for w in words if len(w) >= 2]
        if not kept:
            trace.append(TraceStep(
                step="discord_search",
                action=f"skip_{label}",
                detail="All candidate words were too short.",
                data={"words": words},
            ))
            return None

        posts = self.storage.search_editorial_posts(terms=kept, limit=25)
        if not posts:
            trace.append(TraceStep(
                step="discord_search",
                action=f"miss_{label}",
                detail=f"No cached post matched {' '.join(kept)!r}.",
                data={"words": kept},
            ))
            return None

        # Rank posts that have a cms_edit_url above those that don't,
        # then by earliest appearance of the longest anchor word in the
        # title.
        anchor = max(kept, key=len).lower()

        def sort_key(post: dict[str, Any]) -> tuple[int, int]:
            has_edit_url = 0 if post.get("cms_edit_url") else 1
            title = str(post.get("title") or "").lower()
            position = title.find(anchor)
            return (has_edit_url, position if position >= 0 else 9999)

        best = sorted(posts, key=sort_key)[0]
        trace.append(TraceStep(
            step="discord_search",
            action=f"hit_{label}",
            detail=f"Matched '{str(best.get('title') or '')[:120]}'.",
            matched=True,
            data={
                "words": kept,
                "post_id": best.get("id"),
                "post_title": best.get("title"),
                "cms_edit_url": best.get("cms_edit_url"),
                "article_url": best.get("article_url"),
                "channel": best.get("channel"),
                "message_id": best.get("message_id"),
            },
        ))
        return best

    def _try_title_at_sizes(
        self,
        title: str,
        *,
        source: str,
        trace: list[TraceStep],
        tried: set[tuple[str, int]],
    ) -> dict[str, Any] | None:
        """Discord search at n=5 then n=4 for a single title candidate.

        Records each attempt (including skips/misses) in the trace.
        Returns a hit dict {post, title, n, words, source} or None.
        """
        title = (title or "").strip()
        if not title:
            return None

        for n in (5, 4):
            words = self._first_n_words(title, n)
            if not words:
                continue
            key = (" ".join(w.lower() for w in words), n)
            if key in tried:
                trace.append(TraceStep(
                    step="discord_search",
                    action=f"skip_{source}_{n}w",
                    detail="Already tried these exact words — skipping duplicate lookup.",
                    data={"words": words},
                ))
                continue
            tried.add(key)

            post = self._lookup_discord(
                words,
                trace,
                label=f"{source}_{n}w",
            )
            if post:
                return {
                    "post": post,
                    "title": title,
                    "n": n,
                    "words": words,
                    "source": source,
                }

        return None

    def _try_page_titles(
        self,
        page: dict[str, Any],
        *,
        source_prefix: str,
        trace: list[TraceStep],
        tried: set[tuple[str, int]],
    ) -> dict[str, Any] | None:
        """For a scraped page, try h1 (5w→4w) then <title> (5w→4w)."""
        if not page:
            return None

        h1_text = (page.get("h1") or "").strip()
        # <title> tends to carry a " | Hoodline" suffix — strip common
        # site-name separators before searching Discord.
        page_title_stripped = _strip_site_suffix(
            (page.get("page_title") or "").strip()
        )

        for title_kind, title in (
            ("h1", h1_text),
            ("page_title", page_title_stripped),
        ):
            hit = self._try_title_at_sizes(
                title,
                source=f"{source_prefix}_{title_kind}",
                trace=trace,
                tried=tried,
            )
            if hit:
                return hit

        return None

    def _scrape_page(
        self,
        url: str,
        trace: list[TraceStep],
        *,
        label: str = "",
    ) -> dict[str, Any]:
        suffix = f"_{label}" if label else ""

        if not self.decodo.is_configured():
            trace.append(TraceStep(
                step="page_scrape",
                action=f"not_configured{suffix}",
                detail="Decodo is not configured; cannot scrape for page title.",
                data={"url": url},
            ))
            return {}

        try:
            page = self.decodo.scrape_page(url)
        except Exception as exc:
            trace.append(TraceStep(
                step="page_scrape",
                action=f"error{suffix}",
                detail=str(exc)[:200],
                data={"url": url},
            ))
            return {}

        trace.append(TraceStep(
            step="page_scrape",
            action=f"ok{suffix}",
            detail=f"h1={page.get('h1', '')[:120]!r} title={page.get('page_title', '')[:120]!r}",
            data={
                "url": url,
                "h1": page.get("h1", ""),
                "page_title": page.get("page_title", ""),
                "title_candidates": page.get("title_candidates", []),
            },
        ))
        return page

    def _build_claude_query(
        self,
        subject: str,
        body: str,
        trace: list[TraceStep],
    ) -> str:
        try:
            seed = generate_search_query(subject=subject or "", body=body or "")
        except Exception as exc:
            trace.append(TraceStep(
                step="claude_query",
                action="error",
                detail=str(exc)[:200],
            ))
            return ""

        if not seed:
            trace.append(TraceStep(
                step="claude_query",
                action="empty",
                detail="Claude returned no query seed for the email.",
            ))
            return ""

        query = f"{seed} hoodline"
        trace.append(TraceStep(
            step="claude_query",
            action="ok",
            detail=f"seed={seed!r}",
            data={"seed": seed, "query": query},
        ))
        return query

    def _google_hoodline_urls(
        self,
        *,
        query: str,
        limit: int,
        exclude_url: str,
        trace: list[TraceStep],
    ) -> list[str]:
        if not self.decodo.is_configured():
            trace.append(TraceStep(
                step="google_search",
                action="not_configured",
                detail="Decodo is not configured; cannot run Google search.",
                data={"query": query},
            ))
            return []

        try:
            results = self.decodo.search_google(query)
        except Exception as exc:
            trace.append(TraceStep(
                step="google_search",
                action="error",
                detail=str(exc)[:200],
                data={"query": query},
            ))
            return []

        keep: list[str] = []
        exclude_clean = exclude_url.rstrip(".,)\"'>") if exclude_url else ""
        for item in results:
            url = str(item.get("url") or "").strip().rstrip(".,)\"'>")
            if not url:
                continue
            lowered = url.lower()
            # Only take article-level hoodline.com URLs.
            if "hoodline.com/" not in lowered:
                continue
            if (
                "hoodline.com/tag" in lowered
                or "hoodline.com/news" in lowered
                or lowered.rstrip("/").endswith("hoodline.com")
            ):
                continue
            if url == exclude_clean:
                continue
            if url in keep:
                continue
            keep.append(url)
            if len(keep) >= limit:
                break

        if keep:
            trace.append(TraceStep(
                step="google_search",
                action="ok",
                detail=f"Kept {len(keep)} hoodline.com result(s) for further scraping.",
                matched=False,
                data={"query": query, "urls": keep},
            ))
        else:
            trace.append(TraceStep(
                step="google_search",
                action="miss",
                detail=f"Ran query {query!r} via Decodo but no article-level hoodline.com result.",
                data={"query": query, "result_count": len(results)},
            ))
        return keep

    def _resolved(
        self,
        hit: dict[str, Any],
        trace: list[TraceStep],
        *,
        article_url: str = "",
        google_query: str = "",
    ) -> dict[str, Any]:
        post: dict[str, Any] = hit["post"]
        source: str = hit["source"]
        words: list[str] = hit["words"]
        n: int = hit["n"]
        title: str = hit["title"]

        cms_edit_url = str(post.get("cms_edit_url") or "").strip()
        article_id: int | None = None
        if cms_edit_url:
            m = CMS_EDIT_URL_PATTERN.search(cms_edit_url)
            if m:
                try:
                    article_id = int(m.group(1))
                except ValueError:
                    article_id = None

        match_source_label = _SOURCE_LABELS.get(source, source)
        google_warning = source in _GOOGLE_SOURCES

        return {
            "found": True,
            "cms_edit_url": cms_edit_url or None,
            "article_id": article_id,
            "article_url": str(post.get("article_url") or article_url or "") or None,
            "matched_post": {
                "id": post.get("id"),
                "title": post.get("title"),
                "channel": post.get("channel"),
                "message_id": post.get("message_id"),
            },
            "authoritative_title": title,
            "match_source": source,
            "match_source_label": match_source_label,
            "match_word_count": n,
            "match_words": words,
            "google_search_warning": google_warning,
            "google_query": google_query if google_warning else "",
            "trace": [t.to_dict() for t in trace],
        }

    def _not_found(
        self,
        trace: list[TraceStep],
        *,
        article_url: str = "",
    ) -> dict[str, Any]:
        return {
            "found": False,
            "cms_edit_url": None,
            "article_id": None,
            "article_url": article_url or None,
            "matched_post": None,
            "authoritative_title": "",
            "match_source": "",
            "match_source_label": "",
            "match_word_count": 0,
            "match_words": [],
            "google_search_warning": False,
            "google_query": "",
            "trace": [t.to_dict() for t in trace],
        }
