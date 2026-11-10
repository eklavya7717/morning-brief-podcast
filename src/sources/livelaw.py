"""LiveLaw (livelaw.in) source for The Morning Brief.

LiveLaw is the legal engine of the brief: Supreme Court / High Court judgments,
new Acts & rules, collegium & appointment news and other exam-relevant
developments.  ``fetch(n=15)`` returns a list of ITEM dicts (see
docs/ITEM_CONTRACT.md) sorted by priority (desc).

Where the data comes from
-------------------------
1. ``https://www.livelaw.in/news-sitemap-daily.xml`` -- LiveLaw's Google-News
   sitemap: the freshest ~48 h of articles with the exact publication time,
   the published headline, the keywords string (which very often carries the
   bench, the court and even the case name) and the lead image.  One small
   (~80 KB) request is a far better candidate source than scraping the 1 MB
   homepage, so this is the primary list.
2. The homepage plus the /top-stories and /sc-judgments section pages are read
   for *editorial ordering*: position on the page is an importance signal, and
   they are also a fallback candidate source if the sitemap is unavailable.
3. The top candidates are then enriched by fetching each article page for
   og:description (summary), article:published_time, og:image and the opening
   paragraphs (ratio / bench composition / statutory provision bullets).

Robustness rules
----------------
* every per-article parse is wrapped in try/except and a failed fetch simply
  falls back to sitemap/list-page metadata,
* items older than the configured lookback (default 30 h) are dropped unless
  that would leave fewer than 8 items,
* summaries/bullets have LiveLaw boilerplate ("[Read Order]", "Also Read",
  "Subscribe", "Follow us", ...) stripped,
* every dict is validated by ``fetch.make_item`` (unknown fields rejected).

CLI
---
    python -m src.sources.livelaw --out out/probe/livelaw.json --n 15
    python -m src.sources.livelaw --out out/probe/livelaw.json --no-cache
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from ..core.fetch import (
    ITEM_DEFAULTS,
    ROOT,
    abs_url,
    clean,
    get,
    make_item,
    meta,
    now_ist,
    parse_datetime,
    save_json,
    soup,
    trim_sentences,
)

BASE = "https://www.livelaw.in"
SITEMAP_URL = BASE + "/news-sitemap-daily.xml"
HOME_URL = BASE + "/"
# Section pages are only used for ordering / fallback (2 of the 3 listed here
# are fetched; /supreme-court is a spare if /top-stories ever breaks).
LISTING_URLS = (BASE + "/top-stories", BASE + "/sc-judgments")
CACHE_TTL = 1800  # 30 min disk cache while developing / repeated runs
DEFAULT_LOOKBACK_HOURS = 30.0
MIN_ITEMS = 8

_IST = timezone(timedelta(hours=5, minutes=30))
_ALLOWED_SECTIONS = ("top-stories", "supreme-court", "sc-judgments", "high-court", "news-updates")


# --------------------------------------------------------------------------- URLs
def _url_key(url: str) -> str:
    """Canonical form used for de-duplication (drop query/fragment/slash)."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    if not parsed.scheme:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _path_parts(url: str) -> list[str]:
    return [part for part in urlparse(url).path.split("/") if part]


def _article_info(url: str) -> tuple[str, int] | None:
    """Return ``(section, article_id)`` for a LiveLaw article URL, else None."""
    parts = _path_parts(url)
    if len(parts) < 2 or parts[0] not in _ALLOWED_SECTIONS:
        return None
    last = parts[-1]
    if "-" not in last:
        return None
    tail = last.rsplit("-", 1)[1]
    if not tail.isdigit() or len(tail) < 5:
        return None
    return parts[0], int(tail)


# ------------------------------------------------------------------------ courts
# slug -> canonical court name.  Order/authored from the site's own High Court menu.
_HC_SLUGS: dict[str, str] = {
    "allahabad-high-court": "Allahabad High Court",
    "andhra-pradesh-high-court": "Andhra Pradesh High Court",
    "bombay-high-court": "Bombay High Court",
    "calcutta-high-court": "Calcutta High Court",
    "chattisgarh-high-court": "Chhattisgarh High Court",
    "chhattisgarh-high-court": "Chhattisgarh High Court",
    "delhi-high-court": "Delhi High Court",
    "gauhati-high-court": "Gauhati High Court",
    "gujarat-high-court": "Gujarat High Court",
    "himachal-pradesh-high-court": "Himachal Pradesh High Court",
    "jammu-kashmir": "Jammu & Kashmir and Ladakh High Court",
    "jammu-and-kashmir": "Jammu & Kashmir and Ladakh High Court",
    "jharkhand-high-court": "Jharkhand High Court",
    "karnataka-high-court": "Karnataka High Court",
    "kerala-high-court": "Kerala High Court",
    "madhya-pradesh-high-court": "Madhya Pradesh High Court",
    "madras-high-court": "Madras High Court",
    "manipur-high-court": "Manipur High Court",
    "meghalaya-high-court": "Meghalaya High Court",
    "orissa-high-court": "Orissa High Court",
    "odisha-high-court": "Orissa High Court",
    "patna-high-court": "Patna High Court",
    "punjab-and-haryana-high-court": "Punjab & Haryana High Court",
    "rajasthan-high-court": "Rajasthan High Court",
    "sikkim-high-court": "Sikkim High Court",
    "telangana-high-court": "Telangana High Court",
    "tripura-high-court": "Tripura High Court",
    "uttarakhand-high-court": "Uttarakhand High Court",
}

_TRIBUNALS = ("NCLAT", "NCLT", "ITAT", "CAT", "NCDRC", "DRT", "SAT", "Armed Forces Tribunal", "CESTAT", "TDSAT", "AFT")

# Alias regexes -> canonical court name.  Covers the spellings LiveLaw uses in
# headlines/keywords ("P&H High Court", "Punjab Haryana High Court", ...).
_COURT_ALIASES: list[tuple[re.Pattern, str]] = []


def _alias(pattern: str, canonical: str) -> None:
    _COURT_ALIASES.append((re.compile(pattern, re.I), canonical))


for _slug, _name in _HC_SLUGS.items():
    _flex = re.escape(_name)
    _flex = _flex.replace(r"\ ", r"[\s-]+").replace(r"\&", r"(?:&|and)")
    _alias(r"\b" + _flex + r"\b", _name)
_alias(r"\bpunjab\s*(?:&|and)?\s*haryana\s+high\s+court\b", "Punjab & Haryana High Court")
_alias(r"\bp\s*&\s*h\s+high\s+court\b", "Punjab & Haryana High Court")
_alias(r"\bj\s*&\s*k(?:\s*(?:&|and)\s*ladakh)?\s+high\s+court\b", "Jammu & Kashmir and Ladakh High Court")
_alias(r"\bjammu\s*(?:&|and)\s*kashmir\s*(?:&|and)\s*ladakh\s+high\s+court\b", "Jammu & Kashmir and Ladakh High Court")
_alias(r"\bodisha\s+high\s+court\b", "Orissa High Court")
_alias(r"\bmadurai\s+bench\s+of\s+(?:the\s+)?madras\s+high\s+court\b", "Madras High Court")
_alias(r"\bsupreme\s+court(?:\s+of\s+india)?\b", "Supreme Court")
_alias(r"\bconstitution\s+bench\b", "Supreme Court")
_alias(r"\bsc\b", "Supreme Court")  # only reached via the short "SC" headline prefix
_alias(r"\bnational\s+company\s+law\s+appellate\s+tribunal\b|\bnclat\b", "NCLAT")
_alias(r"\bnational\s+company\s+law\s+tribunal\b|\bnclt\b", "NCLT")
_alias(r"\bincome\s+tax\s+appellate\s+tribunal\b|\bitat\b", "ITAT")
_alias(r"\bcentral\s+administrative\s+tribunal\b|\bcat\b", "CAT")
_alias(r"\bnational\s+consumer\s+disputes\s+redressal\s+commission\b|\bncdrc\b", "NCDRC")
_alias(r"\bdebt\s+recovery\s+tribunal\b|\bdrt\b", "DRT")
_alias(r"\bsecurities\s+appellate\s+tribunal\b|\bsat\b", "SAT")
_alias(r"\barmed\s+forces\s+tribunal\b", "Armed Forces Tribunal")
_alias(r"\bdistrict\s+court\b", "District Court")
_alias(r"\bsessions\s+court\b", "Sessions Court")
_alias(r"\bfamily\s+court\b", "Family Court")
_alias(r"\b(kerala|delhi|mumbai|kolkata|chennai|bengaluru|hyderabad|patna|bhopal|jaipur|lucknow|chandigarh)\s+court\b", "Kerala Court")
_alias(r"\bstate\s+consumer\s+commission\b", "State Consumer Commission")
_alias(r"\bdistrict\s+consumer\s+commission\b", "District Consumer Commission")


