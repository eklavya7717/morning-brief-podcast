"""Narration module — turn an EDITION (brief.json) into a listenable MP3 brief.

``build_audio(brief, out_dir, config) -> dict`` writes

* ``brief.mp3``            — spoken edition (mono 44.1 kHz, 64 kbps MP3)
* ``brief.script.txt``     — the broadcast script that was spoken
* ``brief.mp3.meta.json``  — duration_s, words, provider, generated_at

Pipeline: write a real anchor script from the EDITION -> split it into
paragraphs (<= 1200 chars) -> gTTS (Indian English, ``tld=co.in``) per
paragraph, 3 retries with backoff -> Windows SAPI fallback when gTTS is
unreachable -> normalise each segment with ffmpeg -> join with 350 ms silence
via the ffmpeg concat demuxer -> verify duration/decodability. If the result
runs longer than ``config.audio.max_seconds`` the lowest-priority items are
dropped from the script and the edition is re-rendered (max two retries); a
sentence is never cut in half.

CLI::

    python -m src.audio --brief docs/sample_brief.json --out out/sample
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

IST = timezone(timedelta(hours=5, minutes=30))

# Broadcast target: 780-900 words is ~5-6 minutes at 155 wpm.
MIN_WORDS = 780
MAX_WORDS = 900  # defaults; config.audio.min_words/max_words override
TARGET_WPM = 155
MAX_CHARS = 1200          # gTTS paragraph limit
GAP_MS = 350              # silence between spoken paragraphs
MAX_DURATION_ATTEMPTS = 3  # initial render + two re-renders with fewer items

SECTION_INTROS = {
    "legal": (
        "Now, the Legal Ledger. The judgments, the orders and the legal "
        "developments worth carrying into your preparation today."
    ),
    "national": "Over to the nation. The stories that shape the country this morning.",
    "editorial": "And finally, from the opinion pages.",
}

SOURCE_NAMES = {
    "livelaw": "LiveLaw",
    "indianexpress": "The Indian Express",
    "thehindu": "The Hindu",
    "telegram-pdf": "the print edition",
    "sccblog": "S C C Online",
    "scobserver": "the Supreme Court Observer",
    "barandbench": "Bar and Bench",
}

FILLER_LINES = [
    ("That is the legal picture to carry into your preparation today.", "legal"),
    ("Keep the case names and the one line ratios in your notes for revision.", "legal"),
    ("That is the news you need before your day begins.", "national"),
    ("We will keep tracking these developments through the day.", "national"),
    ("That is where the debate stands this morning.", "editorial"),
    ("That is the edition for this morning.", None),
]

BULLET_PATTERNS = [
    (r"^bench\s*:\s*", "The bench comprised "),
    (r"^twin conditions read down to include\s+", "The twin conditions were read down to include "),
    (r"^reliance placed on\s+", "The court relied on "),
    (r"^directions issued for\s+", "Directions were issued for "),
    (r"^reference arises from\s+", "The reference arises from "),
    (r"^hearings expected\s+", "Hearings are expected "),
    (r"^rules to be notified\s+", "The rules are to be notified "),
    (r"^penalties up to\s+", "Penalties can go up to "),
    (r"^certificate requirement retained", "The certificate requirement was retained"),
    (r"^single judge bench of\s+", "The single judge bench comprised "),
    (r"^nine-judge bench, led by the cji", "A nine judge bench will be led by the Chief Justice of India"),
    (r"^pending strength of the court drops from\s+", "The pending strength of the court drops from "),
    (r"^recommendation marks\s+", "The recommendation marks the "),
    (r"^rajya sabha to take up\s+", "The Rajya Sabha will take up "),
    (r"^points to backlog of\s+", "The report points to a backlog of "),
    (r"^consent managers get\s+", "Consent managers will get a "),
    (r"^food inflation at\s+", "Food inflation stood at "),
    (r"^rbi's next policy review\s+", "The RBI's next policy review is "),
    (r"^points to\s+", "The report points to "),
    (r"^proposes\s+", "The piece proposes "),
    (r"^calls for\s+", "The piece calls for "),
    (r"^editorial calls for\s+", "The editorial calls for "),
    (r"^urges\s+", "The piece urges "),
    (r"^notes that\s+", "The piece notes that "),
    (r"^argues that\s+", "The piece argues that "),
    (r"^flags\s+", "The report flags "),
    (r"^seeks\s+", "The report seeks "),
    (r"^suggests\s+", "The piece suggests "),
    (r"^warns\s+", "The report warns "),
    (r"^questions\s+", "The piece questions "),
]

GENERIC_LABELS = {
    "judicial appointment", "judicial appointments", "breaking", "top story",
    "explained", "opinion", "editorial", "analysis", "exclusive", "interview",
    "legal news", "update", "watch", "report", "law", "news",
}

STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "by",
    "from", "as", "at", "is", "are", "was", "were", "be", "been", "will", "has",
    "have", "had", "that", "this", "its", "it", "their", "his", "her", "said",
    "says", "also", "not", "but", "can", "may", "into", "up", "over", "after",
    "before", "about", "than", "then", "there", "these", "those", "which", "who",
    "what", "when", "where", "why", "how", "been", "being", "would", "could",
}


def _content_words(text: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return {w for w in words if len(w) > 2 and w not in STOPWORDS}


def _is_redundant(text: str, existing: str) -> bool:
    """True when a bullet just repeats words already spoken for the item."""
    candidate = _content_words(text)
    if not candidate:
        return True
    spoken = _content_words(existing)
    return len(candidate & spoken) / len(candidate) >= 0.6


GENERIC_BULLET_LEADS = ["Also in this report,", "Also on this story,", "A detail to note,"]


_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------- helpers
def _now_ist() -> datetime:
    return datetime.now(IST)


def _word_count(text: str) -> int:
    return len(text.split())


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _audio_config(config: dict | None) -> dict:
    cfg = dict((config or {}).get("audio") or {})
    cfg.setdefault("provider", "gtts")
    cfg.setdefault("tld", "co.in")
    cfg.setdefault("max_seconds", 420)
    return cfg


def _split_sentences(text: str) -> list[str]:
    text = _WS_RE.sub(" ", (text or "").strip())
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _headline_sentence(title: str) -> str:
    title = _WS_RE.sub(" ", (title or "").strip()).rstrip(" .,;:")
    if not title:
        return ""
    # drop generic CMS labels such as "Judicial Appointment:" or "Breaking:"
    label = re.match(r"^([^:]{1,40}):\s*(.+)$", title)
    if label and label.group(1).strip().lower() in GENERIC_LABELS:
        title = label.group(2).strip()
    if title.isupper():
        title = title.capitalize()
    title = title[0].upper() + title[1:]
    return title + "."


def _summary_sentences(item: dict, limit: int = 2) -> list[str]:
    sentences = _split_sentences(item.get("summary") or "")
    if sentences:
        return sentences[:limit]
    headline = _headline_sentence(item.get("title") or "")
    return [headline] if headline else []


def _spoken_case_name(case_name: str) -> str:
    """'K. Vijay Kumar v. Union of India' -> 'K. Vijay Kumar versus Union of India'."""
    name = _WS_RE.sub(" ", (case_name or "").strip())
    name = re.sub(r"\bvs?\.\s+", "versus ", name)
    name = re.sub(r"\s+versus\s+", " versus ", name)
    return name.strip()


def _lower_first(text: str) -> str:
    if not text:
        return text
    # keep capitalised statutory / constitutional references intact
    if re.match(r"^(Section|Article|Order|Rule|Rules|Act|Clause|Schedule|Chapter|Part)\b", text):
        return text
    return text[0].lower() + text[1:]


def _court_phrase(court: str | None) -> str:
    court = _WS_RE.sub(" ", (court or "").strip())
    if not court:
        return "the court"
    if court.lower().startswith("the "):
        return court
    return "the " + court


def _is_judgment(item: dict) -> bool:
    return bool(item.get("case_name") and item.get("ratio"))


def _case_sentence(item: dict) -> str:
    """'In <case> versus <party>, the <court> held that <ratio>.'"""
    case = _spoken_case_name(item.get("case_name") or "")
    ratio = _WS_RE.sub(" ", (item.get("ratio") or "").strip()).rstrip(" .;:")
    if not case or not ratio:
        return ""
    return f"In {case}, {_court_phrase(item.get('court'))} held that {_lower_first(ratio)}."


def _bullet_sentence(bullet: str, index: int = 0) -> str:
    """Turn a telegraphic ITEM bullet into a speakable sentence."""
    text = _WS_RE.sub(" ", (bullet or "").strip()).strip("-•· ")
    if not text:
        return ""
    text = text.rstrip(" .;:")
    for pattern, replacement in BULLET_PATTERNS:
        if re.match(pattern, text, flags=re.IGNORECASE):
            text = re.sub(pattern, replacement, text, count=1, flags=re.IGNORECASE)
            break
    else:
        lead = GENERIC_BULLET_LEADS[index % len(GENERIC_BULLET_LEADS)]
        # keep "Rajya Sabha ..." capitalised, lowercase ordinary words
        first_two = re.findall(r"\w+", text)[:2]
        if len(first_two) < 2 or not (first_two[0][:1].isupper() and first_two[1][:1].isupper()):
            text = _lower_first(text)
        text = f"{lead} {text}"
    text = text.strip()
    text = text[0].upper() + text[1:] if text else text
    return text.rstrip(".") + "."


def naturalise(text: str) -> str:
    """Rewrite copy so a TTS engine reads it like a news anchor.

    Expands abbreviations, removes markup/urls/symbols that would be read
    oddly, and keeps sentences short. Safe to call twice.
    """
    if not text:
        return ""
    out = text
    out = re.sub(r"https?://\S+", "", out)
    out = re.sub(r"\bwww\.\S+", "", out)
    # quotation marks / dashes / colons / semicolons that read badly
    for ch in "\u201c\u201d\u2018\u2019\"":
        out = out.replace(ch, "")
    out = re.sub(r"\s*[\u2013\u2014]\s*", ", ", out)
    out = re.sub(r"\s*;\s*", ", ", out)
    out = re.sub(r"\s*:\s*", ", ", out)
    out = re.sub(r"[()\[\]{}]", " ", out)

    # case citations and abbreviations
    out = re.sub(r"\bvs\.?\s+", "versus ", out, flags=re.IGNORECASE)
    out = re.sub(r"(?<=[A-Za-z])\s+v\.\s+", " versus ", out)  # lowercase v. only
    out = re.sub(r"\bS\.C\.", "Supreme Court", out)
    out = re.sub(r"\bH\.C\.", "High Court", out)
    out = re.sub(r"\bCJI\b", "Chief Justice of India", out)
    out = re.sub(r"\bSC\b", "Supreme Court", out)
    out = re.sub(r"\bHC\b", "High Court", out)
    out = re.sub(r"\bRs\.?\s*", "rupees ", out)
    out = re.sub(r"\s*%\s*", " per cent ", out)
    out = re.sub(r"\bArt\.\s*", "Article ", out)
    out = re.sub(r"\bSec\.\s*", "Section ", out)
    out = re.sub(r"\bNo\.\s*", "number ", out)
    out = re.sub(r"\bNos\.\s*", "numbers ", out)
    out = re.sub(r"\bU\.P\.", "Uttar Pradesh", out)
    out = re.sub(r"\bUP\b", "Uttar Pradesh", out)
    out = re.sub(r"\bMP\b", "Madhya Pradesh", out)
    out = re.sub(r"\be\.g\.\s*", "for example ", out, flags=re.IGNORECASE)
    out = re.sub(r"\bi\.e\.\s*", "that is ", out, flags=re.IGNORECASE)
    out = re.sub(r"\bw\.e\.f\.\s*", "with effect from ", out, flags=re.IGNORECASE)
    out = re.sub(r"\betc\.", "and so on", out, flags=re.IGNORECASE)
    out = out.replace("&", " and ")
    out = out.replace("#", " number ")

    # stray symbols / markdown that a TTS engine would spell out
    out = re.sub(r"[*_|~`^<>]", " ", out)
    out = re.sub(r"[^\w\s.,'\-]", " ", out, flags=re.UNICODE)
    out = re.sub(r"\.{2,}", ".", out)
    out = _WS_RE.sub(" ", out).strip()
    return out


def _naturalise_paragraph(text: str) -> str:
    # split on sentence ends first so abbreviations that look like sentence
    # breaks ("Art." -> "Article") do not leave dangling spaces / capitals
    text = naturalise(text)
    sentences = _split_sentences(text)
    return " ".join(sentences)


# ---------------------------------------------------------------- script build
def _date_bits(brief: dict) -> tuple[str, int, str, int]:
    weekday = (brief.get("weekday") or "").strip()
    date_str = (brief.get("date") or "").strip()
    day, month, year = None, "", None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d %B %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            day, month, year = dt.day, dt.strftime("%B"), dt.year
            if not weekday:
                weekday = dt.strftime("%A")
            break
        except ValueError:
            continue
    if day is None:
        dt = _now_ist()
        day, month, year = dt.day, dt.strftime("%B"), dt.year
    if not weekday:
        weekday = "Monday"
    return weekday, day, month, year or _now_ist().year


def _section_items(section: dict) -> list[dict]:
    items = section.get("items") or []
    return [i for i in items if isinstance(i, dict)]


def _stats_sentence(brief: dict) -> str:
    """Edition provenance line, using the real numbers from brief.stats."""
    stats = brief.get("stats") or {}
    sources = stats.get("sources") or {}
    parts: list[str] = []
    for key, count in sources.items():
        name = SOURCE_NAMES.get(str(key).strip().lower())
        if not name:
            continue
        try:
            count = int(count)
        except (TypeError, ValueError):
            continue
        parts.append(f"{count} reports")
        parts.append(f"from {name}")
    # group into "24 reports from LiveLaw, 30 reports from The Indian Express ..."
    grouped: list[str] = []
    for index in range(0, len(parts) - 1, 2):
        grouped.append(f"{parts[index]} {parts[index + 1]}")
    if not grouped:
        return ""
    if len(grouped) == 1:
        joined = grouped[0]
    elif len(grouped) == 2:
        joined = f"{grouped[0]} and {grouped[1]}"
    else:
        joined = ", ".join(grouped[:-1]) + " and " + grouped[-1]
    selected = stats.get("selected")
    try:
        selected = int(selected)
    except (TypeError, ValueError):
        selected = None
    if selected:
        return f"This morning we read {joined}, and picked the {selected} stories that matter most to you."
    return f"This morning we read {joined}."


def _collect_sections(brief: dict) -> list[dict]:
    sections = brief.get("sections") or []
    order = {"legal": 0, "national": 1, "editorial": 2}
    known = [s for s in sections if isinstance(s, dict) and s.get("slug") in order]
    known.sort(key=lambda s: order.get(s.get("slug"), 9))
    return known


def _build_segments(brief: dict, drop_ids: set[str] | None = None) -> list[dict]:
    """Build the ordered paragraph list for the spoken edition."""
    drop_ids = drop_ids or set()
    weekday, day, month, year = _date_bits(brief)
    segments: list[dict] = [
        {
            "kind": "greeting",
            "text": (
                f"Good morning. It's {weekday}, the {_ordinal(day)} of {month} {year}, "
                "and this is The Morning Brief. Five minutes of the law, the courts and "
                "the country, for the law student and the judiciary aspirant in India."
            ),
        }
    ]
    stats_line = _stats_sentence(brief)
    if stats_line:
        segments.append({"kind": "stats", "text": stats_line})

    top = brief.get("top_story") or {}
    top_id = (top.get("id") or "").strip()
    if top and top.get("title"):
        sentences = ["First, the top story.", _headline_sentence(top.get("title") or "")]
        sentences.extend(_summary_sentences(top, limit=2))
        case = _case_sentence(top)
        if case:
            sentences.append(case)
        segments.append(
            {"kind": "top", "text": " ".join(s for s in sentences if s), "item": top, "bullets_used": 0}
        )

    for section in _collect_sections(brief):
        slug = section.get("slug") or ""
        items = [
            item
            for item in _section_items(section)
            if (item.get("id") or "") not in drop_ids and (item.get("id") or "") != top_id
        ]
        if not items:
            continue
        segments.append({"kind": "intro", "text": SECTION_INTROS.get(slug, ""), "slug": slug})
        for item in items:
            text = _item_text(item, slug)
            if text:
                segments.append(
                    {"kind": "item", "text": text, "item": item, "slug": slug, "bullets_used": 0}
                )

    capsule = [c for c in (brief.get("judiciary_capsule") or []) if isinstance(c, dict)]
    capsule_item = next(
        (c for c in capsule if (c.get("id") or "") != top_id and _is_judgment(c)),
        None,
    )
    if capsule_item is None:
        capsule_item = next((c for c in capsule if _is_judgment(c)), None)
    if capsule_item is None and _is_judgment(top):
        capsule_item = top
    if capsule_item is not None:
        case = _case_sentence(capsule_item)
        if case:
            segments.append(
                {
                    "kind": "capsule",
                    "text": "Before we close, here is your one line to remember. " + case,
                    "item": capsule_item,
                    "bullets_used": 0,
                }
            )

    segments.append(
        {
            "kind": "signoff",
            "text": (
                "That's your brief for today. Read the full edition, "
                "and have a productive day."
            ),
        }
    )
    return segments


def _item_text(item: dict, slug: str) -> str:
    """1-2 spoken sentences for one EDITION item."""
    case = _case_sentence(item) if slug == "legal" else ""
    summary = _summary_sentences(item, limit=2)
    headline = _headline_sentence(item.get("title") or "")
    if slug == "legal":
        if case:
            return " ".join([case] + summary[:1])
        # no case name: lead with the headline so the listener knows the subject
        return " ".join([headline] + summary[:1]) if headline else " ".join(summary[:2])
    # THE NATION / OPINION: one sentence per the editorial spec
    if summary:
        return summary[0]
    return headline


def _render_script(segments: list[dict]) -> str:
    paragraphs = []
    for seg in segments:
        text = _naturalise_paragraph(seg.get("text") or "")
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _source_sentence(item: dict) -> str:
    name = SOURCE_NAMES.get((item.get("source") or "").strip().lower())
    if not name:
        return ""
    return f"That report is from {name}."


def _enrich_to_target(segments: list[dict], min_words: int = MIN_WORDS) -> None:
    """Grow the script with spare bullets / attributions / stock lines."""
    target_segments = [
        s for s in segments if s.get("item") and s.get("kind") in ("top", "item")
    ]

    # 1. spare bullets, round-robin so no single item turns into a lecture
    for _ in range(3):
        for seg in target_segments:
            if _word_count(_render_script(segments)) >= min_words:
                return
            item = seg["item"]
            bullets = [b for b in (item.get("bullets") or []) if str(b).strip()]
            while seg.get("bullets_used", 0) < min(len(bullets), 5):
                idx = seg.get("bullets_used", 0)
                seg["bullets_used"] = idx + 1
                raw = str(bullets[idx])
                if _is_redundant(raw, seg.get("text") or ""):
                    continue
                sentence = _bullet_sentence(raw, idx)
                if sentence:
                    seg["text"] = (seg["text"] + " " + sentence).strip()
                break

    # 2. source attribution ("That report is from LiveLaw.")
    for seg in target_segments:
        if _word_count(_render_script(segments)) >= min_words:
            return
        if seg.get("attributed"):
            continue
        sentence = _source_sentence(seg["item"])
        if sentence:
            seg["text"] = (seg["text"] + " " + sentence).strip()
            seg["attributed"] = True

    # 3. stock broadcast lines, placed after the section they close
    for line, slug in FILLER_LINES:
        if _word_count(_render_script(segments)) >= min_words:
            return
        _insert_filler(segments, line, slug)


def _insert_filler(segments: list[dict], text: str, slug: str | None) -> None:
    signoff_index = next(
        (i for i, s in enumerate(segments) if s.get("kind") == "signoff"), len(segments)
    )
    index = signoff_index
    if slug:
        last = None
        for i, seg in enumerate(segments):
            if seg.get("slug") == slug:
                last = i
        if last is not None:
            index = last + 1
    segments.insert(index, {"kind": "filler", "text": text, "slug": slug})


def _drop_candidate(brief: dict, drop_ids: set[str]) -> str | None:
    """Lowest-priority item that may be dropped from the spoken edition."""
    top_id = ((brief.get("top_story") or {}).get("id") or "").strip()
    candidates: list[tuple[int, int, str]] = []
    for order, section in enumerate(_collect_sections(brief)):
        for item in _section_items(section):
            item_id = (item.get("id") or "").strip()
            if not item_id or item_id in drop_ids or item_id == top_id:
                continue
            candidates.append((int(item.get("priority") or 50), order, item_id))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][2]


def build_script(brief: dict, config: dict | None = None, drop_ids: set[str] | None = None) -> str:
    """Write the broadcast script for an EDITION.

    780-900 words, Indian-English anchor register, no urls. If a brief is too
    long for the word budget the lowest-priority items are dropped first.
    """
    acfg = (config or {}).get("audio") or {}
    if acfg.get("deep"):  # long-form mode; None means "no usable material", fall through to the short script
        from src.deepscript import build_deep_script

        deep = build_deep_script(brief, config)
        if deep:
            return deep
    drops = set(drop_ids or set())
    min_words, max_words = int(acfg.get("min_words") or MIN_WORDS), int(acfg.get("max_words") or MAX_WORDS)
    text = ""
    for _ in range(6):
        segments = _build_segments(brief, drops)
        text = _render_script(segments)
        words = _word_count(text)
        if words > max_words:
            candidate = _drop_candidate(brief, drops)
            if not candidate:
                break
            drops.add(candidate)
            continue
        if words < min_words:
            _enrich_to_target(segments, min_words)
            text = _render_script(segments)
        return text
    return text


def split_script(script: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Split the script into <= max_chars TTS paragraphs (paragraph-first)."""
    chunks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", script or ""):
        paragraph = _WS_RE.sub(" ", paragraph).strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            chunks.append(paragraph)
            continue
        current = ""
        for sentence in _split_sentences(paragraph):
            if len(sentence) > max_chars:
                # pathological single sentence: hard-wrap on word boundaries
                if current:
                    chunks.append(current)
                    current = ""
                words = sentence.split()
                piece = ""
                for word in words:
                    if len(piece) + len(word) + 1 > max_chars:
                        chunks.append(piece.strip())
                        piece = word + " "
                    else:
                        piece += word + " "
                if piece.strip():
                    current = piece.strip()
                continue
            if len(current) + len(sentence) + 1 > max_chars:
                chunks.append(current.strip())
                current = sentence
            else:
                current = (current + " " + sentence).strip()
        if current.strip():
            chunks.append(current.strip())
    return [c for c in chunks if c]


