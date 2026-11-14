"""Long-form (30-60 min) audio script: each legal story explained, not just announced.

For every legal item with full text, a free-tier LLM call (see ``src/llm.py``; no subscription) writes a
spoken segment in its own words: what was decided, the legal question, the principle applied, and how it
affects people and practice. A fact guard then drops any sentence whose section/article/number is not in
the source. Everything degrades safely: no API key / rate limit / failed guard -> that item is read from
its summary and holdings instead; no usable items -> ``None`` and the caller uses the short script.

The script carries ``[[CH:title]]`` chapter markers (stripped before speech, turned into MP3 chapters).
"""
from __future__ import annotations

import re
import time

from src import llm
from src.audio import SOURCE_NAMES, _court_phrase, naturalise

SYSTEM = (
    "You write one segment of a daily audio briefing for an Indian law student preparing for the "
    "judicial services exams. Write in your own words, grounded ONLY in the SOURCE: never add facts, "
    "case names, section numbers or holdings that are not in it. Plain spoken English for "
    "text-to-speech: no markdown, no lists, no headings, no brackets. Do not copy sentences from the "
    "source; quote at most a short phrase."
)
SECTIONS = (
    ("supreme-court", "From the Supreme Court."),
    ("high-court", "Now to the High Courts."),
    ("legislation", "New laws and notifications."),
    ("judiciary", "Around the judiciary and the profession."),
)
LEGAL_SOURCES = {"livelaw", "sccblog", "scobserver", "barandbench"}
_cache: dict[str, str] = {}
CH = "[[CH:{}]]"
CH_RE = re.compile(r"^\[\[CH:(.*?)\]\]$")


def _chat(prompt: str, cfg: dict, max_tokens: int) -> str | None:
    return llm.chat(prompt, system=SYSTEM, max_tokens=max_tokens)


def _clean_llm(text: str) -> str:
    text = re.sub(r"[*#_`>\[\]]+", "", text)
    return naturalise(re.sub(r"\n{2,}", "\n", text)).strip()


# ------------------------------------------------------------------ fact guard
_REF_RE = re.compile(r"\b(?:sections?|articles?|orders?|rules?|schedule)\s+\d+[a-z]?(?:\(\w+\))*", re.I)
_NUM_RE = re.compile(r"\b\d{2,}\b")


def _norm(text: str) -> str:
    return re.sub(r"\b(section|article|order|rule)s\b", r"\1", re.sub(r"\s+", " ", text.lower()))


def guard(text: str, source: str, max_drop: float = 0.25) -> str | None:
    """Drop sentences citing a section/article/number that the source never mentions.

    Returns the cleaned text, or None when too much had to go (the caller then falls back)."""
    src, src_nums = _norm(source), set(_NUM_RE.findall(source))
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = []
    for sent in sentences:
        refs = [_norm(r) for r in _REF_RE.findall(sent)]
        nums = _NUM_RE.findall(sent)
        if any(r not in src for r in refs) or any(n not in src_nums for n in nums):
            continue
        kept.append(sent)
    if not kept or (len(sentences) - len(kept)) / len(sentences) > max_drop:
        return None
    return " ".join(kept)


# ------------------------------------------------------------------- segments
def _fallback(item: dict) -> str:
    bits = [item.get("title", "").rstrip(".") + "."]
    if item.get("case_name") and item.get("court"):
        bits.append(f"This is {item['case_name']}, before {_court_phrase(item['court'])}.")
    if item.get("summary"):
        bits.append(item["summary"])
    bits += [b.rstrip(".") + "." for b in (item.get("bullets") or [])[:4]]
    if item.get("ratio"):
        bits.append(f"The principle: {item['ratio'].rstrip('.')}.")
    return naturalise(" ".join(bits))


def facts_len(item: dict) -> list[str]:
    return " ".join([item.get("summary") or ""] + list(item.get("bullets") or [])).split()