def _court_from_url(url: str) -> str | None:
    parts = _path_parts(url)
    if not parts:
        return None
    section = parts[0]
    if section in ("supreme-court", "sc-judgments"):
        return "Supreme Court"
    if section == "high-court" and len(parts) > 1:
        slug = parts[1]
        if slug == "all-high-courts":
            return None
        if slug in _HC_SLUGS:
            return _HC_SLUGS[slug]
        if slug.endswith("-high-court"):
            human = slug[: -len("-high-court")].replace("-", " ").title()
            return f"{human} High Court"
    return None


def _court_from_text(text: str) -> str | None:
    if not text:
        return None
    for pattern, canonical in _COURT_ALIASES:
        if pattern.search(text):
            return canonical
    return None


def _detect_court(url: str, title: str, keywords: str, paragraphs: Sequence[str]) -> str | None:
    """Best-effort court detection: URL > headline > keywords > opening paras.

    The headline wins over keywords because LiveLaw keywords often mention the
    lower court whose order is under appeal while the story is a Supreme Court
    ruling.
    """
    court = _court_from_url(url)
    if court:
        return court
    for text in (title, keywords):
        court = _court_from_text(text)
        if court:
            return court
    for para in paragraphs[:2]:
        court = _court_from_text(para)
        if court:
            return court
    return None


# ------------------------------------------------------------------------ titles
_CITATION_PREFIX_RE = re.compile(r"^\s*\d{4}\s+live\s*law\s*\([A-Z]+\)\s*\d+\s*[|:]\s*", re.I)
_JUNK_TITLE_RE = re.compile(
    r"weekly round-?up|monthly digest|quarterly digest|yearly digest|daily highlights|"
    r"\btop\s+\d+\b|round-?up|digest|live\s*law diwali|diwali offer|subscribe|subscription|"
    r"vacancy|internship|placement|law school|moot|calls? for papers?|results|"
    r"webinar|conference|seminar|book review|podcast|video|interview series",
    re.I,
)


def _is_junk_title(title: str) -> bool:
    return bool(not title or _JUNK_TITLE_RE.search(title))


def _clean_title(title: str) -> str:
    value = clean(title)
    value = re.sub(r"\s*[|\u2013-]\s*(?:live\s*law|livelaw)\s*$", "", value, flags=re.I)
    value = re.sub(r"^(?:live\s*law|livelaw)\s*[:|\u2013-]\s*", "", value, flags=re.I)
    return value.strip(" .|-\u2013")


def _strip_citation(title: str) -> str:
    return _CITATION_PREFIX_RE.sub("", clean(title)).strip()


# --------------------------------------------------------------------- boilerplate
_BOILERPLATE_RE = re.compile(
    r"\[?\s*read\s+(?:the\s+)?(?:full\s+)?(?:order|judgment|judgement|petition|complaint|copy|report|notification)[^\]]*\]?"
    r"|(?:^|\b)also\s+(?:read|see)\b[^.]*"
    r"|(?:^|\b)(?:related|recommended)\s+(?:news|stories|reads)\b"
    r"|(?:^|\b)(?:don'?t|do\s+not)\s+miss\b"
    r"|(?:^|\b)must\s+read\b"
    r"|(?:^|\b)(?:watch|click\s+here|read\s+more|next\s+story|previous\s+story|share\s+this)\b[^.]*"
    r"|(?:^|\b)(?:subscribe|follow\s+us|follow\s+@\w+|download\s+the\s+livelaw\s+app|listen\s+to\s+this)\b[^.]*"
    r"|(?:^|\b)(?:liv\s*law\s+news\s+network|livelaw\s+news\s+network|twitter\s*:|@livelaw\w*)\b[^.]*",
    re.I,
)
_PARA_BOILERPLATE_START_RE = re.compile(
    r"^(?:also\s+(?:read|see)|read\s+(?:the\s+)?(?:full\s+)?(?:order|judgment|judgement)|subscribe|follow\s+us|"
    r"follow\s+@|watch\s*:|recommended|related\s+stories|next\s+story|previous\s+story|share\s+this|"
    r"listen\s+to\s+this|live\s*law\s+news\s+network|twitter\s*:|@livelaw)",
    re.I,
)


def _strip_boilerplate(text: str) -> str:
    """Remove LiveLaw furniture from summaries/bullets (never leaves artifacts)."""
    value = clean(text)
    if not value:
        return ""
    value = _BOILERPLATE_RE.sub(" ", value)
    value = re.sub(r"\s*[\[(]\s*(?:read\s+(?:the\s+)?(?:order|judgment|judgement))\s*[\])]\s*", " ", value, flags=re.I)
    # LiveLaw inline citation tags such as "[2026 LiveLaw (Mad) 538]".
    value = re.sub(r"\[\s*20\d\d\s+live\s*law\s*\([A-Za-z]+\)\s*\d+\s*\]", " ", value, flags=re.I)
    value = re.sub(r"\s*[|]\s*live\s*law.*$", "", value, flags=re.I)
    value = clean(value).strip(" ;:-\u2013|")
    value = re.sub(r"\.{2,}", ".", value)
    return value


def _text_tokens(text: str) -> set[str]:
    """Content tokens (>2 chars) for near-duplicate checks (same as rank.py)."""
    return {word for word in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(word) > 2}


def _near_duplicate(a: str, b: str, threshold: float = 0.8) -> bool:
    """True when ``a`` is essentially the same text as ``b``.

    Deliberately mirrors src/core/rank.py::_near_duplicate so the module never
    ships a summary/ratio that the edition would have to collapse later.
    """
    a_tokens, b_tokens = _text_tokens(a), _text_tokens(b)
    if len(a_tokens) < 4 or len(b_tokens) < 4:
        return False
    return len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens)) >= threshold


def _ends_cleanly(text: str) -> bool:
    """False for meta descriptions truncated mid-sentence ("... from his own")."""
    value = (text or "").strip()
    if not value or not re.search(r"[.!?][\"'\u201d\u2019)]?$", value):
        return False
    last_word = re.findall(r"[A-Za-z']+", value)
    if not last_word:
        return False
    return last_word[-1].lower() not in {
        "the", "of", "a", "an", "and", "or", "to", "in", "on", "for", "with", "that",
        "this", "is", "was", "were", "are", "by", "as", "at", "from", "not", "but",
    }


def _clip_words(text: str, max_words: int) -> str:
    """Clamp ``text`` to at most ``max_words`` words without cutting a word."""
    value = clean(text)
    words = value.split()
    if len(words) <= max_words:
        return value
    # Prefer cutting at a comma inside the budget so the bullet reads cleanly.
    head = " ".join(words[:max_words])
    commas = [m.start() for m in re.finditer(r",", head)]
    if commas and commas[-1] > len(head) * 0.45:
        return head[: commas[-1]].rstrip(" ,;:-") + "…"
    return head.rstrip(" ,;:-") + "…"


