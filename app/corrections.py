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
from typing import Any

import requests

logger = logging.getLogger("hoodline.corrections")


ASSESS_SYSTEM_PROMPT = """
You triage reader correction requests for the Hoodline newsroom.

Output ONLY valid JSON, no markdown fences, no prose, with this exact shape:
{
  "CRVS": integer 0-10,
  "SAS": integer 0-10,
  "crvs_reasoning": string,
  "sas_reasoning": string
}

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
    return {
        "CRVS": crvs,
        "SAS": sas,
        "crvs_reasoning": str(parsed.get("crvs_reasoning") or "").strip(),
        "sas_reasoning": str(parsed.get("sas_reasoning") or "").strip(),
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
) -> dict[str, Any]:
    """Run Claude with web_search on the full request + article context.

    Returns a dict containing the parsed correction JSON plus model metadata.
    """

    model = os.getenv(
        "CORRECTIONS_GENERATE_MODEL",
        os.getenv("VERIFICATION_MODEL", "claude-sonnet-4-20250514"),
    )
    timeout = float(os.getenv("CORRECTIONS_TIMEOUT_SECONDS", "120"))
    max_uses_raw = os.getenv("CORRECTIONS_WEB_SEARCH_MAX_USES", "5").strip()
    try:
        max_uses = max(1, min(20, int(max_uses_raw)))
    except ValueError:
        max_uses = 5

    name_fragment = sender_name.strip() or "name not provided"
    email_fragment = sender_email.strip() or "email not provided"

    user_prompt = (
        "**Correction Request**:\n"
        "We have a correction request that was emailed to Hoodline regarding "
        "an article that we published. The email came from "
        f"{name_fragment}, {email_fragment} with subject line "
        f'"{subject.strip()}":\n'
        f"{body}\n\n"
        "**Our article**:\n"
        f"Title:\n{title}\n\n"
        f"Meta Description:\n{meta_description}\n\n"
        f"Meta Title:\n{meta_title}\n\n"
        f"Excerpt:\n{excerpt}\n\n"
        f"Body (HTML fragment):\n{article_body}\n\n"
        f"Featured Image Attribution:\n{featured_image_attribution}\n\n"
        f"Image URL:\n{image_url}\n\n"
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

    return {
        "correction": cleaned,
        "changes": changes,
        "summary": summary,
        "model": model,
        "raw": parsed,
        "web_search_requests": server_tool_use.get("web_search_requests"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
