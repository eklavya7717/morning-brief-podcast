"""Tests for the LiveLaw source module (offline-safe).

Run without network:
    python -m tests.test_livelaw

The live end-to-end test only runs when LIVELAW_LIVE=1 is set:
    $env:LIVELAW_LIVE=1; python -m tests.test_livelaw
"""
from __future__ import annotations

import os
import sys
import unittest

from src.core.fetch import ITEM_DEFAULTS
from src.sources import livelaw


# --------------------------------------------------------------------- fixtures
SITEMAP_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url>
    <loc>https://www.livelaw.in/high-court/telangana-high-court/example-case-557947</loc>
    <news:news>
      <news:publication_date>2026-11-10T10:30:52+05:30</news:publication_date>
      <news:title><![CDATA[Telangana High Court Upholds ₹10K Fee On Lawyers]]></news:title>
      <news:keywords><![CDATA[Justice N. Tukaramji, Telangana High Court, K Shree Hareesh Kumar v. Bar Council of State of Telangana]]></news:keywords>
    </news:news>
    <image:image><image:loc>https://www.livelaw.in/h-upload/example.webp</image:loc></image:image>
  </url>
  <url>
    <loc>https://www.livelaw.in/job-updates/some-vacancy-557842</loc>
    <news:news>
      <news:publication_date>2026-11-09T08:13:00+05:30</news:publication_date>
      <news:title>Senior Associate Vacancy</news:title>
    </news:news>
  </url>