def _body_paragraphs(page) -> list[str]:
    """Opening paragraphs of a LiveLaw article (10-25 clean strings)."""
    body = page.find("div", class_="details-story-wrapper")
    if body is None:
        body = page.find("div", class_=re.compile(r"details-story|story-wrapper|article-content|entry-content", re.I))
    if body is None:
        return []
    paragraphs: list[str] = []
    for node in body.find_all(["p", "blockquote"]):
        text = _strip_boilerplate(node.get_text(" "))
        if not text or _PARA_BOILERPLATE_START_RE.match(text):
            continue
        if len(text.split()) < 4:
            continue
        paragraphs.append(text)
    return paragraphs


# --------------------------------------------------------------------- case names
_CONNECTORS = {
    "of", "and", "the", "in", "re", "for", "on", "state", "union", "or", "&", "anr", "ors",
    "vs", "v", "versus", "through", "thr", "another", "others", "and", "mr", "mrs", "ms",
}
_LEAD_STOP = {
    "in", "the", "case", "of", "by", "is", "was", "and", "or", "that", "this", "its", "their",
    "on", "for", "to", "a", "an", "as", "at", "from", "with", "under", "after", "before",
    "however", "but", "while", "when", "where", "which", "who", "court", "judge", "justices",
}
# Tokens that can only appear in the *middle* of a case title; a name starting
# with one of these was truncated by keyword slicing and must be rejected.
_BAD_NAME_START = {
    "thr", "through", "minor", "natural", "guardian", "son", "daughter", "wife", "husband",
    "mother", "father", "his", "her", "their", "and", "or", "of", "the", "in", "re", "by",
    "is", "was", "has", "had", "while", "whereas", "versus", "v", "vs", "anr", "ors",
}
_NAME_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Za-z0-9.'()&,\-]*$")


def _is_name_token(token: str) -> bool:
    if not token:
        return False
    bare = token.strip(",;:")
    if not bare:
        return False
    if bare.lower() in _CONNECTORS:
        return True
    return bool(_NAME_TOKEN_RE.match(bare))


def _expand_left(prefix: str) -> str:
    tokens = [t for t in prefix.split() if t]
    out: list[str] = []
    for token in reversed(tokens):
        if len(out) >= 12:
            break
        if not _is_name_token(token):
            break
        # A token ending in sentence punctuation ("Act.") must not leak into a name.
        if token.rstrip(".,;:").lower() in _LEAD_STOP and token[-1:] in ".,;:":
            break
        out.append(token.strip(",;:"))
    while out and out[-1].lower() in _LEAD_STOP:
        out.pop()
    return " ".join(reversed(out)).strip()


def _expand_right(suffix: str) -> str:
    tokens = [t for t in suffix.split() if t]
    out: list[str] = []
    for token in tokens:
        if len(out) >= 12:
            break
        bare = token.strip(",;:")
        if not bare:
            break
        low = bare.lower()
        # Sentence words ("has", "which", ...) end the party name; the lowercase
        # connectors that legitimately occur inside names ("State of X & Ors")
        # and capitalised name tokens ("Andhra") continue it.
        if low not in _CONNECTORS and not _NAME_TOKEN_RE.match(bare):
            break
        out.append(bare)
        if token[-1:] in (",", ";", ":", "."):
            break
    while out and out[-1].lower() in {"and", "or", "of", "the", "&"}:
        out.pop()
    return " ".join(out).strip()