def explain(item: dict, cfg: dict, words: int) -> str:
    """One spoken segment for a legal item (LLM when there is text to ground it)."""
    key = f"{item.get('id')}:{words}"
    if key in _cache:
        return _cache[key]
    text = ""
    body = item.get("body") or ""
    meta = " | ".join(x for x in (item.get("court"), item.get("case_name")) if x)
    acts = ", ".join(((item.get("syllabus") or {}).get("acts") or [])[:3])
    beats = (
        "as flowing speech in five beats, giving the most time to beats 3 and 4: (1) the brief facts in two or "
        "three sentences, just enough to understand the dispute; (2) the legal question; (3) what the court held; "
        "(4) the ratio, the legal principle laid down and the reasoning behind it, stated as a rule a student can "
        "write in an answer, with the Act and section only if the source names them; (5) one closing sentence on "
        "why it matters for exam answers"
        + (f" under {acts}" if acts else "") + "."
    )
    facts = " ".join([item.get("summary") or ""] + list(item.get("bullets") or []) + [item.get("ratio") or ""])
    digest = re.search(r"round-?up|digest", (item.get("title") or "").lower())
    if digest and len(body.split()) >= 120:
        words = int(words * 1.4)
        source = body[:12000]
        prompt = (
            f"This SOURCE is a LiveLaw digest listing many rulings. Write about {words} words as flowing speech: "
            "pick the four most important rulings for a law student, and for each explain in two or three sentences "
            "the legal principle and its practical effect; then say in one sentence how many other rulings the digest "
            "covers. Use only what the source states.\n"
            f"HEADLINE: {item.get('title')}\nSOURCE:\n{source}"
        )
    elif len(body.split()) >= 120:
        source = body[:12000]
        prompt = (
            f"Write about {words} words on this item, {beats} If the source does not say something, skip it "
            "rather than guess.\n"
            f"HEADLINE: {item.get('title')}\n{('COURT/CASE: ' + meta) if meta else ''}\n"
            f"SOURCE:\n{source}"
        )
    else:
        # gated or thin page: only a summary exists. The model may add settled background, clearly marked,
        # but must not invent anything about this particular case.
        source = facts
        words = int(words * 0.7)
        prompt = (
            f"Write about {words} words on this item, {beats} The SOURCE is only a short summary. State what "
            "it says. You may then explain the settled legal background (the statute's purpose, the doctrine "
            "involved) in general terms, introduced with a phrase like 'as background', but say nothing "
            "about this particular case beyond the source.\n"
            f"HEADLINE: {item.get('title')}\n{('COURT/CASE: ' + meta) if meta else ''}\nSOURCE:\n{source}"
        )
    if len(source.split()) >= 25:
        got = _chat(prompt, cfg, max_tokens=int(words * 2.2))
        if got and len(got.split()) >= words * 0.4:
            text = guard(_clean_llm(got), f"{item.get('title')} {source}") or ""
        time.sleep(float(cfg.get("llm_pause_s", 4.5)))  # stay under the free-tier requests-per-minute cap
    text = text or _fallback(item)
    src = SOURCE_NAMES.get(item.get("source"), "")
    if src:
        text += f" That was reported by {src}."
    _cache[key] = text
    return text


def _brief_line(item: dict) -> str:
    first = re.split(r"(?<=[.!?])\s+", item.get("summary") or "")[:2]
    return naturalise(f"{item.get('title', '').rstrip('.')}. {' '.join(first)}".strip())


def recap_text(recap: dict, cfg: dict) -> str | None:
    """'Week/Month in review': ties the period's rulings together by subject, from stored ratios only."""
    items = recap.get("items") or []
    if len(items) < 4:
        return None
    lines = "\n".join(
        f"- [{', '.join(i.get('topics') or ['General'])}] {i.get('case_name') or i.get('title')}: "
        f"{i.get('ratio') or i.get('summary') or ''}" for i in items
    )
    prompt = (
        f"Write about 650 words of flowing speech reviewing this {recap.get('label', 'week')}'s rulings for a law "
        "student. Group them by subject, say what principle each lays down, and point out any common thread. "
        f"Use only what the notes state.\nNOTES:\n{lines}"
    )
    got = _chat(prompt, cfg, max_tokens=1400)
    return guard(_clean_llm(got), lines) if got else None


# --------------------------------------------------------------------- script
def build_deep_script(brief: dict, config: dict | None = None) -> str | None:
    cfg = dict((config or {}).get("audio") or {})
    target_words = int(cfg.get("target_minutes", 40)) * 155
    per_story = int(cfg.get("words_per_story", 300))
    items = [i for s in brief.get("sections", []) for i in s.get("items", [])]
    legal = [i for i in items if i.get("source") in LEGAL_SOURCES
             or i.get("category") in {"supreme-court", "high-court", "legislation", "judiciary"}]
    briefs = [i for i in items if i not in legal and i.get("category") != "editorial"]
    opinion = [i for i in items if i.get("category") == "editorial"]
    if not legal:
        return None
    top = brief.get("top_story")
    legal.sort(key=lambda i: -i.get("priority", 0))
    room = max(4, int((target_words * 0.78) / per_story))
    legal = legal[:room]

    parts = [CH.format("Introduction"),
             f"Good morning. This is Eklavya's Morning Brief for {brief.get('weekday', '')}, {brief.get('date_pretty', '')}. "
             f"Today we go deep on {len(legal)} legal developments, then a quick run through the news, "
             "and the day's opinion pages. Let us begin."]
    if top and top in legal:
        parts += [CH.format("Lead story"), "Our lead story.", explain(top, cfg, int(per_story * 1.5))]
        legal.remove(top)
    for cat, heading in SECTIONS:
        group = [i for i in legal if i.get("category") == cat]
        if cat == "judiciary":
            group = [i for i in legal if i.get("category") not in {"supreme-court", "high-court", "legislation"}]
        if group:
            parts += [CH.format(heading.rstrip(".")), heading]
            parts += [explain(i, cfg, per_story) for i in group]
    recap = brief.get("recap")
    if recap:
        text = recap_text(recap, cfg)
        if text:
            label = recap.get("label", "week").capitalize()
            parts += [CH.format(f"{label} in review"), f"And now, our {recap.get('label', 'week')} in review.", text]
    if briefs:
        parts += [CH.format("News in brief"), "Now, the news in brief, across domains."]
        parts += [_brief_line(i) for i in briefs[:14]]
    if opinion:
        parts += [CH.format("Opinion pages"), "And finally, from the opinion pages."]
        parts += [_brief_line(i) for i in opinion[:4]]
    capsule = [i for i in brief.get("judiciary_capsule") or [] if i.get("case_name") and i.get("ratio")]
    if capsule:
        parts += [CH.format("To remember"), "Before you go, three lines to carry with you."]
        parts += [naturalise(f"{i['case_name']}: {i['ratio']}") for i in capsule]
    parts.append("That is all for today. Revise well, and see you tomorrow.")
    return "\n\n".join(p for p in parts if p.strip())
