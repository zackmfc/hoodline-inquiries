"""Article locator cascade.

Given an inbound correction email (subject + body), try to locate the
editorial Discord message that points at the CMS edit URL for the
Hoodline article the sender is referencing.

Cascade:

1. Extract what we can from the email:
     - any hoodline.com URL, via regex
     - any article-title mention, via Claude (see app.corrections)

2. If we have a candidate title: search the Discord editorial cache
   using the first 4 words of the title.

3. If that misses (or no title was available):
     - make sure we have a hoodline.com URL by searching Google via Decodo
       for `<title or generated keyword> hoodline` and finding the first
       hoodline.com link,
     - scrape that URL to get the authoritative <title> and meta title.

4. Retry the Discord cache search using the authoritative title (first 4
   words). If that misses, retry using the meta title.

5. If nothing hits, return needs_human=True and a structured trace so the
   operator can see exactly what was tried.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.decodo_client import DecodoClient
from app.storage import Storage

logger = logging.getLogger("hoodline.article_locator")

CMS_EDIT_URL_PATTERN = re.compile(
    r"https?://hoodline\.impress3\.com/articles/(\d+)/edit"
)

HOODLINE_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?hoodline\.com/[^\s)\"'>]+"
)

STOP_WORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "by", "and",
    "or", "but", "with", "as", "is", "are", "was", "were", "be", "been",
    "this", "that", "these", "those", "it", "its", "hoodline", "sf",
}


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

        Returns a dict with:
          found (bool)
          cms_edit_url (str | None)
          article_id (int | None)
          article_url (str | None)
          matched_post (dict | None)   the cached editorial post that hit
          authoritative_title (str)    what we ended up searching with
          trace (list[dict])           step-by-step diagnostics

        Cascade:
          1. Parse email for url + title mention.
          2. If a title is present, try Discord directly (first 4 words of
             the sender-supplied title).
          3. If the email has a URL, scrape it via Decodo and try each
             title candidate (<h1>, stripped <title>, og:title, meta title)
             against Discord.
          4. If still no hit, Google "<title> hoodline" (or generated
             keyword + hoodline if no title) via Decodo. If that returns a
             different canonical URL from the email URL, scrape it and try
             its candidates against Discord too.
          5. If nothing matches, flag for manual paste.
        """
        trace: list[TraceStep] = []

        combined = f"{email_subject or ''}\n{email_body or ''}"

        # Only trust hoodline.com / hoodline.impress3.com URLs here. A hinted
        # URL from Claude's assessment may point at a source the email is
        # linking to (e.g. NYPost, a PR service) — not the article we want.
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
            detail=f"hoodline_url={'yes' if email_url else 'no'}; title_mention={'yes' if email_title else 'no'}",
            data={"url": email_url, "title": email_title},
        ))

        tried_titles: set[str] = set()
        final_article_url: str = ""

        # Step 1: Email title → Discord direct
        if email_title:
            tried_titles.add(email_title.lower())
            post = self._search_discord(email_title, trace, label="email_title")
            if post:
                return self._resolved(post, trace, email_title, article_url=email_url)

        # Step 2: Scrape the URL from the email (if any), try every candidate
        email_page: dict[str, Any] = {}
        if email_url:
            email_page = self._scrape_page(email_url, trace, label="email_url")
            result = self._try_page_candidates(
                email_page,
                tried_titles,
                trace,
                label_prefix="email_url",
            )
            if result:
                return self._resolved(
                    result["post"],
                    trace,
                    result["title"],
                    article_url=email_url,
                )
            final_article_url = email_url

        # Step 3: Google search for a (possibly different) canonical URL
        google_seed = email_title or self._generate_keyword(combined)
        google_url = ""
        if google_seed:
            google_label = "title_query" if email_title else "keyword_query"
            google_url = self._find_hoodline_url_via_google(
                query_seed=google_seed,
                trace=trace,
                label=google_label,
            )

        # If Google didn't help and we also had no email URL, bail — there's
        # nothing more to try.
        if not google_url and not final_article_url:
            trace.append(TraceStep(
                step="flag",
                action="no_url_found",
                detail="Exhausted cascade without identifying a hoodline.com URL.",
            ))
            return self._not_found(trace)

        # Only re-scrape when Google surfaced a DIFFERENT URL from the one
        # we already pulled out of the email. Otherwise all candidates are
        # already in tried_titles.
        if google_url:
            if email_url and google_url == email_url:
                trace.append(TraceStep(
                    step="page_scrape",
                    action="skip_google_same_as_email",
                    detail="Google returned the same URL already scraped from the email.",
                    data={"url": google_url},
                ))
            else:
                google_page = self._scrape_page(google_url, trace, label="google_url")
                result = self._try_page_candidates(
                    google_page,
                    tried_titles,
                    trace,
                    label_prefix="google",
                )
                if result:
                    return self._resolved(
                        result["post"],
                        trace,
                        result["title"],
                        article_url=google_url,
                    )
                final_article_url = google_url

        # Step 4: Flag
        trace.append(TraceStep(
            step="flag",
            action="exhausted_cascade",
            detail="No Discord message matched any of the attempted titles.",
            data={
                "tried_titles": sorted(tried_titles),
                "article_url": final_article_url,
            },
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

    def _first_n_words(self, text: str, n: int = 4) -> list[str]:
        cleaned = re.sub(r"[^\w\s]", " ", text or "").strip()
        words = [w for w in cleaned.split() if w]
        return words[:n]

    def _search_discord(
        self,
        title_candidate: str,
        trace: list[TraceStep],
        *,
        label: str,
    ) -> dict[str, Any] | None:
        words = self._first_n_words(title_candidate, 4)
        if not words:
            trace.append(TraceStep(
                step="discord_search",
                action=f"skip_{label}",
                detail="Title candidate produced no usable words.",
            ))
            return None

        matches = self._match_editorial_cache(words)
        if not matches:
            trace.append(TraceStep(
                step="discord_search",
                action=f"miss_{label}",
                detail=f"No cached editorial post matched {' '.join(words)!r}.",
                data={"words": words},
            ))
            return None

        best = matches[0]
        trace.append(TraceStep(
            step="discord_search",
            action=f"hit_{label}",
            detail=f"Matched '{best.get('title', '')[:120]}'.",
            matched=True,
            data={
                "words": words,
                "post_id": best.get("id"),
                "post_title": best.get("title"),
                "cms_edit_url": best.get("cms_edit_url"),
                "article_url": best.get("article_url"),
                "channel": best.get("channel"),
                "message_id": best.get("message_id"),
            },
        ))
        return best

    def _match_editorial_cache(self, words: list[str]) -> list[dict[str, Any]]:
        kept = [w for w in words if len(w) >= 2]
        if not kept:
            return []

        posts = self.storage.search_editorial_posts(terms=kept, limit=25)
        if not posts:
            return []

        # Rank posts that actually have a cms_edit_url above those that don't,
        # then by earliest appearance of the longest anchor word in the title.
        anchor = max(kept, key=len).lower()

        def sort_key(post: dict[str, Any]) -> tuple[int, int]:
            has_edit_url = 0 if post.get("cms_edit_url") else 1
            title = str(post.get("title") or "").lower()
            position = title.find(anchor)
            return (has_edit_url, position if position >= 0 else 9999)

        return sorted(posts, key=sort_key)

    def _find_hoodline_url_via_google(
        self,
        *,
        query_seed: str,
        trace: list[TraceStep],
        label: str = "query",
    ) -> str:
        seed = (query_seed or "").strip()
        if not seed:
            trace.append(TraceStep(
                step="google_search",
                action=f"skip_{label}",
                detail="No query seed available — cannot search Google.",
            ))
            return ""

        query = f"{seed} hoodline"
        if not self.decodo.is_configured():
            trace.append(TraceStep(
                step="google_search",
                action=f"not_configured_{label}",
                detail="Decodo is not configured; cannot run Google search.",
                data={"query": query},
            ))
            return ""

        try:
            results = self.decodo.search_google(query)
        except Exception as exc:
            trace.append(TraceStep(
                step="google_search",
                action=f"error_{label}",
                detail=str(exc)[:200],
                data={"query": query},
            ))
            return ""

        for item in results:
            url = str(item.get("url") or "")
            if (
                "hoodline.com/" in url
                and "hoodline.com/tag" not in url
                and "hoodline.com/news" not in url
            ):
                trace.append(TraceStep(
                    step="google_search",
                    action=f"hit_{label}",
                    detail=url,
                    matched=True,
                    data={"query": query, "title": item.get("title", "")},
                ))
                return url.rstrip(".,)\"'>")

        trace.append(TraceStep(
            step="google_search",
            action=f"miss_{label}",
            detail=f"Ran query {query!r} via Decodo but no hoodline.com article-level result.",
            data={"query": query, "result_count": len(results)},
        ))
        return ""

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
            detail=f"title={page.get('title', '')[:140]!r}",
            data={
                "url": url,
                "title": page.get("title", ""),
                "meta_title": page.get("meta_title", ""),
                "title_candidates": page.get("title_candidates", []),
            },
        ))
        return page

    def _try_page_candidates(
        self,
        page: dict[str, Any],
        tried_titles: set[str],
        trace: list[TraceStep],
        *,
        label_prefix: str,
    ) -> dict[str, Any] | None:
        """Run Discord searches for every title candidate on a scraped page.

        Returns {"post": ..., "title": ...} if any candidate matches in the
        Discord editorial cache, else None. Tracks lowercased already-tried
        titles in `tried_titles` to avoid repeating the same Discord query.
        """
        candidates: list[str] = []
        for cand in page.get("title_candidates") or []:
            if not isinstance(cand, str):
                continue
            c = cand.strip()
            if not c or c.lower() in tried_titles:
                continue
            candidates.append(c)

        meta_title = (page.get("meta_title") or "").strip()
        if (
            meta_title
            and meta_title.lower() not in tried_titles
            and all(meta_title.lower() != c.lower() for c in candidates)
        ):
            candidates.append(meta_title)

        for idx, candidate in enumerate(candidates):
            tried_titles.add(candidate.lower())
            if candidate == meta_title:
                label = f"{label_prefix}_meta_title"
            else:
                label = f"{label_prefix}_page_{idx + 1}"
            post = self._search_discord(candidate, trace, label=label)
            if post:
                return {"post": post, "title": candidate}

        return None

    def _generate_keyword(self, text: str) -> str:
        """Pick the most distinctive single word from the email.

        Cheap local heuristic used only when no title was provided. Picks
        the longest non-stopword. If we later want, we can swap this for a
        Claude call that also tries to guess a short noun phrase.
        """
        tokens = re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text or "")
        candidates: list[str] = []
        for token in tokens:
            low = token.lower()
            if low in STOP_WORDS:
                continue
            candidates.append(token)

        if not candidates:
            return ""

        # Prefer capitalized proper-noun-ish tokens if present.
        proper = [t for t in candidates if t[0].isupper()]
        pool = proper or candidates
        pool.sort(key=len, reverse=True)
        return pool[0]

    def _resolved(
        self,
        post: dict[str, Any],
        trace: list[TraceStep],
        authoritative_title: str,
        *,
        article_url: str = "",
    ) -> dict[str, Any]:
        cms_edit_url = str(post.get("cms_edit_url") or "").strip()
        article_id: int | None = None
        if cms_edit_url:
            match = CMS_EDIT_URL_PATTERN.search(cms_edit_url)
            if match:
                try:
                    article_id = int(match.group(1))
                except ValueError:
                    article_id = None

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
            "authoritative_title": authoritative_title,
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
            "trace": [t.to_dict() for t in trace],
        }
