"""Extra legal sources with full article text: SCC Online blog, Supreme Court Observer, Bar & Bench.

Each returns ITEM dicts whose ``body`` holds the article text, so the long-form
script can explain the legal principle instead of echoing a headline.

* SCC Online blog : RSS carries the full text of case briefs and legislation updates.
* SCO             : its RSS is stale, so the newest ``journal-sitemap*.xml`` URLs are read.
* Bar & Bench     : RSS gives only titles; article pages are read, paywalled ones skipped.

LiveLaw round-ups/digests are read from its news sitemap.
Only text the sites serve openly is used; a paywalled page is dropped, not bypassed.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import feedparser

from src.core.fetch import clean, get, make_item, now_ist, soup

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"}
MAX_AGE_H = 72  # these sites post less often than LiveLaw


def _text(html: str) -> str:
    return clean(re.sub(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", " ", html, flags=re.S))


def _category(tags: list[str], title: str) -> str:
    blob = (" ".join(tags) + " " + title).lower()
    if any(k in " ".join(tags).lower() for k in ("legislation updates", "notifications")) or re.search(r"(notifies|enacts|ordinance|bill passed)", title.lower()):
        return "legislation"
    if "high court" in blob:
        return "high-court"
    if "supreme court" in blob or "constitution" in blob:
        return "supreme-court"
    return "judiciary"


def _priority(category: str, source: str) -> int:
    base = {"supreme-court": 74, "legislation": 68, "high-court": 62, "judiciary": 55}[category]
    return base + (4 if source == "scobserver" else 0)  # SCO pieces are analysis, not just news


def _lead(body: str, words: int = 55) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", body)
    return " ".join(" ".join(sentences[:3]).split()[:words])


def _recent(published: datetime | None) -> bool:
    return published is None or now_ist() - published <= timedelta(hours=MAX_AGE_H)


def _item(source: str, title: str, url: str, tags: list[str], body: str, published: datetime | None) -> dict | None:
    body = clean(body)
    if len(body.split()) < 120 or not _recent(published):
        return None
    cat = _category(tags, title)
    return make_item(
        source=source, category=cat, title=clean(title), url=url, summary=_lead(body),
        priority=_priority(cat, source), tags=tags[:5], body=body,
        published=published.isoformat() if published else None,
    )


def _dt(entry) -> datetime | None:
    parsed = entry.get("published_parsed")
    return datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None


def sccblog(n: int = 10) -> list[dict]:
    feed = feedparser.parse("https://www.scconline.com/blog/feed/", request_headers=UA)
    skip = ("law school news", "moot", "internship", "call for", "job", "vacancy", "events")
    out = []
    for e in feed.entries:
        tags = [t.term for t in e.get("tags", [])]
        if any(s in " ".join(tags).lower() for s in skip):
            continue
        body = _text((e.get("content") or [{}])[0].get("value") or e.get("summary", ""))
        it = _item("sccblog", e.title, e.link, tags, body, _dt(e))
        if it:
            out.append(it)
    return out[:n]


def _article_paragraphs(url: str) -> tuple[str, str]:
    page = soup(get(url, cache_ttl=1800, timeout=30))
    holder = page.find("article") or page.find("main") or page
    paras = [clean(p.get_text(" ")) for p in holder.find_all("p")]
    paras = [p for p in paras if len(p.split()) >= 8 and not re.search(r"subscribe|sign in|follow us|©|all rights reserved", p, re.I)]
    title = clean((page.find("meta", property="og:title") or {}).get("content", "")) or clean(page.title.get_text()) if page.title else ""
    return title, " ".join(paras)


def scobserver(n: int = 5) -> list[dict]:
    xml = ""
    for sm in ("journal-sitemap3.xml", "journal-sitemap2.xml"):
        xml += get(f"https://www.scobserver.in/{sm}", cache_ttl=1800)
    entries = re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", xml)
    entries.sort(key=lambda e: e[1], reverse=True)
    out = []
    for url, lastmod in entries[:12]:
        if "/journal/sco-lr-" in url or "volume-" in url:  # journal issues, not articles
            continue
        try:
            title, body = _article_paragraphs(url)
            it = _item("scobserver", title, url, ["Supreme Court Observer"], body, datetime.fromisoformat(lastmod.replace("Z", "+00:00")))
        except Exception:  # noqa: BLE001 - one bad page must not drop the source
            it = None
        if it:
            out.append(it)
        if len(out) == n:
            break
    return out


def barandbench(n: int = 6) -> list[dict]:
    feed = feedparser.parse("https://www.barandbench.com/feed", request_headers=UA)
    out = []
    for e in feed.entries:
        try:
            _, body = _article_paragraphs(e.link)
            it = _item("barandbench", e.title, e.link, [t.term for t in e.get("tags", [])], body, _dt(e))
        except Exception:  # noqa: BLE001
            it = None
        if it:
            out.append(it)
    return out[:n]


def livelaw_digests(n: int = 3) -> list[dict]:
    """LiveLaw weekly round-ups and monthly/quarterly digests, found via its daily news sitemap."""
    sm = get("https://www.livelaw.in/news-sitemap-daily.xml", cache_ttl=1800)
    urls = [u for u in re.findall(r"<loc>([^<]+)</loc>", sm) if re.search(r"round-?ups?|digest", u) and "/amp/" not in u]
    out = []
    for url in urls[:8]:
        try:
            title, body = _article_paragraphs(url)
            body = re.sub(r"Your support helps us[^.!]*[.!]*(\s*!+)?(\s*All payment options available)?", " ", body)
            it = _item("livelaw", title, url, ["LiveLaw digest"], body, None)
        except Exception:  # noqa: BLE001
            it = None
        if it:
            it["category"] = "high-court" if "/high-court/" in url or "high court" in title.lower() else "supreme-court"
            it["priority"] = 64
            out.append(it)
        if len(out) == n:
            break
    return out


def fetch(n: int = 20, config: dict | None = None) -> list[dict]:
    items, errors = [], []
    for fn in (sccblog, scobserver, barandbench, livelaw_digests):
        try:
            items += fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {type(exc).__name__}: {exc}")
    if errors and not items:
        raise RuntimeError("; ".join(errors))
    return items[:n]


if __name__ == "__main__":
    import sys

    for it in fetch():
        print(it["source"], it["category"], it["priority"], len(it["body"].split()), "w |", it["title"][:80])
    sys.exit(0)