</urlset>
"""

BODY_PARAGRAPHS = [
    "The Telangana High Court dismissed a plea challenging the levy of Rs. 10,000 fees imposed by the Bar Council "
    "of Telangana on lawyers for resuming legal practice after earlier suspending practice.",
    "The single judge bench of Justice N. Tukaramji adjudged a plea by a lawyer who had challenged the resolution "
    "under Section 24 of the Advocates Act, 1961 and the rules framed thereunder.",
]


# ------------------------------------------------------------------------ tests
def test_sitemap_parse_and_junk_filter():
    entries = livelaw._parse_sitemap(SITEMAP_SAMPLE)
    assert len(entries) == 2, entries
    assert entries[0]["title"].startswith("Telangana High Court")
    assert entries[0]["published"].startswith("2026-11-10")
    # the URL filter makes the vacancy (and other non-article paths) invisible
    assert livelaw._article_info("https://www.livelaw.in/job-updates/some-vacancy-557842") is None
    assert livelaw._article_info("https://www.livelaw.in/high-court/telangana-high-court/example-case-557947") == (
        "high-court",
        557947,
    )
    assert livelaw._article_info("https://www.livelaw.in/top-stories/foo-bar-557961") == ("top-stories", 557961)
    assert livelaw._article_info("https://www.livelaw.in/livelawnewsnetwork") is None


def test_court_from_url():
    assert (
        livelaw._court_from_url("https://www.livelaw.in/high-court/delhi-high-court/some-case-557948")
        == "Delhi High Court"
    )
    assert (
        livelaw._court_from_url("https://www.livelaw.in/high-court/punjab-and-haryana-high-court/case-557946")
        == "Punjab & Haryana High Court"
    )
    assert (
        livelaw._court_from_url("https://www.livelaw.in/high-court/jammu-kashmir/case-557800")
        == "Jammu & Kashmir and Ladakh High Court"
    )
    assert livelaw._court_from_url("https://www.livelaw.in/sc-judgments/livelaw-sc-1147-x-557887") == "Supreme Court"
    assert livelaw._court_from_url("https://www.livelaw.in/top-stories/story-557961") is None


def test_court_from_text():
    assert livelaw._court_from_text("A bench of the Delhi High Court held ...") == "Delhi High Court"
    assert livelaw._court_from_text("National Company Law Appellate Tribunal") == "NCLAT"
    assert livelaw._court_from_text("P&H High Court") == "Punjab & Haryana High Court"
    assert livelaw._court_from_text("Madurai Bench of Madras High Court") == "Madras High Court"
    assert livelaw._court_from_text("no court mentioned here") is None


def test_ratio_from_headline():
    assert (
        livelaw._ratio_from_headline(
            "Religious Freedom Subject To Public Order, Health: Bombay High Court Refuses Permission"
        )
        == "Religious Freedom Subject To Public Order, Health"
    )
    # a two-word lead-in such as "Maratha Quota" is not a legal principle
    assert livelaw._ratio_from_headline("Maratha Quota: Bombay High Court Refuses Day-To-Day Hearing") is None
    # citation prefix + section prefix handled
    assert (
        livelaw._ratio_from_headline(
            "2026 LiveLaw (SC) 1147 | Retirement Order Not Punitive : Supreme Court"
        )
        == "Retirement Order Not Punitive"
    )


def test_ratio_from_paragraphs_requires_normative_statement():
    assertive = ["The Court held that a claim of negative equality cannot be used to challenge an order of retirement."]
    narration = ["The Court held that the exercise would cover all undertrial prisoners across the districts."]
    assert livelaw._ratio_from_paragraphs(assertive)
    assert livelaw._ratio_from_paragraphs(narration) is None


def test_case_name_from_clean_keywords():
    name = livelaw._case_name_from_keywords(
        "Justice N. Tukaramji, Telangana High Court, K Shree Hareesh Kumar v. Bar Council of State of Telangana, Fees"
    )
    assert name == "K Shree Hareesh Kumar v. Bar Council of State of Telangana", name


def test_case_name_rejects_garbled_keywords():
    # LiveLaw's keywords field sometimes concatenates words ("RetirementChhattisgarh")
    garbled = "Justice Manmohan, Supreme Court, Chatur Singh Dhruw v State of Compulsory RetirementChhattisgarh & Ors"
    assert livelaw._case_name_from_keywords(garbled) is None


def test_case_name_rejects_judge_initial_false_positive():
    # "Justice V. Mohana" must not become the case "X ... Justice v. Mohana"
    text = (
        "A bench comprising Chief Justice of India Surya Kant, Justice Joymalya Bagchi and Justice V. Mohana "
        "dismissed the petition."
    )
    assert livelaw._case_name_in(text) is None


def test_in_re_citation_case_name():
    assert (
        livelaw._in_re_name("In Re: 2 Million Lives At Risk, Contamination In Jojari River, Rajasthan")
        == "In Re: 2 Million Lives At Risk, Contamination In Jojari River, Rajasthan"
    )
    assert livelaw._in_re_name("J Rajesh v M Kumaran") is None


def test_strip_boilerplate():
    text = "The Supreme Court held that bail is the rule. [Read Order] Also Read: Related Story Subscribe Now"
    cleaned = livelaw._strip_boilerplate(text)
    assert "Read Order" not in cleaned
    assert "Also Read" not in cleaned
    assert "Subscribe" not in cleaned
    assert cleaned.startswith("The Supreme Court held")


def test_clip_words_budget():
    long_text = "word " * 40
    clipped = livelaw._clip_words(long_text, 18)
    assert len(clipped.split()) <= 18, clipped
    assert livelaw._clip_words("short point here", 18) == "short point here"


def test_bullets_bench_and_provision():
    bullets = livelaw._bullets(BODY_PARAGRAPHS, "Telangana High Court Upholds Fee")
    assert bullets, "expected at least one bullet"
    assert len(bullets) <= 3
    joined = " ".join(bullets)
    assert "Justice N. Tukaramji" in joined, bullets
    assert "Advocates Act, 1961" in joined, bullets
    for bullet in bullets:
        assert len(bullet.split()) <= 18, bullet


def test_summary_never_repeats_title():
    title = "Public Servant Arrested & Kept In Custody For More Than 48 Hours Is Deemed Suspended; No Specific Order Needed : Supreme Court"
    case = "Vishwajeet Singh @ Bishwajeet Ramanand v. State of Bihar & Ors."
    # og:description on premium/gated SC judgment pages is literally the headline
    summary = livelaw._summary(title, title, [title], case, "Supreme Court", None)
    assert summary, "summary must never be empty"
    assert not livelaw._near_duplicate(summary, title), summary
    assert "Case:" not in summary
    # with usable body text the body lede is preferred over a composed line
    body = [
        "The Supreme Court observed that a public servant kept in judicial custody for more than 48 hours is "
        "deemed suspended. The Court also clarified that no specific suspension order is required in such a case."
    ]
    summary_body = livelaw._summary(title, title, [title] + body, case, "Supreme Court", None)
    assert not livelaw._near_duplicate(summary_body, title), summary_body
    assert len(summary_body.split()) >= 5, summary_body


def test_detect_ratio_never_echoes_headline():
    title = "Compulsory Retirement Order Not Punitive Or Stigmatic, So No Hearing Of Employee Needed : Supreme Court"
    assert livelaw._detect_ratio(title, title, []) is None
    body = [
        "The court held that a compulsory retirement order is not punitive and no hearing is required before "
        "passing such an order."
    ]
    ratio = livelaw._detect_ratio(title, title, body)
    assert ratio is None or not livelaw._near_duplicate(ratio, title), ratio


def test_near_duplicate_helper():
    assert livelaw._near_duplicate("The Supreme Court held that bail is the rule", "Supreme Court held bail is the rule")
    assert not livelaw._near_duplicate("Court directs SIT probe into illegal mining", "Hindu Succession rights explained")


def test_priority_ranges():
    cases = {
        "constitution": (90, 100),
        "sc_judgment": (75, 88),  # ordinary SC judgments stay below 90
        "legislation": (70, 85),
        "hc_judgment": (55, 70),
        "judiciary": (40, 60),
        "routine": (25, 45),
    }
    for klass, (low, high) in cases.items():
        value = livelaw._priority(klass, "https://www.livelaw.in/top-stories/x-557961", None, None, None)
        assert low <= value <= high, (klass, value)
    # an ordinary SC judgment off the page lead stays below 90; the page lead may
    # sit in the important/landmark band (up to 90).
    assert 75 <= livelaw._priority(
        "sc_judgment", "https://www.livelaw.in/top-stories/x-557961", None, None, None
    ) <= 88
    assert 75 <= livelaw._priority(
        "sc_judgment", "https://www.livelaw.in/top-stories/x-557961", None, None, 0
    ) <= 90


def test_summary_skips_truncated_meta():
    title = "Collector Held Liable For Disobeying Court Orders"
    truncated = "The Gujarat High Court imposed fine of Rs. 1 Lakh on Ahmedabad Collector to be paid from his own"
    lede = (
        "The High Court imposed a cost of Rs. 1 Lakh on the Ahmedabad Collector for disobeying an earlier order "
        "that required him to decide a land-use application within a statutory period."
    )
    summary = livelaw._summary(title, truncated, [lede], None)
    assert "statutory period" in summary, summary
    assert not livelaw._near_duplicate(summary, title), summary
    # with no usable body/ratio/case the module still returns a non-empty summary
    assert livelaw._summary(title, truncated, [], None)


def test_klass_and_category():
    url = "https://www.livelaw.in/high-court/delhi-high-court/example-557900"
    assert livelaw._category(url, "Delhi High Court Upholds Eviction", "Delhi High Court") == "high-court"
    assert (
        livelaw._category(
            "https://www.livelaw.in/top-stories/parliament-passes-new-act-557900",
            "Parliament Passes New Act On Data Protection",
            None,
        )
        == "legislation"
    )
    assert (
        livelaw._klass(
            "supreme-court",
            "Supreme Court",
            "https://www.livelaw.in/supreme-court/constitution-bench-case-557900",
            "Constitution Bench Upholds Validity",
            "",
            "",
        )
        == "constitution"
    )
    # "cannot be challenged" is a holding, not a routine listing
    assert (
        livelaw._klass(
            "supreme-court",
            "Supreme Court",
            "https://www.livelaw.in/top-stories/x-557900",
            "Retirement Not Punitive : Supreme Court",
            "",
            "The Court further held that an order of compulsory retirement cannot be challenged.",
        )
        == "sc_judgment"
    )


def test_select_diverse_caps_supreme_court():
    def item(url, category, priority):
        return {"url": url, "category": category, "priority": priority, "title": url, "published": None}

    items = [item(f"https://x/sc-{i}", "supreme-court", 90 - i) for i in range(10)]
    items += [item(f"https://x/hc-{i}", "high-court", 65 - i) for i in range(10)]
    chosen = livelaw._select_diverse(items, 15)
    assert len(chosen) == 15
    sc = sum(1 for entry in chosen if entry["category"] == "supreme-court")
    assert sc <= 9, sc  # ~60% cap keeps High Court stories in the brief
    assert len({entry["url"] for entry in chosen}) == 15


@unittest.skipUnless(os.environ.get("LIVELAW_LIVE") == "1", "live network test (set LIVELAW_LIVE=1)")
def test_live_fetch():
    items = livelaw.fetch(n=6)
    assert len(items) >= 6
    for item in items:
        assert set(item) == set(ITEM_DEFAULTS) | {"id"}, set(item) - set(ITEM_DEFAULTS)
        assert item["source"] == "livelaw"
        assert item["title"] and item["url"].startswith("https://www.livelaw.in/")
        assert 0 <= item["priority"] <= 100
        assert len(item["summary"].split()) <= 60
        assert not livelaw._near_duplicate(item["summary"], item["title"]), item["title"]
        if item["ratio"]:
            assert not livelaw._near_duplicate(item["ratio"], item["title"]), item["title"]
    assert [entry["priority"] for entry in items] == sorted(
        (entry["priority"] for entry in items), reverse=True
    )


def main() -> int:
    """Run the offline tests without pytest installed."""
    failures = 0
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except unittest.SkipTest as exc:
            print(f"SKIP {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - standalone runner
            failures += 1
            print(f"FAIL {name}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
