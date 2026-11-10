"""The Indian Express source for The Morning Brief.

Backbone: Indian Express RSS feeds (the site's own section feeds) + the
front-page HTML to recover the lead/ordering, because IE's RSS items ship with
an *empty* ``description`` / ``content:encoded``.  Summaries are therefore
recovered from the article page (``og:description``, then the first real
paragraph).  Everything is cached on disk by :mod:`src.core.fetch`.

Public API
----------
``fetch(n=15, config=None) -> list[dict]``
    Returns up to ``n`` ITEM dicts (see ``docs/ITEM_CONTRACT.md``), highest
    priority first.

CLI
---
``python -m src.sources.indianexpress --out out/probe/ie.json [--n 15] [--no-cache]``
"""
from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

from src.core.fetch import (
    abs_url,
    clean,
    dedupe,
    get,
    make_item,
    meta,
    now_ist,
    parse_datetime,
    save_json,
    soup,
)

SOURCE = "indianexpress"
BASE = "https://indianexpress.com"
_IST = timezone(timedelta(hours=5, minutes=30))

#: Filled in by :func:`fetch` so the CLI can report which feeds worked.
LAST_FEED_STATUS: dict[str, str] = {}

#: (url, fallback category, label) — every feed is optional: failures are skipped.
FEEDS: list[tuple[str, str, str]] = [
    ("https://indianexpress.com/feed/", "other", "main"),
    ("https://indianexpress.com/section/india/feed/", "national", "india"),
    ("https://indianexpress.com/section/opinion/editorials/feed/", "editorial", "editorials"),
    ("https://indianexpress.com/section/opinion/feed/", "editorial", "opinion"),
    ("https://indianexpress.com/section/business/feed/", "economy", "business"),
    ("https://indianexpress.com/section/world/feed/", "international", "world"),
    ("https://indianexpress.com/section/explained/feed/", "other", "explained"),
    ("https://indianexpress.com/section/politics/feed/", "national", "politics"),
]

#: URL path fragments we never want in a 5-minute morning brief.
SKIP_PATH_HINTS = (
    "/entertainment/",
    "/sports/",
    "/lifestyle/",
    "/cities/",
    "/trending/",
    "/food/",
    "/travel/",
    "/viral/",
    "/photos/",
    "/web-stories/",
    "/40-years-ago/",
    "/upsc-current-affairs/",
)

#: Soft per-category caps so one section cannot crowd out the brief.
CATEGORY_CAPS = {"editorial": 4, "national": 6, "economy": 4, "international": 4, "other": 3}

#: og:description fields sometimes end with a paywall/truncation artefact.
TRUNCATION_TAIL_RE = re.compile(r"\s*(?:\[\s*(?:…|\.\.\.)\s*\]|…)\s*\.?\s*$")

CATEGORY_PRIORITY = {
    "editorial": 68,
    "national": 62,
    "economy": 63,
    "international": 58,
    "sports": 30,
    "other": 38,
}

CATEGORY_LABELS = {
    "editorial": "Opinion",
    "national": "National",
    "economy": "Economy",
    "international": "World",
    "other": "Explained",
}

#: (tag, regex) — first few matches are attached to the item.
TAG_RULES: list[tuple[str, re.Pattern]] = [
    ("Supreme Court", re.compile(r"\bsupreme court\b|\bcji\b", re.I)),
    ("High Court", re.compile(r"\bhigh court\b", re.I)),
    ("Parliament", re.compile(r"\bparliament\b|\blok sabha\b|\brajya sabha\b|\bbill\b", re.I)),
    ("Judiciary", re.compile(r"\bcollegium\b|\bjudicial\b|\bjudges?\b", re.I)),
    ("RBI", re.compile(r"\brbi\b|\breserve bank\b|\brepo rate\b", re.I)),
    ("Economy", re.compile(r"\beconom(?:y|ic)\b|\bgdp\b|\binflation\b|\bfiscal\b|\brupee\b", re.I)),
    ("Elections", re.compile(r"\belection\b|\bpoll[s]?\b|\beci\b|\bvoter\b", re.I)),
    ("Cabinet", re.compile(r"\bcabinet\b|\bunion government\b", re.I)),
    ("Security", re.compile(r"\barmy\b|\bsecurity\b|\bterror\b|\bnaxal\b|\bpolice\b", re.I)),
    ("Foreign Policy", re.compile(r"\bforeign\b|\bdiplomat\w*\b|\btreaty\b", re.I)),
]