# ---------------------------------------------------------------------- ffmpeg
def _ffmpeg_bin() -> str:
    override = os.environ.get("MORNING_BRIEF_FFMPEG")
    if override and Path(override).exists():
        return override
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - fall back to PATH
        found = shutil.which("ffmpeg")
        if found:
            return found
        raise RuntimeError("ffmpeg not found (install imageio-ffmpeg or put ffmpeg on PATH)")


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )


def _normalise_segment(src: Path, dst: Path, ffmpeg: str) -> Path:
    result = _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
            str(dst),
        ],
        timeout=120,
    )
    if result.returncode != 0 or not dst.exists() or dst.stat().st_size < 500:
        raise RuntimeError(f"ffmpeg could not normalise segment {src.name}: {result.stderr.strip()[:400]}")
    return dst


def _make_silence(dst: Path, ffmpeg: str, ms: int = GAP_MS) -> Path:
    seconds = ms / 1000.0
    result = _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", f"{seconds:.3f}",
            "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
            str(dst),
        ],
        timeout=60,
    )
    if result.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg could not create silence: {result.stderr.strip()[:300]}")
    return dst


def _concat(segments: list[Path], out_mp3: Path, workdir: Path, ffmpeg: str) -> Path:
    silence = _make_silence(workdir / "silence.mp3", ffmpeg)
    ordered: list[Path] = []
    for index, segment in enumerate(segments):
        if index:
            ordered.append(silence)
        ordered.append(segment)
    listing = workdir / "concat.txt"
    lines = []
    for path in ordered:
        safe = path.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k",
            str(out_mp3),
        ],
        timeout=300,
    )
    if result.returncode != 0 or not out_mp3.exists() or out_mp3.stat().st_size < 2000:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr.strip()[:400]}")
    return out_mp3


