"""THE MORNING BRIEF — newspaper composer.

Renders an EDITION dict (see ``docs/ITEM_CONTRACT.md``) into a self-contained,
print-first broadsheet: a fixed A4 sheet per page, ink on warm paper, Georgia
serif, clickable source links. Produces ``<out>/brief.html`` and ``<out>/brief.pdf``.

Public API
----------
``compose(brief, out_dir, config) -> {"html": Path, "pdf": Path, "pages": int}``

CLI
---
    python -m src.compose --brief docs/sample_brief.json --out out/sample
    python -m src.compose --brief docs/sample_brief.json --out out/sample --preview

Layout works in two passes: the browser first measures every atom (masthead,
lead banner, capsule, section heads, each story at its real column width), then
Python packs those measured heights into sheets. That is what keeps 9-12 stories
inside 2-3 A4 pages without clipped columns or an orphaned heading.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = ROOT / "templates" / "newspaper.html"
DEFAULT_CONFIG = ROOT / "config.json"

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------- geometry (mm)
PAGE_W = 210.0
PAGE_H = 297.0
MARGIN = 11.0
CONTENT_W = PAGE_W - 2 * MARGIN              # 188 mm
N_COLS = 3
COL_GAP = 5.0
COL_W = (CONTENT_W - COL_GAP * (N_COLS - 1)) / N_COLS   # 59.33 mm
ITEM_GAP = 3.2
ZONE_GAP = 4.6
HEAD_GAP = 2.6
TOP_GAP = 4.6
CAPSULE_GAP = 4.6
INDEX_GAP = 4.2
RUNHEAD_H = 7.2
RUNHEAD_GAP = 2.8
FOOTER_H = 13.0                              # rule + sources row + audio row
BODY_LIMIT = PAGE_H - 2 * MARGIN - FOOTER_H  # 262.0
P1_LIMIT = BODY_LIMIT - 1.5
PN_LIMIT = BODY_LIMIT - RUNHEAD_H - RUNHEAD_GAP - 1.0

# ------------------------------------------------------------------- labels
SOURCE_LABELS = {
    "livelaw": "LiveLaw",
    "indianexpress": "Indian Express",
    "thehindu": "The Hindu",
    "telegram-pdf": "Telegram PDF",
    "sccblog": "SCC Online",
    "scobserver": "Supreme Court Observer",
    "barandbench": "Bar & Bench",
}
SOURCE_ORDER = ["livelaw", "sccblog", "scobserver", "barandbench", "indianexpress", "thehindu", "telegram-pdf"]

CATEGORY_LABELS = {
    "supreme-court": "Supreme Court",
    "high-court": "High Court",
    "judiciary": "Judiciary",
    "legislation": "Legislation",
    "national": "National",
    "editorial": "Opinion",
    "economy": "Economy",
    "international": "World",
    "sports": "Sport",
    "other": "Report",
}
SECTION_NAMES = {
    "legal": "THE LEGAL LEDGER",
    "national": "THE NATION",
    "editorial": "OPINION & ANALYSIS",
}
ROMANS = ["I", "II", "III", "IV", "V", "VI"]  # kept for edition metadata hints
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
FALLBACK_ITEM_H = 46.0


# ============================================================== small helpers
def _esc(value: Any) -> str:
    return html_mod.escape("" if value is None else str(value), quote=True)


def _clean(value: Any, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", "" if value is None else str(value)).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _h(heights: dict[str, float], key: str, default: float) -> float:
    try:
        return float(heights.get(key, default))
    except (TypeError, ValueError):
        return default


def _domain(url: str) -> str:
    try:
        host = (url.split("//", 1)[-1].split("/", 1)[0] or "").strip().lower()
    except Exception:  # pragma: no cover - defensive
        return "source"
    return host[4:] if host.startswith("www.") else (host or "source")


def _display_url(url: str) -> str:
    """``host/path`` without scheme/trailing slash; the column ellipsises it."""
    if not url:
        return ""
    rest = url.split("//", 1)[-1]
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    return rest.rstrip("/")


def _source_label(item: dict) -> str:
    return SOURCE_LABELS.get(item.get("source") or "", (item.get("source") or "Source").replace("-", " ").title())


def _kicker(item: dict) -> str:
    court = _clean(item.get("court"))
    if court:
        return court
    return CATEGORY_LABELS.get(item.get("category") or "", "Report")


def _read_minutes(item: dict) -> int:
    seconds = item.get("read_seconds")
    try:
        return max(1, int(math.ceil(float(seconds) / 60.0)))
    except (TypeError, ValueError):
        return 2


def _read_label(item: dict) -> str:
    return f"{_read_minutes(item)} min read"


def _parse_date(brief: dict) -> date:
    raw = _clean(brief.get("date"))
    try:
        return date.fromisoformat(raw)
    except ValueError:
        pass
    generated = _clean(brief.get("generated_at"))
    try:
        return datetime.fromisoformat(generated).date()
    except ValueError:
        return datetime.now(IST).date()


def _parse_generated(brief: dict, edition_date: date) -> tuple[str, str]:
    raw = _clean(brief.get("generated_at"))
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(IST)
        return dt.strftime("%H:%M"), dt.strftime("%H:%M")
    except ValueError:
        return "06:30", "06:30"


# ===================================================================== context
@dataclass
class _Context:
    brief: dict
    edition_date: date
    weekday: str
    date_long: str          # 10 November 2026
    date_upper: str         # 10 NOVEMBER 2026
    edition_no: int
    generated_stamp: str    # 06:28
    read_minutes: int
    top: dict | None
    sections: list[dict]    # [{name, slug, items}]
    capsule: list[dict]
    all_items: list[dict]
    story_count: int
    word_count: int
    source_count: int
    sources_line: str
    top_lines: list[str]
    top_size_pt: float
    top_size_px: float | None = None
    section_pages: dict[str, int] = field(default_factory=dict)
    capsule_page: int | None = None
    audio_minutes: int | None = None
    audio_approx: bool = False

    @property
    def page_title(self) -> str:
        return f"The Morning Brief — {self.weekday}, {self.date_long}"


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        if not item:
            continue
        key = _clean(item.get("id")) or _clean(item.get("url")) or _clean(item.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _split_headline(title: str, width_mm: float = CONTENT_W) -> tuple[list[str], float]:
    """Split a headline into exactly two pretty lines and size it to fit."""
    text = _clean(title) or "The Morning Brief"
    words = text.split(" ")
    if len(words) < 2:
        size = min(30.0, (width_mm * 2.83465) / (max(len(text), 1) * 0.52))
        return [text], round(max(16.0, size), 1)
    best: tuple[float, str, str] | None = None
    for cut in range(1, len(words)):
        first, second = " ".join(words[:cut]), " ".join(words[cut:])
        score = max(len(first), len(second)) + 0.45 * abs(len(first) - len(second))
        if best is None or score < best[0]:
            best = (score, first, second)
    _, first, second = best  # type: ignore[misc]
    longest = max(len(first), len(second))
    # Georgia bold averages ~0.52 em per glyph in mixed case; size so the
    # longest line fits comfortably inside the measure.
    size = (width_mm * 2.83465) / (longest * 0.545)
    return [first, second], round(min(29.0, max(15.0, size)), 1)


def _build_context(brief: dict) -> _Context:
    edition_date = _parse_date(brief)
    weekday = _clean(brief.get("weekday")) or edition_date.strftime("%A")
    generated_stamp, _ = _parse_generated(brief, edition_date)

    sections_raw = brief.get("sections")
    if not isinstance(sections_raw, list):
        sections_raw = []

    top = brief.get("top_story") if isinstance(brief.get("top_story"), dict) else None
    top = _clean(top.get("title")) and top or None  # ignore empty shells

    sections: list[dict] = []
    for sec in sections_raw:
        if not isinstance(sec, dict):
            continue
        slug = _clean(sec.get("slug")) or "brief"
        name = _clean(sec.get("name")) or SECTION_NAMES.get(slug, slug.upper())
        items = [i for i in (sec.get("items") or []) if isinstance(i, dict)]
        if top:
            top_id, top_url = _clean(top.get("id")), _clean(top.get("url"))
            items = [i for i in items if not (top_id and _clean(i.get("id")) == top_id) and not (top_url and _clean(i.get("url")) == top_url)]
        if items:
            sections.append({"name": name, "slug": slug, "items": items})

    # An explicitly empty ``judiciary_capsule`` means "no box this morning" and
    # is respected; only a brief that predates the field gets a derived capsule.
    capsule_raw = brief.get("judiciary_capsule")
    capsule = [i for i in (capsule_raw or []) if isinstance(i, dict)]
    capsule = [i for i in capsule if _clean(i.get("case_name")) or _clean(i.get("ratio")) or _clean(i.get("title"))]
    if capsule_raw is None:
        pool = [i for s in sections for i in s["items"] if i.get("ratio") and (i.get("court") or i.get("case_name"))]
        if top and top.get("ratio") and top.get("court"):
            pool = [top] + pool
        capsule = _dedupe(pool)[:4]
    capsule = _dedupe(capsule)[:4]

    all_items = _dedupe(([top] if top else []) + [i for s in sections for i in s["items"]])

    stats = brief.get("stats") or {}
    story_count = len(all_items)
    try:
        selected = int(stats.get("selected"))
        if selected >= story_count:
            story_count = selected
    except (TypeError, ValueError):
        pass
    word_count = 0
    try:
        word_count = int(stats.get("words"))
    except (TypeError, ValueError):
        word_count = sum(len(_clean(i.get("summary")).split()) + len(_clean(i.get("title")).split()) for i in all_items)

    present = {(_clean(i.get("source")) or "").lower() for i in all_items if i.get("source")}
    ordered = [s for s in SOURCE_ORDER if s in present]
    ordered += sorted(present - set(SOURCE_ORDER))
    source_count = len(present) or len(ordered)
    sources_line = " · ".join(SOURCE_LABELS.get(s, s.title()) for s in ordered) or "LiveLaw · Indian Express · The Hindu"

    top_lines, top_size_pt = _split_headline(top.get("title")) if top else ([], 20.0)

    return _Context(
        brief=brief,
        edition_date=edition_date,
        weekday=weekday.upper(),
        date_long=f"{edition_date.day:02d} {MONTHS[edition_date.month - 1]} {edition_date.year}",
        date_upper=f"{edition_date.day:02d} {MONTHS[edition_date.month - 1].upper()} {edition_date.year}",
        edition_no=edition_date.timetuple().tm_yday,
        generated_stamp=generated_stamp,
        read_minutes=int(brief.get("read_minutes") or 5) or 5,
        top=top,
        sections=sections,
        capsule=capsule,
        all_items=all_items,
        story_count=story_count,
        word_count=word_count,
        source_count=source_count,
        sources_line=sources_line,
        top_lines=top_lines,
        top_size_pt=top_size_pt,
    )


# ================================================================ render atoms
# Every atom is rendered identically in the measuring pass and in the final
# sheet, so the measured height is the height that gets typeset.
def _listen_phrase(ctx: _Context) -> str:
    """Duration for the audio edition: measured audio if known, else read time."""
    minutes = ctx.audio_minutes or ctx.read_minutes
    return f"≈{minutes} min listen" if ctx.audio_minutes and ctx.audio_approx else f"{minutes} min listen"


def _resolve_audio(ctx: _Context, out_dir: Path | None = None) -> None:
    """Duration for the audio note: measured audio if main.py wrote audio_meta, else read time.

    main.py renders the PDF before it builds the audio, so on a first build there
    is no ``brief["audio_meta"]`` yet and the read-time estimate is used. A
    re-render of the saved brief.json (which does carry audio_meta) shows the
    real narration length.
    """
    seconds: float | None = None
    meta = ctx.brief.get("audio_meta")
    if isinstance(meta, dict):
        try:
            value = float(meta.get("duration_s") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            seconds = value
    if seconds:
        ctx.audio_minutes = max(1, int(round(seconds / 60.0)))
        ctx.audio_approx = True
    else:
        ctx.audio_minutes = ctx.read_minutes
        ctx.audio_approx = False


def _render_masthead(ctx: _Context) -> str:
    return f"""<header class="masthead">
  <div class="mast-strap">
    <span class="strap-left">Morning Edition · {_esc(ctx.weekday)}, {_esc(ctx.date_upper)}</span>
    <span class="strap-right">Edition No. {ctx.edition_no}</span>
  </div>
  <div class="mast-main">
    <div class="mast-title">The Morning Brief</div>
    <div class="mast-subrow">
      <div class="mast-motto">Law · Nation · Opinion — in five minutes</div>
      <div class="mast-rail">
        <div class="rail-line rail-strong">{ctx.read_minutes}-Min Read · {ctx.story_count} Stories</div>
        <div class="rail-line">Podcast Attached</div>
        <div class="rail-line rail-soft">brief.mp3 · {_listen_phrase(ctx)}</div>
      </div>
    </div>
  </div>
  <div class="mast-rules"></div>
