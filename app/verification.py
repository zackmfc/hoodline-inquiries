from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("hoodline.verification")

LINK_CHECK_SYSTEM = """
You are a newsroom fact-checker. You are given the text of a web page that was
linked from a Hoodline article, along with the specific claim a reader says is
wrong.

Determine whether this source page contains a correction, editor's note, update,
or any information that would affect the claim in question.

Return ONLY valid JSON:
{
  "has_relevant_update": boolean,
  "quote": "exact text from the page that is relevant (max 300 chars)",
  "relevance": "high" | "medium" | "low" | "none"
}
No markdown fences.
""".strip()

SEARCH_QUERY_SYSTEM = """
You are generating web search queries for a newsroom fact-checker.
Given a specific claim that a reader says is wrong in a news article,
generate 2-4 search queries that would help verify or refute the claim.
Prefer queries that target primary sources (official sites, government records,
the subject's own channels).

Return ONLY valid JSON:
{
  "queries": ["query1", "query2", ...]
}
No markdown fences.
""".strip()

CONSISTENCY_SYSTEM = """
You are a newsroom fact-checker. You are given:
1. The body/description of a news article
2. A specific claim a reader says is wrong
3. The reader's proposed correction

Check whether the correction request contradicts other parts of the article that
the reader did NOT flag. Surface any internal inconsistencies.

Return ONLY valid JSON:
{
  "has_internal_inconsistency": boolean,
  "inconsistency_details": "description of any inconsistency found, or empty string",
  "claim_supported_by_article": boolean,
  "article_excerpt": "relevant excerpt from the article body (max 200 chars)"
}
No markdown fences.
""".strip()

CONFIDENCE_SYSTEM = """
You are a newsroom fact-checking supervisor. Given all collected evidence for a
correction request, assign a confidence score and recommend an action.

Evidence includes: outbound link checks, web search results, and internal
consistency analysis.

Return ONLY valid JSON:
{
  "confidence": integer 0-10,
  "recommended_action": "silent_correction" | "update_stamp" | "editors_note_bottom" | "editors_note_top" | "reject" | "needs_human",
  "reasoning": "brief explanation of confidence score",
  "recommended_edit": {
    "field": "body",
    "old_value": "the incorrect text",
    "new_value": "the corrected text"
  }
}
No markdown fences.
""".strip()


