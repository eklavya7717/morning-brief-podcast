import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import audio, deepscript, study


def item(i, **kw):
    base = {"id": f"id{i}", "source": "livelaw", "category": "supreme-court", "title": f"Title {i}", "url": f"https://x/{i}",
            "priority": 80 - i, "court": "Supreme Court", "case_name": f"A{i} v. State", "ratio": f"Principle {i} applies.",
            "summary": "s", "bullets": [], "topics": ["Constitutional law"]}
    base.update(kw)
    return base


def brief(items):
    return {"weekday": "Sunday", "date_pretty": "15 November 2026", "sections": [{"name": "L", "slug": "legal", "items": items}]}


class GuardTests(unittest.TestCase):
    def test_unsupported_section_sentence_is_dropped(self):
        text = "The court applied Section 8 of the Act. It also cited Section 99. The result stands."
        out = deepscript.guard(text, "The court applied Section 8 of the Act.", max_drop=0.5)
        self.assertIn("Section 8", out)
        self.assertNotIn("Section 99", out)

    def test_too_many_drops_returns_none(self):
        self.assertIsNone(deepscript.guard("Section 5 is key. Article 21 too.", "nothing relevant here"))

    def test_unsupported_year_is_dropped(self):
        out = deepscript.guard("It was decided. The law dates from 1956.", "It was decided in court.", max_drop=0.6)
        self.assertNotIn("1956", out)


class MemoryTests(unittest.TestCase):
    def test_seen_items_dropped_but_not_same_day_reruns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seen.json"
            study.mark_seen(brief([item(1), item(2)]), path, "2026-11-10")
            kept, dropped = study.filter_seen({"s": [{"id": "id1"}, {"id": "id9"}]}, path, "2026-11-11")
            self.assertEqual(dropped, 1)
            self.assertEqual([i["id"] for i in kept["s"]], ["id9"])
            kept, dropped = study.filter_seen({"s": [{"id": "id1"}]}, path, "2026-11-10")  # same day: keep
            self.assertEqual(dropped, 0)


class TopicTests(unittest.TestCase):
    def test_topics(self):
        self.assertIn("Criminal procedure (BNSS)", study.topics_for({"title": "SC grants bail under BNSS", "summary": ""}))
        self.assertEqual(study.topics_for({"title": "weather", "summary": ""}), ["General"])


class ArchiveTests(unittest.TestCase):
    def test_archive_recap_cards_and_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for d in range(9, 15):  # six days of archive ending Saturday 14 Nov 2026
                study.archive_edition(brief([item(d * 10 + n) for n in range(2)]), root, f"2026-11-{d:02d}")
            recap = study.recap_for(root, "2026-11-15")  # a Sunday
            self.assertEqual(recap["label"], "week")
            self.assertGreaterEqual(len(recap["items"]), 4)
            self.assertIsNone(study.recap_for(root, "2026-11-13"))
            b = brief([item(1), item(2)])
            self.assertEqual(study.cards_append(b, root, "2026-11-15"), 2)
            self.assertEqual(study.cards_append(b, root, "2026-11-15"), 0)  # no duplicate cards
            site = study.build_site(root).read_text(encoding="utf-8")
            self.assertIn("Principle", site)
            self.assertNotIn("__DATA__", site)


class SheetTests(unittest.TestCase):
    def test_quiz_fallback_and_sheet(self):
        b = brief([item(i) for i in range(6)])
        with mock.patch.object(study.llm, "available", return_value=False):
            qa = study.quiz(b)
        self.assertEqual(len(qa), 5)
        html = study.sheet_html(b, "2026-11-15", qa, study.daily_material("2026-11-15"), None)
        self.assertIn("Maxim of the day", html)
        self.assertIn("Recall quiz", html)

    def test_daily_material_rotates(self):
        self.assertNotEqual(study.daily_material("2026-11-15")["maxim"], study.daily_material("2026-11-16")["maxim"])


class ChapterTests(unittest.TestCase):
    def test_split_and_strip(self):
        script = "[[CH:Intro]]\n\nHello there.\n\n[[CH:Law]]\n\nOne. Two."
        chunks, chapters = audio.split_chapters(script)
        self.assertEqual(chapters, [(0, "Intro"), (1, "Law")])
        self.assertNotIn("[[CH", audio.strip_chapters(script))


if __name__ == "__main__":
    unittest.main()
