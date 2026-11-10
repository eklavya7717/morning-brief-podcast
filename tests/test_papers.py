"""Offline tests for the newspaper source modules (Indian Express + The Hindu).

Run from the project root:

    python -m unittest tests.test_papers -v

The tests are network-free by default.  Set ``MORNING_BRIEF_LIVE_TESTS=1`` to
also run a live end-to-end check of both CLIs (slow, hits the real sites).
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from src.core.fetch import ITEM_DEFAULTS, dedupe, make_item

from src.sources import indianexpress as ie
from src.sources import thehindu as th

LIVE = os.environ.get("MORNING_BRIEF_LIVE_TESTS") == "1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: make_item adds the derived ``id`` field on top of the documented defaults.
REQUIRED_FIELDS = set(ITEM_DEFAULTS) | {"id"}


class ItemContractMixin:
    """Shared ITEM-shape assertions for both papers."""

    module = None  # type: ignore[assignment]
    source_name = ""

    def assert_valid_item(self, item: dict) -> None:
        self.assertEqual(set(item), REQUIRED_FIELDS, "item must match ITEM_CONTRACT.md exactly")
        self.assertEqual(item["source"], self.source_name)
        self.assertIn(
            item["category"],
            {"national", "editorial", "economy", "international", "sports", "other"},
        )
        self.assertTrue(item["title"].strip())
        self.assertTrue(item["url"].startswith("https://"))
        self.assertGreaterEqual(len(item["summary"]), 40, f"summary too short: {item['summary']!r}")
        self.assertLessEqual(len(item["summary"].split()), 60)
        self.assertGreaterEqual(len(item["tags"]), 2)
        self.assertLessEqual(len(item["tags"]), 4)
        self.assertGreaterEqual(item["priority"], 0)
        self.assertLessEqual(item["priority"], 100)
        self.assertTrue(re.match(r"^[0-9a-f]{12}$", item["id"]))
        summary = item["summary"].lower()
        for banned in ("read more", "click here", "<p", "&amp;"):
            self.assertNotIn(banned, summary)


class IndianExpressOfflineTests(ItemContractMixin, unittest.TestCase):
    module = ie
    source_name = "indianexpress"

    def test_fetch_is_callable(self):
        self.assertTrue(callable(ie.fetch))
        self.assertTrue(callable(ie.main))

    def test_clean_title_strips_site_suffix(self):
        self.assertEqual(
            ie.clean_title("Cabinet clears data protection rules - The Indian Express"),
            "Cabinet clears data protection rules",
        )
        self.assertEqual(
            ie.clean_title("Some headline | Indian Express"),
            "Some headline",
        )
        self.assertEqual(ie.clean_title("  spaced   out  "), "spaced out")

    def test_classify(self):
        cases = {
            "https://indianexpress.com/article/india/x-1/": "national",
            "https://indianexpress.com/article/opinion/editorials/x-2/": "editorial",
            "https://indianexpress.com/article/opinion/columns/x-3/": "editorial",
            "https://indianexpress.com/article/business/x-4/": "economy",
            "https://indianexpress.com/article/world/x-5/": "international",
            "https://indianexpress.com/article/sports/cricket/x-6/": "sports",
            "https://indianexpress.com/article/explained/x-7/": "other",
        }
        for url, expected in cases.items():
            self.assertEqual(ie.classify(url), expected, url)

    def test_topical_filter(self):
        self.assertFalse(ie.is_topical("https://indianexpress.com/article/entertainment/x-1/"))
        self.assertFalse(ie.is_topical("https://indianexpress.com/article/sports/cricket/x-2/"))
        self.assertFalse(ie.is_topical("https://indianexpress.com/article/cities/delhi/x-3/"))
        self.assertTrue(ie.is_topical("https://indianexpress.com/article/india/x-4/"))

    def test_canonical_url(self):
        self.assertEqual(
            ie.canonical_url("https://indianexpress.com/article/india/x-1/?utm=abc#frag"),
            "https://indianexpress.com/article/india/x-1/",
        )
        self.assertEqual(
            ie.canonical_url("/article/india/x-1/"),
            "https://indianexpress.com/article/india/x-1/",
        )

    def test_safe_trim(self):
        self.assertLessEqual(len(ie.safe_trim("word " * 100).split()), 61)
        text = "A. B. " + " ".join(f"w{i}" for i in range(100)) + "."
        self.assertLessEqual(len(ie.safe_trim(text).split()), 61)
        two = "First sentence here. Second sentence here. Third sentence here."
        trimmed = ie.safe_trim(two)
        self.assertNotIn("Third", trimmed)

    def test_safe_trim_handles_abbreviations(self):
        text = (
            "The official said Ms. Sharma met Mr. Rao on Monday. "
            "Subsequently the panel submitted its report to the government."
        )
        result = ie.safe_trim(text)
        self.assertIn("Mr. Rao", result)
        self.assertTrue(result.endswith("."))

    def test_looks_incomplete(self):
        self.assertTrue(ie.looks_incomplete("short blurb"))
        self.assertTrue(ie.looks_incomplete("A sentence that is long enough but ends in ellipsis..."))
        self.assertTrue(ie.looks_incomplete("A sentence cut off mid"))
        self.assertFalse(ie.looks_incomplete("A complete sentence that carries enough words to pass the length threshold."))

    def test_tags_are_two_to_four(self):
        tags = ie.tags_for("Supreme Court hears PMLA bail plea", "", "national")
        self.assertGreaterEqual(len(tags), 2)
        self.assertLessEqual(len(tags), 4)
        self.assertIn("Supreme Court", tags)
        self.assertIn("Editorial", ie.tags_for("Any headline", "", "editorial"))

    def test_score_rubric_bounds(self):
        lead = {
            "url": "https://indianexpress.com/article/india/x-1/",
            "title": "Supreme Court verdict on PMLA",
            "category": "national",
            "rss_summary": "",
        }
        self.assertGreaterEqual(ie.score(lead, lead["url"], {lead["url"]: 0}), 75)
        editorial = dict(lead, category="editorial")
        self.assertLessEqual(ie.score(editorial, "other", {}), 75)
        routine = {"url": "https://indianexpress.com/article/explained/x-2/", "title": "Something", "category": "other"}
        self.assertLessEqual(ie.score(routine, "other", {}), 50)

    def test_offline_item_builds_against_contract(self):
        item = make_item(
            source="indianexpress",
            category="national",
            title=ie.clean_title("Cabinet clears rules - The Indian Express"),
            url="https://indianexpress.com/article/india/cabinet-rules/",
            published="2026-11-10T06:00:00+05:30",
            summary="The Union Cabinet approved the rules on Tuesday, setting a compliance window for platforms.",
            priority=70,
            tags=ie.tags_for("Cabinet clears rules", "", "national"),
            image=None,
        )
        self.assert_valid_item(item)

    def test_unknown_item_field_is_rejected(self):
        with self.assertRaises(ValueError):
            make_item(source="indianexpress", category="national", title="t", url="https://x/y", forged="x")

    def test_failing_feeds_are_skipped_not_raised(self):
        from unittest import mock

        def boom(_url, **_kwargs):
            raise RuntimeError("feed blocked")

        with mock.patch.object(ie, "get", side_effect=boom):
            candidates, status = ie._feed_candidates(1800)
        self.assertEqual(candidates, [])
        self.assertTrue(all(value.startswith("failed") for value in status.values()))

    def test_fetch_never_crashes_when_network_is_down(self):
        from unittest import mock

        with mock.patch.object(ie, "get", side_effect=RuntimeError("offline")):
            self.assertEqual(ie.fetch(n=5, config={"_no_cache": True}), [])


class TheHinduOfflineTests(ItemContractMixin, unittest.TestCase):
    module = th
    source_name = "thehindu"

    def test_fetch_is_callable(self):
        self.assertTrue(callable(th.fetch))
        self.assertTrue(callable(th.main))

    def test_clean_title_strips_site_suffix(self):
        self.assertEqual(
            th.clean_title("Cracker conundrum: On enforcement - The Hindu"),
            "Cracker conundrum: On enforcement",
        )

    def test_article_url_pattern(self):
        self.assertTrue(th.ARTICLE_URL_RE.search("https://www.thehindu.com/news/national/x/article71685024.ece"))
        self.assertTrue(
            th.ARTICLE_URL_RE.search(
                "https://www.thehindu.com/todays-paper/2026-11-10/th_chennai/articleGS7GPE25D-17216568.ece"
            )
        )
        self.assertFalse(th.ARTICLE_URL_RE.search("https://www.thehindu.com/opinion/editorial/"))

    def test_print_url_regex(self):
        match = th.PRINT_URL_RE.search(
            "https://www.thehindu.com/todays-paper/2026-11-10/th_chennai/articleGS7GPE25D-17216568.ece"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "2026-11-10")
        self.assertIsNone(th.PRINT_URL_RE.search("https://www.thehindu.com/news/national/x/article71685024.ece"))

    def test_classify_rss_urls(self):
        cases = {
            "https://www.thehindu.com/news/national/x/article71685024.ece": "national",
            "https://www.thehindu.com/opinion/editorial/y/article71683846.ece": "editorial",
            "https://www.thehindu.com/opinion/op-ed/z/article71682424.ece": "editorial",
            "https://www.thehindu.com/business/Economy/w/article71683422.ece": "economy",
            "https://www.thehindu.com/news/international/v/article71683116.ece": "international",
        }
        for url, expected in cases.items():
            self.assertEqual(th.classify(url), expected, url)

    def test_classify_print_avoids_city_noise(self):
        self.assertEqual(th.classify_print("7-member gang beats policeman to death in Theni"), "other")
        self.assertEqual(
            th.classify_print("Gang murders youth in city"), "other"
        )
        self.assertEqual(
            th.classify_print("Parliament passes new evidence bill"), "national"
        )
        self.assertEqual(
            th.classify_print("Trilateral meet with Putin, Xi likely next week, says Trump"), "international"
        )
        self.assertEqual(th.classify_print("Sensex closes at record high as RBI holds repo rate"), "economy")

    def test_tags_print_edition(self):
        tags = th.tags_for("Hasina seeks new passport", "Bangladesh asserts extradition treaty", "international", print_edition=True)
        self.assertIn("Print edition", tags)
        self.assertGreaterEqual(len(tags), 2)
        self.assertLessEqual(len(tags), 4)
        self.assertIn("Editorial", th.tags_for("Any headline", "", "editorial"))

    def test_looks_incomplete(self):
        self.assertTrue(th.looks_incomplete("Cut off mid-sentence without punctuation even though long enough"))
        self.assertTrue(th.looks_incomplete("A truncated standfirst is marked like this and ends here..."))
        self.assertTrue(th.looks_incomplete("Short blurb"))
        self.assertFalse(
            th.looks_incomplete("The Air Quality Index in several north Indian cities was ‘severe’ on Deepavali night.")
        )

    def test_safe_trim(self):
        self.assertLessEqual(len(th.safe_trim("word " * 100).split()), 61)
        self.assertNotIn("Third", th.safe_trim("First here. Second here. Third here."))

    def test_score_caps(self):
        editorial = {"title": "An editorial", "category": "editorial", "rss_summary": ""}
        self.assertLessEqual(th.score(editorial), 75)
        other = {"title": "Some routine story", "category": "other", "rss_summary": ""}
        self.assertLessEqual(th.score(other), 45)
        print_item = {
            "title": "Hasina seeks new passport",
            "category": "international",
            "rss_summary": "",
            "print_edition": True,
            "priority": 86,
        }
        self.assertEqual(th.score(print_item), 86)

    def test_offline_item_builds_against_contract(self):
        item = make_item(
            source="thehindu",
            category="international",
            title=th.clean_title("Trilateral meet with Putin, Xi likely next week, says Trump"),
            url="https://www.thehindu.com/todays-paper/2026-11-10/th_chennai/articleGS7GPE25B-17216572.ece",
            published="2026-11-10T06:00:00+05:30",
            summary="U.S. President Donald Trump said he will likely meet Xi Jinping and Vladimir Putin next week.",
            priority=80,
            tags=th.tags_for("Trilateral meet", "", "international", print_edition=True),
            image=None,
        )
        self.assert_valid_item(item)

    def test_failing_feeds_are_skipped_not_raised(self):
        from unittest import mock

        def boom(_url, **_kwargs):
            raise RuntimeError("feed blocked")

        with mock.patch.object(th, "get", side_effect=boom):
            candidates, status = th._feed_candidates(1800)
        self.assertEqual(candidates, [])
        self.assertTrue(all(value.startswith("failed") for value in status.values()))

    def test_fetch_never_crashes_when_network_is_down(self):
        from unittest import mock

        with mock.patch.object(th, "get", side_effect=RuntimeError("offline")):
            self.assertEqual(th.fetch(n=5, config={"_no_cache": True}), [])


class DedupeTests(unittest.TestCase):
    def test_dedupe_url_and_title(self):
        items = [
            make_item(
                source="indianexpress",
                category="national",
                title="Cabinet clears data protection rules",
                url="https://indianexpress.com/article/india/a/",
                summary="The Union Cabinet approved the rules on Tuesday and set a compliance window.",
                priority=70,
            ),
            make_item(
                source="indianexpress",
                category="national",
                title="Cabinet clears data protection rules",
                url="https://indianexpress.com/article/india/a/?utm=x",
                summary="The Union Cabinet approved the rules on Tuesday and set a compliance window.",
                priority=70,
            ),
            make_item(
                source="thehindu",
                category="editorial",
                title="The long road to reform",
                url="https://www.thehindu.com/opinion/editorial/b/article71685024.ece",
                summary="The editorial argues that faster appointments alone will not fix the system without transparency.",
                priority=62,
            ),
        ]
        out = dedupe(items)
        self.assertEqual(len(out), 2)
        self.assertEqual(len({item["id"] for item in out}), 2)


@unittest.skipUnless(LIVE, "set MORNING_BRIEF_LIVE_TESTS=1 to run live network tests")
class LiveSmokeTests(unittest.TestCase):
    """End-to-end sanity checks — these hit the real news sites."""

    def test_indianexpress_live(self):
        items = ie.fetch(n=12, config={"_no_cache": True})
        self.assertGreaterEqual(len(items), 8)
        self.assertTrue(any(item["category"] == "editorial" for item in items))
        priorities = [item["priority"] for item in items]
        self.assertEqual(priorities, sorted(priorities, reverse=True))

    def test_thehindu_live(self):
        items = th.fetch(n=12, config={"_no_cache": True})
        self.assertGreaterEqual(len(items), 8)
        self.assertTrue(any(item["category"] == "editorial" for item in items))
        self.assertTrue(any("Print edition" in item["tags"] for item in items))


if __name__ == "__main__":
    unittest.main(verbosity=2)
