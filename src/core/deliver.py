"""Delivery: push the finished edition to the user's iPhone/iPad.

Channels
--------
ntfy  : zero-credential push. Text message + audio attachment + PDF attachment.
        The user installs the free ntfy app and subscribes to one private topic.
telegram: optional bot delivery (text + audio + document) when a bot token and
        chat id are configured.

Everything degrades gracefully: a failing channel is logged, never fatal.
"""
from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path

import requests

from . import fetch

NTFY_MAX_BODY = 4096  # ntfy.sh message body limit (bytes, UTF-8)
NTFY_SAFE_BODY = 3800  # keep headroom for multi-byte punctuation


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _trim_to_bytes(text: str, limit: int) -> str:
    """Trim whole trailing lines until the message fits ntfy's byte limit."""
    if _byte_len(text) <= limit:
        return text
    lines = text.split("\n")
    while lines and _byte_len("\n".join(lines)) > limit:
        lines.pop()
    return "\n".join(lines).rstrip() + "\n…"


# --------------------------------------------------------------------- text
def format_push_text(brief: dict, max_chars: int = NTFY_SAFE_BODY) -> str:
    """The notification body: a mini front page that reads in 20 seconds."""
    lines: list[str] = []
    lines.append(f"THE MORNING BRIEF · {brief.get('weekday','')}, {brief.get('date_pretty','')}")
    lines.append(f"~{brief.get('read_minutes', 5)} min read · full audio in your Podcasts app · PDF attached")
    lines.append("")
    top = brief.get("top_story")
    if top:
        lines.append("TOP STORY")
        lines.append(top["title"])
        if top.get("summary"):
            lines.append(top["summary"])
        meta_bits = [b for b in (top.get("court"), top.get("case_name")) if b]
        if meta_bits:
            lines.append(" · ".join(meta_bits))
        lines.append("")
    top_id = (top or {}).get("id")
    for section in brief.get("sections", []):
        lines.append(section.get("name", "").upper())
        for item in section.get("items", []):
            if top_id and item.get("id") == top_id:
                continue
            lines.append(f"• {item['title']}")
            if item.get("ratio"):
                lines.append(f"   {item['ratio']}")
            elif item.get("summary"):
                lines.append(f"   {item['summary']}")
        lines.append("")
    capsule = brief.get("judiciary_capsule") or []
    if capsule:
        lines.append("CASE LAW TO REMEMBER")
        for item in capsule[:2]:
            bits = [b for b in (item.get("case_name"), item.get("court")) if b]
            lines.append(f"- {' / '.join(bits)}: {item.get('ratio') or item.get('summary','')}")
        lines.append("")
    lines.append("Sources: LiveLaw · Indian Express · The Hindu")
    text = "\n".join(lines).strip()
    return _trim_to_bytes(text, max_chars)


# --------------------------------------------------------------------- ntfy
def push_ntfy(
    brief: dict,
    artifacts: dict[str, Path | None],
    config: dict,
    *,
    log=print,
) -> dict:
    cfg = (config.get("delivery") or {}).get("ntfy") or {}
    if not cfg.get("enabled"):
        return {"channel": "ntfy", "status": "disabled"}
    server = (cfg.get("server") or "https://ntfy.sh").rstrip("/")
    topic = cfg.get("topic")
    if not topic:
        return {"channel": "ntfy", "status": "error", "detail": "no topic configured"}

    result: dict = {"channel": "ntfy", "status": "ok", "sent": []}
    title = f"THE MORNING BRIEF · {brief.get('weekday','')[:3]} {brief.get('date_pretty','')} · {brief.get('read_minutes',5)} min"

    # 1. the readable digest
    payload = {
        "topic": topic,
        "title": title,
        "message": format_push_text(brief),
        "priority": cfg.get("priority", 3),
        "tags": cfg.get("tags") or ["newspaper"],
        "click": (brief.get("top_story") or {}).get("url"),
    }
    listen = artifacts.get("listen_url")
    if listen:  # audio is too big to attach: one tap opens it in the browser player
        mins = int((brief.get("audio_meta") or {}).get("duration_s") or 0) // 60
        payload["actions"] = [{"action": "view", "label": f"Listen ({mins} min)" if mins else "Listen", "url": str(listen)}]
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        resp = requests.post(server, json=payload, timeout=30)
        resp.raise_for_status()
        result["sent"].append("text")
        log(f"ntfy: text pushed ({len(payload['message'])} chars)")
    except Exception as exc:  # noqa: BLE001
        result["status"] = "partial"
        result["text_error"] = str(exc)
        log(f"ntfy: text push FAILED: {exc}")

    # 2. attachments (audio first so the listening option is obvious)
    # ntfy caps attachments at ~2 MB, so prefer the compact push copy.
    # NB: ntfy headers must be latin-1 safe - no emoji in X-Title/X-Message.
    audio_path = artifacts.get("mp3_lite") or artifacts.get("mp3")
    attachments = [
        ("headlines", audio_path, "Headlines audio (about 6 min). Full edition is in Podcasts", "headphones"),
        ("print", artifacts.get("pdf"), "Print edition (PDF)", "newspaper"),
        ("study", artifacts.get("study_pdf"), "Study sheet: quiz, maxim, flashcards", "books"),
    ]
    for kind, path, attachment_title, tag in attachments:
        if not path or not Path(path).exists():
            continue
        path = Path(path)
        if path.stat().st_size > float(cfg.get("attach_max_mb", 2)) * 1_000_000:
            log(f"ntfy: {kind} skipped ({path.stat().st_size // 1_000_000} MB is over the attachment cap)")
            continue
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {
            "X-Filename": path.name,
            "X-Title": attachment_title,
            "X-Message": f"Tap to open the {kind} edition",
            "X-Priority": str(cfg.get("priority", 3)),
            "X-Tags": tag,
            "Content-Type": ctype,
        }
        for attempt in range(2):
            try:
                resp = requests.put(
                    f"{server}/{topic}",
                    data=path.read_bytes(),
                    headers=headers,
                    timeout=120,
                )
                resp.raise_for_status()
                result["sent"].append(kind)
                log(f"ntfy: {kind} attached ({path.stat().st_size // 1024} KB)")
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 1:
                    result["status"] = "partial"
                    result.setdefault("attachment_errors", []).append(f"{kind}: {exc}")
                    log(f"ntfy: {kind} attach FAILED: {exc}")
                else:
                    time.sleep(2)
    return result