#: multi-word / acronym signals that justify a bump inside a category.
KEYWORD_BOOST: list[tuple[re.Pattern, int]] = [
    (re.compile(r"\bsupreme court\b|\bcji\b|\bconstitution bench\b", re.I), 7),
    (re.compile(r"\bparliament\b|\bbill\b|\bact\b|\bordinance\b", re.I), 5),
    (re.compile(r"\bcabinet\b|\bprime minister\b|\bpresident\b", re.I), 5),
    (re.compile(r"\brbi\b|\brepo rate\b|\binflation\b|\bgdp\b|\bfiscal deficit\b", re.I), 5),
    (re.compile(r"\belection commission\b|\belections?\b|\bvoter\b", re.I), 4),
    (re.compile(r"\bsupreme court\b.*\b(?:hearing|order|verdict|judgment)\b", re.I), 3),
]

TITLE_TAIL_RE = re.compile(
    r"\s*(?:[|\-–—]\s*(?:The\s+)?Indian Express(?:\s+.*)?)\s*$", re.IGNORECASE
)
LIVE_RE = re.compile(r"\blive\b|\blive updates\b", re.I)
_LEAD_RE = re.compile(r"^https?://indianexpress\.com/article/")


# --------------------------------------------------------------------- helpers
def canonical_url(url: str) -> str:
    """Absolute, query-less, slash-normalised IE article URL."""
    url = (url or "").strip().split("?")[0].split("#")[0]
    if url.startswith("//"):
        url = "https:" + url
    if url and not url.startswith("http"):
        url = abs_url(BASE, url)
    return url.rstrip("/") + "/" if url else ""


def clean_title(value: str | None) -> str:
    """Headline without the site suffix and without ALL-CAPS shouting."""
    title = clean(value)
    title = TITLE_TAIL_RE.sub("", title).strip()
    if len(title) > 12 and title.isupper():
        title = title.title()
    return title


def classify(url: str, fallback: str = "other") -> str:
    """Map an IE article URL to an ITEM category."""
    path = (url or "").lower()
    if "/opinion/editorials/" in path:
        return "editorial"
    if "/opinion/" in path:
        return "editorial"
    if "/business/" in path or "/economy/" in path or "explained-economics" in path:
        return "economy"
    if "/world/" in path:
        return "international"
    if "/india/" in path or "/political-pulse/" in path:
        return "national"
    if "/sports/" in path:
        return "sports"
    return fallback if fallback in CATEGORY_PRIORITY else "other"


def is_topical(url: str) -> bool:
    """Reject entertainment/sport/city/lifestyle noise."""
    path = (url or "").lower()
    return not any(hint in path for hint in SKIP_PATH_HINTS)


def tags_for(title: str, summary: str, category: str) -> list[str]:
    blob = f"{title} {summary}"
    tags: list[str] = []
    if category == "editorial":
        tags.append("Editorial")
    for tag, rx in TAG_RULES:
        if len(tags) >= 4:
            break
        if tag not in tags and rx.search(blob):
            tags.append(tag)
    if len(tags) < 2:
        tags.append(CATEGORY_LABELS.get(category, "Brief"))
    if len(tags) < 2:
        tags.append("India")
    return tags[:4]


def _feed_entries(text: str):
    """Parse RSS text with feedparser; tolerate minor XML breakage."""
    try:
        parsed = feedparser.parse(text.encode("utf-8", errors="replace"))
        return list(parsed.entries)
    except Exception:  # noqa: BLE001 - feed must never crash the module
        return []


def _entry_media(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        items = entry.get(key) or []
        for media in items:
            url = media.get("url") if isinstance(media, dict) else None
            if url:
                return abs_url(BASE, url)
    return None


def _entry_tags(entry) -> list[str]:
    out: list[str] = []
    for tag in entry.get("tags") or []:
        term = clean(tag.get("term") if isinstance(tag, dict) else "")
        if term and len(term) <= 30:
            out.append(term)
    return out


def _entry_summary(entry) -> str:
    """RSS description/content, stripped of HTML (currently empty on IE)."""
    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            return clean_summary(_strip_html(value))
    content = entry.get("content") or []
    for chunk in content:
        value = chunk.get("value") if isinstance(chunk, dict) else None
        if value:
            return clean_summary(_strip_html(value))
    return ""


def _strip_html(value: str) -> str:
    try:
        page = soup(value)
        return page.get_text(" ", strip=True)
    except Exception:  # noqa: BLE001 - fall back to regex stripping
        return re.sub(r"<[^>]+>", " ", str(value))


def clean_summary(value: str | None) -> str:
    """Readable 1-2 sentence summary with web artefacts stripped."""
    text = clean(value)
    text = TRUNCATION_TAIL_RE.sub("", text).strip()
    text = re.sub(r"\s*(?:read more|also read|click here)\b.*$", "", text, flags=re.I).strip()
    return safe_trim(text, max_words=60, max_sentences=2)


_ABBREV_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|St|No|Rs|vs|v|etc|Jr|Sr|Prof|Smt|Shri|Ltd|Co|Govt|cm|km|kg|ft|sq|[A-Z])\."
)


