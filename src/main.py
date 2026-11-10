"""The Morning Brief - daily newspaper-style digest orchestrator.

Usage (from the morning-brief folder):

    python -m src.main                    # full run: collect -> rank -> PDF -> audio -> push
    python -m src.main --dry-run          # build everything, do not push
    python -m src.main --skip-audio       # faster run without narration
    python -m src.main --date 2026-11-10  # rebuild a given edition
    python -m src.main --brief out/2026-11-10/brief.json   # re-render an existing edition
    python -m src.main --test-push        # send the last edition again to test devices
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import fetch, rank  # noqa: E402
from src.core.deliver import deliver_all, format_push_text  # noqa: E402

LOG = logging.getLogger("brief")


# --------------------------------------------------------------------------- setup
def setup_logging(logs_dir: Path, date: str, verbose: bool = True) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"{date}.log"
    LOG.setLevel(logging.INFO)
    LOG.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    LOG.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(message)s"))
        LOG.addHandler(sh)
    return log_file


def load_config(path: Path | None = None) -> dict:
    cfg_path = Path(path) if path else ROOT / "config.json"
    config = json.loads(cfg_path.read_text(encoding="utf-8"))
    config["_config_path"] = str(cfg_path)
    env = os.environ.get  # CI injects secrets here, so no credential ever sits in a file
    if env("NTFY_TOPIC"):
        config["delivery"]["ntfy"]["topic"] = env("NTFY_TOPIC")
    if env("TG_SESSION"):
        config["sources"]["telegram_pdf"] = {
            "api_id": int(env("TG_API_ID")), "api_hash": env("TG_API_HASH"),
            "session_string": env("TG_SESSION"), "max_mb": 80,
            # TG_CHATS="-100123:jaipur|delhi,-100456" (optional city editions, in preference order)
            "chats": [{"id": int(c.split(":")[0]), "match": [x for x in c.partition(":")[2].split("|") if x]} for c in env("TG_CHATS", "").split(",") if c.strip()],
        }
    if env("BRIEF_OUT"):
        config.setdefault("paths", {})["out"] = env("BRIEF_OUT")
    return config


# ------------------------------------------------------------------------- collect
def collect(config: dict, *, no_cache: bool = False) -> tuple[dict, dict]:
    """Fetch all enabled sources in parallel. Returns (items_by_source, errors)."""
    sources_cfg = config.get("sources") or {}
    if no_cache:
        # source modules cache HTTP responses internally; a cold cache is the
        # simple, uniform way to force fresh fetches.
        for cached in fetch.CACHE_DIR.glob("*.txt"):
            try:
                cached.unlink()
            except OSError:
                pass
    jobs: dict[str, callable] = {}

    if sources_cfg.get("livelaw", True):
        from src.sources import livelaw

        jobs["livelaw"] = lambda: livelaw.fetch(n=18, config=config)

    if sources_cfg.get("indianexpress", True):
        from src.sources import indianexpress

        jobs["indianexpress"] = lambda: indianexpress.fetch(n=15, config=config)

    if sources_cfg.get("thehindu", True):
        from src.sources import thehindu

        jobs["thehindu"] = lambda: thehindu.fetch(n=15, config=config)

    if sources_cfg.get("legalfeeds", True):
        from src.sources import legalfeeds

        jobs["legalfeeds"] = lambda: legalfeeds.fetch(n=22, config=config)

    if sources_cfg.get("telegram_pdf"):  # dict with api_id/api_hash/chats; False = off
        from src.sources import telegram_pdf

        jobs["telegram-pdf"] = lambda: telegram_pdf.fetch(n=12, config=config)

    items: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    if not jobs:
        return items, {"all": "no sources enabled"}

    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        futures = {pool.submit(fn): name for name, fn in jobs.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                got = future.result()
                items[name] = got or []
                LOG.info(f"collect: {name}: {len(items[name])} items")
            except Exception as exc:  # noqa: BLE001
                errors[name] = f"{type(exc).__name__}: {exc}"
                LOG.error(f"collect: {name} FAILED: {exc}")
                LOG.debug(traceback.format_exc())
    return items, errors


# -------------------------------------------------------------------------- build
# ntfy.sh rejects attachments above ~2 MB, so the push gets a compressed copy.
NTFY_AUDIO_LIMIT = 1_900_000


def make_lite_mp3(mp3: Path, target_bytes: int = NTFY_AUDIO_LIMIT) -> Path | None:
    """Transcode the narration down to fit ntfy's attachment cap (voice is fine)."""
    try:
        import subprocess

        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        lite = mp3.with_name(mp3.stem + ".lite.mp3")
        for bitrate, rate in ((48, 24000), (40, 24000), (32, 22050), (24, 16000)):
            subprocess.run(
                [
                    ff, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(mp3),
                    "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k",
                    "-ar", str(rate), "-ac", "1", str(lite),
                ],
                check=True,
            )
            if lite.exists() and lite.stat().st_size <= target_bytes:
                LOG.info(f"audio: push copy {lite.name} ({lite.stat().st_size // 1024} KB @ {bitrate}k)")
                return lite
        return lite if lite.exists() else None
    except Exception as exc:  # noqa: BLE001
        LOG.warning(f"audio: could not build push copy: {exc}")
        return None


