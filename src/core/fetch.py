"""Shared HTTP + parsing helpers for the Morning Brief pipeline.

Every source module (src/sources/*.py) returns a ``list[dict]`` of ITEM dicts.
The canonical item shape and the edition (brief.json) shape are documented in
docs/ITEM_CONTRACT.md. Use ``make_item`` so every field is present and clean.
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True, parents=True)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

SESSION = requests.Session()
_IST = timezone(timedelta(hours=5, minutes=30))

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# --------------------------------------------------------------------------- time
def now_ist() -> datetime:
    """Current time in India Standard Time (aware datetime)."""
    return datetime.now(_IST)


def today_ist() -> str:
    """Edition date as YYYY-MM-DD in IST."""
    return now_ist().strftime("%Y-%m-%d")


def parse_datetime(value) -> str | None:
    """Best-effort ISO-8601 normaliser for feed/published values."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        try:
            from email.utils import parsedate_to_datetime

            dt = parsedate_to_datetime(text)
        except Exception:
            dt = None
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
                "%d %b %Y",
                "%B %d, %Y",
            ):
                try:
                    dt = datetime.strptime(text[: len(fmt) + 8], fmt)
                    break
                except Exception:
                    continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_IST)
    return dt.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- http
def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".txt")


def get(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    cache_ttl: int | None = None,
    headers: dict | None = None,
    allow_redirects: bool = True,
) -> str:
    """GET a URL and return text.

    ``cache_ttl`` (seconds) caches the response body on disk. Use it while
    developing so we never hammer the news sites: e.g. cache_ttl=1800.
    """
    cache_file = _cache_path(url)
    if cache_ttl is not None and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < cache_ttl:
            return cache_file.read_text(encoding="utf-8", errors="replace")

    last: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            h = dict(BASE_HEADERS)
            h["User-Agent"] = random.choice(USER_AGENTS)
            if headers:
                h.update(headers)
            resp = SESSION.get(url, headers=h, timeout=timeout, allow_redirects=allow_redirects)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code} for {url}")
            resp.raise_for_status()
            if not resp.encoding or resp.encoding.lower() in ("iso-8859-1",):
                resp.encoding = resp.apparent_encoding or "utf-8"
            text = resp.text
            if cache_ttl is not None:
                cache_file.write_text(text, encoding="utf-8")
            return text
        except Exception as exc:  # noqa: BLE001 - retry any transport error
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} attempts: {url} ({last})")


def get_json(url: str, **kwargs) -> dict | list:
    text = get(url, headers={"Accept": "application/json"}, **kwargs)
    return json.loads(text)


# ------------------------------------------------------------------------ parsing
def soup(text: str) -> BeautifulSoup:
    """Parse HTML markup. Uses lxml when available, stdlib parser otherwise."""
    try:
        return BeautifulSoup(text, "lxml")
    except Exception:
        return BeautifulSoup(text, "html.parser")


def clean(text: str | None, *, max_chars: int | None = None) -> str:
    """Normalise whitespace / entities and drop boilerplate tails."""
    if not text:
        return ""
    value = html_mod.unescape(str(text))
    value = value.replace("\u00a0", " ").replace("\u200b", "")
    value = re.sub(r"\s+", " ", value).strip()
    # boilerplate suffixes that pollute og:description fields
    value = re.sub(
        r"\s*(read more|read full (story|article)|continue reading|also read|click here to read)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\s*[-|–]\s*(LiveLaw|The Hindu|Indian Express)\s*$", "", value, flags=re.IGNORECASE)
    if max_chars and len(value) > max_chars:
        cut = value[:max_chars].rsplit(" ", 1)[0]
        value = cut.rstrip(",;:-") + "…"
    return value


def meta(page: BeautifulSoup, *names: str) -> str:
    """Return the first non-empty og:/name= meta value for ``names``."""
    for name in names:
        tag = page.find("meta", attrs={"property": name}) or page.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            value = clean(tag["content"])
            if value:
                return value
    return ""