def _split_sentences(text: str) -> list[str]:
    """Sentence split that does not break on ``Mr.`` / ``Ms.`` style abbreviations."""
    protected = _ABBREV_RE.sub(lambda m: m.group(0).replace(".", "\u2024"), text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9“\"'(])", protected)
    return [part.replace("\u2024", ".").strip() for part in parts if part.strip()]


def safe_trim(text: str, max_words: int = 60, max_sentences: int = 2) -> str:
    """Trim to whole sentences within word/sentence caps; never cut mid-word."""
    text = clean(text)
    if not text:
        return ""
    out: list[str] = []
    count = 0
    for sentence in _split_sentences(text):
        words = len(sentence.split())
        if out and (count + words > max_words or len(out) >= max_sentences):
            break
        out.append(sentence)
        count += words
    result = " ".join(out).strip()
    if len(result.split()) > max_words:
        result = " ".join(result.split()[:max_words]).rstrip(",;:-") + "…"
    if result and result[-1] not in ".!?…\"'":
        result += "."
    return result


def looks_incomplete(text: str | None) -> bool:
    """True when a blurb got cut off mid-sentence."""
    raw = clean(text)
    if not raw:
        return True
    if len(raw) < 60:
        return True
    if raw.endswith(("...", "…")):
        return True
    if raw.count("“") != raw.count("”"):
        return True
    return not re.search(r"[.!?][\"'”’)\]]?$", raw)


def _article_details(url: str, cache_ttl: int | None) -> dict:
    """Fetch an article page and extract og:description / published / image."""
    html = get(url, cache_ttl=cache_ttl, timeout=25, retries=2)
    page = soup(html)
    raw_summary = meta(page, "og:description", "description", "twitter:description")
    summary = clean_summary(raw_summary)
    if looks_incomplete(raw_summary) or len(summary) < 60:
        for para in page.select("p"):
            text = clean(para.get_text(" ", strip=True))
            if len(text) >= 80 and not text.lower().startswith(("read more", "also read", "advertisement")):
                candidate = clean_summary(text)
                if len(candidate) > len(summary):
                    summary = candidate
                break
    published = meta(page, "article:published_time")
    image = meta(page, "og:image")
    return {"summary": summary, "published": parse_datetime(published), "image": image or None}


def _front_page(cache_ttl: int | None) -> tuple[str, dict[str, int], list[dict]]:
    """Return (lead_url, {canonical url: rank}, top candidates) from the homepage."""
    try:
        html = get(BASE + "/", cache_ttl=cache_ttl, timeout=25, retries=2)
    except Exception:  # noqa: BLE001 - front page is a bonus, never fatal
        return "", {}, []
    page = soup(html)
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in page.select('a[href*="/article/"]'):
        url = canonical_url(abs_url(BASE, anchor.get("href")))
        if not url or url in seen or not _LEAD_RE.match(url):
            continue
        seen.add(url)
        ordered.append((url, clean(anchor.get_text(" ", strip=True))))

    lead_url = ""
    h1 = page.find("h1")
    if h1 is not None:
        lead_text = clean(h1.get_text(" ", strip=True))
        lead_words = set(re.sub(r"[^a-z0-9 ]", "", lead_text.lower()).split())
        for url, text in ordered:
            if not text or not lead_text:
                continue
            if text == lead_text or lead_text in text:
                lead_url = url
                break
            words = set(re.sub(r"[^a-z0-9 ]", "", text.lower()).split())
            if lead_words and words and len(lead_words & words) / min(len(lead_words), len(words)) >= 0.8:
                lead_url = url
                break
        if not lead_url:
            parent = h1.find_parent("a")
            if parent is not None and parent.get("href"):
                lead_url = canonical_url(abs_url(BASE, parent.get("href")))
    if not lead_url and ordered:
        lead_url = ordered[0][0]  # first article anchor in DOM order is the front-page lead

    ranks = {url: rank for rank, (url, _) in enumerate(ordered)}
    candidates: list[dict] = []
    for rank, (url, text) in enumerate(ordered):
        if rank >= 14:
            break
        if not is_topical(url) or len(text) < 20:
            continue
        candidates.append(
            {
                "url": url,
                "title": clean_title(text),
                "category": classify(url),
                "image": None,
                "published": None,
                "home_rank": rank,
                "tags": [],
            }
        )
    return lead_url, ranks, candidates


