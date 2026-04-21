from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha1
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.storage import Storage


@dataclass
class ResolverConfig:
    min_similarity: float = 0.65
    max_candidates: int = 250


class ArticleResolver:
    def __init__(self, *, storage: Storage, cms_base_url: str, config: ResolverConfig | None = None) -> None:
        self.storage = storage
        self.cms_base_url = cms_base_url.rstrip("/")
        self.config = config or ResolverConfig()

    def resolve(
        self,
        *,
        article_hint: str,
        classifier_hint: str | None,
        seed_editorial_post: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        effective_hint = (article_hint or "").strip() or (classifier_hint or "").strip()
        if not effective_hint:
            raise ValueError("article_hint is required")

        seeded = None
        if seed_editorial_post:
            seeded = self._seed_editorial_post(seed_editorial_post)

        direct = self._extract_hoodline_url(effective_hint)
        if direct:
            cms_id = self._parse_cms_id(None, direct)
            return {
                "article_url": direct,
                "article_cms_id": cms_id,
                "article_edit_url": self._build_edit_url(cms_id, None),
                "resolver_confidence": 0.99,
                "resolver_strategy": "direct_url",
                "needs_human": False,
                "matched_editorial_post": None,
                "seeded_editorial_post": seeded,
            }

        candidates = self.storage.list_editorial_posts(limit=self.config.max_candidates)
        ranked = self._rank_candidates(effective_hint, candidates)
        best = ranked[0] if ranked else None

        if best and best["similarity"] >= self.config.min_similarity:
            cms_id = self._parse_cms_id(best.get("cms_edit_url"), best["article_url"])
            confidence = max(0.65, min(0.98, round(best["similarity"], 2)))
            return {
                "article_url": best["article_url"],
                "article_cms_id": cms_id,
                "article_edit_url": self._build_edit_url(cms_id, best.get("cms_edit_url")),
                "resolver_confidence": confidence,
                "resolver_strategy": "editorial_cache_match",
                "needs_human": confidence < 0.8,
                "matched_editorial_post": {
                    "id": best.get("id"),
                    "title": best.get("title"),
                    "similarity": round(best["similarity"], 3),
                    "article_url": best.get("article_url"),
                    "cms_edit_url": best.get("cms_edit_url"),
                },
                "seeded_editorial_post": seeded,
            }

        fallback_url = self._build_fallback_url(effective_hint)
        fallback_id = self._parse_cms_id(None, fallback_url)
        best_similarity = round(best["similarity"], 3) if best else None
        return {
            "article_url": fallback_url,
            "article_cms_id": fallback_id,
            "article_edit_url": self._build_edit_url(fallback_id, None),
            "resolver_confidence": 0.35,
            "resolver_strategy": "heuristic_fallback",
            "needs_human": True,
            "matched_editorial_post": None,
            "best_similarity": best_similarity,
            "seeded_editorial_post": seeded,
        }

    def _seed_editorial_post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        title = (payload.get("title") or "").strip()
        article_url = (payload.get("article_url") or "").strip()
        if not title and not article_url:
            return None
        if not title or not article_url:
            raise ValueError("seed_title and seed_article_url must both be provided")

        normalized_url = self._canonicalize_hoodline_url(article_url)
        if not normalized_url:
            raise ValueError("seed_article_url must be a valid hoodline.com URL")

        cms_edit_url = (payload.get("cms_edit_url") or "").strip() or None
        content = (payload.get("content") or "").strip() or None
        channel = (payload.get("channel") or "").strip() or None
        message_id = (payload.get("message_id") or "").strip() or None

        row = self.storage.upsert_editorial_post(
            source="discord",
            channel=channel,
            message_id=message_id,
            title=title,
            article_url=normalized_url,
            cms_edit_url=cms_edit_url,
            content=content,
            posted_at=None,
        )

        return {
            "id": row["id"],
            "title": row["title"],
            "article_url": row["article_url"],
            "cms_edit_url": row.get("cms_edit_url"),
        }

    def _rank_candidates(self, hint: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hint_tokens = self._tokens(hint)
        ranked: list[dict[str, Any]] = []

        for candidate in candidates:
            title_text = str(candidate.get("title") or "").strip()
            candidate_text = " ".join(
                [
                    title_text,
                    str(candidate.get("content") or ""),
                    str(candidate.get("article_url") or ""),
                ]
            ).strip()
            if not candidate_text:
                continue

            title_tokens = self._tokens(title_text)
            candidate_tokens = self._tokens(candidate_text)
            title_similarity = self._similarity(hint, title_text, hint_tokens, title_tokens)
            full_similarity = self._similarity(hint, candidate_text, hint_tokens, candidate_tokens)
            similarity = max(title_similarity, full_similarity)
            row = dict(candidate)
            row["similarity"] = similarity
            ranked.append(row)

        ranked.sort(key=lambda row: row["similarity"], reverse=True)
        return ranked

    def _similarity(self, hint: str, candidate_text: str, hint_tokens: set[str], candidate_tokens: set[str]) -> float:
        if not hint_tokens or not candidate_tokens:
            return SequenceMatcher(None, hint.lower(), candidate_text.lower()).ratio()

        overlap = len(hint_tokens.intersection(candidate_tokens))
        coverage = overlap / len(hint_tokens)
        seq_ratio = SequenceMatcher(None, hint.lower(), candidate_text.lower()).ratio()
        return (coverage * 0.7) + (seq_ratio * 0.3)

    def _tokens(self, text: str) -> set[str]:
        tokens = []
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            if len(token) <= 2:
                continue
            if len(token) > 3 and token.endswith("s"):
                token = token[:-1]
            tokens.append(token)
        return set(tokens)

    def _extract_hoodline_url(self, text: str) -> str | None:
        match = re.search(r"https?://[^\s]+", text)
        if not match:
            return None

        return self._canonicalize_hoodline_url(match.group(0))

    def _canonicalize_hoodline_url(self, url: str) -> str | None:
        cleaned = url.strip().rstrip(".,)")
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"http", "https"}:
            return None
        netloc = (parsed.netloc or "").lower()
        if "hoodline.com" not in netloc and "hoodline.impress3.com" not in netloc:
            return None

        path = parsed.path or "/"
        canonical = f"https://{parsed.netloc}{path}".rstrip("/") + "/"
        return canonical

    def _build_fallback_url(self, hint: str) -> str:
        tokens = re.findall(r"[a-z0-9]+", hint.lower())[:8]
        slug = "-".join(tokens) if tokens else "resolved-article"
        return f"https://hoodline.com/{slug}/"

    def _parse_cms_id(self, cms_edit_url: str | None, article_url: str) -> int:
        if cms_edit_url:
            parsed = urlparse(cms_edit_url)
            qs = parse_qs(parsed.query)
            post_values = qs.get("post")
            if post_values:
                try:
                    return int(post_values[0])
                except ValueError:
                    pass

        digest = sha1(article_url.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % 100000

    def _build_edit_url(self, cms_id: int, cms_edit_url: str | None) -> str:
        if cms_edit_url:
            return cms_edit_url
        return f"{self.cms_base_url}/wp-admin/post.php?post={cms_id}&action=edit"