def _norm_case_name(raw: str | None) -> str | None:
    """Validate/normalise ``X v. Y``; return None rather than a garbled name."""
    if not raw:
        return None
    value = _strip_citation(clean(raw)).strip(" .,;:-|\u2013")
    value = re.split(r"\s*[|]\s*", value)[0]
    # "X : Supreme Court" -> "X"
    value = re.split(r"\s*:\s*(?=(?:Supreme|High|District|Sessions|Family|NCLAT|NCLT|ITAT|CAT|NCDRC|Bombay|Delhi|Madras|Calcutta|Allahabad|Telangana|Gujarat|Kerala|Karnataka|Rajasthan|Patna|Jharkhand|Meghalaya|Sikkim|Punjab|Himachal|Uttarakhand|Tripura|Manipur|Gauhati|Andhra|Madhya|Orissa|Odisha)\b)", value)[0]
    value = re.sub(r"\s+[-\u2013]\s*live\s*law$", "", value, flags=re.I)
    value = re.sub(r"\s+(?:vs\.?|versus)\s+", " v. ", value, flags=re.I)
    value = re.sub(r"\s+v\s+", " v. ", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value or " v. " not in value or len(value) > 125:
        return None
    # "Justice V. Mohana" style false positives: a judge's initial is not a case separator.
    if re.search(r"\b(?:justice|cji|chief\s+justice|bench|comprising|judges?)\b", value, re.I):
        return None
    # CamelCase concatenation ("RetirementChhattisgarh") is the signature of the
    # buggy keywords field -- reject rather than ship a mangled case name.
    if re.search(r"[a-z][A-Z]", value):
        return None
    left, right = (part.strip() for part in value.split(" v. ", 1))
    if not left or not right or len(left) < 2 or len(right) < 2:
        return None
    if len(left.split()) > 12 or len(right.split()) > 12:
        return None
    if re.search(r"\b(?:high court|supreme court|district court|sessions court)\b", value, re.I):
        return None
    while left.split() and left.split()[0].lower() in _LEAD_STOP:
        left = left.split(" ", 1)[1] if " " in left else ""
    while right.split() and right.split()[0].lower() in _LEAD_STOP:
        right = right.split(" ", 1)[1] if " " in right else ""
    if len(left.split()) < 1 or len(right.split()) < 1:
        return None
    if left.split()[0].lower() in _BAD_NAME_START or left.split()[0].lower() in _LEAD_STOP:
        return None
    if right.split()[0].lower() in _BAD_NAME_START:
        return None
    return f"{left} v. {right}"


def _case_name_in(text: str) -> str | None:
    value = clean(text)
    if not value or " v" not in value.lower():
        return None
    for match in re.finditer(r"\s(?:v\.|vs\.|versus|v)\s", value, re.I):
        left = _expand_left(value[: match.start()])
        right = _expand_right(value[match.end():])
        if left and right:
            name = _norm_case_name(f"{left} v. {right}")
            if name:
                return name
    return None


def _case_name_from_keywords(keywords: str) -> str | None:
    if not keywords:
        return None
    for segment in re.split(r"[,;]", keywords):
        segment = clean(segment)
        if not re.search(r"\s(?:v\.|vs\.|versus|v)\s", segment, re.I):
            continue
        name = _case_name_in(segment)
        if name:
            return name
    return None


def _in_re_name(raw: str | None) -> str | None:
    """Suo-moto / 'In Re' titles have no 'v.' separator but are real case names."""
    value = _strip_citation(clean(raw or "")).strip(" .,;:-|\u2013")
    match = re.match(r"^(in\s+re:?)\s+(.{6,85})$", value, re.I)
    if not match:
        return None
    body = match.group(2).strip(" .,;:")
    body = re.sub(r"\s+(?:live\s*law|digest|round-?up)$", "", body, flags=re.I)
    if not body or len(body) > 85:
        return None
    return f"In Re: {body}"


def _detect_case_name(url: str, title: str, keywords: str, paragraphs: Sequence[str]) -> str | None:
    # 1) SC judgment citation titles: "2026 LiveLaw (SC) 1147 | Case v. Case"
    if "/sc-judgments/" in url and "|" in title:
        case_part = title.split("|", 1)[1]
        name = _norm_case_name(case_part) or _in_re_name(case_part)
        if name:
            return name
    # 2) the published headline itself
    name = _case_name_in(_strip_citation(title))
    if name:
        return name
    # 3) LiveLaw keywords -- cleanest source of party names
    name = _case_name_from_keywords(keywords)
    if name:
        return name
    # 4) opening paragraph ("... in X v. Y ...")
    for paragraph in paragraphs[:4]:
        name = _case_name_in(paragraph)
        if name:
            return name
    return None


# ------------------------------------------------------------------------ ratio
_COURTISH_RE = re.compile(
    r"\b(?:supreme court|high court|nclat|nclt|itat|cat|ncdrc|tribunal|commission|court)\b", re.I
)
_HELD_RE = re.compile(
    r"\b(?:held|holding|observ(?:es|ed|ing)|ruled|opined|clarified|noted|stated|said)\s+that\s+([^.;\"]{15,240})",
    re.I,
)


def _ratio_from_headline(title: str) -> str | None:
    """LiveLaw's headline convention: "<legal principle> : <Court>"."""
    value = _strip_citation(clean(title))
    if " | " in value:
        value = value.rsplit(" | ", 1)[-1]
    for match in re.finditer(r"\s*:\s*", value):
        left = value[: match.start()].strip()
        right = value[match.end():].strip()
        if ":" in right:
            continue
        if not _COURTISH_RE.search(right) or _COURTISH_RE.search(left):
            continue
        if re.search(r"\d{4}|live\s*law|digest|round-?up", left, re.I):
            continue
        words = left.split()
        if 4 <= len(words) <= 25:
            return left.rstrip(" ,;:-")
    return None


_NORMATIVE_RE = re.compile(
    r"\b(?:cannot|can'?t|must|shall|deemed|liable|entitled|obligation|required|only\s+if|"
    r"provided\s+that|is\s+to\s+be|are\s+to\s+be|no\s+(?:right|ground|bar|defect)|not\s+(?:liable|punitive|"
    r"stigmatic|maintainable|an\s+offence)|subject\s+to|duty\s+to|ought\s+to)\b",
    re.I,
)


def _ratio_from_paragraphs(paragraphs: Sequence[str]) -> str | None:
    for paragraph in paragraphs[:4]:
        match = _HELD_RE.search(paragraph)
        if not match:
            continue
        clause = clean(match.group(1)).strip(" ,;:-")
        words = clause.split()
        if len(words) < 5:
            continue
        # Only keep a body-derived ratio when it states a rule, not a narration
        # of facts ("The exercise would cover all undertrial prisoners...").
        if not _NORMATIVE_RE.search(clause):
            continue
        clause = _clip_words(clause, 25)
        clause = clause[0].upper() + clause[1:] if clause else clause
        return clause
    return None


def _detect_ratio(title: str, description: str, paragraphs: Sequence[str]) -> str | None:
    """One-line legal principle, or None.

    A ratio that merely repeats the headline is useless in the "case law you
    can quote" box (and rank.py would null it anyway), so such candidates are
    skipped and the article's holding paragraph is mined instead.
    """
    for candidate in (_ratio_from_headline(title), _ratio_from_headline(description)):
        if candidate and not _near_duplicate(candidate, title):
            return candidate
    body_ratio = _ratio_from_paragraphs(paragraphs)
    if body_ratio and not _near_duplicate(body_ratio, title):
        return body_ratio
    return None


# ---------------------------------------------------------------------- bullets
_JUSTICE_RE = re.compile(
    r"\b(?:Justice|CJI|Chief\s+Justice(?:\s+of\s+India)?)\s+"
    r"([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,3})"
)
_PROVISION_RE = re.compile(
    r"\b((?:Section|Sections|Article|Articles|Order|Rule|Rules|Regulation|Regulations)\s+"
    r"\d{1,4}[A-Za-z]?(?:\([0-9A-Za-z]{1,6}\))?"
    r"(?:\s+(?:of|under|read\s+with)\s+(?:the\s+)?[A-Z][A-Za-z0-9,&.\- ]{2,55}?"
    r"(?:Act|Code|Rules|Regulations|Constitution|Order)(?:\s*,\s*\d{4})?)?)",
)


def _bench_names(text: str) -> list[str]:
    names: list[str] = []
    keys: set[str] = set()
    for match in _JUSTICE_RE.finditer(text):
        name = clean(match.group(1))
        name = re.sub(r"\s+(?:and|or|of|the|in|on|to|for|with|while|who|which|has|had|was|were|is|are)\b.*$", "", name)
        name = name.rstrip(".,;:")
        # "JJ Munir" and "J.J. Munir" are the same judge: dedupe on a loose key.
        key = re.sub(r"[^a-z ]", "", name.lower()).replace(" ", "")
        if 2 <= len(name) <= 45 and key not in keys:
            keys.add(key)
            names.append(name)
    return names


def _bullets(paragraphs: Sequence[str], title_keywords: str) -> list[str]:
    """Up to 3 crisp bullets: bench composition, holding/direction, provision."""
    bullets: list[str] = []
    opening = paragraphs[:6]
    text = " ".join(opening) + " " + title_keywords

    names = _bench_names(text)
    if names:
        prefix = "Bench"
        if re.search(r"constitution\s+bench", text, re.I):
            prefix = "Constitution Bench"
        elif re.search(r"division\s+bench", text, re.I):
            prefix = "Division Bench"
        elif len(names) == 1 and re.search(r"single\s+judge", text, re.I):
            prefix = "Single Judge"
        shown = ", ".join(f"Justice {name}" for name in names[:3])
        bullets.append(_clip_words(f"{prefix}: {shown}", 18))

    provision = None
    for paragraph in opening:
        match = _PROVISION_RE.search(paragraph)
        if not match:
            continue
        candidate = clean(match.group(1)).strip(" ,;:-")
        # A bare "Sections 85" is not a useful bullet; require the statute too.
        if not re.search(r"\b(?:Act|Code|Constitution|Rules|Regulations|Order)\b", candidate, re.I):
            continue
        if 3 <= len(candidate.split()) <= 14:
            provision = candidate
            break
    if provision:
        bullets.append(_clip_words(f"Provision: {provision}", 18))

    held = 0
    for paragraph in paragraphs[:14]:
        match = re.search(
            r"\b(?:held|observed|ruled|opined|clarified|directed|ordered|noted|stated)\b[^.;]{10,150}",
            paragraph,
            re.I,
        )
        if not match:
            continue
        candidate = clean(match.group(0)).strip(" ,;:-")
        if len(candidate.split()) < 5:
            continue
        bullet = _clip_words(candidate[0].upper() + candidate[1:], 26)
        if bullet and bullet not in bullets:
            bullets.append(bullet)
            held += 1
            if held == 3:  # exam-useful holdings, not just the first one
                break

    # De-duplicate while preserving order and the 18-word ceiling.
    out: list[str] = []
    for bullet in bullets:
        bullet = _clip_words(_strip_boilerplate(bullet), 26)
        if bullet and bullet not in out:
            out.append(bullet)
    return out[:5]


# ------------------------------------------------------------------- categories
_LEGISLATION_ACT_RE = re.compile(
    r"\b(?:act|bill|ordinance|rules|regulations|notification)\b", re.I
)
_LEGISLATION_VERB_RE = re.compile(
    r"\b(?:passes?|passed|notif(?:y|ied|ies)|introduc(?:e|es|ed|ing)|amend(?:s|ed|ing|ment)|"
    r"enact(?:s|ed|ing)?|comes?\s+into\s+force|approv(?:e|es|ed)|clear(?:s|ed)|"
    r"brings?\s+(?:in|out)|new\s+(?:act|law|rules|bill|ordinance)|fram(?:e|es|ed)\s+(?:rules|policy)|"
    r"rules?\s+(?:notified|amended|framed|eased|relaxed)|gazette)\b",
    re.I,
)
_JUDICIARY_RE = re.compile(
    r"\b(?:collegium|appoint(?:s|ed|ment|ments)?|elevat(?:e|ed|ion)|transfer(?:s|red|ring)?|"
    r"chief\s+justice\s+(?:appointed|takes|took|sworn)|sworn\s+in|bar\s+council|"
    r"ad hoc\s+judge|additional\s+judge|senior\s+advocate\s+designat|designat(?:e|ed|ion)\s+of\s+senior)\b",
    re.I,
)
_JUDGMENT_VERB_RE = re.compile(
    r"\b(?:upholds?|upheld|strikes?\s+down|struck\s+down|sets?\s+aside|set\s+aside|quashes|quashed|"
    r"dismisses?|dismissed|holds?|held|rules?|ruled|declares?|declared|directs|directed|orders?|ordered|"
    r"observes?|observed|rejects?|rejected|sentences?|convicts?|acquits?|restrains?|awards?|"
    r"cannot|can'?t|entitled|liable|duty\s+to|no\s+(?:right|ground|bar|defect)|"
    r"not\s+(?:punitive|stigmatic|liable|an\s+offence|maintainable)|subject\s+to|must\s+be|shall\s+be)\b",
    re.I,
)
_ROUTINE_RE = re.compile(
    r"seeks?\s+(?:a\s+)?(?:response|reply|explanation)|seek(?:s|ing)?\s+(?:response|reply)|"
    r"issues?\s+notice|notice\s+(?:issued|to)|asks?\s+[^.]{0,45}(?:respond|reply|explain)|"
    r"lists?\s+[^.]{0,35}for|adjourns?|reserves?\s+(?:order|judgment)|"
    r"grants?\s+(?:interim\s+|anticipatory\s+|regular\s+|pre-?arrest\s+)?bail|"
    r"denies?\s+(?:interim\s+|anticipatory\s+|regular\s+|pre-?arrest\s+)?bail|"
    r"refuses?\s+(?:interim\s+|anticipatory\s+|regular\s+|pre-?arrest\s+)?bail|anticipatory\s+bail|"
    r"extends?\s+(?:interim\s+|protection)|withdraws?\s+(?:plea|petition)|challenges?\b|"
    r"plea\s+(?:filed|seeks|challeng)|pil\s+(?:filed|in|seeks|challeng)|cognizance|"
    r"permits?\s+[^.]{0,35}to\s+(?:file|withdraw)|allows?\s+[^.]{0,35}to\s+(?:file|withdraw)|"
    r"disposes?\s+of|transfers?\s+[^.]{0,25}(?:case|petition)",
    re.I,
)

_PRIORITY_RANGES = {
    "constitution": (90, 100),
    "sc_judgment": (75, 78),
    "sc_order": (65, 80),
    "legislation": (70, 85),
    "hc_judgment": (55, 60),
    "hc_news": (45, 52),
    "judiciary": (40, 60),
    "tribunal": (45, 65),
    "routine": (25, 45),
    "other": (30, 50),
}
_PRIORITY_BASE = {
    "constitution": 92,
    "sc_judgment": 75,
    "sc_order": 72,
    "legislation": 75,
    "hc_judgment": 57,
    "hc_news": 47,
    "judiciary": 48,
    "tribunal": 52,
    "routine": 36,
    "other": 38,
}


def _category(url: str, title: str, court: str | None) -> str:
    parts = _path_parts(url)
    section = parts[0] if parts else ""
    title_l = title.lower()
    if _LEGISLATION_ACT_RE.search(title) and _LEGISLATION_VERB_RE.search(title):
        return "legislation"
    if _JUDICIARY_RE.search(title):
        return "judiciary"
    if court in _TRIBUNALS:
        return "other"
    if section in ("sc-judgments", "supreme-court") or court == "Supreme Court":
        return "supreme-court"
    if section == "high-court" or (court and court.endswith("High Court")):
        return "high-court"
    return "other"


def _looks_judgment(url: str, title: str, keywords: str) -> bool:
    if "/sc-judgments/" in url:
        return True
    combined = f"{title} {keywords}"
    if re.search(r"\s(?:v\.|vs\.|versus|v)\s", combined, re.I):
        return True
    return bool(_JUDGMENT_VERB_RE.search(title))


def _klass(category: str, court: str | None, url: str, title: str, keywords: str, description: str) -> str:
    text = f"{title} {description} {keywords}".lower()
    if re.search(
        r"constitution\s+bench|\b(?:five|5|seven|7|nine|9|eleven|11)[-\s]judge|larger\s+bench|"
        r"\blandmark\b|\bhistoric\s+(?:judgment|verdict|ruling)\b",
        text,
    ):
        return "constitution"
    if category == "legislation":
        return "legislation"
    if category == "judiciary":
        return "judiciary"
    routine = bool(_ROUTINE_RE.search(f"{title} {keywords}") or _ROUTINE_RE.search(description or ""))
    if court in _TRIBUNALS:
        return "routine" if routine else "tribunal"
    if category == "supreme-court":
        if routine:
            return "routine"
        return "sc_judgment" if _looks_judgment(url, title, keywords) else "sc_order"
    if category == "high-court":
        if routine:
            return "routine"
        return "hc_judgment" if _looks_judgment(url, title, keywords) else "hc_news"
    return "routine" if routine else "other"


def _to_dt(value) -> datetime | None:
    iso = parse_datetime(value)
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_IST)
    return parsed


