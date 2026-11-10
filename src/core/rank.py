"""Editorial desk: turn raw source items into one ranked EDITION.

Input:  {source_name: [ITEM, ...]}
Output: EDITION dict (see docs/ITEM_CONTRACT.md) written to out/<date>/brief.json

The desk has three jobs: cross-source de-duplication, section allocation
(legal vs national vs editorial), and choosing the top story + judiciary capsule.
"""
from __future__ import annotations

import math
import re
from datetime import datetime

from . import fetch

LEGAL_CATEGORIES = {"supreme-court", "high-court", "judicial", "judiciary", "legislation"}

SECTION_TITLES = {
    "legal": "THE LEGAL LEDGER",
    "national": "THE NATION",
    "editorial": "OPINION & ANALYSIS",
}

# things that make a legal story exam-relevant for a judiciary aspirant
JUDICIARY_SIGNALS = [
    "constitution bench",
    "article 21",
    "article 14",
    "article 19",
    "article 32",
    "article 226",
    "landmark",
    "locus standi",
    "natural justice",
    "ultra vires",
    "read down",
    "struck down",
    "quashed",
    "criminal procedure",
    "bail",
    "reservation",
    "basic structure",
    "judicial review",
]

# stories the reader does not want in a law-and-nation brief
IRRELEVANT_PATTERNS = [
    "bollywood",
    "box office",
    "film review",
    "movie",
    "actress",
    "actor ",
    "celebration",
    "wedding",
    "divorce",
    "separation",
    "relationship",
    "memoir",
    "cricket",
    "ipl",
    "t20",
    "world cup",
    "match report",
    "olympic",
    "football",
    "tennis",
    "horoscope",
    "recipe",
    "fashion",
    "style",
    "viral",
    "instagram",
    "reels",
    "entertainment",
    "stock market live",
    "live updates",
    "petrol price",
    "gold rate",
    "kills ",
    "murder",
    "rape",
    "found dead",
    "suicide",
    "organs",
]

NATIONAL_SIGNALS = [
    "parliament",
    "cabinet",
    "bill",
    "ordinance",
    "gazette",
    "notification",
    "supreme court",
    "high court",
    "election",
    "constitution",
    "constitutional",
    "president",
    "chief justice",
    "rbi",
    "policy",
    "act",
    "budget",
    "inflation",
    "security",
    "treaty",
]


def _words(text: str) -> int:
    return len(fetch.clean(text).split())


def _recency_bonus(published: str | None) -> int:
    """Small boost for items published in the last 12 hours."""
    if not published:
        return 0
    try:
        dt = datetime.fromisoformat(published)
    except Exception:
        return 0
    age_h = (fetch.now_ist() - dt).total_seconds() / 3600
    if age_h < 0:
        return 0
    if age_h <= 12:
        return 4
    if age_h <= 24:
        return 2
    return 0


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}


def _near_duplicate(a: str, b: str, threshold: float = 0.8) -> bool:
    """True when `a` is essentially the same text as `b`."""
    ta, tb = _tokens(a), _tokens(b)
    if len(ta) < 4 or len(tb) < 4:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold


def sanitise_item(item: dict) -> dict:
    """Remove self-repetition inside one story.

    Sources sometimes emit the headline as the summary/ratio/bullet (LiveLaw's
    og:description often equals the headline). Repeating the headline in the
    body wastes the reader's five minutes, so collapse it instead.
    """
    title = item.get("title") or ""

    summary = item.get("summary") or ""
    bullets = list(item.get("bullets") or [])
    if summary and _near_duplicate(summary, title):
        # promote the first bullet that actually adds information
        replacement = next((b for b in bullets if not _near_duplicate(b, title)), "")
        item["summary"] = replacement
        if replacement:
            bullets = [b for b in bullets if b != replacement]

    item["bullets"] = [b for b in bullets if not _near_duplicate(b, title)][:5]

    # bullets that merely restate the summary/bullets above are noise
    if item["summary"]:
        seen_bullets: list[str] = []
        kept: list[str] = []
        for bullet in item["bullets"]:
            if _near_duplicate(bullet, item["summary"], threshold=0.7):
                continue
            if any(_near_duplicate(bullet, other, threshold=0.8) for other in seen_bullets):
                continue
            kept.append(bullet)
            seen_bullets.append(bullet)
        item["bullets"] = kept
    ratio = item.get("ratio") or ""
    if ratio and _near_duplicate(ratio, title):
        item["ratio"] = None

    # a case name that is just the headline is noise
    case = item.get("case_name") or ""
    if case and _near_duplicate(case, title):
        item["case_name"] = None
    return item


def is_irrelevant(item: dict) -> bool:
    """Drop entertainment/sport/lifestyle noise that leaks in from RSS feeds."""
    text = f"{item.get('title','')} {item.get('summary','')}".lower()
    if item.get("category") in {"sports", "entertainment"}:
        return True
    if item.get("source") in LEGAL_SOURCES or item.get("category") in LEGAL_CATEGORIES:
        return False  # "divorce", "relationship" etc. are family-law rulings here, not gossip
    return any(pattern in text for pattern in IRRELEVANT_PATTERNS)


