"""Quick QA report for probe outputs and rendered editions.

    python tools/qa_report.py                # all probes + latest edition
    python tools/qa_report.py out/probe/ie.json
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def show_items(path: Path, limit: int = 8) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or data
    print(f"\n=== {path} - {len(data)} items")
    for item in data[:limit]:
        src = item.get("source", "?")
        court = item.get("court") or ""
        case = item.get("case_name") or ""
        ratio = item.get("ratio") or ""
        print(f"[{item.get('priority', 0):>3}] {src:<13} {item.get('category',''):<12} {item.get('title','')[:78]}")
        if item.get("summary"):
            print(f"      {item['summary'][:170]}")
        if court or case or ratio:
            print(f"      court={court!r} case={case!r} ratio={ratio[:110]!r}")
        print(f"      pub={item.get('published')} tags={item.get('tags')}")
        if not item.get("summary"):
            print("      !! EMPTY SUMMARY")


def mp3_duration(path: Path) -> float | None:
    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        out = subprocess.run([ff, "-i", str(path)], capture_output=True, text=True, errors="replace").stderr
        for line in out.splitlines():
            if "Duration:" in line:
                stamp = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = stamp.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return None
    return None


def show_edition(path: Path) -> None:
    brief = json.loads(path.read_text(encoding="utf-8"))
    print(f"\n=== EDITION {path}")
    print(f"date={brief.get('date')} weekday={brief.get('weekday')} read={brief.get('read_minutes')} min "
          f"selected={brief.get('stats', {}).get('selected')} words={brief.get('stats', {}).get('words')}")
    print(f"top: {(brief.get('top_story') or {}).get('title', '-')[:90]}")
    for section in brief.get("sections", []):
        print(f"  {section['name']} ({len(section['items'])})")
        for item in section["items"]:
            print(f"    - [{item.get('priority')}] {item.get('title','')[:80]}")
    print("  capsule:", [c.get("case_name") or c.get("title", "")[:40] for c in brief.get("judiciary_capsule", [])])
    d = path.parent
    for name in ("brief.pdf", "brief.mp3", "brief.html", "feed.xml"):
        f = d / name
        if f.exists():
            extra = f" ({mp3_duration(f):.0f}s)" if name.endswith(".mp3") and mp3_duration(f) else ""
            print(f"  artifact: {name} {f.stat().st_size // 1024} KB{extra}")


def main() -> int:
    targets = sys.argv[1:]
    if targets:
        for t in targets:
            p = Path(t)
            if p.suffix == ".json" and p.name == "brief.json":
                show_edition(p)
            elif p.suffix == ".json":
                show_items(p)
            else:
                print(f"{p}: {p.stat().st_size // 1024} KB")
        return 0

    for probe in sorted((ROOT / "out" / "probe").glob("*.json")):
        show_items(probe)
    editions = sorted((ROOT / "out").glob("*/brief.json"))
    if editions:
        show_edition(editions[-1])
    feeds = sorted((ROOT / "out").glob("feed.xml"))
    for feed in feeds:
        print(f"\n=== FEED {feed} ({feed.stat().st_size // 1024} KB)")
        head = feed.read_text(encoding="utf-8")[:400]
        print(head)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