def _priority(klass: str, url: str, court: str | None, published, position: int | None) -> int:
    low, high = _PRIORITY_RANGES.get(klass, (30, 50))
    value = _PRIORITY_BASE.get(klass, 38)
    if klass == "routine" and court == "Supreme Court":
        value = 42
    if klass == "sc_judgment" and position == 0:
        # The page lead of the morning: an ordinary SC judgment may sit in the
        # "important/landmark" band when it is the edition's top story.
        low, high, value = 75, 90, 84
    published_dt = _to_dt(published)
    if published_dt is not None:
        age_hours = (now_ist() - published_dt).total_seconds() / 3600.0
        if age_hours <= 2:
            value += 3
        elif age_hours <= 6:
            value += 2
        elif age_hours <= 12:
            value += 1
    if position is not None:
        if position <= 2:
            value += 5
        elif position <= 5:
            value += 3
        elif position <= 10:
            value += 2
        elif position <= 15:
            value += 1
    if "/sc-judgments/" in url:
        value += 2
    return max(low, min(high, value))


# ------------------------------------------------------------------------- tags
_TAG_SKIP_RE = re.compile(r"justice|cji|high court|supreme court|tribunal|commission|digest", re.I)


def _tags(court: str | None, title: str, keywords: str, klass: str, case_name: str | None, url: str) -> list[str]:
    tags: list[str] = []
    if court:
        tags.append(court)
    if klass in ("sc_judgment", "hc_judgment", "tribunal"):
        tags.append("Judgment")
    elif klass == "legislation":
        tags.append("Legislation")
    elif klass == "judiciary":
        tags.append("Judiciary")
    elif klass == "constitution":
        tags.append("Constitution Bench")
    if "/sc-judgments/" in url:
        citation_match = re.match(r"(\d{4}\s+Live\s*Law\s*\(SC\)\s*\d+)", clean(title), re.I)
        if citation_match:
            tags.append(citation_match.group(1))
    for segment in re.split(r"[,;]", keywords or ""):
        segment = clean(segment)
        if not segment or len(segment.split()) > 4 or len(segment) > 34:
            continue
        if _TAG_SKIP_RE.search(segment) or (case_name and segment.lower() in case_name.lower()):
            continue
        if segment.lower() not in (tag.lower() for tag in tags):
            tags.append(segment)
        if len(tags) >= 5:
            break
    return tags[:6]