def abs_url(base: str, href: str | None) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        from urllib.parse import urlparse

        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{href}"
    from urllib.parse import urlparse

    p = urlparse(base)
    return f"{p.scheme}://{p.netloc}/{href.lstrip('./')}"


def trim_sentences(text: str, max_words: int = 55) -> str:
    """Trim to whole sentences within ``max_words``; never cut mid-word."""
    text = clean(text)
    if not text:
        return ""
    words = text.split()
    if len(words) <= max_words:
        if text[-1] not in ".!?…\"'":
            text += "."
        return text
    out: list[str] = []
    count = 0
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sw = len(sentence.split())
        if count + sw > max_words and out:
            break
        out.append(sentence)
        count += sw
    result = " ".join(out).strip()
    if not result:
        result = " ".join(words[:max_words]).rstrip(",;:-") + "…"
    return result


def read_seconds(text: str, wpm: int = 200) -> int:
    words = len(clean(text).split())
    return max(20, int(words / wpm * 60))


def item_id(url: str) -> str:
    return hashlib.sha1(clean(url).encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------------- items
ITEM_DEFAULTS = {
    "source": "",
    "category": "national",
    "title": "",
    "url": "",
    "published": None,
    "summary": "",
    "bullets": [],
    "priority": 50,
    "tags": [],
    "image": None,
    "court": None,
    "case_name": None,
    "ratio": None,
    "read_seconds": 30,
    "body": "",  # full article text (<=14k chars) for the long-form script; not printed
}


def make_item(**kwargs) -> dict:
    """Build a validated ITEM dict (see docs/ITEM_CONTRACT.md).

    Accepts the documented fields; unknown keys are rejected so contract drift
    fails loudly instead of silently shipping broken items.
    """
    unknown = set(kwargs) - set(ITEM_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown item field(s): {sorted(unknown)}")
    item = dict(ITEM_DEFAULTS)
    item.update(kwargs)
    item["title"] = clean(item["title"])
    item["url"] = (item["url"] or "").strip()
    item["summary"] = trim_sentences(item.get("summary") or "", max_words=110)
    bullets = [clean(b) for b in (item.get("bullets") or [])]
    item["bullets"] = [b for b in bullets if b][:5]
    item["tags"] = [clean(t) for t in (item.get("tags") or []) if clean(t)][:6]
    item["body"] = clean(item.get("body") or "", max_chars=14000)
    item["priority"] = max(0, min(100, int(item.get("priority") or 50)))
    item["published"] = parse_datetime(item.get("published"))
    text = " ".join([item["title"], item["summary"], " ".join(item["bullets"])])
    item["read_seconds"] = read_seconds(text)
    item["id"] = item_id(item["url"]) if item["url"] else hashlib.sha1(
        item["title"].encode("utf-8")
    ).hexdigest()[:12]
    if not item["title"] or not item["url"]:
        raise ValueError(f"item needs title and url: {item['title']!r} {item['url']!r}")
    return item


def dedupe(items: list[dict]) -> list[dict]:
    """Drop duplicate items by URL, then by near-identical title."""
    seen_urls: set[str] = set()
    seen_titles: list[str] = []
    out: list[dict] = []
    for item in items:
        url = (item.get("url") or "").split("?")[0].rstrip("/")
        if url in seen_urls:
            continue
        t = re.sub(r"[^a-z0-9 ]", "", (item.get("title") or "").lower())
        if any(_title_similar(t, other) for other in seen_titles):
            continue
        seen_urls.add(url)
        seen_titles.append(t)
        out.append(item)
    return out


def _title_similar(a: str, b: str, threshold: float = 0.82) -> bool:
    if not a or not b:
        return False
    aw, bw = set(a.split()), set(b.split())
    if not aw or not bw:
        return False
    return len(aw & bw) / min(len(aw), len(bw)) >= threshold


def save_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