</header>"""


def _render_top_story(ctx: _Context) -> str:
    item = ctx.top
    if not item:
        return ""
    size = f"{ctx.top_size_px:.2f}px" if ctx.top_size_px else f"{ctx.top_size_pt}pt"
    lines = ctx.top_lines or [_clean(item.get("title"))]
    headline = " ".join(f'<span class="hl-line">{_esc(line)}</span>' for line in lines)
    kicker = _kicker(item)
    source = _source_label(item)
    url = _clean(item.get("url"))
    case = _clean(item.get("case_name"))
    bullets = [b for b in (item.get("bullets") or []) if _clean(b)]
    points = "".join(f"<li>{_esc(_clean(b))}</li>" for b in bullets)
    points_html = f'<ul class="top-points">{points}</ul>' if points else ""
    case_html = f'<span class="sep">·</span><span class="case">{_esc(case)}</span>' if case else ""
    link_html = f'<a class="meta-link" href="{_esc(url)}">{_esc(_domain(url))} ↗</a>' if url else ""
    return f"""<article class="top-story">
  <div class="top-kicker">
    <span class="lead-tag">The Lead</span>
    <span class="kicker-text">{_esc(kicker)} · {_esc(source)}</span>
    <span class="top-read">{_esc(_read_label(item))}</span>
  </div>
  <h1 class="top-headline" style="--hl:{size}">{headline}</h1>
  <p class="top-standfirst">{_esc(_clean(item.get('summary')))}</p>
  <div class="top-meta">
    <span class="byline">Source · {_esc(source)}</span>
    <span class="sep">·</span>
    <span>{_esc(kicker)}</span>
    {case_html}
    {link_html}
  </div>
  {points_html}
