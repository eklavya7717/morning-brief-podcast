"""The Hindu source for The Morning Brief.

The reader follows the print edition, so this module treats
https://www.thehindu.com/todays-paper/ (the e-paper's front-page hub) as the
primary source: it exposes today's print-edition articles, in print order, with
their standfirsts and page numbers.  RSS section feeds supply the rest
(national / editorial / business / international), with editorial feed items
always represented.  If a feed is stale or blocked we fall back to the HTML
front page.

Public API
----------
``fetch(n=15, config=None) -> list[dict]``
    Returns up to ``n`` ITEM dicts (see ``docs/ITEM_CONTRACT.md``), highest
    priority first.

CLI
---
``python -m src.sources.thehindu --out out/probe/th.json [--n 15] [--no-cache]``
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
    today_ist,
)

SOURCE = "thehindu"
BASE = "https://www.thehindu.com"
PRINT_HUB = BASE + "/todays-paper/"
EDITORIAL_PAGE = BASE + "/opinion/editorial/"
_IST = timezone(timedelta(hours=5, minutes=30))

#: (url, fallback category, label) — every feed is optional: failures are skipped.
FEEDS: list[tuple[str, str, str]] = [
    ("https://www.thehindu.com/opinion/editorial/feeder/default.rss", "editorial", "editorial"),
    ("https://www.thehindu.com/news/national/feeder/default.rss", "national", "national"),
    ("https://www.thehindu.com/business/feeder/default.rss", "economy", "business"),
    ("https://www.thehindu.com/news/international/feeder/default.rss", "international", "international"),
    ("https://www.thehindu.com/opinion/feeder/default.rss", "editorial", "opinion"),
    ("https://www.thehindu.com/feeder/default.rss", "other", "top"),
]

#: Filled in by :func:`fetch` so the CLI can report which feeds worked.
LAST_FEED_STATUS: dict[str, str] = {}

SKIP_PATH_HINTS = (
    "/entertainment/",
    "/sport/",
    "/videos/",
    "/video/",
    "/photos/",
    "/topic/",
    "/tag/",
    "/todays-paper/tp-sport/",
)

CATEGORY_PRIORITY = {
    "editorial": 70,
    "national": 62,
    "economy": 62,
    "international": 58,
    "sports": 30,
    "other": 38,
}

CATEGORY_LABELS = {
    "editorial": "Opinion",
    "national": "National",
    "economy": "Economy",
    "international": "World",
    "other": "Brief",
}

#: Soft per-category caps so one section cannot crowd out the brief.
CATEGORY_CAPS = {"editorial": 4, "national": 6, "economy": 4, "international": 4, "other": 5}

TAG_RULES: list[tuple[str, re.Pattern]] = [
    ("Supreme Court", re.compile(r"\bsupreme court\b|\bcji\b", re.I)),
    ("High Court", re.compile(r"\bhigh court\b", re.I)),
    ("Parliament", re.compile(r"\bparliament\b|\blok sabha\b|\brajya sabha\b|\bbill\b", re.I)),
    ("Judiciary", re.compile(r"\bcollegium\b|\bjudicial\b|\bjudges?\b", re.I)),
    ("RBI", re.compile(r"\brbi\b|\breserve bank\b|\brepo rate\b", re.I)),
    ("Economy", re.compile(r"\beconom(?:y|ic)\b|\bgdp\b|\binflation\b|\bfiscal\b|\btrade\b", re.I)),
    ("Elections", re.compile(r"\belection\b|\bpoll[s]?\b|\beci\b|\bvoter\b", re.I)),
    ("Cabinet", re.compile(r"\bcabinet\b|\bunion government\b|\bcentre\b", re.I)),
    ("Security", re.compile(r"\barmy\b|\bsecurity\b|\bterror\b|\bnaxal\b|\bpolice\b", re.I)),
    ("Foreign Policy", re.compile(r"\bforeign\b|\bdiplomat\w*\b|\btreaty\b", re.I)),
]

KEYWORD_BOOST: list[tuple[re.Pattern, int]] = [
    (re.compile(r"\bsupreme court\b|\bcji\b|\bconstitution bench\b", re.I), 7),
    (re.compile(r"\bparliament\b|\bbill\b|\bact\b|\bordinance\b", re.I), 5),
    (re.compile(r"\bcabinet\b|\bprime minister\b|\bpresident\b", re.I), 5),
    (re.compile(r"\brbi\b|\brepo rate\b|\binflation\b|\bgdp\b|\bfiscal deficit\b", re.I), 5),
    (re.compile(r"\belection commission\b|\belections?\b|\bvoter\b", re.I), 4),
]

TITLE_TAIL_RE = re.compile(r"\s*(?:[|\-–—]\s*(?:The\s+)?Hindu(?:\s+.*)?)\s*$", re.IGNORECASE)
TRUNCATION_TAIL_RE = re.compile(r"\s*(?:\[\s*(?:…|\.\.\.)\s*\]|…|\.\.\.)\s*$")
PRINT_URL_RE = re.compile(r"/todays-paper/(\d{4}-\d{2}-\d{2})/[a-z_]+/")
PAGE_NUM_RE = re.compile(r"page\s*no\.?\s*(\d+)", re.I)
#: The Hindu article URLs end in ``/articleNNNNNNN.ece`` (not ``/article/``).
ARTICLE_URL_RE = re.compile(r"/article[A-Za-z0-9][A-Za-z0-9-]*\.ece/?$")
LOCAL_MARKER_RE = re.compile(
    r"\b(?:t\.n\.|tamil nadu|chennai|theni|thiruverkadu|vyasarpadi|coimbatore|madurai|tiruchi|salem|erode|"
    r"thoothukudi|kanyakumari|hosur|kancheepuram|villupuram|city|zonal|ward)\b",
    re.I,
)


# --------------------------------------------------------------------- helpers
def canonical_url(url: str) -> str:
    url = (url or "").strip().split("?")[0].split("#")[0]
    if url.startswith("//"):
        url = "https:" + url
    if url and not url.startswith("http"):
        url = abs_url(BASE, url)
    return url.rstrip("/") + "/" if url else ""


def clean_title(value: str | None) -> str:
    title = clean(value)
    title = TITLE_TAIL_RE.sub("", title).strip()
    if len(title) > 12 and title.isupper():
        title = title.title()
    return title


def clean_summary(value: str | None) -> str:
    text = clean(value)
    text = TRUNCATION_TAIL_RE.sub("", text).strip()
    text = re.sub(r"\s*(?:read more|also read|click here)\b.*$", "", text, flags=re.I).strip()
    text = re.sub(r"^[A-Z][A-Z .&]{2,}:\s+", "", text)  # drop print datelines like "CHENNAI:"
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
    """True when a feed/standfirst blurb was cut off mid-sentence (``...`` included)."""
    raw = clean(text)
    if not raw:
        return True
    if len(raw) < 60:
        return True
    if raw.endswith(("...", "…")):
        return True  # news sites mark truncation with an ellipsis
    if raw.count("“") != raw.count("”"):
        return True  # quote cut open, e.g. a truncated dek
    return not re.search(r"[.!?][\"'”’)\]]?$", raw)


def classify(url: str, fallback: str = "other") -> str:
    """Map a The Hindu article URL to an ITEM category."""
    path = (url or "").lower()
    if "/opinion/editorial/" in path:
        return "editorial"
    if "/opinion/" in path:
        return "editorial"
    if "/business/" in path or "/economy/" in path:
        return "economy"
    if "/news/international/" in path or "/international/" in path:
        return "international"
    if "/sport/" in path:
        return "sports"
    if "/news/cities/" in path or "/cities/" in path:
        return "other"
    if "/news/" in path or "/national/" in path:
        return "national"
    if "/todays-paper/" in path:
        return fallback if fallback in CATEGORY_PRIORITY else "other"
    return fallback if fallback in CATEGORY_PRIORITY else "other"


def classify_print(text: str) -> str:
    """Best-effort category for a print-edition item from its headline/standfirst."""
    lowered = (text or "").lower()
    if re.search(r"\b(cricket|test match|odi|t20|ipl|hockey|football|olympic|sport)\b", lowered):
        return "sports"
    if re.search(
        r"\b(economy|economic|market|sensex|nifty|rbi|rupee|gdp|inflation|fiscal|budget|tax|tariff|trade|export|import|bank|insurance|msme|industry)\b",
        lowered,
    ):
        return "economy"
    if re.search(
        r"\b(united states|u\.s\.|china|pakistan|bangladesh|israel|iran|russia|ukraine|trump|putin|xi jinping|united nations|world|foreign minister|diplomat|trilateral)\b",
        lowered,
    ):
        return "international"
    if LOCAL_MARKER_RE.search(lowered):
        # Local/city reporting from the regional print pages — routine for this brief.
        return "other"
    if re.search(
        r"\b(supreme court|high court|parliament|lok sabha|rajya sabha|union government|prime minister|"
        r"chief minister|cabinet|election commission|assembly|constitution|bill|act)\b",
        lowered,
    ):
        return "national"
    if re.search(r"\b(government|minister|election|police)\b", lowered):
        return "national"
    return "other"


def is_topical(url: str) -> bool:
    path = (url or "").lower()
    return not any(hint in path for hint in SKIP_PATH_HINTS)


def tags_for(title: str, summary: str, category: str, *, print_edition: bool = False) -> list[str]:
    blob = f"{title} {summary}"
    tags: list[str] = []
    if print_edition:
        tags.append("Print edition")
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
    try:
        parsed = feedparser.parse(text.encode("utf-8", errors="replace"))
        return list(parsed.entries)
    except Exception:  # noqa: BLE001 - a feed must never crash the module
        return []


def _entry_media(entry) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = media.get("url") if isinstance(media, dict) else None
            if url:
                return abs_url(BASE, url)
    return None


def _entry_summary(entry) -> str:
    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            return _strip_html(value)
    return ""


def _strip_html(value: str) -> str:
    try:
        return soup(value).get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", str(value))


def _article_details(url: str, cache_ttl: int | None) -> dict:
    """Fetch an article page and extract og:description / published / image."""
    html = get(url, cache_ttl=cache_ttl, timeout=25, retries=2)
    page = soup(html)
    summary = clean_summary(meta(page, "og:description", "description", "twitter:description"))
    if len(summary) < 60:
        for para in page.select("p"):
            text = clean(para.get_text(" ", strip=True))
            if len(text) >= 80 and not text.lower().startswith(("read more", "also read", "advertisement")):
                summary = clean_summary(text)
                break
    published = meta(page, "article:published_time")
    image = meta(page, "og:image")
    return {"summary": summary, "published": parse_datetime(published), "image": image or None}


def _print_article_details(url: str, cache_ttl: int | None) -> dict:
    """Extract the print-edition article body (the todays-paper pages are not og:-tagged)."""
    html = get(url, cache_ttl=cache_ttl, timeout=25, retries=2)
    page = soup(html)
    paragraphs: list[str] = []
    body = page.select_one("div.articlebodycontent") or page.select_one("div.articlebody")
    if body is not None:
        for para in body.select("p"):
            text = clean(para.get_text(" ", strip=True))
            if len(text) < 60:
                continue
            if text.lower().startswith(("read more", "also read", "advertisement", "listen to this")):
                continue
            paragraphs.append(text)
            if sum(len(p.split()) for p in paragraphs) >= 55:
                break
    summary = clean_summary(" ".join(paragraphs))
    if len(summary) < 60:
        og = clean(meta(page, "og:description", "description"))
        if og and "print headlines" not in og.lower() and "todays paper" not in og.lower():
            summary = clean_summary(og)
    return {"summary": summary, "published": None, "image": None}


# --------------------------------------------------------------- print edition
def _print_edition_candidates(cache_ttl: int | None) -> tuple[list[dict], str]:
    """Scrape today's print-edition hub. Returns (candidates, status line)."""
    today = today_ist()
    try:
        html = get(PRINT_HUB, cache_ttl=cache_ttl, timeout=25, retries=2)
    except Exception as exc:  # noqa: BLE001 - print page is a bonus, never fatal
        return [], f"failed ({type(exc).__name__})"
    page = soup(html)
    candidates: list[dict] = []
    seen: set[str] = set()
    order = 0
    for element in page.select("div.element"):
        title_anchor = element.select_one("h2.title a, h3.title a, h2 a, h3 a")
        if title_anchor is None:
            continue
        url = canonical_url(abs_url(BASE, title_anchor.get("href")))
        match = PRINT_URL_RE.search(url)
        if not match or url in seen:
            continue
        edition_date = match.group(1)
        if edition_date != today:
            # The print hub has not rolled over to today's edition yet.
            continue
        title = clean_title(title_anchor.get_text(" ", strip=True))
        if len(title) < 12:
            continue
        seen.add(url)
        sub = element.select_one("div.sub-text a") or element.select_one("div.sub-text")
        raw_summary = _strip_html(sub.get_text(" ", strip=True)) if sub is not None else ""
        summary = clean_summary(raw_summary)
        picture = element.select_one("div.picture img")
        image = None
        if picture is not None:
            image = (
                picture.get("src")
                or picture.get("data-src")
                or (picture.get("data-srcset") or "").split(",")[0].strip().split(" ")[0]
                or None
            )
            if image:
                image = abs_url(BASE, image)
        heading = title_anchor.find_parent(["h2", "h3"])
        page_num_el = element.select_one("div.page-num")
        page_num_match = PAGE_NUM_RE.search(page_num_el.get_text(" ", strip=True)) if page_num_el else None
        page_no = int(page_num_match.group(1)) if page_num_match else 0
        candidate_category = classify_print(f"{title} {summary}")
        significant = candidate_category in ("national", "economy", "international")
        if order == 0:
            priority = 86 if significant else 72
        elif order == 1:
            priority = 80 if significant else 60
        elif order <= 3:
            priority = 74 if significant else 50
        else:
            priority = 66 if significant else 44
        candidates.append(
            {
                "url": url,
                "title": title,
                "summary": summary,
                "category": candidate_category,
                "image": image,
                "published": f"{edition_date}T06:00:00+05:30",
                "priority": priority,
                "order": order,
                "page_no": page_no,
                "heading": heading.name if heading is not None else "h3",
                "print_edition": True,
                "tags": [],
                "rss_summary": raw_summary,
            }
        )
        order += 1
    status = f"ok ({len(candidates)} today, edition {today})" if candidates else f"stale/empty (no {today} edition)"
    return candidates, status


