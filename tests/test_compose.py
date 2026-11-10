"""Tests for the newspaper composer: ``src/compose.py``.

Runs the full brief.json -> brief.html + brief.pdf path with the canonical
sample edition, then checks the product the reader actually receives: A4 page
count, masthead, sections, the judiciary capsule, live links in the PDF, one
consolidated footer, and the phone-readable HTML fallback.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from src import compose as composer

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_BRIEF = ROOT / "docs" / "sample_brief.json"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def _sample() -> dict:
    return json.loads(SAMPLE_BRIEF.read_text(encoding="utf-8"))


class ComposeSampleEdition(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="mb-test-")
        cls.out = Path(cls._tmp.name)
        if not composer._find_browser(CONFIG):
            raise unittest.SkipTest("no headless browser available for PDF rendering")
        cls.result = composer.compose(_sample(), cls.out, CONFIG)
        cls.html = Path(cls.result["html"]).read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # ------------------------------------------------------------- artifacts
    def test_returns_expected_paths(self) -> None:
        self.assertEqual(self.result["html"].name, "brief.html")
        self.assertEqual(self.result["pdf"].name, "brief.pdf")
        self.assertTrue(self.result["html"].is_file())
        self.assertTrue(self.result["pdf"].is_file())
        self.assertGreater(self.result["pdf"].stat().st_size, 10_000)

    def test_page_count_is_two_to_four(self) -> None:
        self.assertGreaterEqual(self.result["pages"], 2)
        self.assertLessEqual(self.result["pages"], 4)
        self.assertLessEqual(self.html.count('class="sheet"'), 4)

    def test_a4_pdf_page_size(self) -> None:
        from pypdf import PdfReader

        reader = PdfReader(str(self.result["pdf"]))
        for page in reader.pages:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            self.assertAlmostEqual(width, 595, delta=2)   # A4 at 72 dpi
            self.assertAlmostEqual(height, 842, delta=2)

    # -------------------------------------------------------------- content
    def test_masthead_and_identity(self) -> None:
        upper = self.html.upper()
        self.assertIn("THE MORNING BRIEF", upper)
        self.assertIn("MORNING EDITION", upper)
        self.assertIn("MONDAY, 10 NOVEMBER 2026", upper)
        self.assertIn("EDITION NO. 314", upper)
        self.assertIn("Law · Nation · Opinion — in five minutes", self.html)
        self.assertIn("5-MIN READ", upper)
        self.assertIn("PODCAST ATTACHED", upper)
        self.assertIn("SOURCE · LIVELAW", upper)

    def test_all_sections_and_stories_present(self) -> None:
        import html as html_mod

        text = html_mod.unescape(re.sub(r"<[^>]+>", " ", self.html))
        text = re.sub(r"\s+", " ", text)
        for name in ("THE LEGAL LEDGER", "THE NATION", "OPINION & ANALYSIS"):
            self.assertIn(name, text)
        sample = _sample()
        titles = [item["title"] for section in sample["sections"] for item in section["items"]]
        for title in titles:
            self.assertIn(re.sub(r"\s+", " ", title), text)

    def test_judiciary_capsule_is_prominent(self) -> None:
        import html as html_mod

        text = html_mod.unescape(self.html)
        self.assertIn("CASE LAW YOU CAN QUOTE", text.upper())
        self.assertIn("judiciary capsule", text.lower())
        for item in _sample()["judiciary_capsule"]:
            self.assertIn(item["case_name"], text)
            self.assertIn(item["ratio"], text)

    def test_every_story_has_a_clickable_source(self) -> None:
        sample = _sample()
        urls = {item["url"] for section in sample["sections"] for item in section["items"]}
        urls |= {item["url"] for item in sample["judiciary_capsule"]}
        urls.add(sample["top_story"]["url"])
        for url in urls:
            self.assertIn(f'href="{url}"', self.html)

    def test_pdf_keeps_live_links(self) -> None:
        from pypdf import PdfReader

        reader = PdfReader(str(self.result["pdf"]))
        uris: set[str] = set()
        for page in reader.pages:
            annots = page.get("/Annots") or []
            for annot in annots:
                target = annot.get_object().get("/A") or {}
                uri = target.get("/URI") if hasattr(target, "get") else None
                if uri:
                    uris.add(str(uri))
        self.assertTrue(uris, "PDF should contain link annotations")
        self.assertTrue(any("livelaw.in" in uri for uri in uris))

    # --------------------------------------------------------------- design
    def test_self_contained_no_external_resources(self) -> None:
        self.assertNotRegex(self.html, r'(?i)<link[^>]+href="https?://')
        self.assertNotRegex(self.html, r'(?i)<script[^>]+src="https?://')
        self.assertNotRegex(self.html, r'(?i)@import\s+url')
        self.assertNotRegex(self.html, r'(?i)src="https?://')

    def test_paper_and_print_rules(self) -> None:
        self.assertIn("#faf8f3", self.html)
        self.assertIn("@page{size:A4;margin:0;}", self.html)
        self.assertIn("-webkit-line-clamp:4", self.html)
        self.assertIn("break-after:page", self.html)

    def test_one_consolidated_footer_per_page(self) -> None:
        sheets = self.html.count('class="sheet"')
        self.assertEqual(self.html.count('<footer class="sheet-footer">'), sheets)
        self.assertEqual(self.html.count('class="foot-audio"'), sheets)
        self.assertEqual(self.html.count("Sources: LiveLaw · Indian Express · The Hindu"), sheets)
        self.assertNotIn("A4 · 11 MM MARGINS", self.html.upper())
        self.assertNotIn("listen-band", self.html)
        # audio strip comes before the footer rail on every page
        for footer in re.findall(r'<footer class="sheet-footer">(.*?)</footer>', self.html, re.S):
            self.assertLess(footer.find("foot-audio"), footer.find("foot-row"))
            self.assertIn("Audio edition: brief.mp3 attached — 5 min listen.", footer)

    def test_phone_fallback_media_query(self) -> None:
        self.assertIn("@media screen and (max-width:600px)", self.html)
        self.assertIn("grid-template-columns:repeat(3,1fr)", self.html)

    def test_audio_duration_uses_audio_meta(self) -> None:
        brief = _sample()
        brief["audio_meta"] = {"duration_s": 375.0}
        with tempfile.TemporaryDirectory(prefix="mb-test-audio-") as tmp:
            result = composer.compose(brief, Path(tmp), CONFIG)
            html = Path(result["html"]).read_text(encoding="utf-8")
        self.assertIn("≈6 min listen", html)

    def test_empty_judiciary_capsule_omits_the_box(self) -> None:
        brief = _sample()
        brief["judiciary_capsule"] = []
        with tempfile.TemporaryDirectory(prefix="mb-test-nocap-") as tmp:
            result = composer.compose(brief, Path(tmp), CONFIG)
            html = Path(result["html"]).read_text(encoding="utf-8")
        self.assertNotIn('class="capsule"', html)
        self.assertNotIn("CASE LAW YOU CAN QUOTE", html.upper())
        self.assertGreaterEqual(result["pages"], 2)
        self.assertLessEqual(result["pages"], 3)


class LayoutUnits(unittest.TestCase):
    """Fast, browser-free checks of the layout decisions."""

    def test_continued_section_head_shows_real_count(self) -> None:
        section = {"name": "THE LEGAL LEDGER", "slug": "legal", "items": [{}, {}]}
        head = composer._render_section_head(section, True, 2)
        self.assertIn("THE LEGAL LEDGER", head)
        self.assertIn("Continued · 2 stories", head)
        self.assertNotIn("sec-num", head)

    def test_best_split_keeps_reading_order_and_fills_left(self) -> None:
        take, cut1, cut2 = composer._best_split([50.0, 50.0, 50.0], 200.0)
        self.assertEqual(take, 3)
        self.assertEqual((cut1, cut2), (1, 2))

    def test_best_split_of_two_uses_first_two_columns(self) -> None:
        take, cut1, cut2 = composer._best_split([40.0, 40.0], 200.0)
        self.assertEqual(take, 2)
        self.assertEqual((cut1, cut2), (1, 2))

    def test_headline_splits_into_two_lines(self) -> None:
        lines, size = composer._split_headline(
            "Supreme Court Upholds Validity Of Section 45 PMLA, Says Twin Conditions For Bail Do Not Violate Article 21"
        )
        self.assertEqual(len(lines), 2)
        self.assertGreater(size, 10)
        self.assertLess(size, 30)

    def test_layout_keeps_no_story_taller_than_its_page(self) -> None:
        from src import compose as c

        brief = _sample()
        ctx = c._build_context(brief)
        c._resolve_audio(ctx, Path(tempfile.gettempdir()))
        heights = c._estimate_heights(ctx)
        sheets = c._layout(ctx, heights)
        self.assertTrue(sheets)
        for sheet in sheets:
            self.assertLessEqual(sheet.y, sheet.limit + 3.0)


if __name__ == "__main__":
    unittest.main()