</article>"""


def _render_section_head(sec: dict, continued: bool, chunk_count: int | None = None) -> str:
    count = len(sec["items"])
    if continued:
        shown = chunk_count if chunk_count is not None else count
        stories = "story" if shown == 1 else "stories"
        meta = f"Continued · {shown} {stories}"
    else:
        stories = "story" if count == 1 else "stories"
        meta = f"{count} {stories}"
    return (
        '<span class="sec-mark" aria-hidden="true"></span>'
        f'<span class="sec-name">{_esc(sec["name"])}</span>'
        f'<span class="sec-meta">{meta}</span>'
    )


def _render_item(item: dict, ctx: _Context) -> str:
    title = _clean(item.get("title")) or "Untitled"
    summary = _clean(item.get("summary"))
    kicker = _kicker(item)
    source = _source_label(item)
    url = _clean(item.get("url"))
    bullets = [_clean(b) for b in (item.get("bullets") or []) if _clean(b)]
    points = "".join(f"<li>{_esc(b)}</li>" for b in bullets[:5])
    points_html = f'<ul class="item-points">{points}</ul>' if points else ""
    kicker_html = f'{_esc(kicker)}<span class="kdot">·</span>{_esc(source)}'
    anchor = f'<a class="item-url" href="{_esc(url)}">{_esc(_display_url(url))}</a>' if url else ""
    return f"""<article class="item" data-id="{_esc(item.get('id') or '')}">
  <div class="item-kicker">{kicker_html}</div>
  <h3 class="item-head"><a href="{_esc(url)}">{_esc(title)}</a></h3>
  <p class="item-sum">{_esc(summary)}</p>
  {points_html}
  <div class="item-foot">
    <span class="item-src">{_esc(source)}<span class="fs">·</span>{_esc(_read_label(item))}</span>
    {anchor}
  </div>
