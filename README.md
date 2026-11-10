# The Morning Brief

A five-minute newspaper-style digest that lands on your iPhone/iPad at 6:30 every morning: legal developments from **LiveLaw** curated for a law student / judiciary aspirant, plus the important national stories and editorials from **The Indian Express** and **The Hindu** — rendered as a print-style **PDF**, pushed as a **notification**, and narrated as a **5-6 minute audio edition (MP3 podcast)**.

```
                 ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 6:25 daily ───► │ collect      │──►│ editorial    │──►│ compose      │──► brief.pdf
                 │ LiveLaw      │   │ desk: dedupe │   │ (newspaper)  │
                 │ Indian Exp.  │   │ rank, quotas │   ├──────────────┤
                 │ The Hindu    │   │ top story    │──►│ audio: gTTS  │──► brief.mp3
                 └──────────────┘   └──────────────┘   └──────────────┘
                                                         │
                                          ntfy push ◄────┤ text + PDF + MP3
                                          Telegram ◄─────┘ (optional)
```

## What you get every morning

| artifact | where | size |
|---|---|---|
| Push notification | ntfy app on iPhone/iPad (and Telegram if configured) | full brief in ~35 lines |
| `brief.pdf` | attachment: print-style 2-page newspaper | ~185 KB |
| `brief.mp3` | attachment: 5-6 min Indian-English narration (32 kbps push copy; 64 kbps master kept locally) | ~1.5 MB push / ~3 MB local |
| `brief.html`, `brief.txt`, `brief.script.txt`, `brief.json` | `out/<YYYY-MM-DD>/` in this folder | — |

## Daily run

```powershell
cd C:\Users\Eklav\OneDrive\Documents\deepseek-harness\default-workspace\morning-brief
python -m src.main                # collect -> rank -> PDF -> audio -> push
python -m src.main --dry-run      # build everything, push nothing
python -m src.main --skip-audio   # 60-second run
python -m src.main --test-push    # re-send today's edition to the devices
```

Or `.\run.ps1` (sets UTF-8, logs to `logs\run-<date>.log`).

## First-time setup

1. Install the ntfy app (free) on the iPhone and iPad from the App Store, open it, tap **+**, and subscribe to topic:
   **`YOUR-PRIVATE-TOPIC`** (also in `config.json` → `delivery.ntfy.topic`).
2. Test: `python -m src.main --test-push`
3. Install the 6:25 Windows schedule (no admin needed): see [docs/SETUP.md](docs/SETUP.md).

## Configuration (`config.json`)

| key | meaning |
|---|---|
| `edition.read_minutes` / `max_items` | target length of the edition (default 5 min, 12 stories) |
| `edition.sections` | quota per section: legal 6, national 3, editorial 3 |
| `sources.*` | enable/disable LiveLaw, Indian Express, The Hindu |
| `delivery.ntfy.topic` | your private push topic |
| `delivery.telegram.bot_token` / `chat_id` | optional Telegram bot delivery |
| `audio.provider` | `gtts` (default, Indian-English) with Windows SAPI fallback |
| `paths.out` / `paths.logs` | where editions and logs live |

## Project layout

```
morning-brief/
├── config.json                  # topics, quotas, delivery
├── run.ps1                      # PowerShell entry point (used by the scheduler)
├── src/
│   ├── main.py                  # orchestrator (collect -> rank -> render -> deliver)
│   ├── compose.py               # brief.json -> newspaper HTML + print PDF (headless Edge)
│   ├── audio.py                 # brief.json -> narration script -> MP3 (gTTS + ffmpeg)
│   ├── podcast.py               # private RSS feed for a podcast app
│   ├── core/
│   │   ├── fetch.py             # HTTP cache, HTML/XML helpers, ITEM factory
│   │   ├── rank.py              # editorial desk: dedupe, bucketing, top story, capsule
│   │   └── deliver.py           # ntfy push + Telegram bot delivery
│   └── sources/
│       ├── livelaw.py           # judgments, courts, case names, one-line ratios
│       ├── indianexpress.py     # RSS + front page + editorials
│       └── thehindu.py          # RSS + print edition (todays-paper) + editorials
├── templates/newspaper.html     # the look
├── docs/                        # SETUP.md, ITEM_CONTRACT.md, sample_brief.json
├── out/<date>/                  # brief.json/html/pdf/mp3/txt + manifest.json
├── logs/                        # one log per run
└── tools/                       # install_schedule.ps1, qa_report.py
```

## Design decisions

- **No LLM API needed.** Summaries come from each article's own standfirst/`og:description` plus the lede, so the brief is deterministic, fast and free. Ranking is a transparent scoring rubric (`docs/ITEM_CONTRACT.md`).
- **Legal first.** LiveLaw fills the "Legal Ledger"; Supreme Court judgments and legislation get the highest priority; a **Judiciary Capsule** box keeps the case name + one-line ratio for revision.
- **Print-first.** The PDF is designed like a broadsheet (serif, columns, rules) and stays under 3 A4 pages so it reads in one sitting.
- **Audio is a first-class edition**, written as a spoken script (top story → legal ledger → nation → opinion → one line to remember), not a text dump.
- **Graceful failure.** A dead feed or a failed TTS provider degrades that one artifact; the run still pushes what it has and logs why.