# ----------------------------------------------------------------- telegram
def telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def discover_chat_id(token: str, *, log=print) -> str | None:
    """After the user presses Start on the bot, find the chat id."""
    try:
        resp = requests.get(telegram_api(token, "getUpdates"), timeout=30)
        data = resp.json()
        for update in reversed(data.get("result") or []):
            msg = update.get("message") or update.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                log(f"telegram: chat found ({chat.get('type')}) id={chat['id']}")
                return str(chat["id"])
        log("telegram: no updates yet - press Start in the bot chat first")
    except Exception as exc:  # noqa: BLE001
        log(f"telegram: discover failed: {exc}")
    return None


def push_telegram(
    brief: dict,
    artifacts: dict[str, Path | None],
    config: dict,
    *,
    log=print,
) -> dict:
    cfg = (config.get("delivery") or {}).get("telegram") or {}
    if not cfg.get("enabled"):
        return {"channel": "telegram", "status": "disabled"}
    token = cfg.get("bot_token") or ""
    chat_id = str(cfg.get("chat_id") or "")
    if not token or not chat_id:
        return {"channel": "telegram", "status": "error", "detail": "bot_token/chat_id missing"}

    result: dict = {"channel": "telegram", "status": "ok", "sent": []}

    # 1. text
    try:
        resp = requests.post(
            telegram_api(token, "sendMessage"),
            data={
                "chat_id": chat_id,
                "text": format_push_text(brief, max_chars=4000),
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        resp.raise_for_status()
        result["sent"].append("text")
        log("telegram: text pushed")
    except Exception as exc:  # noqa: BLE001
        result["status"] = "partial"
        result["text_error"] = str(exc)
        log(f"telegram: text FAILED: {exc}")

    # 2. audio as a proper voice-note style audio file
    mp3 = artifacts.get("mp3")
    if mp3 and Path(mp3).exists():
        try:
            with open(mp3, "rb") as fh:
                resp = requests.post(
                    telegram_api(token, "sendAudio"),
                    data={
                        "chat_id": chat_id,
                        "title": f"The Morning Brief - {brief.get('date_pretty','')}",
                        "performer": "The Morning Brief",
                        "duration": int((brief.get("audio_meta") or {}).get("duration_s") or 0),
                    },
                    files={"audio": (Path(mp3).name, fh, "audio/mpeg")},
                    timeout=180,
                )
            resp.raise_for_status()
            result["sent"].append("audio")
            log("telegram: audio pushed")
        except Exception as exc:  # noqa: BLE001
            result["status"] = "partial"
            result.setdefault("attachment_errors", []).append(f"audio: {exc}")
            log(f"telegram: audio FAILED: {exc}")

    # 3. PDF as a document
    pdf = artifacts.get("pdf")
    if pdf and Path(pdf).exists():
        try:
            with open(pdf, "rb") as fh:
                resp = requests.post(
                    telegram_api(token, "sendDocument"),
                    data={"chat_id": chat_id, "caption": "Print edition (PDF) - 5 min read"},
                    files={"document": (Path(pdf).name, fh, "application/pdf")},
                    timeout=180,
                )
            resp.raise_for_status()
            result["sent"].append("pdf")
            log("telegram: pdf pushed")
        except Exception as exc:  # noqa: BLE001
            result["status"] = "partial"
            result.setdefault("attachment_errors", []).append(f"pdf: {exc}")
            log(f"telegram: pdf FAILED: {exc}")

    return result


# -------------------------------------------------------------------- entry
def deliver_all(
    brief: dict,
    artifacts: dict[str, Path | None],
    config: dict,
    *,
    log=print,
) -> list[dict]:
    results = []
    results.append(push_ntfy(brief, artifacts, config, log=log))
    results.append(push_telegram(brief, artifacts, config, log=log))
    return results


def _load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


if __name__ == "__main__":  # small utilities
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    ap = argparse.ArgumentParser(description="Morning Brief delivery utilities")
    ap.add_argument("--brief", type=Path, help="brief.json to push as a test")
    ap.add_argument("--out", type=Path, default=None, help="dir with brief.mp3/brief.pdf")
    ap.add_argument("--config", type=Path, default=None)
    ap.add_argument("--telegram-discover", action="store_true", help="print the chat id after /start")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    cfg_path = args.config or (root / "config.json")
    config = json.loads(cfg_path.read_text(encoding="utf-8"))

    if args.telegram_discover:
        token = ((config.get("delivery") or {}).get("telegram") or {}).get("bot_token")
        if not token:
            raise SystemExit("set delivery.telegram.bot_token in config.json first")
        discover_chat_id(token)

    if args.brief:
        brief = _load_json(args.brief)
        out_dir = args.out or args.brief.parent
        artifacts = {"pdf": Path(out_dir) / "brief.pdf", "mp3": Path(out_dir) / "brief.mp3"}
        for res in deliver_all(brief, artifacts, config):
            print(json.dumps(res, indent=2))