def _duration_s(mp3: Path, ffmpeg: str) -> float:
    """Parse the duration ffmpeg prints in its stderr banner (no ffprobe needed)."""
    result = _run([ffmpeg, "-hide_banner", "-i", str(mp3)], timeout=60)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        raise RuntimeError(f"could not read duration of {mp3.name}: {(result.stderr or '')[:300]}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _verify_decodable(mp3: Path, ffmpeg: str) -> None:
    result = _run(
        [ffmpeg, "-v", "error", "-i", str(mp3), "-f", "null", "-"], timeout=180
    )
    if result.returncode != 0:
        raise RuntimeError(f"output mp3 is not decodable: {(result.stderr or '')[:400]}")


# ------------------------------------------------------------------------- TTS
def _gtts_chunk(text: str, path: Path, tld: str, retries: int = 3) -> Path:
    from gtts import gTTS

    last: Exception | None = None
    for attempt in range(retries):
        try:
            # timeout is essential: without it a stalled Google connection would
            # hang the whole morning run (gtts defaults to waiting forever).
            gTTS(text=text, lang="en", tld=tld or "co.in", slow=False, timeout=20).save(str(path))
            if path.exists() and path.stat().st_size > 500:
                return path
            raise RuntimeError("gTTS returned an empty file")
        except Exception as exc:  # noqa: BLE001 - retry any TTS failure
            last = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"gTTS failed after {retries} attempts: {last}")