# ------------------------------------------------------------------- candidates
def _parse_sitemap(raw: str) -> list[dict]:
    """Parse LiveLaw's news sitemap with an ElementTree -> regex fallback."""
    entries: list[dict] = []
    namespaces = {
        "s": "http://www.sitemaps.org/schemas/sitemap/0.9",
        "news": "http://www.google.com/schemas/sitemap-news/0.9",
        "image": "http://www.google.com/schemas/sitemap-image/1.1",
    }
    try:
        root = ET.fromstring(raw.encode("utf-8"))
        for node in root.findall("s:url", namespaces):
            loc = (node.findtext("s:loc", default="", namespaces=namespaces) or "").strip()
            news = node.find("news:news", namespaces)
            published = news.findtext("news:publication_date", default="", namespaces=namespaces) if news is not None else ""
            title = news.findtext("news:title", default="", namespaces=namespaces) if news is not None else ""
            keywords = news.findtext("news:keywords", default="", namespaces=namespaces) if news is not None else ""
            image = node.findtext("image:image/image:loc", default="", namespaces=namespaces)
            entries.append({"url": loc, "published": published, "title": title, "keywords": keywords, "image": image})
    except ET.ParseError:
        entries = []
    if entries:
        return entries
    # Fallback: regex over <url> blocks (handles slightly malformed feeds).
    for block in re.findall(r"<url>(.*?)</url>", raw, re.S | re.I):
        loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.S)
        published = re.search(r"<news:publication_date>\s*(.*?)\s*</news:publication_date>", block, re.S)
        title = re.search(r"<news:title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</news:title>", block, re.S)
        keywords = re.search(
            r"<news:keywords>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</news:keywords>", block, re.S
        )
        image = re.search(r"<image:loc>\s*(.*?)\s*</image:loc>", block, re.S)
        entries.append(
            {
                "url": clean(loc.group(1)) if loc else "",
                "published": clean(published.group(1)) if published else "",
                "title": clean(title.group(1)) if title else "",
                "keywords": clean(keywords.group(1)) if keywords else "",
                "image": clean(image.group(1)) if image else "",
            }
        )
    return entries


def _sitemap_candidates(cache_ttl: int | None) -> dict[str, dict]:
    try:
        raw = get(SITEMAP_URL, cache_ttl=cache_ttl, timeout=30)
    except Exception:
        return {}
    candidates: dict[str, dict] = {}
    for entry in _parse_sitemap(raw):
        url = _url_key(entry["url"])
        if not _article_info(url):
            continue
        title = _clean_title(entry["title"])
        if _is_junk_title(title):
            continue
        candidates[url] = {
            "url": url,
            "title": title,
            "published": parse_datetime(entry["published"]),
            "keywords": entry["keywords"],
            "image": entry["image"] or None,
            "from_sitemap": True,
        }
    return candidates


def _listing_title(anchor) -> str:
    heading = anchor.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if heading is not None:
        value = clean(heading.get_text(" "))
        if value:
            return value
    tag = anchor.find("img", alt=True)
    if tag is not None and tag.get("alt"):
        value = clean(tag["alt"])
        if value:
            return value
    return clean(anchor.get_text(" "))


def _listing_pages(cache_ttl: int | None) -> tuple[dict[str, int], dict[str, dict]]:
    """Return (prominence ranking, fallback candidates) from homepage + sections."""
    positions: dict[str, int] = {}
    fallbacks: dict[str, dict] = {}
    for page_url in (HOME_URL,) + LISTING_URLS:
        try:
            page = soup(get(page_url, cache_ttl=cache_ttl, timeout=30))
        except Exception:
            continue
        root = page.find("main", id="main-content") or page.find("main") or page
        rank = 0
        for anchor in root.find_all("a", href=True):
            url = _url_key(abs_url(page_url, anchor["href"]))
            if not _article_info(url):
                continue
            title = _clean_title(_listing_title(anchor))
            if _is_junk_title(title):
                continue
            if url not in fallbacks:
                fallbacks[url] = {
                    "url": url,
                    "title": title,
                    "published": None,
                    "keywords": "",
                    "image": None,
                    "from_sitemap": False,
                }
            if url not in positions:
                positions[url] = rank
                rank += 1
    return positions, fallbacks


def _collect_candidates(cache_ttl: int | None) -> tuple[dict[str, dict], dict[str, int]]:
    """Sitemap-first candidates; page listings are ordering + fallback only.

    The sitemap is the authoritative fresh (~48 h) list, so listing-only URLs
    (which are typically older archive pages) are only used when the sitemap
    yields too few candidates to fill the brief.
    """
    positions, fallbacks = _listing_pages(cache_ttl)
    candidates = _sitemap_candidates(cache_ttl)
    if len(candidates) < MIN_ITEMS:
        for url, candidate in fallbacks.items():
            if url not in candidates:
                candidates[url] = candidate
            elif not candidates[url].get("title"):
                candidates[url]["title"] = candidate["title"]
    else:
        for url, candidate in fallbacks.items():
            if url in candidates and not candidates[url].get("title"):
                candidates[url]["title"] = candidate["title"]
    return candidates, positions


# ---------------------------------------------------------------------- enrich
def _page_data(url: str, cache_ttl: int | None) -> dict:
    """Fetch one article page and pull the fields the sitemap cannot give us."""
    page = soup(get(url, cache_ttl=cache_ttl, timeout=30))
    description = meta(page, "og:description", "description", "twitter:description")
    published = meta(page, "article:published_time", "article:modified_time")
    image = meta(page, "og:image", "twitter:image")
    headline = meta(page, "og:title", "twitter:title")
    paragraphs = _body_paragraphs(page)
    if not description and paragraphs:
        description = paragraphs[0]
    return {
        "description": _strip_boilerplate(description),
        "published": published or None,
        "image": image or None,
        "headline": _clean_title(headline) if headline else "",
        "paragraphs": paragraphs,
    }


_POOR_LEAD_RE = re.compile(
    r"^(?:with\s+this|with\s+that|further|furthermore|also|in\s+doing\s+so|it\s+(?:was|is)|meanwhile|"
    r"however|earlier|here|similarly|according\s+to|the\s+court\s+(?:further|also)|the\s+bench\s+(?:also|further)|"
    r"on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))",
    re.I,
)


_LEAD_SIGNAL_RE = re.compile(
    r"\b(?:held|observ(?:e|es|ed|ing)|ruled|directed|orders?|no\s+ground|cannot|can'?t|must|shall|liable|"
    r"entitled|duty|subject\s+to|dismissed|upheld|quashed|set\s+aside|clarified|reiterated|deemed|stated|noted)\b",
    re.I,
)
_NAMED_COURT_START_RE = re.compile(
    r"^(?:(?:The|A|An)\s+)?(?:"
    r"(?:[A-Z][A-Za-z&.\-]*(?:\s+[A-Z][A-Za-z&.\-]*){0,4})\s+High\s+Court"
    r"|Supreme\s+Court|NCLAT|NCLT|ITAT|CAT|NCDRC"
    r"|bench|division\s+bench|single\s+judge"
    r")\b"
)


def _summary_lead_score(text: str) -> int:
    """Favour a body paragraph that opens like a report and states the ruling."""
    value = clean(text or "")
    score = 0
    if _NAMED_COURT_START_RE.match(value):
        score += 3
    elif re.match(r"^The\s+court\b", value, re.I):
        score += 1
    if re.match(r"^[\"\u201c']", value):
        score -= 2  # a bare quotation is not a standalone summary
    score += min(2, len(_LEAD_SIGNAL_RE.findall(value)))
    return score


def _tidy_summary(text: str) -> str:
    """Drop truncated trailing fragments / unbalanced quotes left by clipping."""
    value = clean(text)
    value = re.sub(r"([.!?])(?=[A-Z\u201c\"'])", r"\1 ", value)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", value) if s.strip()]
    # A source-truncated tail ("... noting that the judicial.") leaves a short
    # dangling clause; drop it so the summary ends on a complete thought.
    if sentences and len(sentences[-1].split()) > 12:
        match = re.search(r",\s*([^,]{1,45})$", sentences[-1])
        if match and len(match.group(1).split()) < 5:
            head = sentences[-1][: match.start()].rstrip(" ,;:-")
            if len(head.split()) >= 6:
                sentences[-1] = head + "."
    while sentences:
        last = sentences[-1]
        unbalanced = last.count('"') % 2 == 1 or last.count("\u201c") != last.count("\u201d")
        if len(last.split()) < 6 or unbalanced:
            sentences.pop()
        else:
            break
    if not sentences:
        return value
    out = " ".join(sentences)
    if out[-1] not in ".!?\u2026":
        out += "."
    return out