def build_short_audio(brief: dict, config: dict, out_dir: Path) -> Path | None:
    """The 5-6 minute headlines edition (the original extractive script), rendered beside the long one."""
    try:
        short_dir = out_dir / "short"
        short_dir.mkdir(exist_ok=True)
        cfg = json.loads(json.dumps({k: v for k, v in config.items() if not k.startswith("_")}))
        cfg["audio"].update(deep=False, min_words=780, max_words=900, max_seconds=420)
        cfg_path = short_dir / "config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        subprocess.run([sys.executable, "-m", "src.audio", "--brief", str(out_dir / "brief.json"),
                        "--out", str(short_dir), "--config", str(cfg_path)],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=600, check=True)
        mp3 = short_dir / "brief.mp3"
        return mp3 if mp3.exists() and mp3.stat().st_size > 10_000 else None
    except Exception as exc:  # noqa: BLE001 - the headlines copy is a bonus
        LOG.warning(f"audio: headlines version failed: {exc}")
        return None


def build_artifacts(
    brief: dict,
    config: dict,
    out_dir: Path,
    *,
    skip_audio: bool = False,
    skip_pdf: bool = False,
    log=LOG.info,
) -> tuple[dict, dict]:
    """Render PDF + audio from an edition. Returns (artifacts, extra)."""
    artifacts: dict[str, Path | None] = {
        "pdf": None,
        "mp3": None,
        "mp3_lite": None,
        "script": None,
        "feed": None,
    }
    extra: dict = {}

    # Audio first: compose() reads brief["audio_meta"] to print the real
    # "≈6 min listen" duration on the masthead and footer.
    if not skip_audio and (config.get("audio") or {}).get("enabled", True):
        # Run narrration as an isolated process with a hard deadline: a stalled
        # TTS request must never stop the morning push.
        audio_cfg = config.get("audio") or {}
        audio_timeout = int(audio_cfg.get("build_timeout_s") or 300)
        try:
            cmd = [
                sys.executable, "-m", "src.audio",
                "--brief", str(out_dir / "brief.json"),
                "--out", str(out_dir),
                "--config", str(config.get("_config_path") or (ROOT / "config.json")),
            ]
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=audio_timeout,
            )
            mp3 = out_dir / "brief.mp3"
            if proc.returncode != 0 or not mp3.exists() or mp3.stat().st_size < 10_000:
                tail = (proc.stderr or proc.stdout or "")[-700:]
                raise RuntimeError(f"audio build failed (exit {proc.returncode}): {tail}")
            meta_path = out_dir / "brief.mp3.meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            artifacts["mp3"] = mp3
            script_path = out_dir / "brief.script.txt"
            artifacts["script"] = script_path if script_path.exists() else None
            # a long edition cannot fit ntfy's attachment cap; it ships via the podcast instead
            if (meta.get("duration_s") or 0) < 900:
                artifacts["mp3_lite"] = make_lite_mp3(mp3)
            else:  # long edition: ntfy gets a ~6 minute headlines version instead
                short = build_short_audio(brief, config, out_dir)
                artifacts["mp3_lite"] = make_lite_mp3(short) if short else None
            brief["audio_meta"] = {
                "duration_s": meta.get("duration_s"),
                "words": meta.get("words"),
                "provider": meta.get("provider"),
            }
            extra["audio_duration_s"] = meta.get("duration_s")
            duration = int(meta.get("duration_s") or 0)
            log(f"audio: {mp3.name} ({duration // 60}:{duration % 60:02d}, provider={meta.get('provider')})")
        except subprocess.TimeoutExpired:
            extra["audio_error"] = f"timeout after {audio_timeout}s"
            log(f"audio: TIMEOUT after {audio_timeout}s - delivering without audio")
        except Exception as exc:  # noqa: BLE001
            extra["audio_error"] = f"{type(exc).__name__}: {exc}"
            log(f"audio: FAILED: {exc}")
            log.debug(traceback.format_exc())

    if not skip_pdf and (config.get("pdf") or {}).get("enabled", True):
        try:
            from src.compose import compose

            result = compose(brief, out_dir, config)
            artifacts["pdf"] = Path(result["pdf"])
            artifacts["html"] = Path(result["html"])
            extra["pdf_pages"] = result.get("pages")
            log(f"pdf: {artifacts['pdf'].name} ({extra['pdf_pages']} pages)")
        except Exception as exc:  # noqa: BLE001
            extra["pdf_error"] = f"{type(exc).__name__}: {exc}"
            log(f"pdf: FAILED: {exc}")
            log.debug(traceback.format_exc())

    try:
        from src.podcast import build_feed

        artifacts["feed"] = build_feed(ROOT / (config.get("paths", {}).get("out") or "out"), config)
        log(f"podcast: {Path(artifacts['feed']).name}")
    except Exception as exc:  # noqa: BLE001
        extra["podcast_error"] = f"{type(exc).__name__}: {exc}"
        log(f"podcast: feed not built: {exc}")

    return artifacts, extra