SAPI_SCRIPT = r"""param(
    [Parameter(Mandatory=$true)][string]$TextFile,
    [Parameter(Mandatory=$true)][string]$OutWav,
    [string]$VoiceName = "Microsoft Zira Desktop"
)
Add-Type -AssemblyName System.Speech
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $synth.SelectVoice($VoiceName) } catch { }
$synth.Rate = 1
$synth.Volume = 100
$synth.SetOutputToWaveFile($OutWav)
$synth.Speak($text)
$synth.Dispose()
"""


def _powershell_exe() -> str:
    for name in ("powershell.exe", "pwsh.exe"):
        found = shutil.which(name)
        if found:
            return found
    return "powershell.exe"


def _sapi_chunk(text: str, index: int, workdir: Path, voice: str = "Microsoft Zira Desktop") -> Path:
    script = workdir / "sapi_speak.ps1"
    if not script.exists():
        script.write_text(SAPI_SCRIPT, encoding="utf-8")
    text_file = workdir / f"sapi_text_{index:03d}.txt"
    out_wav = workdir / f"sapi_{index:03d}.wav"
    text_file.write_text(text, encoding="utf-8")
    result = _run(
        [
            _powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-TextFile", str(text_file),
            "-OutWav", str(out_wav),
            "-VoiceName", voice,
        ],
        timeout=300,
    )
    if result.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size < 2000:
        raise RuntimeError(f"Windows SAPI failed: {(result.stderr or result.stdout or '')[:400]}")
    return out_wav


