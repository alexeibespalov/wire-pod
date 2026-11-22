"""Podcasts: find a show in Apple's public podcast directory, read its RSS feed,
stream an episode. Feeds are untrusted XML: parsed with defusedxml, size-capped."""
import re
from email.utils import parsedate_to_datetime

import httpx
from defusedxml import ElementTree as ET

_http = httpx.AsyncClient(timeout=15, follow_redirects=True,
                          headers={"User-Agent": "vector-agent/1.0 (home robot podcast player)"})
MAX_FEED_BYTES = 8_000_000


async def find_show(query: str) -> dict | None:
    r = await _http.get("https://itunes.apple.com/search",
                        params={"media": "podcast", "entity": "podcast", "term": query, "limit": 5})
    r.raise_for_status()
    results = [x for x in r.json().get("results", []) if x.get("feedUrl")]
    return results[0] if results else None


async def episodes(feed_url: str, limit: int = 40) -> list[dict]:
    if not feed_url.startswith(("https://", "http://")):
        raise ValueError("bad feed URL")
    async with _http.stream("GET", feed_url) as r:
        r.raise_for_status()
        data = b""
        async for chunk in r.aiter_bytes():
            data += chunk
            if len(data) > MAX_FEED_BYTES:
                raise ValueError("feed too large")
    root = ET.fromstring(data)
    eps = []
    for item in root.iter("item"):
        enc = item.find("enclosure")
        url = enc.get("url") if enc is not None else None
        if not url or not url.startswith(("https://", "http://")):
            continue
        title = (item.findtext("title") or "").strip()
        when = item.findtext("pubDate") or ""
        try:
            date = parsedate_to_datetime(when).strftime("%-d %B %Y")
        except Exception:
            date = ""
        eps.append({"title": title, "url": url, "date": date})
        if len(eps) >= limit:
            break
    return eps


def pick(eps: list[dict], query: str | None) -> dict | None:
    if not eps:
        return None
    if not query:
        return eps[0]  # feeds list newest first
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    scored = sorted(eps, key=lambda e: -sum(w in e["title"].lower() for w in words))
    return scored[0]