</article>"""


def _render_capsule(items: list[dict], ctx: _Context) -> str:
    cards: list[str] = []
    for item in items:
        case = _clean(item.get("case_name")) or _clean(item.get("title")) or "Case"
        court = _clean(item.get("court")) or CATEGORY_LABELS.get(item.get("category") or "", "Court")
        ratio = _clean(item.get("ratio"))
        url = _clean(item.get("url"))
        ratio_html = (
            f'<p class="cap-ratio"><span class="ratio-lead">Ratio</span>{_esc(ratio)}</p>'
            if ratio else ""
        )
        link_html = (
            f'<a class="cap-link" href="{_esc(url)}">{_esc(_domain(url))} ↗</a>' if url else ""
        )
        cards.append(
            '<div class="capsule-item">'
            f'<div class="cap-case">{_esc(case)}<span class="cap-court">— {_esc(court)}</span></div>'
            f"{ratio_html}{link_html}"
            "</div>"
        )
    return f"""<aside class="capsule">
  <div class="capsule-head">
    <div class="capsule-title">Case Law You Can Quote <span class="amp">§</span></div>
    <div class="capsule-sub">Judiciary Capsule · ratios for the answer sheet</div>
  </div>
  <div class="capsule-items">{''.join(cards)}</div>
</aside>"""


def _render_index(ctx: _Context) -> str:
    entries: list[str] = []
    for sec in ctx.sections:
        page = ctx.section_pages.get(sec["slug"])
        entries.append(
            f'<span class="index-link">{_esc(sec["name"])} · <b>p. {page or "—"}</b></span>'
        )
    return f"""<div class="index-band">
  <span class="index-tag">Inside</span>
  <span class="index-links">{''.join(entries)}</span>
  <span class="index-extra">Audio attached · brief.mp3</span>
</div>"""


def _render_zone(sec: dict, chunks: list[list[dict]], gap: float, ctx: _Context,
                 continued: bool = False) -> str:
    chunk_count = sum(len(chunk) for chunk in chunks)
    head = _render_section_head(sec, continued, chunk_count)
    cols: list[str] = []
    for chunk in chunks:
        slots: list[str] = []
        for idx, item in enumerate(chunk):
            margin = ITEM_GAP if idx else 0.0
            slots.append(
                f'<div class="item-slot" style="margin-top:{margin:.2f}mm">{_render_item(item, ctx)}</div>'
            )
        cols.append(f'<div class="col">{"".join(slots)}</div>')
    while len(cols) < N_COLS:
        cols.append('<div class="col"></div>')
    return (
        f'<section class="zone" style="margin-top:{gap:.2f}mm">'
        f'<div class="section-head" style="margin-bottom:{HEAD_GAP}mm">{head}</div>'
        f'<div class="cols">{"".join(cols)}</div>'
        f"</section>"
    )


def _render_runhead(ctx: _Context, page: int, total: int) -> str:
    return (
        '<div class="run-head" '
        f'style="margin-bottom:{RUNHEAD_GAP}mm">'
        '<span class="rh-title">The Morning Brief</span>'
        f"<span>{_esc(ctx.weekday)}, {_esc(ctx.date_upper)} · Edition No. {ctx.edition_no}</span>"
        f'<span class="rh-page">Page {page} of {total}</span>'
        "</div>"
    )


def _render_footer(ctx: _Context, page: int, total: int) -> str:
    return f"""<footer class="sheet-footer">
  <div class="foot-audio">
    <span class="hl-tag">How to Listen</span>
    <span class="listen-text">Audio edition: brief.mp3 attached — {_listen_phrase(ctx)}.</span>
  </div>
  <div class="foot-row">
    <span class="foot-edition">The Morning Brief · {_esc(ctx.date_upper)}</span>
    <span class="foot-sources">Sources: {_esc(ctx.sources_line)} · Generated {_esc(ctx.generated_stamp)} IST · Private edition for Eklav</span>
    <span class="foot-page">Page {page} of {total}</span>
  </div>