def _is_good_summary_lead(text: str) -> bool:
    value = clean(text)
    if _POOR_LEAD_RE.match(value):
        return False
    # "...observed so while hearing the appeal" refers to the previous sentence
    # (the headline) and makes no sense as a standalone summary.
    if re.search(r"\b(?:observed|held|noted|said|stated|ruled)\s+so\b", value, re.I):
        return False
    return True


_NUM_DISTRICT_RE = re.compile(r"\b(\d{1,3})\s+(?:districts?)\b", re.I)


def _summary(
    title: str,
    description: str,
    paragraphs: Sequence[str],
    case_name: str | None,
    court: str | None = None,
    ratio: str | None = None,
) -> str:
    """A self-contained 1-2 sentence summary (<= 60 words, contract enforced later).

    Rule: the summary must never be a near-verbatim copy of the headline.  When
    og:description repeats the headline (premium SC-judgment pages do exactly
    this) we walk the article body for a lede; a mid-article continuation
    ("With this, the bench also...") is a last resort, and if there is no usable
    body text we compose a factual "Court judgment in X — the ruling: ..." line.
    """
    candidates: list[str] = []
    if description:
        candidates.append(description)
    if paragraphs:
        candidates.extend(paragraphs[:6])
    title_districts = _NUM_DISTRICT_RE.search(title)
    good_candidates: list[str] = []
    poor_candidates: list[str] = []
    for candidate in candidates:
        text = _strip_boilerplate(candidate)
        # The headline occasionally says "75 districts" while the copy says "55
        # districts" (UP has 75; NALSA's exercise covered 55). Never print an
        # internal contradiction in one item: fall through to a sentence without
        # the conflicting figure.
        candidate_districts = _NUM_DISTRICT_RE.search(text)
        if (
            title_districts
            and candidate_districts
            and candidate_districts.group(1) != title_districts.group(1)
        ):
            continue
        if len(text.split()) < 6 or _near_duplicate(text, title):
            # headline echoed back: a later sentence of the same paragraph often
            # carries the real substance ("The Court has stated that ...").
            sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 8]
            for sentence in sentences[1:]:
                if not _near_duplicate(sentence, title):
                    if _is_good_summary_lead(sentence):
                        good_candidates.append(sentence)
                    elif not _near_duplicate(sentence, title):
                        poor_candidates.append(sentence)
            continue
        if not _ends_cleanly(text):
            continue
        if _is_good_summary_lead(text):
            good_candidates.append(text)
        else:
            poor_candidates.append(text)
    tidied: list[str] = []
    for candidate in good_candidates:
        trimmed = _tidy_summary(trim_sentences(candidate, max_words=110))
        if len(trimmed.split()) >= 6 and not _near_duplicate(trimmed, title):
            tidied.append(trimmed)
    for candidate in tidied:
        if _summary_lead_score(candidate) >= 2:
            return candidate
    # No usable lede (premium/gated judgment page): compose a factual line
    # that does not simply echo the headline.
    parts: list[str] = []
    if case_name:
        parts.append(f"{court or 'Supreme Court'} judgment in {case_name.rstrip(' .')}.")
    if ratio and not _near_duplicate(ratio, title):
        parts.append(f"The ruling: {ratio.rstrip('.')}.")
    if parts:
        return trim_sentences(" ".join(parts), max_words=110)
    if tidied:
        return tidied[0]
    if poor_candidates:
        return _tidy_summary(trim_sentences(poor_candidates[0], max_words=110))
    # Last resort: the headline with the site tag stripped (still valid contract).
    return trim_sentences(_strip_boilerplate(title), max_words=110)


def _build_item(candidate: dict, cache_ttl: int | None, position: int | None) -> dict | None:
    url = candidate["url"]
    is_sc_citation = "/sc-judgments/" in url
    title = _clean_title(candidate.get("title") or "")
    sitemap_published = candidate.get("published")
    keywords = candidate.get("keywords") or ""
    image = candidate.get("image")

    data: dict = {}
    try:
        data = _page_data(url, cache_ttl)
    except Exception:
        data = {}
    paragraphs = data.get("paragraphs") or []
    description = data.get("description") or ""
    published = data.get("published") or sitemap_published
    image = data.get("image") or image
    headline = data.get("headline") or ""

    # --- title: SC-judgment citation pages use the real headline (og:description
    # is the working headline) and keep the case name for the case-law capsule.
    citation = ""
    if is_sc_citation and "|" in title:
        citation, case_part = (part.strip() for part in title.split("|", 1))
        citation = citation.strip()
        if description and _COURTISH_RE.search(description) and ":" in description:
            title = description
        elif case_part:
            title = case_part
    if not title:
        title = headline
    if not title:
        return None
    title = _clean_title(title)
    if not title:
        return None

    court = _detect_court(url, title, keywords, paragraphs)
    category = _category(url, title, court)
    case_name = _detect_case_name(url, candidate.get("title") or title, keywords, paragraphs)
    ratio = _detect_ratio(title, description, paragraphs)
    bullets = _bullets(paragraphs, f"{title} {keywords}")
    klass = _klass(category, court, url, title, keywords, description)
    priority = _priority(klass, url, court, published, position)
    summary = _summary(title, description, paragraphs, case_name, court, ratio)

    # Never let a field merely echo the headline (the edition's #1 reader
    # complaint). rank.py sanitises duplicates, but the module shouldn't ship
    # them in the first place.
    if ratio and _near_duplicate(ratio, title):
        ratio = None
    if case_name and _near_duplicate(case_name, title):
        case_name = None
    bullets = [bullet for bullet in bullets if not _near_duplicate(bullet, title)][:5]
    if not summary or _near_duplicate(summary, title):
        summary = _summary(title, "", paragraphs, case_name, court, ratio)
    if not summary:
        summary = title

    tags = _tags(court, title, keywords, klass, case_name, url)
    if citation and citation not in tags and len(tags) < 6:
        tags.append(citation)

    try:
        return make_item(
            source="livelaw",
            category=category,
            title=title,
            url=url,
            published=published,
            summary=summary,
            bullets=bullets,
            priority=priority,
            tags=tags,
            image=image,
            court=court,
            case_name=case_name,
            ratio=ratio,
        )
    except Exception:
        return None


# ------------------------------------------------------------------ merge/dedupe
def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", clean(title).lower())


def _similar_title(a: str, b: str, threshold: float = 0.82) -> bool:
    aw, bw = set(_title_key(a).split()), set(_title_key(b).split())
    if not aw or not bw:
        return False
    return len(aw & bw) / min(len(aw), len(bw)) >= threshold


def _case_quality(name: str | None) -> int:
    """Rough quality score so a citation-derived case name beats a sliced one."""
    if not name:
        return 0
    value = clean(name)
    if value.lower().startswith("in re"):
        return 3
    score = 1
    left = value.split(" v. ", 1)[0]
    if len(left.split()) >= 2:
        score += 1
    if re.search(r"\b(?:ors?|anr|another)\b\.?$", value, re.I):
        score += 1
    if left.split() and left.split()[0].lower() in _BAD_NAME_START:
        return 0
    return score


def _summary_is_composed(summary: str) -> bool:
    """True for the factual fallback line built when a gated page has no lede."""
    return bool(re.match(r"^(?:Supreme Court|High Court|NCLAT|NCLT|ITAT|CAT|NCDRC|District Court)\s+judgment in\s+", clean(summary or ""), re.I))


