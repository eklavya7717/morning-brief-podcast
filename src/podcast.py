"""Private podcast feed builder for The Morning Brief.

``build_feed(out_root, config) -> Path`` scans ``out/YYYY-MM-DD/brief.mp3``
editions and writes ``out/feed.xml``: an Apple-Podcasts-compatible private
RSS feed (channel title "The Morning Brief", author "Eklav", language
``en-IN``). Each item points at the MP3 through ``config.delivery.podcast``
``.base_url`` plus the edition-relative path, with guid, RFC-822 pubDate,
``itunes:duration`` and a description built from that edition's headlines.

When no ``base_url`` is configured the feed is still valid XML: it uses the
placeholder ``https://example.invalid/`` and prints a clear warning so hosting
can be wired later without breaking the pipeline.

CLI::

    python -m src.podcast --out out
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))

FEED_FILENAME = "feed.xml"
MAX_ITEMS = 30
PLACEHOLDER_BASE = "https://example.invalid/"
DEFAULT_TIME = (6, 30)  # edition is published at 06:30 IST

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)

CHANNEL_TITLE = "Eklavya's Morning Brief"
CHANNEL_DESCRIPTION = (
    "A five to six minute daily audio digest of Supreme Court and High Court judgments, "
    "new laws, national news and editorials, for law students and judiciary aspirants in India."
)
CHANNEL_AUTHOR = "Eklavya"


# --------------------------------------------------------------------- helpers
def _now_ist() -> datetime:
    return datetime.now(IST)


def _rfc822(dt: datetime) -> str:
    """RFC-822 / RSS 2.0 date, e.g. 'Mon, 10 Nov 2026 06:30:00 +0530'."""
    return dt.astimezone(IST).strftime("%a, %d %b %Y %H:%M:%S %z")


def _fmt_duration(seconds: float) -> str:
    total = max(0, int(round(float(seconds or 0))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _podcast_config(config: dict | None) -> dict:
    delivery = (config or {}).get("delivery") or {}
    return dict(delivery.get("podcast") or {})


def _edition_date(directory_name: str) -> datetime | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", directory_name or ""):
        return None
    try:
        day = datetime.strptime(directory_name, "%Y-%m-%d")
    except ValueError:
        return None
    return day.replace(
        hour=DEFAULT_TIME[0], minute=DEFAULT_TIME[1], tzinfo=IST
    )


def _edition_items(edition: dict | None) -> list[str]:
    """Headlines for the show notes, most important first."""
    if not isinstance(edition, dict):
        return []
    titles: list[str] = []
    top = edition.get("top_story") or {}
    if top.get("title"):
        titles.append(str(top["title"]).strip())
    for section in edition.get("sections") or []:
        for item in (section.get("items") or []) if isinstance(section, dict) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if title and title not in titles:
                titles.append(title)
    return titles


def _description(edition: dict | None, date: datetime) -> tuple[str, str]:
    """(item description, itunes summary) for one edition."""
    titles = _edition_items(edition)
    day = date.strftime("%A, %d %B %Y")
    if not titles:
        return (
            f"The Morning Brief for {day}. Five minutes of the law, the courts and the country.",
            f"The Morning Brief for {day}. Five minutes of the law, the courts and the country.",
        )
    shown = titles[:6]
    body = "; ".join(shown)
    if len(titles) > len(shown):
        body += f"; and {len(titles) - len(shown)} more stories"
    description = f"The Morning Brief for {day}. In this edition: {body}."
    return description, description


def _edition_duration(mp3: Path) -> float:
    meta = _load_json(mp3.parent / (mp3.name + ".meta.json")) or {}
    try:
        duration = float(meta.get("duration_s") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration > 0:
        return duration
    # 64 kbps mono MP3 => 8000 bytes per second
    try:
        size = mp3.stat().st_size
    except OSError:
        return 0.0
    return size / 8000.0


def _edition_weekday(edition: dict | None, date: datetime) -> str:
    weekday = ""
    if isinstance(edition, dict):
        weekday = str(edition.get("weekday") or "").strip()
    return weekday or date.strftime("%A")


LEDGER = "episodes.json"


def _ledger(out_root: Path) -> dict:
    return _load_json(Path(out_root) / LEDGER) or {}


def collect_episodes(out_root: Path, limit: int = MAX_ITEMS) -> list[dict]:
    """Newest-first list of editions: local mp3 files plus the ledger (audio hosted elsewhere)."""
    out_root = Path(out_root)
    if not out_root.exists():
        return []
    episodes: list[dict] = []
    for date_str, e in _ledger(out_root).items():
        date = _edition_date(date_str)
        if date and not (out_root / date_str / "brief.mp3").exists():
            episodes.append({**e, "date": date, "date_str": date_str, "mp3": None,
                             "title": f"{CHANNEL_TITLE}, {date.strftime('%A %d %B %Y')}"})
    for mp3 in out_root.glob("*/brief.mp3"):
        date = _edition_date(mp3.parent.name)
        if date is None or not mp3.is_file():
            continue
        edition = _load_json(mp3.parent / "brief.json")
        description, summary = _description(edition, date)
        episodes.append(
            {
                "date": date,
                "date_str": mp3.parent.name,
                "mp3": mp3,
                "edition": edition,
                "title": (
                    f"{CHANNEL_TITLE}, {_edition_weekday(edition, date)} "
                    f"{date.strftime('%d %B %Y')}"
                ),
                "description": description,
                "summary": summary,
                "duration_s": _edition_duration(mp3),
                "size_bytes": mp3.stat().st_size,
            }
        )
    episodes.sort(key=lambda episode: episode["date"], reverse=True)
    return episodes[:limit]


def _remember(out_root: Path, episodes: list[dict], keep: int) -> None:
    """Persist show notes so an episode survives after its local mp3 is gone."""
    ledger = {
        e["date_str"]: {k: e[k] for k in ("title", "description", "summary", "duration_s", "size_bytes")}
        for e in episodes[:keep]
    }
    (Path(out_root) / LEDGER).write_text(json.dumps(ledger, indent=1, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------------ feed
def _sub(parent: ET.Element, tag: str, value: str | None = None, **attrs) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    if value is not None:
        element.text = str(value)
    return element


def _itunes(parent: ET.Element, name: str, value: str | None = None, **attrs) -> ET.Element:
    return _sub(parent, f"{{{ITUNES_NS}}}{name}", value, **attrs)


def build_feed(out_root: Path, config: dict | None = None) -> Path:
    """Write ``out/feed.xml`` for every edition under ``out_root``. Returns the path."""
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    pod = _podcast_config(config)
    base_url = str(pod.get("base_url") or "").strip()
    placeholder = False
    if not base_url:
        base_url = PLACEHOLDER_BASE
        placeholder = True
        print(
            "[podcast] warning: config.delivery.podcast.base_url is not set — "
            f"using placeholder {PLACEHOLDER_BASE} for enclosure URLs. "
            "The feed is valid XML but the audio links will not resolve until hosting is configured.",
            file=sys.stderr,
        )
    base = base_url.rstrip("/")
    owner_email = str(pod.get("email") or "eklav@example.invalid").strip() or "eklav@example.invalid"
    image_url = str(pod.get("image_url") or "").strip()

    keep = int(pod.get("max_items") or MAX_ITEMS)
    episodes = collect_episodes(out_root, keep)
    release_repo = str(pod.get("release_repo") or "").strip()  # audio hosted as GitHub release assets
    if release_repo:
        _remember(out_root, episodes, keep)
    now = _now_ist()

    rss = ET.Element(
        "rss",
        {
            "version": "2.0",
            f"xmlns:{'itunes'}": ITUNES_NS,
            f"xmlns:{'atom'}": ATOM_NS,
        },
    )
    # ET will move/keep the namespace declarations; strip the duplicate textual ones
    rss.attrib.pop("xmlns:itunes", None)
    rss.attrib.pop("xmlns:atom", None)
    channel = _sub(rss, "channel")

    _sub(channel, "title", CHANNEL_TITLE)
    _sub(channel, "link", base + "/")
    _sub(channel, "description", CHANNEL_DESCRIPTION)
    _sub(channel, "language", "en-IN")
    _sub(channel, "copyright", CHANNEL_AUTHOR)
    _sub(channel, "lastBuildDate", _rfc822(now))
    _sub(channel, "generator", "morning-brief/src/podcast.py")

    _itunes(channel, "author", CHANNEL_AUTHOR)
    _itunes(channel, "explicit", "false")
    _itunes(channel, "category", None, text="News")
    owner = _itunes(channel, "owner")
    _itunes(owner, "name", CHANNEL_AUTHOR)
    _itunes(owner, "email", owner_email)
    if image_url:
        _itunes(channel, "image", None, href=image_url)
        img = _sub(channel, "image")  # plain RSS cover for players that ignore the iTunes tag
        _sub(img, "url", image_url)
        _sub(img, "title", CHANNEL_TITLE)
        _sub(img, "link", base + "/")
    _sub(
        channel,
        f"{{{ATOM_NS}}}link",
        None,
        href=base + "/" + FEED_FILENAME,
        rel="self",
        type="application/rss+xml",
    )

    for episode in episodes:
        item = _sub(channel, "item")
        _sub(item, "title", episode["title"])
        _sub(item, "link", f"{base}/{episode['date_str']}/")
        _sub(item, "guid", f"morning-brief-{episode['date_str']}", isPermaLink="false")
        _sub(item, "pubDate", _rfc822(episode["date"]))
        _sub(item, "description", episode["description"])
        _itunes(item, "title", episode["title"])
        _itunes(item, "summary", episode["summary"])
        _itunes(item, "duration", _fmt_duration(episode["duration_s"]))
        _itunes(item, "explicit", "false")
        _sub(
            item,
            "enclosure",
            None,
            url=(f"https://github.com/{release_repo}/releases/download/ed-{episode['date_str']}/brief.mp3"
                 if release_repo else f"{base}/{episode['date_str']}/brief.mp3"),
            length=str(episode["size_bytes"]),
            type="audio/mpeg",
        )

    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")
    feed_path = out_root / FEED_FILENAME
    tree.write(feed_path, encoding="utf-8", xml_declaration=True)
    # self-check: the feed must parse back cleanly
    ET.parse(feed_path)

    print(
        f"[podcast] wrote {feed_path} with {len(episodes)} episode(s)"
        + (" (placeholder base URL)" if placeholder else f" (base URL {base})")
    )
    return feed_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the private Morning Brief podcast feed")
    parser.add_argument("--out", default=str(ROOT / "out"), help="output root containing YYYY-MM-DD editions")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="config.json path")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = _load_json(config_path) if config_path.exists() else {}
    feed = build_feed(Path(args.out), config or {})
    try:
        channel = ET.parse(feed).getroot().find("channel")
        count = len(channel.findall("item")) if channel is not None else 0
    except Exception:  # pragma: no cover - already self-checked in build_feed
        count = -1
    print(f"[podcast] feed {feed} | items {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
