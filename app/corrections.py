"""Correction intake assistant.

Provides two Claude-backed operations used by the /corrections wizard:

- assess_email(): Scores an inbound correction email on CRVS (Correction
  Request Validity Score) and SAS (Sender's Authority Score).
- generate_correction(): Sends the email together with the current CMS
  article fields to Claude with the built-in web_search tool, and returns a
  structured correction JSON (t, md, mt, ex, b, fia, if, CRVS2).
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from bs4 import BeautifulSoup

from app.decodo_client import DecodoClient

logger = logging.getLogger("hoodline.corrections")


ASSESS_SYSTEM_PROMPT = """
You triage reader correction requests for the Hoodline newsroom.

Output ONLY valid JSON, no markdown fences, no prose.

Scoring rules:
- CRVS (Correction Request Validity Score): higher when the correction
  request appears legitimate, specific, and well-founded. Lower when the
  request is unfounded, vague, or self-serving. If the email is delusional,
  incoherent, or reads like the ramblings of a person suffering from a
  mental health issue, CRVS must rank very low.
- SAS (Sender's Authority Score): higher when the sender's identity appears
  official or otherwise highly legitimate for this topic (first party,
  verified expert, authority on record). Lower when the sender is clearly
  illegitimate (spoofed, anonymous without context, unrelated). If the
  sender's authority is difficult to establish or unclear, SAS should rank
  in the 6-7 range.

Also extract, if present in the email (otherwise empty string):
{
  "article_url": string   // a hoodline.com URL to the article, or ""
  "article_title": string // the article's title as the sender quotes it,
                          // or the title they appear to be describing.
                          // Prefer exact quoted title; fall back to a
                          // close paraphrase only if a clear title mention
                          // is implied. Otherwise "".
}

Your full output must be a single JSON object:
{
  "CRVS": integer 0-10,
  "SAS": integer 0-10,
  "crvs_reasoning": string,
  "sas_reasoning": string,
  "article_url": string,
  "article_title": string
}
""".strip()


GENERATE_SYSTEM_PROMPT = """
You are reviewing a reader correction request against the live Hoodline
article it refers to, and proposing the edit.

You have access to a web_search tool. Use it to verify claims before
proposing changes. Prefer primary sources (official sites, government
records, the subject's own channels).

When you are done researching, output ONLY valid JSON, no markdown fences,
no prose outside the JSON, with any of the following keys that need to be
changed, added, or corrected:

{
  "t":   optional string  // new Title
  "md":  optional string  // new Meta Description
  "mt":  optional string  // new Meta Title
  "ex":  optional string  // new Excerpt
  "b":   optional string  // new Body (HTML fragment)
  "fia": optional string  // new Featured Image Attribution
  "if":  optional boolean // Image Flag — true if you suspect the image needs to be changed
  "CRVS2": integer 0-10   // Correction Request Validity Score v2 — 0 = definitely not necessary, 10 = definitely necessary
  "changes": optional object // Per-field, plain-English description of what
                             // you changed. Keys must be from {t, md, mt,
                             // ex, b, fia, if}. Values are one short
                             // sentence each. For "b", be specific about
                             // which section(s) of the body changed and
                             // why (e.g. "Updated the third paragraph to
                             // say 2023 instead of 2024, and added an
                             // Editor's Note at the bottom about the
                             // timeline."). Only include keys for fields
                             // you actually changed.
  "summary": optional string // One short sentence summarizing the overall
                             // set of changes. Omit if no changes were
                             // made.
}

Only include fields that actually need to change. Always include CRVS2.
If you propose any text-field change, include a matching entry in "changes".

Editor's Note rules:
- If you make ANY correction (any change to t, md, mt, ex, b, or fia), you
  MUST include an Editor's Note in the Body ("b").
- Place the Editor's Note at the TOP of the Body when the change is more
  significant (e.g., material factual errors, corrections involving named
  individuals, changes that affect how a reader would interpret the whole
  piece).
- Place the Editor's Note at the BOTTOM of the Body when the change is of
  lesser importance (e.g., minor date, figure, attribution, or timeline
  corrections that do not change the thrust of the article).
- Be intentionally a little bit vague. Do NOT frame the correction as a
  glaring mistake.
- NEVER refer to the correction as a hallucination, AI error, model error,
  automated system, or generation issue.
- Example: if a past event was published as a current event, the Editor's
  Note should say something implying that the timeline of events was fixed,
  not that the information was fabricated.
- Keep it neutral, past tense, one or two sentences.
""".strip()


def _http_post_messages(payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not configured")

    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    response = requests.post(
        f"{base_url}/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise ValueError(f"Anthropic API error {response.status_code}: {response.text[:400]}")
    return response.json()


def _collect_text(data: dict[str, Any]) -> str:
    blocks = data.get("content") or []
    parts: list[str] = []
    for block in blocks:
        if block.get("type") == "text" and block.get("text"):
            parts.append(block["text"])
    return "\n".join(parts).strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("Empty model response")

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response")

    return json.loads(match.group(0))


def _clamp_int(value: Any, *, low: int = 0, high: int = 10) -> int:
    try:
        as_int = int(round(float(value)))
    except (TypeError, ValueError):
        return low
    return max(low, min(high, as_int))


def assess_email(
    *,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    """Run Claude on the raw email and return CRVS / SAS plus reasoning."""

    model = os.getenv(
        "CORRECTIONS_ASSESS_MODEL",
        os.getenv("CLASSIFIER_MODEL", "claude-sonnet-4-20250514"),
    )
    timeout = float(os.getenv("CORRECTIONS_TIMEOUT_SECONDS", "60"))

    name_fragment = sender_name.strip() or "name not provided"
    user_prompt = (
        "We have a correction request that was emailed to Hoodline regarding "
        "an article that we published. The email came from "
        f"{name_fragment}, {sender_email.strip() or 'email not provided'} with "
        f'subject line "{subject.strip()}":\n\n{body}'
    )

    payload = {
        "model": model,
        "max_tokens": 600,
        "temperature": 0,
        "system": ASSESS_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    data = _http_post_messages(payload, timeout=timeout)
    text = _collect_text(data)
    parsed = _extract_json_object(text)

    crvs = _clamp_int(parsed.get("CRVS"))
    sas = _clamp_int(parsed.get("SAS"))
    article_url_hint = str(parsed.get("article_url") or "").strip()
    article_title_hint = str(parsed.get("article_title") or "").strip()
    return {
        "CRVS": crvs,
        "SAS": sas,
        "crvs_reasoning": str(parsed.get("crvs_reasoning") or "").strip(),
        "sas_reasoning": str(parsed.get("sas_reasoning") or "").strip(),
        "article_url_hint": article_url_hint,
        "article_title_hint": article_title_hint,
        "model": model,
        "raw": parsed,
    }


def extract_cms_edit_link(discord_message: str) -> dict[str, Any]:
    """Pull the Hoodline CMS edit link (and article ID) out of a pasted Discord message."""
    text = discord_message or ""
    match = re.search(
        r"https?://hoodline\.impress3\.com/articles/(\d+)/edit",
        text,
    )
    if not match:
        return {
            "found": False,
            "cms_edit_url": None,
            "article_id": None,
        }

    url = match.group(0).rstrip(".,)")
    article_id = int(match.group(1))
    return {
        "found": True,
        "cms_edit_url": url,
        "article_id": article_id,
    }


def _extract_outbound_links(article_body_html: str) -> list[str]:
    """Return unique external anchor hrefs from the article body HTML.

    Skips hoodline.com / hoodline.impress3.com (same context, not external
    evidence) and obvious non-HTTP(S) schemes. Preserves original order
    so that the first few links (usually the most important sources) get
    fetched first when we cap the list.
    """
    if not article_body_html:
        return []

    soup = BeautifulSoup(article_body_html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href.startswith(("http://", "https://")):
            continue
        lower = href.lower()
        if "hoodline.com" in lower or "hoodline.impress3.com" in lower:
            continue
        cleaned = href.rstrip(".,)\"'>")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        urls.append(cleaned)
    return urls


def _fetch_outbound_snapshots(
    decodo: DecodoClient,
    links: list[str],
    *,
    max_links: int,
    max_chars: int,
    max_workers: int = 4,
) -> list[dict[str, Any]]:
    """Scrape the first N outbound links concurrently via Decodo."""
    if not links or not decodo.is_configured():
        return []

    bounded = links[:max_links]
    snapshots: list[dict[str, Any] | None] = [None] * len(bounded)

    def fetch(idx_url: tuple[int, str]) -> None:
        idx, url = idx_url
        try:
            snap = decodo.scrape_page_text(url, max_chars=max_chars)
        except Exception as exc:
            logger.warning("outbound snapshot failed for %s: %s", url, exc)
            snapshots[idx] = {
                "requested_url": url,
                "final_url": url,
                "redirected": False,
                "title": "",
                "meta_description": "",
                "text": "",
                "error": str(exc)[:200],
            }
            return
        snapshots[idx] = snap

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(bounded)))) as pool:
        list(pool.map(fetch, list(enumerate(bounded))))

    return [s for s in snapshots if s is not None]


def _format_outbound_snapshots(snapshots: list[dict[str, Any]]) -> str:
    if not snapshots:
        return ""

    chunks: list[str] = []
    for i, snap in enumerate(snapshots, start=1):
        requested = str(snap.get("requested_url") or "").strip()
        final = str(snap.get("final_url") or requested).strip()
        redirected = bool(snap.get("redirected"))
        title = str(snap.get("title") or "").strip()
        meta = str(snap.get("meta_description") or "").strip()
        text = str(snap.get("text") or "").strip()
        error = str(snap.get("error") or "").strip()

        header_bits = [f"Source {i}: {requested}"]
        if redirected and final and final != requested:
            header_bits.append(f"(redirected to: {final})")
        if error:
            header_bits.append(f"[fetch error: {error}]")
        header = "  ".join(header_bits)

        body_lines = []
        if title:
            body_lines.append(f"Title: {title}")
        if meta:
            body_lines.append(f"Meta description: {meta}")
        if text:
            body_lines.append(f"Content: {text}")
        if not body_lines and not error:
            body_lines.append("(no readable content extracted)")

        chunks.append(header + ("\n" + "\n".join(body_lines) if body_lines else ""))

    return (
        "**Outbound link snapshots** (live fetches via Decodo of every "
        "external source cited in the article body — consult these before "
        "running web_search, because if a cited source has been redirected, "
        "updated, or corrected, that is often decisive evidence for whether "
        "the correction request is warranted):\n\n"
        + "\n\n---\n\n".join(chunks)
    )


def generate_correction(
    *,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
    title: str,
    meta_description: str,
    meta_title: str,
    excerpt: str,
    article_body: str,
    featured_image_attribution: str,
    image_url: str,
    decodo: DecodoClient | None = None,
) -> dict[str, Any]:
    """Run Claude with web_search on the full request + article context.

    Before calling Claude, we scrape every external link in article_body
    via Decodo and include each source's current title/meta/visible text
    in the prompt. This is how we catch cases where a cited article has
    been redirected to an updated version (e.g. a news outlet's follow-up
    saying charges were dropped).

    Returns a dict containing the parsed correction JSON plus model metadata.
    """

    model = os.getenv(
        "CORRECTIONS_GENERATE_MODEL",
        os.getenv("VERIFICATION_MODEL", "claude-sonnet-4-20250514"),
    )
    timeout = float(os.getenv("CORRECTIONS_TIMEOUT_SECONDS", "180"))
    max_uses_raw = os.getenv("CORRECTIONS_WEB_SEARCH_MAX_USES", "5").strip()
    try:
        max_uses = max(1, min(20, int(max_uses_raw)))
    except ValueError:
        max_uses = 5

    max_links_raw = os.getenv("CORRECTIONS_MAX_OUTBOUND_LINKS", "8").strip()
    try:
        max_outbound_links = max(0, min(20, int(max_links_raw)))
    except ValueError:
        max_outbound_links = 8

    snapshot_chars_raw = os.getenv("CORRECTIONS_OUTBOUND_CHARS", "3500").strip()
    try:
        snapshot_chars = max(500, min(8000, int(snapshot_chars_raw)))
    except ValueError:
        snapshot_chars = 3500

    decodo = decodo or DecodoClient()

    outbound_links = _extract_outbound_links(article_body)
    snapshots: list[dict[str, Any]] = []
    if max_outbound_links > 0 and outbound_links:
        snapshots = _fetch_outbound_snapshots(
            decodo,
            outbound_links,
            max_links=max_outbound_links,
            max_chars=snapshot_chars,
        )

    snapshots_section = _format_outbound_snapshots(snapshots)

    name_fragment = sender_name.strip() or "name not provided"
    email_fragment = sender_email.strip() or "email not provided"

    prompt_parts = [
        "**Correction Request**:",
        (
            "We have a correction request that was emailed to Hoodline regarding "
            "an article that we published. The email came from "
            f"{name_fragment}, {email_fragment} with subject line "
            f'"{subject.strip()}":\n{body}'
        ),
        "**Our article**:",
        f"Title:\n{title}",
        f"Meta Description:\n{meta_description}",
        f"Meta Title:\n{meta_title}",
        f"Excerpt:\n{excerpt}",
        f"Body (HTML fragment):\n{article_body}",
        f"Featured Image Attribution:\n{featured_image_attribution}",
        f"Image URL:\n{image_url}",
    ]

    if snapshots_section:
        prompt_parts.append(snapshots_section)

    prompt_parts.append(
        "**Your Task**:\n"
        "Create a JSON with any of the following, should they need to be "
        "edited, updated or corrected: t, md, mt, ex, b, fia, if (for Image "
        "Flag boolean to indicate if you suspect the image needs to be "
        "changed), CRVS2 (for Correction Request Validity Score v2 which "
        "should assess if we need to make this correction at all, wherein 0 "
        "is definitely not necessary and 10 is definitely necessary). If the "
        "correction warrants it, you should add an Editor's Note to either "
        "the top or the bottom of the Body {b} being intentionally a little "
        "bit vague to avoid making the correction seem like a glaring "
        "mistake. It should never refer to the correction as a hallucination "
        "or major error. For example, if we published something that "
        "happened a year ago, but it was published as a current event, we "
        "would correct all necessary fields of the article, then the "
        "Editor's Note would say something implying that we fixed the "
        "timeline of events."
    )

    user_prompt = "\n\n".join(prompt_parts)

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 4096,
        "temperature": 0,
        "system": GENERATE_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_uses,
            }
        ],
    }

    data = _http_post_messages(payload, timeout=timeout)
    text = _collect_text(data)
    parsed = _extract_json_object(text)

    crvs2 = _clamp_int(parsed.get("CRVS2"))
    image_flag_raw = parsed.get("if")
    image_flag: bool | None = None
    if isinstance(image_flag_raw, bool):
        image_flag = image_flag_raw
    elif isinstance(image_flag_raw, str):
        low = image_flag_raw.strip().lower()
        if low in {"true", "yes", "1"}:
            image_flag = True
        elif low in {"false", "no", "0"}:
            image_flag = False

    cleaned: dict[str, Any] = {}
    for key in ("t", "md", "mt", "ex", "b", "fia"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            cleaned[key] = value

    if image_flag is not None:
        cleaned["if"] = image_flag
    cleaned["CRVS2"] = crvs2

    raw_changes = parsed.get("changes") if isinstance(parsed.get("changes"), dict) else {}
    changes: dict[str, str] = {}
    allowed_change_keys = {"t", "md", "mt", "ex", "b", "fia", "if"}
    for key, value in raw_changes.items():
        if key not in allowed_change_keys:
            continue
        if isinstance(value, str) and value.strip():
            changes[key] = value.strip()

    summary_raw = parsed.get("summary")
    summary = summary_raw.strip() if isinstance(summary_raw, str) else ""

    usage = data.get("usage") or {}
    server_tool_use = usage.get("server_tool_use") or {}

    snapshots_meta = [
        {
            "requested_url": s.get("requested_url", ""),
            "final_url": s.get("final_url", ""),
            "redirected": bool(s.get("redirected")),
            "title": s.get("title", ""),
            "has_text": bool((s.get("text") or "").strip()),
            "error": s.get("error") or None,
        }
        for s in snapshots
    ]

    return {
        "correction": cleaned,
        "changes": changes,
        "summary": summary,
        "model": model,
        "raw": parsed,
        "web_search_requests": server_tool_use.get("web_search_requests"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "outbound_snapshots": snapshots_meta,
        "outbound_links_found": len(outbound_links),
    }
