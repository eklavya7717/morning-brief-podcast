"""E-paper PDFs from Telegram (Indian Express / The Hindu) -> ITEM dicts.

Reads recent PDF documents from the chats/channels listed in
``config.sources.telegram_pdf`` with a Telegram *user* session (Telethon), so
files above the 20 MB bot-download cap work and no forwarding is needed.
Headlines are the largest-font lines on the first pages; the paragraph after
each is the summary.

One-time login (interactive, run by the user):  python -m src.sources.telegram_pdf --login
Parse a PDF offline:                            python -m src.sources.telegram_pdf --pdf file.pdf
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.fetch import ROOT, CACHE_DIR, clean, make_item

SOURCE = "telegram-pdf"
PAGES = 2  # shortcut: front pages only (OCR is slow), raise for more coverage
OCR_DPI = 130


def _ocr_boxes(pdf_path: Path, pages: int = PAGES) -> list[dict]:
    """OCR the first pages (e-paper PDFs have no copyable text). Free, offline."""
    import fitz  # pymupdf
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    boxes: list[dict] = []
    with fitz.open(str(pdf_path)) as doc:
        for pno in range(min(pages, len(doc))):
            pix = doc[pno].get_pixmap(dpi=OCR_DPI)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
            result, _ = engine(img)
            for quad, text, conf in result or []:
                xs, ys = [p[0] for p in quad], [p[1] for p in quad]
                text = clean(text)
                if text and float(conf) > 0.6:
                    boxes.append({"page": pno, "x0": min(xs), "x1": max(xs), "y0": min(ys),
                                  "y1": max(ys), "h": max(ys) - min(ys), "text": text})
    return boxes


def parse_pdf(pdf_path: Path, paper: str, published: str | None, url: str, n: int = 8) -> list[dict]:
    boxes = _ocr_boxes(Path(pdf_path))
    if not boxes:
        return []
    body_h = statistics.median(b["h"] for b in boxes)
    heads: list[dict] = []
    for b in sorted((b for b in boxes if b["h"] >= body_h * 1.7), key=lambda b: (b["page"], b["x0"] // 400, b["y0"])):
        last = heads[-1] if heads else None
        # a headline wraps onto the next line: same page, close below, similar size and column
        if last and last["page"] == b["page"] and 0 <= b["y0"] - last["y1"] < last["h"] and abs(b["h"] - last["h"]) < last["h"] * 0.3 and abs(b["x0"] - last["x0"]) < 80:
            last["text"] += " " + b["text"]
            last["y1"] = b["y1"]
            last["x1"] = max(last["x1"], b["x1"])
        else:
            heads.append(dict(b))
    items, seen = [], set()
    for hd in sorted(heads, key=lambda h: -h["h"]):
        words = len(hd["text"].split())
        if not 4 <= words <= 25 or hd["text"].lower() in seen:
            continue
        seen.add(hd["text"].lower())
        below = sorted(
            (b for b in boxes if b["page"] == hd["page"] and b["h"] < body_h * 1.4
             and 0 <= b["y0"] - hd["y1"] < body_h * 12 and b["x0"] >= hd["x0"] - 40 and b["x0"] < hd["x1"]),
            key=lambda b: b["y0"])
        summary = " ".join(b["text"] for b in below[:14])
        items.append((hd["h"], hd["text"], summary))
        if len(items) == n:
            break
    return [make_item(source=SOURCE, category="national", title=t, summary=sm, url=f"{url}#{i}",
                      published=published, priority=max(50, 78 - i * 3), tags=[paper])
            for i, (_, t, sm) in enumerate(items)]


def _paper_name(filename: str) -> str:
    low = filename.lower()
    return "The Hindu" if "hindu" in low else "Times of India" if "times" in low or "toi" in low else "Indian Express" if "express" in low or "ie" in low else "E-paper"


async def _fetch(cfg: dict, lookback_h: float) -> list[dict]:
    from telethon import TelegramClient

    since = datetime.now(timezone.utc) - timedelta(days=int(cfg.get("max_age_days", 4)))
    max_bytes = int(cfg.get("max_mb", 80)) * 1_000_000
    session = str(ROOT / cfg.get("session", "tg"))
    items: list[dict] = []
    if cfg.get("session_string"):
        from telethon.sessions import StringSession

        session = StringSession(cfg["session_string"])
    client = TelegramClient(session, int(cfg["api_id"]), cfg["api_hash"])
    await client.connect()  # not start(): that would prompt for a phone on a scheduled run
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("telegram not logged in: run python -m src.sources.telegram_pdf --login")
        for entry in cfg.get("chats", []):
            # entry: chat id, or {"id": chat id, "match": "Delhi"} to pick one city edition
            chat, match = (entry["id"], (entry.get("match") or "").lower()) if isinstance(entry, dict) else (entry, "")
            async for msg in client.iter_messages(chat, limit=120):
                if msg.date < since:
                    break
                doc = msg.document
                name = msg.file.name if msg.file else ""
                if not doc or doc.mime_type != "application/pdf" or doc.size > max_bytes:
                    continue
                if match and match not in (name or "").lower():
                    continue
                dest = CACHE_DIR / f"tg_{chat}_{msg.id}.pdf"
                if not dest.exists():
                    part = dest.with_suffix(".part")  # a killed download must not look complete
                    await msg.download_media(file=str(part))
                    part.replace(dest)
                day = msg.date.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b")
                got = parse_pdf(dest, _paper_name(name or str(chat)), msg.date.isoformat(), f"tg://{chat}/{msg.id}")
                for it in got:  # stale paper (no upload today) is labelled with its own date
                    it["tags"] = it["tags"][:5] + [f"e-paper {day}"]
                items += got
                break  # newest PDF only: a missing upload falls back to the previous day's
    finally:
        await client.disconnect()
    return items


def fetch(n: int = 15, config: dict | None = None) -> list[dict]:
    config = config or {}
    cfg = (config.get("sources") or {}).get("telegram_pdf")
    if not isinstance(cfg, dict) or not cfg.get("api_id"):
        raise RuntimeError("sources.telegram_pdf needs api_id, api_hash, chats in config.json")
    lookback = float((config.get("edition") or {}).get("lookback_hours", 30))
    return asyncio.run(_fetch(cfg, lookback))[:n]


if __name__ == "__main__":
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--pdf", type=Path)
    args = ap.parse_args()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    tg = (cfg.get("sources") or {}).get("telegram_pdf") or {}
    if args.pdf:
        for it in parse_pdf(args.pdf, _paper_name(args.pdf.name), None, "file://test"):
            print(it["priority"], it["title"], "|", it["summary"][:80])
    elif args.login:
        from telethon.sync import TelegramClient

        with TelegramClient(str(ROOT / tg.get("session", "tg")), int(tg["api_id"]), tg["api_hash"]) as c:
            print("logged in as", c.get_me().first_name)
            for d in c.iter_dialogs(limit=40):
                print(f"  {d.id}\t{d.name}")
