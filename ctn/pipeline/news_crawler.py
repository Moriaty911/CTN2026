"""Async crawler and normalizer for official in-universe publications (`capturethenarrative.NewsSites`).

Reads JSON endpoints from `https://sites.legitreal.com` (e.g., `kingstonia-herald`,
`daily-kingstonia`, and campaign sites) without consuming any Legit Social API rate limit.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from typing import Any, Mapping

from ..client.legit_v1 import parse_timestamp
from ..jev.postflight import check_incoming_post_language

from capturethenarrative import DEFAULT_NEWS_SITES_URL, NewsSites  # noqa: E402

# Canonical mapping from publication sub_url / name to our internal snake_case newspaper key.
SITE_KEY_ALIASES: dict[str, str] = {
    "daily-kingstonia": "daily_kingstonia",
    "the-daily-kingstonia": "daily_kingstonia",
    "daily_kingstonia": "daily_kingstonia",
    "kingstonia-herald": "kingstonia_herald",
    "the-kingstonia-herald": "kingstonia_herald",
    "kingstonia_herald": "kingstonia_herald",
}


def canonical_site_key(site: str | Mapping[str, Any]) -> str:
    """Convert a publication sub_url or site dict into a snake_case key (e.g. `'kingstonia_herald'`)."""
    if isinstance(site, Mapping):
        raw = str(site.get("sub_url") or site.get("slug") or site.get("name") or "")
    else:
        raw = str(site or "")
    clean = raw.strip().strip("/").lower().replace(" ", "-")
    if clean in SITE_KEY_ALIASES:
        return SITE_KEY_ALIASES[clean]
    return clean.replace("-", "_") or "unknown_site"


def extract_byline_author(byline: Any, fallback: str = "") -> str:
    """Extract a clean author/journalist handle or display name from an article's `byline` field."""
    if isinstance(byline, Mapping):
        return str(
            byline.get("username")
            or byline.get("handle")
            or byline.get("name")
            or byline.get("display_name")
            or fallback
        ).strip()
    if isinstance(byline, str) and byline.strip():
        return byline.strip()
    return fallback