# ----------------------------------------------------------------------- feeds
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
            if not ARTICLE_URL_RE.search(link) or not is_topical(link):
                continue
            candidates.append(
                {
                    "url": link,
                    "title": clean_title(entry.get("title")),
                    "category": classify(link, fallback),
                    "image": _entry_media(entry),
                    "published": parse_datetime(entry.get("published") or entry.get("updated")),
                    "print_edition": False,
                    "tags": [],
                    "rss_summary": _entry_summary(entry),
                }
            )
    return candidates, status

def _html_link_candidates(page_url: str, cache_ttl: int | None, *, label: str) -> tuple[list[dict], str]:
    """Fallback: scrape article links from an HTML page when RSS is stale/blocked."""
    try:
        html = get(page_url, cache_ttl=cache_ttl, timeout=25, retries=2)
    except Exception as exc:  # noqa: BLE001
        return [], f"{label}=failed ({type(exc).__name__})"
    page = soup(html)
    out: list[dict] = []
    seen: set[str] = set()
    for anchor in page.select("a[href]"):
        url = canonical_url(abs_url(BASE, anchor.get("href")))
        if not url or url in seen or not ARTICLE_URL_RE.search(url) or not is_topical(url):
            continue
        title = clean_title(anchor.get_text(" ", strip=True))
        if len(title) < 20:
            continue
        seen.add(url)
        out.append(
            {
                "url": url,
                "title": title,
                "category": classify(url, "other"),
                "image": None,
                "published": None,
                "print_edition": False,
                "tags": [],
                "rss_summary": "",
            }
        )
        if len(out) >= 20:
            break
    return out, f"{label}=ok ({len(out)})"


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


