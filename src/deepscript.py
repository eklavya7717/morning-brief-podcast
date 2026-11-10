"""Long-form (30-60 min) audio script: each legal story explained, not just announced.

For every legal item with full text, a free-tier LLM call (Groq/Gemini/OpenRouter key, no
subscription) writes a spoken segment in its own words: what was decided, the
legal question, the principle applied, and how it affects people and practice.
Everything degrades safely: no API key / rate limit / bad output -> that item is
read from its summary and holdings instead; no usable items -> ``None`` and the
caller uses the short extractive script.
"""
from __future__ import annotations

import os
import re
import sys
import time

import requests

from src.audio import SOURCE_NAMES, _court_phrase, naturalise

# Any OpenAI-compatible free-tier provider works; the key's env var picks it (all have a free tier, no card).
PROVIDERS = (
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b"),
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-2.5-flash"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions", "meta-llama/llama-3.3-70b-instruct:free"),
)
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
_cache: dict[str, str] = {}


def _provider():
    for env, url, model in PROVIDERS:
        if os.environ.get(env):
            return os.environ[env], url, os.environ.get("LLM_MODEL") or model
    return None


def _chat(prompt: str, cfg: dict, max_tokens: int) -> str | None:
    prov = _provider()
    if not prov:
        return None
    key, url, model = prov
    body = {"model": model, "max_tokens": max_tokens + 1500, "temperature": 0.4,  # headroom: reasoning models think first
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"
    for attempt in range(4):
        try:
            r = requests.post(url, json=body, timeout=120, headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 429:  # free tiers are rate limited: wait as told, then retry
                time.sleep(min(60, int(float(r.headers.get("retry-after", 10 * (attempt + 1))))))
                continue
            r.raise_for_status()
            return (r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:  # noqa: BLE001
            print(f"[deepscript] llm attempt {attempt + 1} failed: {exc}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    return None


def _clean_llm(text: str) -> str:
    text = re.sub(r"[*#_`>\[\]]+", "", text)
    return naturalise(re.sub(r"\n{2,}", "\n", text)).strip()


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
    beats = (
        "as flowing speech in four beats: (1) what was decided or enacted, and by whom; (2) the legal "
        "question; (3) the legal principle and the reasoning the court or law applies; (4) how it affects "
        "litigants, authorities or legal practice, and what a student should take from it."
    )
    digest = re.search(r"round-?up|digest", (item.get("title") or "").lower())
    if digest and len(body.split()) >= 120:
        words = int(words * 1.4)
        prompt = (
            f"This SOURCE is a LiveLaw digest listing many rulings. Write about {words} words as flowing speech: "
            "pick the four most important rulings for a law student, and for each explain in two or three sentences "
            "the legal principle and its practical effect; then say in one sentence how many other rulings the digest "
            "covers. Use only what the source states.\n"
            f"HEADLINE: {item.get('title')}\nSOURCE:\n{body[:12000]}"
        )
    elif len(body.split()) >= 120:
        prompt = (
            f"Write about {words} words on this item, {beats} If the source does not say something, skip it "
            "rather than guess.\n"
            f"HEADLINE: {item.get('title')}\n{('COURT/CASE: ' + meta) if meta else ''}\n"
            f"SOURCE:\n{body[:12000]}"
        )
    else:
        # gated or thin page: only a summary exists. The model may add settled background, clearly marked,
        # but must not invent anything about this particular case.
        facts = " ".join([item.get("summary") or ""] + list(item.get("bullets") or []) + [item.get("ratio") or ""])
        words = int(words * 0.7)
        prompt = (
            f"Write about {words} words on this item, {beats} The SOURCE is only a short summary. State what "
            "it says. You may then explain the settled legal background (the statute's purpose, the doctrine "
            "involved) in general terms, introduced with a phrase like 'as background', but say nothing "
            "about this particular case beyond the source.\n"
            f"HEADLINE: {item.get('title')}\n{('COURT/CASE: ' + meta) if meta else ''}\nSOURCE:\n{facts}"
        )
    if len(body.split()) >= 120 or len(facts_len(item)) >= 25:
        got = _chat(prompt, cfg, max_tokens=int(words * 2.2))
        text = _clean_llm(got) if got and len(got.split()) >= words * 0.4 else ""
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


def build_deep_script(brief: dict, config: dict | None = None) -> str | None:
    cfg = dict((config or {}).get("audio") or {})
    target_words = int(cfg.get("target_minutes", 40)) * 155
    per_story = int(cfg.get("words_per_story", 300))
    items = [i for s in brief.get("sections", []) for i in s.get("items", [])]
    legal = [i for i in items if i.get("source") in {"livelaw", "sccblog", "scobserver", "barandbench"}
             or i.get("category") in {"supreme-court", "high-court", "legislation", "judiciary"}]
    briefs = [i for i in items if i not in legal and i.get("category") != "editorial"]
    opinion = [i for i in items if i.get("category") == "editorial"]
    if not legal:
        return None
    top = brief.get("top_story")
    legal.sort(key=lambda i: -i.get("priority", 0))
    room = max(4, int((target_words * 0.78) / per_story))
    legal = legal[:room]

    parts = [f"Good morning. This is Eklavya's Morning Brief for {brief.get('weekday', '')}, {brief.get('date_pretty', '')}. "
             f"Today we go deep on {len(legal)} legal developments, then a quick run through the news, "
             "and the day's opinion pages. Let us begin."]
    if top and top in legal:
        parts += ["Our lead story.", explain(top, cfg, int(per_story * 1.5))]
        legal.remove(top)
    for cat, heading in SECTIONS:
        group = [i for i in legal if i.get("category") == cat]
        if cat == "judiciary":
            group = [i for i in legal if i.get("category") not in {"supreme-court", "high-court", "legislation"}]
        if group:
            parts.append(heading)
            parts += [explain(i, cfg, per_story) for i in group]
    if briefs:
        parts.append("Now, the news in brief, across domains.")
        parts += [_brief_line(i) for i in briefs[:14]]
    if opinion:
        parts.append("And finally, from the opinion pages.")
        parts += [_brief_line(i) for i in opinion[:4]]
    capsule = [i for i in brief.get("judiciary_capsule") or [] if i.get("case_name") and i.get("ratio")]
    if capsule:
        parts.append("Before you go, three lines to carry with you.")
        parts += [naturalise(f"{i['case_name']}: {i['ratio']}") for i in capsule]
    parts.append("That is all for today. Revise well, and see you tomorrow.")
    return "\n\n".join(p for p in parts if p.strip())