def _render_gtts(chunks: list[str], workdir: Path, tld: str, budget_s: float = 150.0) -> list[Path]:
    """Render chunks via gTTS, aborting early if the service is throttling us."""
    started = time.time()
    paths: list[Path] = []
    for index, chunk in enumerate(chunks):
        if time.time() - started > budget_s:
            raise RuntimeError(
                f"gTTS budget of {budget_s:.0f}s exceeded after {index}/{len(chunks)} chunks"
            )
        paths.append(_gtts_chunk(chunk, workdir / f"gtts_{index:03d}.mp3", tld))
    return paths


def _render_sapi(chunks: list[str], workdir: Path, voice: str) -> list[Path]:
    """Offline fallback: synthesise the whole script in ONE SAPI call.

    Per-chunk PowerShell processes are very slow (start-up cost dominates);
    System.Speech has no length limit, so the fallback speaks the full script
    as a single WAV and lets ffmpeg split/normalise it afterwards.
    """
    script = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
    wav = _sapi_chunk(script, 0, workdir, voice)
    return [wav]


def _edge_chunk(text: str, path: Path, voice: str, rate: str, retries: int = 3) -> Path:
    """Microsoft Edge neural voice: free, no key, far more natural than gTTS."""
    import asyncio

    import edge_tts

    last = None
    for attempt in range(retries):
        try:
            asyncio.run(asyncio.wait_for(
                edge_tts.Communicate(text, voice, rate=rate).save(str(path)), timeout=60))
            if path.exists() and path.stat().st_size > 1000:
                return path
            raise RuntimeError("edge-tts returned an empty file")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"edge-tts failed after {retries} attempts: {last}")