def score(candidate: dict) -> int:
    category = candidate["category"]
    title = candidate.get("title") or ""
    summary = candidate.get("rss_summary") or ""
    priority = int(candidate.get("priority") or CATEGORY_PRIORITY.get(category, 38))
    if not candidate.get("print_edition"):
        blob = f"{title} {summary}"
        for rx, bump in KEYWORD_BOOST:
            if rx.search(blob):
                priority += bump
                break
        if category == "editorial":
            priority = min(priority, 75)
        if category == "other":
            priority = min(priority, 45)
    return max(20, min(92, priority))


# ----------------------------------------------------------------------- fetch
def fetch(n: int = 15, config: dict | None = None) -> list[dict]:
    """Return up to ``n`` The Hindu ITEM dicts, highest priority first."""
    n = max(1, int(n or 15))
    config = config or {}
    cache_ttl = None if config.get("_no_cache") else int(config.get("cache_ttl", 1800))
    lookback = float((config.get("edition") or {}).get("lookback_hours", 30))

    status: dict[str, str] = {}
    print_candidates, print_status = _print_edition_candidates(cache_ttl)
    status["print"] = print_status
    candidates, feed_status = _feed_candidates(cache_ttl)
    status.update(feed_status)

    fresh = [c for c in candidates if _is_fresh(c.get("published"), lookback)]
    if len(fresh) < n:
        fresh = list(candidates)  # quiet news day: accept older items rather than ship an empty brief

    # HTML fallbacks: front page if RSS is thin, editorial page if the feed failed.
    if len(fresh) + len(print_candidates) < max(n * 2, 16):
        extra, line = _html_link_candidates(BASE + "/", cache_ttl, label="frontpage")
        status["frontpage"] = line
        fresh += extra
    if not any(c["category"] == "editorial" for c in fresh + print_candidates):
        extra, line = _html_link_candidates(EDITORIAL_PAGE, cache_ttl, label="editorial-page")
        status["editorial-page"] = line
        for cand in extra:
            cand["category"] = "editorial"
        fresh += extra

    # Print-edition candidates win ties (they carry the reader's print order).
    merged: dict[str, dict] = {}
    for cand in print_candidates + fresh:
        key = cand["url"]
        if key not in merged:
            merged[key] = dict(cand)
        else:
            existing = merged[key]
            merged_summary = existing.get("rss_summary") or cand.get("rss_summary") or ""
            existing.update({k: v for k, v in cand.items() if v not in (None, "", [])})
            existing["rss_summary"] = merged_summary
            existing["print_edition"] = existing["print_edition"] or cand["print_edition"]

    for cand in merged.values():
        cand["priority"] = score(cand)
    ordered = sorted(
        merged.values(),
        key=lambda c: (-c["priority"], c.get("order", 999) if c.get("print_edition") else 999),
    )

    # Hydrate missing/short summaries (print standfirsts are usually good already).
    shortlist = ordered[: max(n * 2, 22)]
    for cand in ordered:
        if cand["category"] == "editorial" and cand not in shortlist:
            shortlist.append(cand)

    def hydrate(cand: dict) -> dict:
        raw = cand.get("rss_summary") or ""
        summary = clean_summary(raw)
        published = cand.get("published")
        image = cand.get("image")
        if cand.get("print_edition"):
            if not summary or looks_incomplete(raw):
                try:
                    details = _print_article_details(cand["url"], cache_ttl)
                except Exception:  # noqa: BLE001 - one dead article never stops the run
                    details = {"summary": "", "published": None, "image": None}
                if len(details["summary"]) > len(summary):
                    summary = details["summary"]
        elif looks_incomplete(raw):
            try:
                details = _article_details(cand["url"], cache_ttl)
            except Exception:  # noqa: BLE001
                details = {"summary": "", "published": None, "image": None}
            if len(details["summary"]) > len(summary):
                summary = details["summary"]
            published = published or details["published"]
            image = image or details["image"]
        cand["summary"] = summary
        cand["published"] = published
        cand["image"] = image
        return cand

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(hydrate, cand) for cand in shortlist]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:  # noqa: BLE001
                continue

    items: list[dict] = []
    print_order: dict[str, int] = {}
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
                tags=tags_for(
                    title, summary, cand["category"], print_edition=bool(cand.get("print_edition"))
                ),
                image=cand.get("image"),
            )
        except Exception:  # noqa: BLE001 - malformed item is skipped, not fatal
            continue
        if cand.get("print_edition") and cand.get("order") is not None:
            print_order[item["id"]] = int(cand["order"])
        items.append(item)

    items.sort(key=lambda it: (-it["priority"], print_order.get(it["id"], 999)))
    items = dedupe(items)

    counts: dict[str, int] = {}
    selection: list[dict] = []
    for item in items:
        if len(selection) >= n:
            break
        category = item["category"]
        if counts.get(category, 0) >= CATEGORY_CAPS.get(category, 5):
            continue
        counts[category] = counts.get(category, 0) + 1
        selection.append(item)
    if len(selection) < n:
        chosen = {it["id"] for it in selection}
        selection += [it for it in items if it["id"] not in chosen][: n - len(selection)]

    if not any(it["category"] == "editorial" for it in selection):
        editorial_pool = [it for it in items if it["category"] == "editorial"]
        if editorial_pool:
            selection = selection[: max(0, n - 1)] + [editorial_pool[0]]

    LAST_FEED_STATUS.clear()
    LAST_FEED_STATUS.update(status)
    return selection


# ------------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch The Hindu stories for The Morning Brief")
    parser.add_argument("--out", default="out/probe/th.json", help="JSON output path")
    parser.add_argument("--n", type=int, default=15, help="number of items (default 15)")
    parser.add_argument("--no-cache", action="store_true", help="bypass the on-disk HTTP cache")
    args = parser.parse_args(argv)

    try:
        items = fetch(n=args.n, config={"_no_cache": args.no_cache})
    except Exception as exc:  # noqa: BLE001 - CLI must not traceback on the user
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parents[2] / out
    save_json(out, items)

    print(f"source: {SOURCE} | items: {len(items)} | out: {out}")
    if LAST_FEED_STATUS:
        print("feeds: " + ", ".join(f"{k}={v}" for k, v in LAST_FEED_STATUS.items()))
    print("priority | category      | title")
    for item in items:
        print(f"{item['priority']:>8} | {item['category']:<13} | {item['title']}")
    if not items:
        print("WARNING: no items produced (feeds reachable?)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