def enhance_priority(item: dict) -> int:
    """Nudge base priority using category + text signals."""
    score = int(item.get("priority") or 50)
    text = f"{item.get('title','')} {item.get('summary','')}".lower()

    if any(sig in text for sig in JUDICIARY_SIGNALS):
        score += 3
    if any(sig in text for sig in NATIONAL_SIGNALS):
        score += 4
    if item.get("category") in LEGAL_CATEGORIES:
        if item.get("case_name"):
            score += 2
        if item.get("ratio"):
            score += 2
    else:
        # legal news leads the edition; everything else tops out below it
        score = min(score, 80)
    score += _recency_bonus(item.get("published"))
    return max(0, min(100, score))


LEGAL_SOURCES = {"livelaw", "sccblog", "scobserver", "barandbench"}


def _is_legal(item: dict) -> bool:
    return item.get("source") in LEGAL_SOURCES or item.get("category") in LEGAL_CATEGORIES


def _pretty_date(date_str: str) -> tuple[str, str]:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%A"), dt.strftime("%d %B %Y").lstrip("0")


def build_edition(
    source_items: dict[str, list[dict]],
    config: dict,
    date: str | None = None,
    *,
    stats: dict | None = None,
) -> dict:
    edition_cfg = config.get("edition", {}) or {}
    quotas: dict[str, int] = dict(
        edition_cfg.get("sections") or {"legal": 6, "national": 3, "editorial": 3}
    )
    max_items = int(edition_cfg.get("max_items", 12))
    date = date or fetch.today_ist()

    counts = {name: len(items or []) for name, items in source_items.items()}

    # 1. flatten + normalise + drop noise + cross-source de-duplicate
    flat: list[dict] = []
    dropped = 0
    for name, items in source_items.items():
        for raw in items or []:
            item = dict(raw)
            item.setdefault("source", name)
            if is_irrelevant(item):
                dropped += 1
                continue
            item = sanitise_item(item)
            item["priority"] = enhance_priority(item)
            flat.append(item)
    flat = fetch.dedupe(sorted(flat, key=lambda i: -i.get("priority", 0)))

    # 2. bucket into sections
    legal: list[dict] = []
    national: list[dict] = []
    editorial: list[dict] = []
    for item in flat:
        if item.get("category") == "editorial":
            editorial.append(item)
        elif _is_legal(item):
            legal.append(item)
        else:
            national.append(item)

    # 3. take quotas, then fill any shortfall from leftovers by priority
    selected: dict[str, list[dict]] = {}
    leftovers: list[dict] = []
    for slug, pool in (("legal", legal), ("national", national), ("editorial", editorial)):
        take = max(0, int(quotas.get(slug, 0)))
        chosen = pool[:take]
        selected[slug] = chosen
        leftovers.extend(pool[take:])

    used = sum(len(v) for v in selected.values())
    if used < max_items:
        leftovers.sort(key=lambda i: (-i.get("priority", 0), i.get("published") or ""))
        for item in leftovers:
            if used >= max_items:
                break
            slug = "editorial" if item.get("category") == "editorial" else ("legal" if _is_legal(item) else "national")
            selected[slug].append(item)
            used += 1

    for slug in selected:
        selected[slug].sort(key=lambda i: (-i.get("priority", 0), i.get("published") or ""))
        selected[slug] = fetch.dedupe(selected[slug])

    sections = [
        {"name": SECTION_TITLES[slug], "slug": slug, "items": selected.get(slug, [])}
        for slug in ("legal", "national", "editorial")
        if selected.get(slug)
    ]

    all_selected = [i for s in sections for i in s["items"]]

    # 4. top story: prefer the strongest legal story, else overall leader
    legal_selected = [i for i in all_selected if _is_legal(i)]
    top_pool = legal_selected or all_selected
    top_story = max(top_pool, key=lambda i: (i.get("priority", 0), _recency_bonus(i.get("published")))) if top_pool else None

    # 5. judiciary capsule: judgments with a case name or one-line ratio
    capsule = [
        i
        for i in legal_selected
        if i.get("case_name") or i.get("ratio")
    ]
    capsule = sorted(capsule, key=lambda i: -i.get("priority", 0))[:3]
    for item in capsule:
        if item.get("ratio"):
            item["ratio"] = fetch.trim_sentences(item["ratio"], max_words=30)

    # 6. read-time estimate
    words = 0
    for item in all_selected:
        words += _words(item.get("title", "")) + _words(item.get("summary", ""))
        words += sum(_words(b) for b in (item.get("bullets") or []))
    words += sum(_words(i.get("ratio") or "") for i in capsule)
    read_minutes = max(3, min(14, math.ceil(words / 220)))

    weekday, pretty = _pretty_date(date)
    edition = {
        "date": date,
        "weekday": weekday,
        "date_pretty": pretty,
        "generated_at": fetch.now_ist().isoformat(timespec="seconds"),
        "read_minutes": read_minutes,
        "sections": sections,
        "top_story": top_story,
        "judiciary_capsule": capsule,
        "stats": {
            "sources": counts,
            "candidates": len(flat),
            "dropped_noise": dropped,
            "selected": len(all_selected),
            "words": words,
            "generated_in_seconds": (stats or {}).get("generated_in_seconds"),
            **(stats or {}),
        },
    }
    return edition