# ------------------------------------------------------------------- candidate
def _feed_candidates(cache_ttl: int | None) -> tuple[list[dict], dict[str, str]]:
    candidates: list[dict] = []
    status: dict[str, str] = {}
    for url, fallback, label in FEEDS:
        try:
            entries = _feed_entries(get(url, cache_ttl=cache_ttl, timeout=25, retries=2))
        except Exception as exc:  # noqa: BLE001 - skip failing feed
            status[label] = f"failed ({type(exc).__name__})"
            continue
        if not entries:
            status[label] = "empty"
            continue
        status[label] = f"ok ({len(entries)})"
        for entry in entries:
            link = canonical_url((entry.get("links") or [{}])[0].get("href") or entry.get("link") or "")
            if not link or not _LEAD_RE.match(link) or not is_topical(link):
                continue
            candidates.append(
                {
                    "url": link,
                    "title": clean_title(entry.get("title")),
                    "category": classify(link, fallback),
                    "image": _entry_media(entry),
                    "published": parse_datetime(entry.get("published") or entry.get("updated")),
                    "home_rank": None,
                    "tags": _entry_tags(entry),
                    "rss_summary": _entry_summary(entry),
                }
            )
    return candidates, status


def _is_fresh(published: str | None, lookback_hours: float) -> bool:
    if not published:
        return True
    try:
        dt = datetime.fromisoformat(published)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_IST)
    return now_ist() - dt <= timedelta(hours=lookback_hours)


def score(candidate: dict, lead_url: str, ranks: dict[str, int]) -> int:
    """Priority per docs/ITEM_CONTRACT.md (papers rubric)."""
    category = candidate["category"]
    title = candidate.get("title") or ""
    summary = candidate.get("rss_summary") or ""
    base = CATEGORY_PRIORITY.get(category, 38)
    priority = base

    url = candidate.get("url") or ""
    rank = candidate.get("home_rank")
    if rank is None:
        rank = ranks.get(url)
    if rank is not None:
        if url and url == lead_url:
            priority = max(priority, 86)
        elif rank < 3:
            priority = max(priority, 78)
        elif rank < 6:
            priority = max(priority, 74)
        elif rank < 10:
            priority = max(priority, 70)
        elif rank < 20:
            priority = max(priority, 63)
        else:
            priority = max(priority, 54)

    blob = f"{title} {summary}"
    for rx, bump in KEYWORD_BOOST:
        if rx.search(blob):
            priority += bump
            break
    if LIVE_RE.search(title):
        priority -= 6
    if category == "editorial":
        priority = min(priority, 75)
    return max(20, min(92, priority))


