# Morning Brief — data contracts

Everything in the pipeline speaks two JSON shapes. Do not invent new fields;
`src/core/fetch.py::make_item` rejects unknown keys on purpose.

## 1. ITEM (one story)

| field | type | required | rules |
|---|---|---|---|
| `id` | str | auto | sha1(url)[:12] — set by `make_item`, do not set |
| `source` | str | yes | `livelaw` \| `indianexpress` \| `thehindu` \| `telegram-pdf` |
| `category` | str | yes | `supreme-court`, `high-court`, `judiciary`, `legislation`, `national`, `editorial`, `economy`, `international`, `sports`, `other` |
| `title` | str | yes | headline as published, no trailing site name, no ALL-CAPS shouting |
| `url` | str | yes | absolute canonical article URL |
| `published` | iso str \| null | no | normalised by `make_item` |
| `summary` | str | yes | max **60 words**, 1–2 sentences, self-contained, no "Read more", no byline, no "The court on Monday..." fluff when avoidable |
| `bullets` | str[] | no | 0–5 crisp points, each ≤ 18 words (facts, numbers, names, holdings) |
| `priority` | int | yes | 0–100 editorial importance; see rubric below |
| `tags` | str[] | no | ≤ 6, e.g. `Supreme Court`, `Constitution`, `CJI`, `UPSC`, `Bihar` |
| `image` | str \| null | no | absolute image URL if easily available |
| `court` | str \| null | legal only | e.g. `Supreme Court`, `Delhi High Court`, `NCLAT` |
| `case_name` | str \| null | legal only | e.g. `State of Punjab v. Davinder Singh` |
| `ratio` | str \| null | legal only | the legal principle / ruling in ≤ 25 words ("one-line ratio" for exam revision) |
| `read_seconds` | int | auto | computed by `make_item` |

### Priority rubric (legal news, LiveLaw)

| signal | priority |
|---|---|
| Supreme Court Constitution Bench / 5+ judge bench judgment | 90–100 |
| SC judgment or important order; landmark High Court ruling | 75–90 |
| new Act / Ordinance / Rules / Bill passed; major policy | 70–85 |
| High Court judgments of general application | 55–70 |
| Judicial appointments, transfers, contempt, bar/news | 40–60 |
| routine orders, listings, "court seeks response" | 25–45 |

### Priority rubric (Indian Express / The Hindu)

| signal | priority |
|---|---|
| front-page lead / editorial / national policy decision | 75–90 |
| significant national story (economy, security, appointments, law) | 60–75 |
| state politics with national implications, exams/education | 45–60 |
| routine | 25–40 |

## 2. EDITION (`out/YYYY-MM-DD/brief.json`) — produced by `src/core/rank.py`

```json
{
  "date": "2026-11-10",
  "weekday": "Monday",
  "generated_at": "2026-11-10T06:28:11+05:30",
  "read_minutes": 5,
  "sections": [
    {"name": "THE LEGAL LEDGER", "slug": "legal", "items": [ITEM, ...]},
    {"name": "THE NATION", "slug": "national", "items": [ITEM, ...]},
    {"name": "OPINION & ANALYSIS", "slug": "editorial", "items": [ITEM, ...]}
  ],
  "top_story": ITEM,
  "judiciary_capsule": [ITEM, ...],
  "stats": {"sources": {"livelaw": 24, "indianexpress": 30, "thehindu": 20},
             "selected": 12, "words": 780, "generated_in_seconds": 21.4}
}
```

`top_story` is the single most important story of the morning (may also appear
in its section). `judiciary_capsule` is 2–4 legal items whose `ratio`/`case_name`
are filled — used by the composer for the "case law you can quote" box.

Consumers:

* `src/compose.py` — renders `brief.html`, `brief.pdf`
* `src/audio.py` — renders `brief.mp3` (+ `brief.script.txt`)
* `src/core/deliver.py` — push text, attachments
* `src/main.py` — orchestrates everything

Sources modules must be runnable standalone:

```powershell
python -m src.sources.livelaw --out out/probe/livelaw.json
python -m src.sources.indianexpress --out out/probe/ie.json
python -m src.sources.thehindu --out out/probe/th.json
```