class VerificationAgent:
    def __init__(self) -> None:
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
        self.model = os.getenv("VERIFICATION_MODEL", "claude-sonnet-4-20250514")
        self.timeout_seconds = float(os.getenv("VERIFICATION_TIMEOUT_SECONDS", "60"))
        self.link_fetch_timeout = float(os.getenv("VERIFICATION_LINK_TIMEOUT", "15"))
        self.max_links = int(os.getenv("VERIFICATION_MAX_LINKS", "8"))
        self.max_search_results = int(os.getenv("VERIFICATION_MAX_SEARCH_RESULTS", "5"))
        self.user_agent = "HoodlineVerificationBot/1.0"

    def verify(
        self,
        *,
        specific_claim: str,
        proposed_correction: str,
        article_body: str,
        outbound_links: list[str],
        request_type: str,
        headline: str = "",
    ) -> dict[str, Any]:
        if not specific_claim.strip():
            return self._fallback_result(specific_claim, "No specific claim provided")

        has_claude = bool(self.api_key)
        backend = "claude" if has_claude else "rules"

        link_results: list[dict[str, Any]] = []
        search_results: list[dict[str, Any]] = []
        consistency_result: dict[str, Any] = {}

        if has_claude:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {
                    pool.submit(
                        self._check_outbound_links,
                        outbound_links[:self.max_links],
                        specific_claim,
                    ): "links",
                    pool.submit(
                        self._web_search_grounding,
                        specific_claim,
                        proposed_correction,
                        headline,
                    ): "search",
                    pool.submit(
                        self._check_internal_consistency,
                        article_body,
                        specific_claim,
                        proposed_correction,
                    ): "consistency",
                }

                for future in as_completed(futures, timeout=self.timeout_seconds * 2):
                    key = futures[future]
                    try:
                        result = future.result()
                        if key == "links":
                            link_results = result
                        elif key == "search":
                            search_results = result
                        elif key == "consistency":
                            consistency_result = result
                    except Exception as exc:
                        logger.warning("Verification sub-check '%s' failed: %s", key, exc)

            confidence_result = self._compute_confidence(
                specific_claim=specific_claim,
                proposed_correction=proposed_correction,
                request_type=request_type,
                link_results=link_results,
                search_results=search_results,
                consistency_result=consistency_result,
            )
        else:
            confidence_result = self._rules_confidence(specific_claim, request_type)

        evidence = []
        contradicting = []

        for lr in link_results:
            if lr.get("has_relevant_update"):
                evidence.append({
                    "source_url": lr.get("url", ""),
                    "quote": lr.get("quote", ""),
                    "weight": 0.8 if lr.get("relevance") == "high" else 0.5,
                })

        for sr in search_results:
            entry = {
                "source_url": sr.get("url", ""),
                "quote": sr.get("snippet", ""),
                "weight": sr.get("weight", 0.5),
            }
            if sr.get("supports_correction", True):
                evidence.append(entry)
            else:
                contradicting.append(entry)

        if consistency_result.get("has_internal_inconsistency"):
            contradicting.append({
                "source_url": "internal_consistency_check",
                "quote": consistency_result.get("inconsistency_details", ""),
                "weight": 0.6,
            })

        confidence = confidence_result.get("confidence", 5)
        recommended_action = confidence_result.get("recommended_action", "needs_human")
        recommended_edit = confidence_result.get("recommended_edit", {
            "field": "body",
            "old_value": "",
            "new_value": proposed_correction,
        })

        return {
            "confidence": confidence,
            "recommended_action": recommended_action,
            "evidence": evidence,
            "contradicting_evidence": contradicting,
            "recommended_edit": recommended_edit,
            "reasoning": confidence_result.get("reasoning", ""),
            "verification_backend": backend,
            "verification_model": self.model if has_claude else "rules",
            "link_checks_performed": len(link_results),
            "search_queries_performed": len(search_results),
            "consistency_check": consistency_result,
        }

    def _check_outbound_links(
        self, links: list[str], specific_claim: str
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for url in links:
            try:
                page_text = self._fetch_page_text(url)
                if not page_text:
                    continue

                truncated = page_text[:4000]
                user_prompt = (
                    f"Source page URL: {url}\n"
                    f"Page content (truncated):\n{truncated}\n\n"
                    f"Specific claim in question: {specific_claim}\n\n"
                    "Does this source page contain a correction, update, or information "
                    "relevant to the claim?"
                )

                parsed = self._call_claude(LINK_CHECK_SYSTEM, user_prompt)
                parsed["url"] = url
                results.append(parsed)
            except Exception as exc:
                logger.warning("Link check failed for %s: %s", url, exc)
                results.append({
                    "url": url,
                    "has_relevant_update": False,
                    "error": str(exc),
                })

        return results

    def _web_search_grounding(
        self, specific_claim: str, proposed_correction: str, headline: str
    ) -> list[dict[str, Any]]:
        try:
            queries = self._generate_search_queries(specific_claim, proposed_correction, headline)
        except Exception as exc:
            logger.warning("Search query generation failed: %s", exc)
            queries = [specific_claim[:100]]

        results: list[dict[str, Any]] = []
        for query in queries[:4]:
            try:
                search_hits = self._execute_search(query)
                for hit in search_hits[:self.max_search_results]:
                    results.append({
                        "url": hit.get("url", ""),
                        "snippet": hit.get("snippet", ""),
                        "title": hit.get("title", ""),
                        "query": query,
                        "supports_correction": True,
                        "weight": 0.5,
                    })
            except Exception as exc:
                logger.warning("Search failed for query '%s': %s", query, exc)

        return results

    def _generate_search_queries(
        self, specific_claim: str, proposed_correction: str, headline: str
    ) -> list[str]:
        user_prompt = (
            f"Article headline: {headline}\n"
            f"Specific claim the reader says is wrong: {specific_claim}\n"
            f"Reader's proposed correction: {proposed_correction}\n\n"
            "Generate 2-4 web search queries to verify this claim."
        )

        parsed = self._call_claude(SEARCH_QUERY_SYSTEM, user_prompt)
        queries = parsed.get("queries", [])
        if not isinstance(queries, list) or not queries:
            return [specific_claim[:100]]
        return [str(q) for q in queries[:4]]

    def _execute_search(self, query: str) -> list[dict[str, Any]]:
        """Search using DuckDuckGo HTML. Returns list of {url, title, snippet}."""
        try:
            encoded = quote_plus(query)
            url = f"https://html.duckduckgo.com/html/?q={encoded}"
            response = requests.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=self.link_fetch_timeout,
            )
            if response.status_code != 200:
                return []

            soup = BeautifulSoup(response.text, "html.parser")
            results: list[dict[str, Any]] = []

            for result_div in soup.select(".result"):
                title_el = result_div.select_one(".result__title a, .result__a")
                snippet_el = result_div.select_one(".result__snippet")

                if not title_el:
                    continue

                href = title_el.get("href", "")
                if href.startswith("//duckduckgo.com/l/"):
                    ud_match = re.search(r"uddg=([^&]+)", href)
                    if ud_match:
                        from urllib.parse import unquote
                        href = unquote(ud_match.group(1))

                results.append({
                    "url": href,
                    "title": title_el.get_text(strip=True),
                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                })

                if len(results) >= self.max_search_results:
                    break

            return results
        except Exception as exc:
            logger.warning("DuckDuckGo search failed: %s", exc)
            return []

    def _check_internal_consistency(
        self, article_body: str, specific_claim: str, proposed_correction: str
    ) -> dict[str, Any]:
        if not article_body.strip():
            return {
                "has_internal_inconsistency": False,
                "inconsistency_details": "",
                "claim_supported_by_article": False,
                "article_excerpt": "",
            }

        truncated = article_body[:6000]
        user_prompt = (
            f"Article body (truncated):\n{truncated}\n\n"
            f"Specific claim the reader says is wrong: {specific_claim}\n"
            f"Reader's proposed correction: {proposed_correction}\n\n"
            "Check for internal inconsistencies."
        )

        try:
            return self._call_claude(CONSISTENCY_SYSTEM, user_prompt)
        except Exception as exc:
            logger.warning("Consistency check failed: %s", exc)
            return {
                "has_internal_inconsistency": False,
                "inconsistency_details": "",
                "claim_supported_by_article": False,
                "article_excerpt": "",
                "error": str(exc),
            }

    def _compute_confidence(
        self,
        *,
        specific_claim: str,
        proposed_correction: str,
        request_type: str,
        link_results: list[dict[str, Any]],
        search_results: list[dict[str, Any]],
        consistency_result: dict[str, Any],
    ) -> dict[str, Any]:
        evidence_summary = []

        supporting_links = [lr for lr in link_results if lr.get("has_relevant_update")]
        if supporting_links:
            evidence_summary.append(
                f"Link checks: {len(supporting_links)}/{len(link_results)} sources "
                f"show relevant updates."
            )
        else:
            evidence_summary.append(f"Link checks: 0/{len(link_results)} sources show updates.")

        if search_results:
            evidence_summary.append(
                f"Web search: found {len(search_results)} results across queries."
            )

        if consistency_result.get("has_internal_inconsistency"):
            evidence_summary.append(
                f"Internal consistency: inconsistency found — "
                f"{consistency_result.get('inconsistency_details', '')[:200]}"
            )
        elif consistency_result:
            evidence_summary.append("Internal consistency: no issues found.")

        evidence_text = "\n".join(evidence_summary)

        user_prompt = (
            f"Specific claim: {specific_claim}\n"
            f"Proposed correction: {proposed_correction}\n"
            f"Request type: {request_type}\n\n"
            f"Evidence collected:\n{evidence_text}\n\n"
            f"Link check details: {json.dumps(link_results[:5], default=str)[:2000]}\n"
            f"Search results: {json.dumps(search_results[:5], default=str)[:2000]}\n"
            f"Consistency: {json.dumps(consistency_result, default=str)[:1000]}\n\n"
            "Assign a confidence score 0-10 and recommend an action."
        )

        try:
            return self._call_claude(CONFIDENCE_SYSTEM, user_prompt)
        except Exception as exc:
            logger.warning("Confidence computation failed: %s", exc)
            return self._rules_confidence(specific_claim, request_type)

    def _rules_confidence(self, specific_claim: str, request_type: str) -> dict[str, Any]:
        """Fallback confidence scoring when Claude is unavailable."""
        lowered = specific_claim.lower()
        confidence = 5

        if any(t in lowered for t in ["wrong", "incorrect", "inaccurate", "false"]):
            confidence = 7
        if any(t in lowered for t in ["confirmed", "official", "records show"]):
            confidence = 8
        if any(t in lowered for t in ["might", "maybe", "unclear", "i think"]):
            confidence = 4

        if request_type == "factual_error":
            confidence = min(10, confidence + 1)
        elif request_type == "opinion_disagreement":
            confidence = max(0, confidence - 3)

        if confidence >= 8:
            action = "editors_note_bottom"
        elif confidence >= 5:
            action = "update_stamp"
        elif confidence >= 3:
            action = "needs_human"
        else:
            action = "reject"

        return {
            "confidence": confidence,
            "recommended_action": action,
            "reasoning": f"Rules-based scoring (no LLM available). Request type: {request_type}.",
            "recommended_edit": {
                "field": "body",
                "old_value": "",
                "new_value": "",
            },
        }

    def _call_claude(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")

        url = f"{self.base_url}/v1/messages"
        payload = {
            "model": self.model,
            "max_tokens": 1000,
            "temperature": 0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        response = requests.post(
            url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )

        if response.status_code >= 400:
            raise ValueError(
                f"Anthropic API error {response.status_code}: {response.text[:300]}"
            )

        data = response.json()
        content_blocks = data.get("content", [])
        text_parts = [
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        ]
        raw_text = "\n".join(part for part in text_parts if part)
        return self._extract_json(raw_text)

    def _fetch_page_text(self, url: str) -> str:
        """Fetch a URL and return plain text content (truncated)."""
        try:
            response = requests.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=self.link_fetch_timeout,
            )
            if response.status_code != 200:
                return ""

            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            return soup.get_text(" ", strip=True)[:8000]
        except Exception:
            return ""

    def _extract_json(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ValueError("Empty response from Claude")

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            raise ValueError(f"No JSON in response: {text[:200]}")

        return json.loads(match.group(0))