# ----------------------------------------------------------------------- fetch
def fetch(n: int = 15, config: dict | None = None) -> list[dict]:
    """Return up to ``n`` Indian Express ITEM dicts, highest priority first."""
    n = max(1, int(n or 15))
    config = config or {}
    cache_ttl = None if config.get("_no_cache") else int(config.get("cache_ttl", 1800))
    lookback = float(
        (config.get("edition") or {}).get("lookback_hours", 30)
    )

    candidates, feed_status = _feed_candidates(cache_ttl)
    lead_url, ranks, home_candidates = _front_page(cache_ttl)

    # Merge homepage candidates (they add front-page ordering even if a feed missed them).
    by_url: dict[str, dict] = {}
    for cand in home_candidates + candidates:
        url = cand["url"]
        if url in by_url:
            merged = dict(by_url[url])
            merged.update({k: v for k, v in cand.items() if v not in (None, "")})
            merged["home_rank"] = cand.get("home_rank") if cand.get("home_rank") is not None else merged.get("home_rank")
            by_url[url] = merged
        else:
            by_url[url] = dict(cand)

    fresh = [c for c in by_url.values() if _is_fresh(c.get("published"), lookback)]
    if len(fresh) < n:
        fresh = list(by_url.values())  # stale-ish morning, still better than nothing

    for cand in fresh:
        cand["priority"] = score(cand, lead_url, ranks)
    fresh.sort(key=lambda c: (-c["priority"], c.get("home_rank") if c.get("home_rank") is not None else 999))

    # Summaries: IE RSS ships no description, so hydrate a balanced shortlist
    # (editorial-heavy posting means a plain top-N would fetch only opinions).
    editorial_pool = [c for c in fresh if c["category"] == "editorial"]
    other_pool = [c for c in fresh if c["category"] != "editorial"]
    shortlist = other_pool[: max(n * 2 - 8, 16)] + editorial_pool[: max(6, n // 2)]
    for cand in editorial_pool[:3]:
        if cand not in shortlist:
            shortlist.append(cand)

    def hydrate(cand: dict) -> dict:
        summary = clean_summary(cand.get("rss_summary") or "")
        published = cand.get("published")
        image = cand.get("image")
        if not summary or looks_incomplete(cand.get("rss_summary")):
            try:
                details = _article_details(cand["url"], cache_ttl)
            except Exception:  # noqa: BLE001 - one dead article never stops the run
                details = {"summary": "", "published": None, "image": None}
            if len(details["summary"]) > len(summary):
                summary = details["summary"]
            published = published or details["published"]
            image = image or details["image"]
        cand["summary"] = summary
        cand["published"] = published
        cand["image"] = image
        return cand

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(hydrate, cand) for cand in shortlist]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:  # noqa: BLE001
                continue

    items: list[dict] = []
    for cand in shortlist:
        summary = cand.get("summary") or ""
        title = cand.get("title") or ""
        if len(summary) < 40 or len(title) < 12:
            continue
        try:
            item = make_item(
                source=SOURCE,
                category=cand["category"],
                title=title,
                url=cand["url"],
                published=cand.get("published"),
                summary=summary,
                priority=cand["priority"],
                tags=tags_for(title, summary, cand["category"]),
                image=cand.get("image"),
            )
        except Exception:  # noqa: BLE001 - malformed item is skipped, not fatal
            continue
        items.append(item)

    items.sort(key=lambda it: it["priority"], reverse=True)
    items = dedupe(items)

    # Greedy selection with soft per-category caps, then fill any remainder.
    counts: dict[str, int] = {}
    selection: list[dict] = []
    for item in items:
        category = item["category"]
        if len(selection) >= n:
            break
        cap = CATEGORY_CAPS.get(category, 5)
        if counts.get(category, 0) >= cap:
            continue
        counts[category] = counts.get(category, 0) + 1
        selection.append(item)
    if len(selection) < n:
        chosen = {it["id"] for it in selection}
        selection += [it for it in items if it["id"] not in chosen][: n - len(selection)]

    # Editorials are required output: force the best one in if caps squeezed them out.
    if not any(it["category"] == "editorial" for it in selection):
        editorial_pool = [it for it in items if it["category"] == "editorial"]
        if editorial_pool:
            selection = selection[: max(0, n - 1)] + [editorial_pool[0]]

    LAST_FEED_STATUS.clear()
    LAST_FEED_STATUS.update(feed_status)
    return selection


# ------------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch The Indian Express stories for The Morning Brief")
    parser.add_argument("--out", default="out/probe/ie.json", help="JSON output path")
    parser.add_argument("--n", type=int, default=15, help="number of items (default 15)")
    parser.add_argument("--no-cache", action="store_true", help="bypass the on-disk HTTP cache")
    args = parser.parse_args(argv)

    config = {"_no_cache": args.no_cache}
    try:
        items = fetch(n=args.n, config=config)
    except Exception as exc:  # noqa: BLE001 - CLI must not traceback on the user
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parents[2] / out
    save_json(out, items)

    print(f"source: {SOURCE} | items: {len(items)} | out: {out}")
    if LAST_FEED_STATUS:
        worked = [f"{k}={v}" for k, v in LAST_FEED_STATUS.items()]
        print("feeds: " + ", ".join(worked))
    print("priority | category      | title")
    for item in items:
        print(f"{item['priority']:>8} | {item['category']:<13} | {item['title']}")
    if not items:
        print("WARNING: no items produced (feeds reachable?)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