def _render_edge(chunks: list[str], workdir: Path, voice: str, rate: str, budget_s: float = 400.0) -> list[Path]:
    started = time.monotonic()
    paths = []
    for index, chunk in enumerate(chunks):
        if time.monotonic() - started > budget_s:
            raise RuntimeError(f"edge-tts budget exceeded after {index}/{len(chunks)} chunks")
        paths.append(_edge_chunk(chunk, workdir / f"edge_{index:03d}.mp3", voice, rate))
    return paths


def synthesize(script: str, workdir: Path, config: dict | None = None) -> tuple[list[Path], str]:
    """Render script chunks to audio files; gTTS first, Windows SAPI as fallback."""
    # gTTS/requests has no timeout of its own; a stalled TCP read must never hang
    # the morning run, so give every socket a hard ceiling.
    socket.setdefaulttimeout(25)
    cfg = _audio_config(config)
    chunks = split_script(script)
    if not chunks:
        raise RuntimeError("empty narration script")
    tld = cfg.get("tld") or "co.in"
    voice = cfg.get("sapi_voice") or "Microsoft Zira Desktop"
    budget = float(cfg.get("gtts_budget_s") or 150)
    if cfg.get("provider") == "edge":
        try:
            return _render_edge(chunks, workdir, cfg.get("edge_voice") or "en-IN-NeerjaNeural",
                                cfg.get("edge_rate") or "+0%", float(cfg.get("edge_budget_s") or 900)), "edge"
        except Exception as edge_error:  # noqa: BLE001 - fall back to gTTS
            print(f"[audio] edge-tts unavailable ({edge_error}); falling back to gTTS", file=sys.stderr)
    try:
        return _render_gtts(chunks, workdir, tld, budget_s=budget), "gtts"
    except Exception as gtts_error:  # noqa: BLE001 - fall back to offline SAPI
        print(f"[audio] gTTS unavailable ({gtts_error}); falling back to Windows SAPI", file=sys.stderr)
    try:
        return _render_sapi(chunks, workdir, voice), "sapi"
    except Exception as sapi_error:  # noqa: BLE001
        raise RuntimeError(
            f"both TTS providers failed. gTTS: {gtts_error}; SAPI: {sapi_error}"
        ) from sapi_error


