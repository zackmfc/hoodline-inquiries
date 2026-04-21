"""Discord guild-wide article cache populator.

Scans all text channels in the configured guild for messages containing
article titles followed by hoodline.impress3.com URLs. Extracts the article
ID from the edit URL and upserts into the editorial_posts table.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger("hoodline.discord_cache")

HOODLINE_URL_PATTERN = re.compile(
    r"https?://hoodline\.impress3\.com/articles/(\d+)/edit"
)


class DiscordCachePopulator:
    BASE_URL = "https://discord.com/api/v10"

    def __init__(self) -> None:
        self.bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        self.guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        self.cache_days = int(os.getenv("DISCORD_CACHE_DAYS", "90"))

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self.bot_token}",
            "Content-Type": "application/json",
        }

    def is_configured(self) -> bool:
        return bool(self.bot_token) and bool(self.guild_id)

    def populate(self, storage: Any) -> dict[str, Any]:
        """Scan all text channels in the guild and cache article references."""
        if not self.is_configured():
            return {"status": "skipped", "reason": "DISCORD_BOT_TOKEN or DISCORD_GUILD_ID not set"}

        channels = self._list_text_channels()
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cache_days)

        total_messages = 0
        total_articles = 0
        channel_results: list[dict[str, Any]] = []

        for channel in channels:
            channel_id = channel["id"]
            channel_name = channel.get("name", channel_id)
            articles_found = 0

            try:
                for message in self._iter_channel_messages(channel_id, cutoff):
                    total_messages += 1
                    parsed = self._parse_article_from_message(message)
                    if parsed is None:
                        continue

                    storage.upsert_editorial_post(
                        source="discord",
                        channel=channel_name,
                        message_id=str(message["id"]),
                        title=parsed["title"],
                        article_url=parsed["article_url"],
                        cms_edit_url=parsed["cms_edit_url"],
                        content=parsed.get("content"),
                        posted_at=_parse_timestamp(message.get("timestamp")),
                    )
                    articles_found += 1
                    total_articles += 1
            except requests.HTTPError as exc:
                logger.warning("Failed to read channel %s: %s", channel_name, exc)
                channel_results.append({
                    "channel": channel_name,
                    "error": str(exc),
                })
                continue

            if articles_found > 0:
                channel_results.append({
                    "channel": channel_name,
                    "articles_found": articles_found,
                })

        return {
            "status": "ok",
            "guild_id": self.guild_id,
            "channels_scanned": len(channels),
            "messages_scanned": total_messages,
            "articles_cached": total_articles,
            "channel_details": channel_results,
        }

    def _list_text_channels(self) -> list[dict[str, Any]]:
        """Fetch all text channels (type 0) in the guild."""
        url = f"{self.BASE_URL}/guilds/{self.guild_id}/channels"
        resp = requests.get(url, headers=self._headers, timeout=15)
        resp.raise_for_status()
        all_channels = resp.json()
        return [ch for ch in all_channels if ch.get("type") == 0]

    def _iter_channel_messages(
        self,
        channel_id: str,
        cutoff: datetime,
    ) -> Any:
        """Yield messages from a channel, newest first, stopping at cutoff."""
        url = f"{self.BASE_URL}/channels/{channel_id}/messages"
        params: dict[str, Any] = {"limit": 100}

        while True:
            resp = requests.get(url, headers=self._headers, params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()

            if not batch:
                break

            for msg in batch:
                msg_time = _parse_timestamp(msg.get("timestamp"))
                if msg_time and msg_time < cutoff:
                    return
                yield msg

            params["before"] = batch[-1]["id"]

    def _parse_article_from_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Extract title + hoodline edit URL from a message.

        Expected pattern: message content has a title (text before the URL)
        followed by a hoodline.impress3.com/articles/{id}/edit URL.
        """
        content = message.get("content", "")
        if not content:
            return None

        match = HOODLINE_URL_PATTERN.search(content)
        if match is None:
            return None

        article_id = match.group(1)
        cms_edit_url = match.group(0)

        title = content[:match.start()].strip()
        remaining = content[match.end():].strip()

        title = _clean_title(title)
        if not title:
            return None

        return {
            "title": title,
            "article_url": f"https://hoodline.com/articles/{article_id}/",
            "cms_edit_url": cms_edit_url,
            "article_cms_id": int(article_id),
            "content": remaining if remaining else None,
        }


def _clean_title(raw: str) -> str:
    """Remove markdown formatting, trailing colons/dashes, and excess whitespace."""
    text = re.sub(r"\*\*|__|\*|_|~~|`", "", raw)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = text.strip().rstrip(":-–—").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