</footer>"""


# ================================================================= measuring
_MEASURE_STYLE = """<style>
.mb-measure{position:absolute;left:-12000px;top:0;width:210mm;}
.mb-measure .sheet{height:auto!important;overflow:visible!important;box-shadow:none!important;margin:0!important;}
.mb-measure .sheet-frame{height:auto!important;}
.mb-measure .cols{align-items:start;}
</style>"""

_MEASURE_SCRIPT = """<script>
(function(){
  var out = {};
  var headline = document.querySelector('.top-headline');
  if (headline) {
    var spans = headline.querySelectorAll('.hl-line');
    var widest = 0;
    for (var i = 0; i < spans.length; i++) {
      widest = Math.max(widest, spans[i].getBoundingClientRect().width);
    }
    var available = headline.getBoundingClientRect().width;
    var fs = parseFloat(window.getComputedStyle(headline).fontSize) || 20;
    if (widest > available && widest > 0 && available > 0) {
      fs = Math.max(10, fs * available / widest * 0.985);
      headline.style.fontSize = fs + 'px';
    }
    out['__hl_px'] = Math.round(fs * 100) / 100;
  }
  var nodes = document.querySelectorAll('[data-mb]');
  for (var n = 0; n < nodes.length; n++) {
    var el = nodes[n];
    var rect = el.getBoundingClientRect();
    out[el.getAttribute('data-mb')] = Math.round(rect.height * 1000) / 1000;
  }
  var pre = document.createElement('pre');
  pre.id = 'MB_METRICS';
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>"""


def _measure_markup(ctx: _Context) -> str:
    parts: list[str] = [
        '<div class="mb-measure">',
        '<section class="sheet" style="height:auto;overflow:visible;box-shadow:none;margin:0">',
        '<div class="sheet-frame" style="height:auto">',
        f'<div data-mb="masthead">{_render_masthead(ctx)}</div>',
    ]
    if ctx.top:
        parts.append(f'<div data-mb="top">{_render_top_story(ctx)}</div>')
    for sec in ctx.sections:
        parts.append(
            f'<div class="section-head" data-mb="head:{_esc(sec["slug"])}" '
            f'style="margin-bottom:0">{_render_section_head(sec, False, len(sec["items"]))}</div>'
        )
    parts.append('<div class="cols">')
    for item in _dedupe(ctx.all_items):
        key = _esc(item.get("id") or _clean(item.get("url")) or _clean(item.get("title")))
        parts.append(f'<div class="col" data-mb="item:{key}">{_render_item(item, ctx)}</div>')
    parts.append("</div>")
    if ctx.capsule:
        parts.append(f'<div data-mb="capsule">{_render_capsule(ctx.capsule, ctx)}</div>')
    parts.append(f'<div data-mb="index">{_render_index(ctx)}</div>')
    parts.append("</div></section></div>")
    return "".join(parts)


def _parse_metrics(stdout: str) -> dict[str, float]:
    match = re.search(r'id="MB_METRICS"[^>]*>(.*?)</pre>', stdout, re.S)
    if not match:
        raise ValueError("measurement pass produced no MB_METRICS payload")
    data = json.loads(html_mod.unescape(match.group(1).strip()))
    px_to_mm = 25.4 / 96.0
    heights: dict[str, float] = {}
    for key, value in data.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if str(key) == "__hl_px":
            heights[str(key)] = number
        else:
            # getBoundingClientRect returns CSS pixels; the layout engine works
            # in millimetres (1 mm = 96 / 25.4 CSS px).
            heights[str(key)] = round(number * px_to_mm, 3)
    return heights


def _measure_heights(ctx: _Context, browser: str) -> dict[str, float]:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    page = (
        template.replace("{{page_title}}", "measure")
        .replace("{{sheets}}", _measure_markup(ctx))
    )
    page = page.replace("</head>", _MEASURE_STYLE + "</head>")
    page = page.replace("</body>", _MEASURE_SCRIPT + "\n</body>")
    with tempfile.TemporaryDirectory(prefix="mb-measure-") as tmp:
        tmp_dir = Path(tmp)
        page_path = tmp_dir / "measure.html"
        page_path.write_text(page, encoding="utf-8")
        stdout = _browser_dump_dom(browser, page_path, tmp_dir / "profile")
    return _parse_metrics(stdout)


def _estimate_heights(ctx: _Context) -> dict[str, float]:
    """Conservative fallback used only if the browser measuring pass is unavailable."""

    def lines(text: str, pt: float, width_mm: float) -> int:
        text = _clean(text)
        if not text:
            return 0
        per_line = max(10, int(width_mm * 2.83465 / (pt * 0.5)))
        count, current = 1, 0
        for word in text.split():
            size = len(word) + 1
            if current + size > per_line:
                count += 1
                current = len(word)
            else:
                current += size
        return count

    def line_mm(pt: float, leading: float) -> float:
        return pt * 0.3528 * leading

    heights: dict[str, float] = {"masthead": 34.0, "index": 9.5}
    if ctx.top:
        headline_lines = len(ctx.top_lines) or 2
        top = 6.0 + headline_lines * line_mm(ctx.top_size_pt, 1.05)
        top += lines(ctx.top.get("summary"), 10.6, CONTENT_W) * line_mm(10.6, 1.46)
        top += 4.0 + 4.0
        bullets = [b for b in (ctx.top.get("bullets") or []) if _clean(b)]
        if bullets:
            rows = math.ceil(len(bullets) / 3)
            for row in range(rows):
                subset = bullets[row * 3:(row + 1) * 3]
                top += max(lines(b, 7.8, COL_W) for b in subset) * line_mm(7.8, 1.32) + 1.2
            top += 5.0
        heights["top"] = top + 2.0
    for sec in ctx.sections:
        heights[f"head:{sec['slug']}"] = 9.4
        for item in sec["items"]:
            title_lines = min(4, lines(item.get("title"), 11.6, COL_W))
            summary_lines = min(4, lines(item.get("summary"), 8.5, COL_W))
            h = 3.6 + title_lines * line_mm(11.6, 1.12)
            h += summary_lines * line_mm(8.5, 1.36) + 1.4
            bullets = [b for b in (item.get("bullets") or []) if _clean(b)][:3]
            for bullet in bullets:
                h += min(3, lines(bullet, 7.4, COL_W)) * line_mm(7.4, 1.28) + 0.7
            h += 8.0
            heights[f"item:{item.get('id') or _clean(item.get('url'))}"] = h + 1.0
    if ctx.capsule:
        cards = []
        for item in ctx.capsule:
            card = 3.6 + lines(item.get("case_name") or item.get("title"), 9.1, 52) * line_mm(9.1, 1.2)
            card += lines(item.get("ratio"), 7.9, 52) * line_mm(7.9, 1.42) + 2.0
            cards.append(card)
        columns = min(3, max(1, len(cards)))
        tallest = max(cards) if cards else 10.0
        heights["capsule"] = 13.0 + tallest * math.ceil(len(cards) / columns) + 6.0
    return heights


# ==================================================================== layout
@dataclass
class _Block:
    html: str
    gap: float
    wrapped: bool = True
    section: str | None = None


@dataclass
class _Sheet:
    number: int
    limit: float
    y: float = 0.0
    blocks: list[_Block] = field(default_factory=list)


def _col_height(heights: list[float]) -> float:
    if not heights:
        return 0.0
    return sum(heights) + ITEM_GAP * (len(heights) - 1)


def _best_split(heights: list[float], available: float) -> tuple[int, int, int]:
    """Largest contiguous k items that fit in 3 balanced columns, and the split."""
    count = len(heights)
    for take in range(count, 0, -1):
        best: tuple[float, float, int, int] | None = None
        for cut1 in range(0, take + 1):
            for cut2 in range(cut1, take + 1):
                chunks = (heights[0:cut1], heights[cut1:cut2], heights[cut2:take])
                col_heights = [_col_height(list(chunk)) for chunk in chunks]
                if max(col_heights) > available + 0.01:
                    continue
                used = [h for h in col_heights if h > 0]
                spread = (max(used) - min(used)) if len(used) > 1 else 0.0
                # Prefer filled columns from the left (no phantom gutter before
                # the first story) and never leave an empty middle column.
                leading = 0 if cut1 > 0 else 1
                middle_gap = sum(1 for chunk in chunks[:-1] if not chunk)
                score = (max(col_heights), spread, leading, middle_gap, cut1, cut2)
                if best is None or score < best:
                    best = score
        if best is not None:
            return take, best[4], best[5]
    return 0, 0, 0


def _layout(ctx: _Context, heights: dict[str, float]) -> list[_Sheet]:
    sheets: list[_Sheet] = [_Sheet(1, P1_LIMIT)]

    def current() -> _Sheet:
        return sheets[-1]

    def new_page() -> _Sheet:
        sheet = _Sheet(len(sheets) + 1, PN_LIMIT, y=RUNHEAD_H + RUNHEAD_GAP)
        sheets.append(sheet)
        return sheet

    def add_full(key: str, html: str, gap: float) -> _Sheet:
        sheet = current()
        height = _h(heights, key, 20.0)
        if sheet.blocks and sheet.y + gap + height > sheet.limit + 0.01:
            sheet = new_page()
            gap = 0.0
        sheet.blocks.append(_Block(html=html, gap=gap, wrapped=True))
        sheet.y += gap + height
        return sheet

    add_full("masthead", _render_masthead(ctx), 0.0)
    if ctx.top:
        add_full("top", _render_top_story(ctx), TOP_GAP)

    capsule_done = not ctx.capsule
    capsule_after = "legal" if any(s["slug"] == "legal" for s in ctx.sections) else (ctx.sections[0]["slug"] if ctx.sections else "")

    for index, sec in enumerate(ctx.sections):
        remaining = list(sec["items"])
        first_chunk = True
        while remaining:
            sheet = current()
            head_h = _h(heights, f"head:{sec['slug']}", 9.4)
            item_heights = [_h(heights, f"item:{_clean(i.get('id'))}", FALLBACK_ITEM_H) for i in remaining]
            gap = ZONE_GAP if sheet.blocks else 0.0
            if sheet.blocks and sheet.y + gap + head_h + HEAD_GAP + min(item_heights) > sheet.limit + 0.01:
                new_page()
                continue
            available = sheet.limit - (sheet.y + gap + head_h + HEAD_GAP)
            take, cut1, cut2 = _best_split(item_heights, available)
            if take == 0:
                if sheet.blocks:
                    new_page()
                    continue
                take, cut1, cut2 = 1, 1, 1  # item taller than the page: unavoidable
            chunks = [remaining[:cut1], remaining[cut1:cut2], remaining[cut2:take]]
            chunk_heights = [
                _col_height(item_heights[0:cut1]),
                _col_height(item_heights[cut1:cut2]),
                _col_height(item_heights[cut2:take]),
            ]
            zone_height = max(chunk_heights)
            html = _render_zone(sec, chunks, gap, ctx, continued=not first_chunk)
            sheet.blocks.append(_Block(html=html, gap=0.0, wrapped=False, section=sec["slug"]))
            ctx.section_pages.setdefault(sec["slug"], sheet.number)
            sheet.y = sheet.y + gap + head_h + HEAD_GAP + zone_height
            remaining = remaining[take:]
            first_chunk = False
            if remaining:
                new_page()
        if not capsule_done and sec["slug"] == capsule_after:
            sheet = add_full("capsule", _render_capsule(ctx.capsule, ctx), CAPSULE_GAP)
            ctx.capsule_page = sheet.number
            capsule_done = True
        if not capsule_done and index == len(ctx.sections) - 1:
            sheet = add_full("capsule", _render_capsule(ctx.capsule, ctx), CAPSULE_GAP)
            ctx.capsule_page = sheet.number
            capsule_done = True

    if not capsule_done and ctx.capsule:
        sheet = add_full("capsule", _render_capsule(ctx.capsule, ctx), CAPSULE_GAP)
        ctx.capsule_page = sheet.number

    # Front-page index strip: only when the capsule closes page one and the
    # strip still fits; it is the classic "inside this edition" footer.
    first = sheets[0]
    if (
        capsule_done
        and first.blocks
        and first.blocks[-1].html.lstrip().startswith('<aside class="capsule"')
        and first.y + INDEX_GAP + _h(heights, "index", 9.0) <= first.limit
    ):
        first.blocks.append(_Block(html=_render_index(ctx), gap=INDEX_GAP, wrapped=True))
        first.y += INDEX_GAP + _h(heights, "index", 9.0)

    return sheets


# =================================================================== rendering
def _render_sheet(sheet: _Sheet, ctx: _Context, total: int) -> str:
    body: list[str] = ['<div class="page-body">']
    if sheet.number > 1:
        body.append(_render_runhead(ctx, sheet.number, total))
    for block in sheet.blocks:
        if block.wrapped:
            body.append(f'<div class="block" style="margin-top:{block.gap:.2f}mm">{block.html}</div>')
        else:
            body.append(block.html)
    body.append("</div>")
    footer = _render_footer(ctx, sheet.number, total)
    return (
        f'<section class="sheet" id="p{sheet.number}">'
        '<div class="sheet-frame">'
        f'{"".join(body)}'
        f"{footer}"
        "</div></section>"
    )


def _render_document(ctx: _Context, sheets: list[_Sheet], template: str) -> str:
    rendered = "".join(_render_sheet(sheet, ctx, len(sheets)) for sheet in sheets)
    return template.replace("{{page_title}}", _esc(ctx.page_title)).replace("{{sheets}}", rendered)


# ==================================================================== browser
def _find_browser(config: dict) -> str | None:
    pdf_cfg = config.get("pdf") or {}
    candidates: list[str] = []
    configured = _clean(pdf_cfg.get("edge_path"))
    if configured:
        candidates.append(configured)
    candidates += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        try:
            if candidate and Path(candidate).is_file():
                return candidate
        except OSError:
            continue
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _browser_run(browser: str, args: list[str], profile_dir: Path, timeout: float = 120.0) -> subprocess.CompletedProcess:
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        f"--user-data-dir={profile_dir}",
        *args,
    ]
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - machine dependent
        raise RuntimeError(f"headless browser timed out after {timeout:.0f}s: {browser}") from exc
    except OSError as exc:  # pragma: no cover - machine dependent
        raise RuntimeError(f"could not start headless browser {browser}: {exc}") from exc


def _browser_dump_dom(browser: str, page_path: Path, profile_dir: Path) -> str:
    result = _browser_run(
        browser,
        [
            "--window-size=794,1123",
            "--virtual-time-budget=2000",
            "--dump-dom",
            page_path.resolve().as_uri(),
        ],
        profile_dir,
    )
    if not (result.stdout or "").strip():
        raise RuntimeError(f"browser returned no DOM (exit {result.returncode}): {(result.stderr or '')[-400:]}")
    return result.stdout


def _browser_print_pdf(browser: str, page_path: Path, pdf_path: Path, profile_dir: Path) -> None:
    pdf_path = pdf_path.resolve()
    result = _browser_run(
        browser,
        [
            "--no-pdf-header-footer",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={pdf_path}",
            "--virtual-time-budget=3000",
            page_path.resolve().as_uri(),
        ],
        profile_dir,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if pdf_path.is_file() and pdf_path.stat().st_size > 0:
            return
        time.sleep(0.2)
    raise RuntimeError(
        "headless browser did not produce a PDF "
        f"(exit {result.returncode}): {(result.stderr or '')[-500:]}"
    )


def _browser_screenshot(browser: str, page_path: Path, png_path: Path, profile_dir: Path) -> None:
    png_path = png_path.resolve()
    result = _browser_run(
        browser,
        [
            "--window-size=794,1123",
            "--force-device-scale-factor=2",
            "--virtual-time-budget=2000",
            f"--screenshot={png_path}",
            page_path.resolve().as_uri(),
        ],
        profile_dir,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if png_path.is_file() and png_path.stat().st_size > 0:
            return
        time.sleep(0.2)
    raise RuntimeError(
        "headless browser did not produce a screenshot "
        f"(exit {result.returncode}): {(result.stderr or '')[-400:]}"
    )


# ======================================================================= PDF
def _pdf_page_count(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader  # type: ignore

        return len(PdfReader(str(pdf_path)).pages)
    except Exception:  # pragma: no cover - pypdf always present per requirements
        data = pdf_path.read_bytes()
        return len(re.findall(rb"/Type\s*/Page(?![s])", data))


# ====================================================================== API
def compose(brief: dict, out_dir: Path | str, config: dict | None = None) -> dict:
    """Compose ``brief`` into ``<out_dir>/brief.html`` and ``<out_dir>/brief.pdf``."""
    if not isinstance(brief, dict):
        raise TypeError("brief must be a dict (the EDITION shape)")
    config = config or {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = _build_context(brief)
    _resolve_audio(ctx, out_dir)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    browser = _find_browser(config)

    if browser:
        try:
            heights = _measure_heights(ctx, browser)
            hl_px = heights.pop("__hl_px", None)
            if hl_px:
                ctx.top_size_px = float(hl_px)
        except Exception as exc:  # measuring is a nicety; never block the artefact
            print(f"[compose] measuring pass failed ({exc}); using estimates", file=sys.stderr)
            heights = _estimate_heights(ctx)
    else:
        heights = _estimate_heights(ctx)

    sheets = _layout(ctx, heights)
    document = _render_document(ctx, sheets, template)

    html_path = out_dir / "brief.html"
    html_path.write_text(document, encoding="utf-8", newline="\n")

    if not browser:
        raise RuntimeError(
            "No headless browser found: cannot render brief.pdf. "
            "Set config.pdf.edge_path (Microsoft Edge or Google Chrome) and retry."
        )

    pdf_path = out_dir / "brief.pdf"
    if pdf_path.exists():
        pdf_path.unlink()
    with tempfile.TemporaryDirectory(prefix="mb-pdf-") as tmp:
        _browser_print_pdf(browser, html_path, pdf_path, Path(tmp) / "profile")

    pages = _pdf_page_count(pdf_path)
    if pages != len(sheets):
        print(
            f"[compose] warning: {len(sheets)} composed sheets but PDF has {pages} pages",
            file=sys.stderr,
        )
    return {"html": html_path, "pdf": pdf_path, "pages": pages}


# =============================================================== preview tools
def render_previews(brief: dict, out_dir: Path | str, config: dict | None = None) -> list[Path]:
    """Render every composed page to a PNG (used for visual verification)."""
    config = config or {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    browser = _find_browser(config)
    if not browser:
        raise RuntimeError("No headless browser found for preview rendering.")
    ctx = _build_context(brief)
    _resolve_audio(ctx, out_dir)
    heights = _measure_heights(ctx, browser)
    hl_px = heights.pop("__hl_px", None)
    if hl_px:
        ctx.top_size_px = float(hl_px)
    sheets = _layout(ctx, heights)
    document = _render_document(ctx, sheets, TEMPLATE_PATH.read_text(encoding="utf-8"))
    preview_dir = out_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    pngs: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="mb-preview-") as tmp:
        profile = Path(tmp) / "profile"
        for sheet in sheets:
            hide = "".join(
                f"#p{other}{{display:none!important}}"
                for other in range(1, len(sheets) + 1)
                if other != sheet.number
            )
            style = (
                "<style>"
                f"{hide}"
                "body.mb{background:#faf8f3!important;padding:0!important;margin:0!important;}"
                ".sheet{margin:0!important;box-shadow:none!important;border-radius:0!important;}"
                "</style>"
            )
            page_doc = document.replace("</head>", style + "</head>")
            page_path = preview_dir / f"page{sheet.number}.html"
            page_path.write_text(page_doc, encoding="utf-8")
            png_path = preview_dir / f"page{sheet.number}.png"
            if png_path.exists():
                png_path.unlink()
            _browser_screenshot(browser, page_path, png_path, profile)
            pngs.append(png_path)
    return pngs


# ======================================================================= CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.compose",
        description="Compose the MORNING BRIEF: brief.json -> newspaper HTML + A4 PDF.",
    )
    parser.add_argument("--brief", required=True, help="path to the EDITION json (brief.json)")
    parser.add_argument("--out", required=True, help="output directory, e.g. out/sample")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config.json path")
    parser.add_argument("--preview", action="store_true", help="also write page PNGs to <out>/preview")
    parser.add_argument("--quiet", action="store_true", help="only print errors")
    args = parser.parse_args(argv)

    brief_path = Path(args.brief)
    brief = json.loads(brief_path.read_text(encoding="utf-8-sig"))
    config: dict = {}
    config_path = Path(args.config)
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))

    result = compose(brief, Path(args.out), config)
    html_path: Path = result["html"]
    pdf_path: Path = result["pdf"]
    if not args.quiet:
        print(f"HTML  {html_path}  ({html_path.stat().st_size:,} bytes)")
        print(f"PDF   {pdf_path}  ({pdf_path.stat().st_size:,} bytes)")
        print(f"Pages {result['pages']}")
    if args.preview:
        for png in render_previews(brief, Path(args.out), config):
            if not args.quiet:
                print(f"PNG   {png}  ({png.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