class NewsSitesCrawler:
    """Async adapter around `capturethenarrative.NewsSites` (`https://sites.legitreal.com`)."""

    def __init__(
        self,
        url: str | None = None,
        *,
        timeout: float = 20.0,
        max_retries: int = 2,
        news_sites: NewsSites | None = None,
    ) -> None:
        self.url = (url or DEFAULT_NEWS_SITES_URL).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.news: NewsSites = news_sites or NewsSites(
            self.url, timeout=self.timeout, max_retries=self.max_retries
        )

    @staticmethod
    def normalize_article(
        raw: Mapping[str, Any],
        *,
        default_site: str | Mapping[str, Any] = "",
        body_override: str | None = None,
    ) -> dict[str, Any]:
        """Normalize an official NewsSites article dict into a unified schema compatible with
        both `MacroStateCollector` (`post_id`, `author`, `text`) and rich article consumers.
        """
        if not isinstance(raw, Mapping):
            return {}

        site_info = raw.get("site") if isinstance(raw.get("site"), Mapping) else (
            default_site if isinstance(default_site, Mapping) else {}
        )
        sub_url = str(
            site_info.get("sub_url")
            or (raw.get("site") if isinstance(raw.get("site"), str) else "")
            or (default_site if isinstance(default_site, str) else "")
        ).strip().strip("/")
        site_name = str(site_info.get("name") or sub_url)
        site_key = canonical_site_key(site_info or sub_url)

        slug = str(raw.get("slug") or raw.get("id") or raw.get("post_id") or "").strip()
        headline = str(raw.get("headline") or raw.get("title") or "").strip()
        summary = str(raw.get("summary") or raw.get("excerpt") or "").strip()
        body = str(
            body_override if body_override is not None else (raw.get("body") or "")
        ).strip()

        if headline and summary:
            combined_text = f"{headline} — {summary}"
        elif headline and body:
            combined_text = f"{headline} — {body[:240]}"
        else:
            combined_text = headline or summary or body or str(raw.get("text") or "")

        author = extract_byline_author(
            raw.get("byline") or raw.get("author"),
            fallback=site_name or site_key,
        )
        published_at_str = str(raw.get("published_at") or raw.get("created_at") or "")
        published_ts = parse_timestamp(published_at_str)
        post_id = f"{sub_url}:{slug}" if (sub_url and slug) else (slug or headline[:32])

        return {
            **dict(raw),
            "post_id": post_id,
            "slug": slug,
            "site_key": site_key,
            "sub_url": sub_url,
            "site_name": site_name,
            "author": author,
            "headline": headline,
            "summary": summary,
            "body": body,
            "text": combined_text,
            "url": str(raw.get("url") or (f"/{sub_url}/articles/{slug}" if sub_url and slug else "")),
            "published_at": published_at_str,
            "created_at": published_ts,
            "likes": int(raw.get("likes") or 0),
            "replies": int(raw.get("replies") or 0),
        }

    async def list_sites(self) -> list[dict[str, Any]]:
        """Fetch all active publications from `/index.json`."""
        try:
            raw_sites = await asyncio.to_thread(self.news.sites)
        except Exception:
            return []
        return [dict(s) for s in (raw_sites or []) if isinstance(s, Mapping)]

    async def fetch_article(self, site: str | Mapping[str, Any], slug: str) -> dict[str, Any]:
        """Fetch one full article (including markdown `body`) from `/{site}/articles/{slug}.json`."""
        raw = await asyncio.to_thread(self.news.article, site, slug)
        if not isinstance(raw, Mapping):
            return {}
        return self.normalize_article(raw, default_site=site)

    async def fetch_site_articles(
        self,
        site: str | Mapping[str, Any],
        *,
        limit: int = 5,
        include_body: bool = False,
        english_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch the newest front-page articles for `site` (`/{site}.json`)."""
        try:
            raw_articles = await asyncio.to_thread(self.news.articles, site)
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for item in (raw_articles or [])[: max(1, limit)]:
            if not isinstance(item, Mapping):
                continue
            body_text: str | None = None
            slug = str(item.get("slug") or "").strip()
            if include_body and slug and not item.get("body"):
                try:
                    full = await asyncio.to_thread(self.news.article, site, slug)
                    if isinstance(full, Mapping) and full.get("body"):
                        body_text = str(full["body"])
                except Exception:
                    body_text = None
            norm = self.normalize_article(item, default_site=site, body_override=body_text)
            if english_only and norm.get("text"):
                lang_ok, _ = check_incoming_post_language(str(norm["text"]))
                if not lang_ok:
                    continue
            out.append(norm)
        return out

    async def fetch_latest(
        self,
        *,
        limit: int = 10,
        include_body: bool = False,
        english_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch the newest articles across all active publications on `sites.legitreal.com`."""
        try:
            raw_items = await asyncio.to_thread(self.news.latest, limit=limit)
        except Exception:
            return []

        out: list[dict[str, Any]] = []
        for item in raw_items or []:
            if not isinstance(item, Mapping):
                continue
            site_obj = item.get("site") or ""
            slug = str(item.get("slug") or "").strip()
            body_text: str | None = None
            if include_body and slug and site_obj and not item.get("body"):
                try:
                    full = await asyncio.to_thread(self.news.article, site_obj, slug)
                    if isinstance(full, Mapping) and full.get("body"):
                        body_text = str(full["body"])
                except Exception:
                    body_text = None
            norm = self.normalize_article(item, default_site=site_obj, body_override=body_text)
            if english_only and norm.get("text"):
                lang_ok, _ = check_incoming_post_language(str(norm["text"]))
                if not lang_ok:
                    continue
            out.append(norm)
        return out

    async def fetch_newspapers_snapshot(
        self,
        *,
        limit_per_site: int = 5,
        include_body: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Build the `newspapers` dictionary for `MacroStateCollector.collect()`.

        Always includes `'daily_kingstonia'` and `'kingstonia_herald'`, plus any other active
        publications discovered via `/index.json`.
        """
        snapshot: dict[str, list[dict[str, Any]]] = {
            "daily_kingstonia": [],
            "kingstonia_herald": [],
        }
        sites = await self.list_sites()
        if sites:
            for site_dict in sites:
                key = canonical_site_key(site_dict)
                articles = await self.fetch_site_articles(
                    site_dict, limit=limit_per_site, include_body=include_body
                )
                snapshot[key] = articles
        else:
            # Fallback to direct sub_url fetches if /index.json was unavailable
            for sub_url, key in (
                ("daily-kingstonia", "daily_kingstonia"),
                ("kingstonia-herald", "kingstonia_herald"),
            ):
                snapshot[key] = await self.fetch_site_articles(
                    sub_url, limit=limit_per_site, include_body=include_body
                )
        return snapshot