def _merge_and_dedupe(items: list[dict]) -> list[dict]:
    """Sort by priority then fold near-identical stories (SC citation vs top story)."""
    items = sorted(
        items,
        key=lambda item: (item.get("priority") or 0, item.get("published") or ""),
        reverse=True,
    )
    kept: list[dict] = []
    for item in items:
        duplicate = next((existing for existing in kept if _similar_title(existing["title"], item["title"])), None)
        if duplicate is None:
            kept.append(item)
            continue
        # Keep the richer headline, but never lose case_name/ratio/court/tags.
        for field in ("court", "image"):
            if not duplicate.get(field) and item.get(field):
                duplicate[field] = item[field]
        if _case_quality(item.get("case_name")) > _case_quality(duplicate.get("case_name")):
            duplicate["case_name"] = item["case_name"]
        # The SC-citation card only carries the headline, while the companion
        # top story carries the real lede and bench bullets: prefer the richer copy.
        duplicate_ratio = duplicate.get("ratio")
        if (not duplicate_ratio or _near_duplicate(duplicate_ratio, duplicate["title"])) and item.get("ratio"):
            if not _near_duplicate(item["ratio"], duplicate["title"]):
                duplicate["ratio"] = item["ratio"]
        item_summary = item.get("summary") or ""
        duplicate_summary = duplicate.get("summary") or ""
        item_summary_is_better = (
            _is_good_summary_lead(item_summary)
            and not _near_duplicate(item_summary, duplicate["title"])
            and _summary_lead_score(item_summary) >= 2
            # don't swap one composed fallback line for another (the citation
            # card carries the cleaner case name)
            and not (_summary_is_composed(item_summary) and _summary_is_composed(duplicate_summary))
        )
        if item_summary and item_summary_is_better and (
            not duplicate_summary
            or _near_duplicate(duplicate_summary, duplicate["title"])
            or _summary_is_composed(duplicate_summary)
            or not _is_good_summary_lead(duplicate_summary)
        ):
            duplicate["summary"] = item_summary
        if not duplicate.get("bullets") and item.get("bullets"):
            duplicate["bullets"] = item["bullets"]
        merged_tags = list(dict.fromkeys((duplicate.get("tags") or []) + (item.get("tags") or [])))[:6]
        duplicate["tags"] = merged_tags
        # A composed fallback can gain the real ruling once a companion article
        # supplied the body-derived ratio.
        ratio_final = duplicate.get("ratio")
        composed = duplicate.get("summary") or ""
        if (
            _summary_is_composed(composed)
            and ratio_final
            and "The ruling:" not in composed
            and not _near_duplicate(ratio_final, duplicate["title"])
        ):
            duplicate["summary"] = trim_sentences(
                f"{composed.rstrip('.')}. The ruling: {ratio_final.rstrip('.')}.", max_words=110
            )
    return kept


def _within_lookback(item: dict, lookback_hours: float) -> bool:
    published = _to_dt(item.get("published"))
    if published is None:
        return True
    return (now_ist() - published) <= timedelta(hours=lookback_hours)


# -------------------------------------------------------------------------- main
def _load_config(config: dict | None) -> dict:
    if config:
        return config
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _prelim_score(candidate: dict, config: dict) -> tuple[int, int]:
    title = candidate.get("title") or ""
    keywords = candidate.get("keywords") or ""
    published = candidate.get("published")
    court = _detect_court(candidate["url"], title, keywords, ())
    category = _category(candidate["url"], title, court)
    klass = _klass(category, court, candidate["url"], title, keywords, "")
    priority = _priority(klass, candidate["url"], court, published, candidate.get("_position"))
    if not candidate.get("from_sitemap"):
        priority -= 2
    article_id = _article_info(candidate["url"])
    recency_key = article_id[1] if article_id else 0
    return priority, recency_key


def _candidate_fresh(candidate: dict, lookback_hours: float) -> bool:
    published = _to_dt(candidate.get("published"))
    if published is None:
        return True  # listing fallback has no timestamp; trust the page position
    return (now_ist() - published) <= timedelta(hours=lookback_hours)


def _select_diverse(items: list[dict], n: int) -> list[dict]:
    """Priority-sorted pick that still leaves room for High Courts / new law.

    The priority rubric puts every Supreme Court judgment above every High
    Court one, so a naive top-N would be all SC and the legal page would lose
    the High Court judgments and legislation entirely. Cap the SC share at
    ~60% of the request and fill the rest from the best remaining items.
    """
    ordered = sorted(
        items,
        key=lambda item: (item.get("priority") or 0, item.get("published") or ""),
        reverse=True,
    )
    supreme = [item for item in ordered if item.get("category") == "supreme-court"]
    rest = [item for item in ordered if item.get("category") != "supreme-court"]
    sc_cap = max(1, min(len(supreme), int(round(n * 0.6))))
    chosen = supreme[:sc_cap] + rest[: max(0, n - sc_cap)]
    if len(chosen) < n:
        used = {item["url"] for item in chosen}
        for item in ordered:
            if item["url"] not in used:
                chosen.append(item)
            if len(chosen) >= n:
                break
    chosen.sort(key=lambda item: (item.get("priority") or 0, item.get("published") or ""), reverse=True)
    return chosen[:n]


def fetch(n: int = 15, config: dict | None = None, use_cache: bool = True) -> list[dict]:
    """Fetch the freshest, most important LiveLaw legal stories.

    Returns up to ``n`` ITEM dicts sorted by priority (desc) then recency.
    Never raises for per-article parse failures; returns ``[]`` only when the
    whole site is unreachable.
    """
    n = max(1, int(n or 15))
    cfg = _load_config(config)
    lookback = float((cfg.get("edition") or {}).get("lookback_hours", DEFAULT_LOOKBACK_HOURS) or DEFAULT_LOOKBACK_HOURS)
    cache_ttl = CACHE_TTL if use_cache else None

    candidates, positions = _collect_candidates(cache_ttl)
    if not candidates:
        return []
    for url, candidate in candidates.items():
        candidate["_position"] = positions.get(url)

    pool = list(candidates.values())
    fresh_pool = [candidate for candidate in pool if _candidate_fresh(candidate, lookback)]
    if len(fresh_pool) >= MIN_ITEMS:
        pool = fresh_pool
    ordered = sorted(pool, key=lambda cand: _prelim_score(cand, cfg), reverse=True)
    enrich_limit = min(len(ordered), max(18, n + 8), 26)
    items: list[dict] = []
    for candidate in ordered[:enrich_limit]:
        try:
            item = _build_item(candidate, cache_ttl, candidate.get("_position"))
        except Exception:
            item = None
        if item:
            items.append(item)

    items = _merge_and_dedupe(items)
    fresh = [item for item in items if _within_lookback(item, lookback)]
    selected = fresh if len(fresh) >= MIN_ITEMS else items
    chosen = _select_diverse(selected, n)
    # _merge_and_dedupe may enrich summary/bullets after make_item(); revalidate
    # so read_seconds/id stay contract-consistent.
    validated: list[dict] = []
    for item in chosen:
        try:
            validated.append(make_item(**{key: item.get(key) for key in ITEM_DEFAULTS}))
        except Exception:
            continue
    return validated


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch LiveLaw legal news for The Morning Brief")
    parser.add_argument("--out", default="out/probe/livelaw.json", help="output JSON path")
    parser.add_argument("--n", type=int, default=15, help="number of items to emit")
    parser.add_argument("--no-cache", action="store_true", help="bypass the 30-minute HTTP cache")
    args = parser.parse_args(argv)

    items = fetch(n=args.n, use_cache=not args.no_cache)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    save_json(out_path, items)

    print(f"livelaw: {len(items)} items -> {out_path}")
    print(f"{'priority':>8} | {'court':28} | title")
    print("-" * 100)
    for item in items:
        court = item.get("court") or "-"
        print(f"{item['priority']:>8} | {court[:28]:28} | {item['title'][:70]}")
    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