def write_edition_files(brief: dict, out_dir: Path, config: dict) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    brief_path = fetch.save_json(out_dir / "brief.json", brief)
    text = format_push_text(brief, max_chars=20000)
    (out_dir / "brief.txt").write_text(text, encoding="utf-8")
    return {"brief": brief_path, "text": out_dir / "brief.txt"}


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build and deliver The Morning Brief")
    ap.add_argument("--date", help="edition date YYYY-MM-DD (default: today IST)")
    ap.add_argument("--config", type=Path, help="alternate config.json")
    ap.add_argument("--brief", type=Path, help="re-render/push an existing brief.json")
    ap.add_argument("--dry-run", action="store_true", help="build but do not push")
    ap.add_argument("--skip-audio", action="store_true")
    ap.add_argument("--skip-pdf", action="store_true")
    ap.add_argument("--no-cache", action="store_true", help="ignore HTTP cache")
    ap.add_argument("--test-push", action="store_true", help="re-push the latest built edition")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    started = time.time()
    config = load_config(args.config)
    date = args.date or fetch.today_ist()
    out_root = ROOT / (config.get("paths", {}).get("out") or "out")
    out_dir = out_root / date

    log_file = setup_logging(ROOT / (config.get("paths", {}).get("logs") or "logs"), date, verbose=not args.quiet)
    LOG.info("=" * 70)
    LOG.info(f"THE MORNING BRIEF · {date}")
    LOG.info("=" * 70)

    if args.test_push:
        brief_file = out_root / date / "brief.json"
        if not brief_file.exists():
            latest = sorted(out_root.glob("*/brief.json"))
            if not latest:
                LOG.error("no built edition to push")
                return 1
            brief_file = latest[-1]
        brief = json.loads(brief_file.read_text(encoding="utf-8"))
        out_dir = brief_file.parent
        artifacts = {"pdf": out_dir / "brief.pdf", "mp3": out_dir / "brief.mp3"}
        lite = out_dir / "brief.lite.mp3"
        if lite.exists():
            artifacts["mp3_lite"] = lite
        results = deliver_all(brief, artifacts, config, log=LOG.info)
        summary = {"test_push": True, "date": brief.get("date"), "delivery": results}
        print(json.dumps(summary, indent=2))
        return 0

    errors: dict = {}
    if args.brief:
        brief = json.loads(Path(args.brief).read_text(encoding="utf-8"))
        out_dir = Path(args.brief).parent
        LOG.info(f"re-rendering existing edition: {args.brief}")
        stats = dict(brief.get("stats") or {})
    else:
        items, errors = collect(config, no_cache=args.no_cache)
        if config.get("archive"):  # study layer on (cloud config): skip stories used in the last 14 days
            from src import study

            items, repeats = study.filter_seen(items, ROOT / "seen.json", date)
            LOG.info(f"no-repeat: skipped {repeats} stories already used")
        if not items:
            LOG.error("all sources failed - nothing to publish")
            return 1
        stats = {"generated_in_seconds": round(time.time() - started, 1)}
        brief = rank.build_edition(items, config, date=date, stats=stats)
        if not brief.get("sections"):
            LOG.error("editorial desk produced an empty edition")
            return 1
        LOG.info(
            f"ranked: {brief['stats']['selected']} stories, ~{brief['read_minutes']} min read"
        )

    if config.get("archive"):
        from src import study

        study.prepare(brief, ROOT, date)
    files = write_edition_files(brief, out_dir, config)
    artifacts, extra = build_artifacts(
        brief, config, out_dir, skip_audio=args.skip_audio, skip_pdf=args.skip_pdf
    )
    # persist audio metadata learned during rendering
    fetch.save_json(out_dir / "brief.json", brief)

    if config.get("archive") and not args.dry_run:
        try:
            from src import study

            study.finish(brief, config, out_dir, ROOT, date, artifacts, log=LOG.info)
        except Exception as exc:  # noqa: BLE001 - study extras must never block the edition
            LOG.error(f"study layer FAILED: {exc}")
    pod = (config.get("delivery") or {}).get("podcast") or {}
    if pod.get("release_repo") and artifacts.get("mp3") and not args.dry_run:
        from src.publish import upload_release

        artifacts["listen_url"] = upload_release(pod["release_repo"], date, artifacts["mp3"], log=LOG.info)

    delivery = []
    if args.dry_run:
        LOG.info("dry-run: not pushing")
    else:
        delivery = deliver_all(brief, artifacts, config, log=LOG.info)
        if (config.get("delivery", {}).get("podcast") or {}).get("site_repo"):
            from src.publish import publish_podcast

            try:
                delivery.append(publish_podcast(out_root, config, log=LOG.info))
            except Exception as exc:  # noqa: BLE001
                LOG.error(f"podcast publish FAILED: {exc}")
                delivery.append({"channel": "podcast", "status": "error", "detail": str(exc)})

    elapsed = round(time.time() - started, 1)
    manifest = {
        "date": brief.get("date"),
        "generated_at": brief.get("generated_at"),
        "elapsed_s": elapsed,
        "read_minutes": brief.get("read_minutes"),
        "items": brief.get("stats", {}).get("selected"),
        "artifacts": {k: str(v) for k, v in artifacts.items() if v},
        "extra": extra,
        "errors": errors,
        "delivery": delivery,
        "log": str(log_file),
    }
    fetch.save_json(out_dir / "manifest.json", manifest)

    LOG.info("-" * 70)
    LOG.info(f"done in {elapsed}s · read ~{brief.get('read_minutes')} min · delivery: "
             + (", ".join(f"{d['channel']}={d['status']}" for d in delivery) if delivery else "skipped"))
    if errors:
        LOG.warning(f"source errors: {errors}")
    print(json.dumps({k: manifest[k] for k in ("date", "elapsed_s", "read_minutes", "artifacts", "delivery", "errors")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