def render_audio(script: str, out_mp3: Path, workdir: Path, config: dict | None = None) -> tuple[float, str]:
    """Script -> MP3 on disk. Returns (duration_s, provider)."""
    ffmpeg = _ffmpeg_bin()
    raw_segments, provider = synthesize(script, workdir, config)
    segments: list[Path] = []
    for index, raw in enumerate(raw_segments):
        segments.append(_normalise_segment(raw, workdir / f"seg_{index:03d}.mp3", ffmpeg))
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    _concat(segments, out_mp3, workdir, ffmpeg)
    duration = _duration_s(out_mp3, ffmpeg)
    _verify_decodable(out_mp3, ffmpeg)
    return duration, provider


# ---------------------------------------------------------------------- public
def build_audio(brief: dict, out_dir: Path, config: dict | None = None) -> dict:
    """Render the listenable edition for ``brief`` into ``out_dir``.

    Returns ``{"mp3": Path, "script": Path, "duration_s": float, "words": int,
    "provider": str, "meta": Path}``.
    """
    if not isinstance(brief, dict) or not brief.get("top_story") and not brief.get("sections"):
        raise ValueError("brief must be an EDITION dict with sections/top_story")
    cfg = _audio_config(config)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    max_seconds = float(cfg.get("max_seconds") or 420)

    drop_ids: set[str] = set()
    script = ""
    duration = 0.0
    provider = "gtts"
    with tempfile.TemporaryDirectory(prefix="mb_audio_", dir=None) as tmp:
        workdir = Path(tmp)
        for attempt in range(1, MAX_DURATION_ATTEMPTS + 1):
            script = build_script(brief, config, drop_ids)
            mp3_path = out_dir / "brief.mp3"
            duration, provider = render_audio(script, mp3_path, workdir, config)
            words = _word_count(script)
            print(
                f"[audio] pass {attempt}: {words} words, {provider}, "
                f"{duration:.1f}s ({duration / 60:.2f} min)"
            )
            if duration <= max_seconds or attempt == MAX_DURATION_ATTEMPTS:
                break
            candidate = _drop_candidate(brief, drop_ids)
            if not candidate:
                print("[audio] over duration budget but nothing left to drop", file=sys.stderr)
                break
            drop_ids.add(candidate)
            print(
                f"[audio] over {max_seconds:.0f}s; dropping item {candidate} and re-rendering",
                file=sys.stderr,
            )

    script_path = out_dir / "brief.script.txt"
    script_path.write_text(script + "\n", encoding="utf-8")
    words = _word_count(script)
    meta = {
        "duration_s": round(duration, 3),
        "words": words,
        "provider": provider,
        "generated_at": _now_ist().isoformat(timespec="seconds"),
    }
    meta_path = out_dir / "brief.mp3.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    if duration > max_seconds:
        print(
            f"[audio] warning: {duration:.1f}s exceeds audio.max_seconds={max_seconds:.0f}",
            file=sys.stderr,
        )
    if duration < 270:
        print(f"[audio] warning: {duration:.1f}s is shorter than 4:30", file=sys.stderr)
    return {
        "mp3": out_dir / "brief.mp3",
        "script": script_path,
        "duration_s": duration,
        "words": words,
        "provider": provider,
        "meta": meta_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the audio edition of The Morning Brief")
    parser.add_argument("--brief", required=True, help="path to brief.json EDITION")
    parser.add_argument("--out", required=True, help="output directory (out/YYYY-MM-DD)")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="config.json path")
    args = parser.parse_args(argv)

    brief = _load_json(Path(args.brief))
    config = _load_json(Path(args.config)) if Path(args.config).exists() else {}
    result = build_audio(brief, Path(args.out), config)
    mp3 = result["mp3"]
    size_mb = mp3.stat().st_size / (1024 * 1024)
    minutes, seconds = divmod(int(round(result["duration_s"])), 60)
    print(
        f"[audio] wrote {mp3} ({size_mb:.2f} MB)\n"
        f"[audio] duration {minutes}:{seconds:02d} | words {result['words']} | "
        f"provider {result['provider']}\n"
        f"[audio] script {result['script']}\n"
        f"[audio] meta   {result['meta']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
