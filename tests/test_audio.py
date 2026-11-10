"""Tests for the audio narration and podcast feed modules.

Run with::

    python -m unittest tests.test_audio -v
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src import audio, podcast

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_BRIEF = ROOT / "docs" / "sample_brief.json"
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


def load_sample() -> dict:
    return json.loads(SAMPLE_BRIEF.read_text(encoding="utf-8"))


class ScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.brief = load_sample()
        cls.script = audio.build_script(cls.brief, {})

    def test_word_count_in_broadcast_range(self):
        words = len(self.script.split())
        self.assertGreaterEqual(words, audio.MIN_WORDS)
        self.assertLessEqual(words, audio.MAX_WORDS)

    def test_greeting_and_signoff(self):
        self.assertTrue(
            self.script.startswith(
                "Good morning. It's Monday, the 10th of November 2026, and this is The Morning Brief"
            ),
            self.script[:120],
        )
        self.assertIn(
            "That's your brief for today. Read the full edition, and have a productive day.",
            self.script,
        )

    def test_sections_present_in_order(self):
        order = [
            self.script.index("First, the top story."),
            self.script.index("Now, the Legal Ledger."),
            self.script.index("Over to the nation."),
            self.script.index("And finally, from the opinion pages."),
            self.script.index("one line to remember"),
            self.script.index("That's your brief for today."),
        ]
        self.assertEqual(order, sorted(order))

    def test_judgment_is_read_with_case_name_and_ratio(self):
        self.assertIn("In K. Vijay Kumar versus Union of India", self.script)
        self.assertIn("the Supreme Court held that", self.script)
        self.assertIn("In Rakesh Gupta versus State of Uttar Pradesh", self.script)

    def test_no_urls_or_raw_symbols(self):
        for forbidden in ("http", "www.", "&", "%", "Rs ", " CJI ", " v. ", "#"):
            self.assertNotIn(forbidden, self.script, f"script still contains {forbidden!r}")

    def test_no_all_caps_shouting_abbreviations(self):
        # naturalisation should expand the common legal abbreviations
        self.assertNotIn("SC", self.script)
        self.assertNotIn("HC", self.script)

    def test_stats_and_sources_are_spoken(self):
        self.assertIn("24 reports from LiveLaw", self.script)
        self.assertIn("That report is from", self.script)

    def test_dropping_items_shortens_the_script(self):
        short = audio.build_script(self.brief, {}, drop_ids={"c3d4e5f6a7b8"})
        self.assertLess(len(short.split()), len(self.script.split()) + 80)
        self.assertNotIn("Bharatiya Sakshya", short)

    def test_brief_without_stats_still_scripts(self):
        brief = load_sample()
        brief.pop("stats", None)
        script = audio.build_script(brief, {})
        self.assertGreaterEqual(len(script.split()), audio.MIN_WORDS - 40)
        self.assertNotIn("This morning we read", script)


class NaturaliseTests(unittest.TestCase):
    def test_abbreviations(self):
        text = audio.naturalise(
            "SC and HC judges, CJI said: Rs 250 crore is 4.1% under Art. 21 and No. 5. K. Vijay Kumar v. Union of India, R. & Co."
        )
        self.assertIn("Supreme Court", text)
        self.assertIn("High Court", text)
        self.assertIn("Chief Justice of India", text)
        self.assertIn("rupees 250 crore", text)
        self.assertIn("4.1 per cent", text)
        self.assertIn("Article 21", text)
        self.assertIn("number 5", text)
        self.assertIn("versus Union of India", text)
        self.assertNotIn("&", text)
        self.assertNotIn("%", text)

    def test_keeps_initial_capital_of_names(self):
        text = audio.naturalise("Bench: Justice V. Mohana and K. Vijay Kumar")
        self.assertIn("Justice V. Mohana", text)
        self.assertIn("K. Vijay Kumar", text)

    def test_strips_quotes_and_urls(self):
        text = audio.naturalise('He said "no" to https://example.com/story and left.')
        self.assertNotIn('"', text)
        self.assertNotIn("http", text)
        self.assertIn("He said no", text)

    def test_idempotent(self):
        once = audio.naturalise("SC held 4.1% for Rs 5 crore & more.")
        self.assertEqual(audio.naturalise(once), once)


class SplitScriptTests(unittest.TestCase):
    def test_chunks_respect_twin_limits(self):
        script = audio.build_script(load_sample(), {})
        chunks = audio.split_script(script)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), audio.MAX_CHARS)
            self.assertTrue(chunk.strip())

    def test_all_words_are_preserved(self):
        script = audio.build_script(load_sample(), {})
        chunks = audio.split_script(script)
        self.assertEqual(len(" ".join(chunks).split()), len(script.split()))

    def test_long_single_sentence_is_split(self):
        sentence = "word " * 600 + "end."
        chunks = audio.split_script(sentence)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), audio.MAX_CHARS)


class DropSelectionTests(unittest.TestCase):
    def test_lowest_priority_item_drops_first(self):
        brief = load_sample()
        candidate = audio._drop_candidate(brief, set())
        # opinion item priority 55 and the economy item 58 are the weakest stories
        self.assertIn(candidate, {"c9d0e1f2a3b4", "a7b8c9d0e1f2"})

    def test_top_story_is_never_dropped(self):
        brief = load_sample()
        drop_ids = set()
        for _ in range(12):
            candidate = audio._drop_candidate(brief, drop_ids)
            if candidate is None:
                break
            drop_ids.add(candidate)
        self.assertNotIn("a1b2c3d4e5f6", drop_ids)


def _make_silent_mp3(path: Path, seconds: float = 0.4) -> Path:
    ffmpeg = audio._ffmpeg_bin()
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(seconds), "-ar", "44100", "-ac", "1",
            "-c:a", "libmp3lame", "-b:a", "64k", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class AudioRenderTests(unittest.TestCase):
    """Offline end-to-end test: TTS is stubbed with short silent clips."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)
        self.brief = load_sample()

    def _fake_synthesize(self, script, workdir, config=None):
        segments = [
            _make_silent_mp3(Path(workdir) / "fake_0.mp3"),
            _make_silent_mp3(Path(workdir) / "fake_1.mp3"),
        ]
        return segments, "gtts"

    def test_build_audio_writes_all_outputs(self):
        out_dir = self.workdir / "edition"
        with mock.patch.object(audio, "synthesize", side_effect=self._fake_synthesize):
            result = audio.build_audio(self.brief, out_dir, {"audio": {"max_seconds": 420}})
        self.assertTrue(result["mp3"].exists())
        self.assertGreater(result["mp3"].stat().st_size, 2000)
        self.assertTrue(result["script"].exists())
        self.assertGreaterEqual(result["words"], audio.MIN_WORDS)
        self.assertGreater(result["duration_s"], 0)
        self.assertEqual(result["provider"], "gtts")
        meta = json.loads((out_dir / "brief.mp3.meta.json").read_text(encoding="utf-8"))
        self.assertIn("duration_s", meta)
        self.assertIn("words", meta)
        self.assertIn("provider", meta)
        self.assertIn("generated_at", meta)
        # decodable by ffmpeg (the acceptance check)
        check = subprocess.run(
            [audio._ffmpeg_bin(), "-v", "error", "-i", str(result["mp3"]), "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_build_audio_rejects_empty_brief(self):
        with self.assertRaises(ValueError):
            audio.build_audio({}, self.workdir / "x", {})

    def test_over_long_audio_drops_items_and_retries(self):
        out_dir = self.workdir / "long"
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with mock.patch.object(audio, "synthesize", side_effect=self._fake_synthesize):
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                result = audio.build_audio(self.brief, out_dir, {"audio": {"max_seconds": 1.0}})
        stdout, stderr = out_buf.getvalue(), err_buf.getvalue()
        self.assertIn("pass 3", stdout)  # initial render + two retries
        self.assertIn("dropping item", stderr)
        self.assertTrue(result["script"].exists())
        self.assertGreater(result["mp3"].stat().st_size, 2000)

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_sapi_fallback_renders_audio(self):
        workdir = self.workdir / "sapi"
        workdir.mkdir()
        wav = audio._sapi_chunk("Good morning. This is the offline voice fallback.", 0, workdir)
        self.assertGreater(wav.stat().st_size, 2000)
        with mock.patch.object(
            audio, "_render_gtts", side_effect=RuntimeError("simulated gTTS block")
        ):
            raw, provider = audio.synthesize(
                "Good morning. This is a test of the fallback.", workdir, {}
            )
        self.assertEqual(provider, "sapi")
        self.assertTrue(raw)
        self.assertTrue(all(path.exists() for path in raw))


def _write_edition(root: Path, date_str: str, titles: list[str], duration_s: float) -> Path:
    edition_dir = root / date_str
    edition_dir.mkdir(parents=True, exist_ok=True)
    (edition_dir / "brief.mp3").write_bytes(b"ID3" + b"\x00" * 4000)
    (edition_dir / "brief.mp3.meta.json").write_text(
        json.dumps({"duration_s": duration_s, "words": 800, "provider": "gtts", "generated_at": "x"}),
        encoding="utf-8",
    )
    (edition_dir / "brief.json").write_text(
        json.dumps(
            {
                "date": date_str,
                "weekday": datetime.strptime(date_str, "%Y-%m-%d").strftime("%A"),
                "top_story": {"title": titles[0]},
                "sections": [{"name": "THE NATION", "slug": "national", "items": [
                    {"title": title} for title in titles[1:]
                ]}],
            }
        ),
        encoding="utf-8",
    )
    return edition_dir


class PodcastFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "out"
        _write_edition(
            self.out, "2026-11-10", ["Supreme Court Upholds PMLA Section 45", "Cabinet Clears DPDP Rules"], 373.06
        )
        _write_edition(
            self.out, "2026-11-09", ["Top Court On Basic Structure", "Retail Inflation Eases"], 401.5
        )
        (self.out / "sample").mkdir(parents=True, exist_ok=True)
        (self.out / "sample" / "brief.mp3").write_bytes(b"ID3" + b"\x00" * 100)

    def _build(self, config):
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            feed = podcast.build_feed(self.out, config)
        return feed, captured.getvalue()

    def test_feed_is_valid_xml_with_placeholder_warning(self):
        feed, stderr = self._build({})
        self.assertEqual(feed, self.out / "feed.xml")
        self.assertIn("warning", stderr)
        self.assertIn("base_url", stderr)
        root = ET.parse(feed).getroot()
        self.assertEqual(root.tag, "rss")
        channel = root.find("channel")
        self.assertEqual(channel.findtext("title"), "The Morning Brief")
        self.assertEqual(channel.findtext("language"), "en-IN")
        self.assertEqual(channel.findtext(f"{ITUNES}author"), "Eklav")
        self.assertIsNotNone(channel.find(f"{ITUNES}category"))

    def test_items_newest_first_with_enclosures(self):
        feed, _ = self._build({})
        channel = ET.parse(feed).getroot().find("channel")
        items = channel.findall("item")
        self.assertEqual(len(items), 2)  # 'sample' dir is not a dated edition
        first = items[0]
        self.assertIn("2026-11-10", first.findtext("guid"))
        self.assertEqual(
            first.find("enclosure").get("url"),
            "https://example.invalid/2026-11-10/brief.mp3",
        )
        self.assertEqual(first.find("enclosure").get("type"), "audio/mpeg")
        self.assertEqual(first.find("enclosure").get("length"), "4003")
        self.assertIn("Supreme Court Upholds PMLA Section 45", first.findtext("description"))
        self.assertEqual(first.findtext(f"{ITUNES}duration"), "6:13")
        self.assertRegex(first.findtext("pubDate"), r"^Tue, 10 Nov 2026 \d{2}:\d{2}:00 \+0530$")

    def test_configured_base_url_is_used(self):
        config = {"delivery": {"podcast": {"base_url": "https://pod.example.com/brief/", "email": "me@example.com"}}}
        feed, stderr = self._build(config)
        self.assertNotIn("warning", stderr)
        channel = ET.parse(feed).getroot().find("channel")
        first = channel.findall("item")[0]
        self.assertEqual(
            first.find("enclosure").get("url"),
            "https://pod.example.com/brief/2026-11-10/brief.mp3",
        )
        self.assertEqual(channel.findtext(f"{ITUNES}owner/{ITUNES}email"), "me@example.com")
        self.assertEqual(
            channel.find("{http://www.w3.org/2005/Atom}link").get("href"),
            "https://pod.example.com/brief/feed.xml",
        )

    def test_empty_out_root_still_emits_valid_feed(self):
        empty = Path(self.tmp.name) / "empty_out"
        empty.mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            feed = podcast.build_feed(empty, {})
        channel = ET.parse(feed).getroot().find("channel")
        self.assertEqual(channel.findall("item"), [])

    def test_duration_formatting(self):
        self.assertEqual(podcast._fmt_duration(373.06), "6:13")
        self.assertEqual(podcast._fmt_duration(59), "0:59")
        self.assertEqual(podcast._fmt_duration(3725), "1:02:05")


if __name__ == "__main__":
    unittest.main()
