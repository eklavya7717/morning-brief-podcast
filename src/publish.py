"""Publish feed.xml + recent episode MP3s to a GitHub Pages repo (the podcast host).

config.delivery.podcast = {"site_repo": "<user>/<repo>", "base_url": "https://<user>.github.io/<repo>"}
The local clone lives in ``<root>/podcast-site``. Uses the machine's existing git/gh auth.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from src.podcast import MAX_ITEMS, build_feed, collect_episodes

ROOT = Path(__file__).resolve().parents[1]


def _git(site: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(site), *args], capture_output=True, text=True, timeout=180)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def publish_podcast(out_root: Path, config: dict, *, log=print) -> dict:
    pod = config["delivery"]["podcast"]
    site = ROOT / "podcast-site"
    if not (site / ".git").exists():
        subprocess.run(["git", "clone", f"https://github.com/{pod['site_repo']}.git", str(site)],
                       check=True, capture_output=True, timeout=180)
    _git(site, "pull", "--rebase", "--autostash")

    episodes = collect_episodes(out_root, MAX_ITEMS)
    keep = {e["date_str"] for e in episodes}
    for old in site.glob("20??-??-??"):
        if old.name not in keep:
            shutil.rmtree(old)
    for e in episodes:
        dest = site / e["date_str"]
        dest.mkdir(exist_ok=True)
        shutil.copy2(e["mp3"], dest / "brief.mp3")
    feed = build_feed(out_root, config)
    shutil.copy2(feed, site / "feed.xml")
    (site / "index.html").write_text(
        "<!doctype html><title>The Morning Brief</title><h1>The Morning Brief</h1>"
        "<p>Private podcast feed: <a href='feed.xml'>feed.xml</a></p>", encoding="utf-8")

    _git(site, "add", "-A")
    if not _git(site, "status", "--porcelain").strip():
        return {"channel": "podcast", "status": "ok", "detail": "no changes"}
    _git(site, "commit", "-m", f"Edition {episodes[0]['date_str']}" if episodes else "Update")
    _git(site, "push")
    log(f"podcast: published {len(episodes)} episodes to {pod['base_url']}/feed.xml")
    return {"channel": "podcast", "status": "ok", "feed": f"{pod['base_url']}/feed.xml"}


def upload_release(repo: str, date: str, mp3: Path, *, log=print) -> str | None:
    """Host the episode audio as a GitHub release asset; returns its public URL (None on failure)."""
    tag = f"ed-{date}"
    base = ["gh", "release"]
    try:
        r = subprocess.run([*base, "create", tag, str(mp3), "--repo", repo, "--title", f"Edition {date}",
                            "--notes", f"Audio for Eklavya's Morning Brief, {date}"], capture_output=True, text=True, timeout=600)
        if r.returncode:  # tag exists (re-run): replace the asset instead
            subprocess.run([*base, "upload", tag, str(mp3), "--repo", repo, "--clobber"],
                           check=True, capture_output=True, text=True, timeout=600)
        url = f"https://github.com/{repo}/releases/download/{tag}/{Path(mp3).name}"
        log(f"podcast: audio uploaded -> {url}")
        return url
    except Exception as exc:  # noqa: BLE001 - delivery must not depend on hosting
        log(f"podcast: release upload FAILED: {exc}")
        return None
