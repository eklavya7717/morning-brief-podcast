"""Free-tier LLM access with provider fallback (OpenAI-compatible chat endpoints).

Set any of GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY; providers are tried in that order and
one that keeps failing is skipped for the rest of the run. ``chat`` returns ``None`` when nothing works,
and every caller has a non-LLM fallback, so a dead provider never stops the morning edition.
"""
from __future__ import annotations

import os
import sys
import time

import requests

PROVIDERS = (
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b"),
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemini-2.5-flash"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions", "meta-llama/llama-3.3-70b-instruct:free"),
)
_dead: set[str] = set()


def available() -> bool:
    return any(os.environ.get(env) for env, _, _ in PROVIDERS)


def chat(prompt: str, *, system: str, max_tokens: int, temperature: float = 0.4) -> str | None:
    first = True
    for env, url, model in PROVIDERS:
        key = os.environ.get(env)
        if not key or env in _dead:
            continue
        if first and os.environ.get("LLM_MODEL"):  # an override applies to the primary provider only
            model = os.environ["LLM_MODEL"]
        first = False
        body = {"model": model, "max_tokens": max_tokens + 1500, "temperature": temperature,  # reasoning models think first
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        for attempt in range(3):
            try:
                r = requests.post(url, json=body, timeout=120, headers={"Authorization": f"Bearer {key}"})
                if r.status_code == 429:  # rate limited: wait as told, then retry
                    time.sleep(min(60, int(float(r.headers.get("retry-after", 10 * (attempt + 1))))))
                    continue
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
            except Exception as exc:  # noqa: BLE001
                print(f"[llm] {env} attempt {attempt + 1} failed: {exc}", file=sys.stderr)
                time.sleep(3 * (attempt + 1))
        _dead.add(env)
        print(f"[llm] {env} disabled for this run; trying the next provider", file=sys.stderr)
    return None
