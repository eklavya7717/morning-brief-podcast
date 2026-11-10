# Setup — getting the brief on your iPhone and iPad

Everything below is done once. After that, the brief builds itself at **6:25 AM** and lands on both devices.

---

## 1. The push (ntfy) — no account, 2 minutes, works today

The phone/iPad delivery uses [ntfy.sh](https://ntfy.sh), a free open-source push service. No sign-up, no email, no password — just a private topic name that only you know.

1. Install **ntfy** from the App Store on the iPhone **and** the iPad (it is free).
2. Open the app → tap **`+`** (top right) → **Subscribe to topic**.
3. Type this exact topic and confirm:
   ```
   YOUR-PRIVATE-TOPIC
   ```
   (If you ever want a new topic, edit `delivery.ntfy.topic` in [config.json](../config.json) and re-subscribe — old topic stops working immediately.)
4. Test it now from this folder:
   ```powershell
   python -m src.main --test-push
   ```
   Within a few seconds both devices get a notification titled **THE MORNING BRIEF · …** with the full digest; below it, two attachments:
   - **Audio edition — 5 min listen** (tap to play the ~6 minute narration)
   - **Print edition (PDF) — 5 min read** (tap to open the 2-page newspaper)

   If nothing arrives: check the app is allowed to send notifications (iOS Settings → ntfy → Notifications) and that the topic was typed exactly, all lowercase, no spaces.

**Tip for the iPad:** open the audio attachment once — iOS keeps it in the Files app and the player has a 15-second skip if you want to skim.

---

## 2. The daily build — Windows Task Scheduler

The pipeline runs on this PC, so the PC must be on (or asleep) at 6:25 AM. The task is registered with "wake the computer", so sleep is fine; a fully shut-down PC is not.

Install it once:

```powershell
cd C:\Users\Eklav\OneDrive\Documents\deepseek-harness\default-workspace\morning-brief
powershell -ExecutionPolicy Bypass -File tools\install_schedule.ps1
```

It creates a task named **The Morning Brief** that runs `run.ps1` daily at **06:25**, so the push lands by ~**06:28**.

- Change the time: `... -File tools\install_schedule.ps1 -Time 06:10`
- Run it right now to check: `schtasks /Run /TN "The Morning Brief"`
- Watch the live log: `Get-Content logs\run-$(Get-Date -Format yyyy-MM-dd).log -Wait`
- Remove it: `... -File tools\install_schedule.ps1 -Remove`

You can also install the same build as a DeepSeek Harness scheduled task if you prefer it living in the app; don't enable both — you would get two pushes.

---

## 3. Optional: Telegram bot delivery (best in-app listening)

If you want the brief inside Telegram (your existing habit) in addition to ntfy:

1. In Telegram, search **@BotFather** → `/newbot` → name it e.g. `Morning Brief` → pick a username ending in `bot` (e.g. `eklav_brief_bot`).
2. BotFather replies with a token like `8123456789:AAH...`. Copy it.
3. Open your new bot's chat (BotFather gives the link) and press **Start**.
4. Paste the token + chat id into `config.json`:
   ```json
   "telegram": { "enabled": true, "bot_token": "PASTE_TOKEN", "chat_id": "PASTE_CHAT_ID" }
   ```
   Don't know the chat id? Run this after pressing Start:
   ```powershell
   python -m src.core.deliver --telegram-discover
   ```
   It prints the chat id — put it in `config.json`, or ask me and I'll wire it up.
5. Test: `python -m src.main --test-push`. You get the digest text, the audio as a playable audio message, and the PDF as a document.

---

## 4. Optional: private podcast in Apple Podcasts

The MP3 arrives attached each morning, so you can already listen from the notification. A *subscribable* podcast needs the episodes and a feed on a public URL:

- The pipeline already writes `out/feed.xml` (Apple-Podcasts-compatible, `en-IN`, 30 newest episodes).
- `src/podcast.py` builds episode URLs from `config.json → delivery.podcast.base_url`.
- Hosting options, easiest first:
  1. **GitHub (recommended):** a tiny private repo + a token; I push `feed.xml` and the MP3s to GitHub Pages after every run and Apple Podcasts subscribes to the Pages URL. Tell me your GitHub handle and we'll wire it in ~10 minutes.
  2. **Cloudflare R2 / Pages:** similar, needs a Cloudflare account.
  3. **A local server** on this PC (works only when the iPhone is on the same Wi-Fi and the PC is on — not recommended for a morning podcast).

Nothing needs to change in code — just set `delivery.podcast.base_url` once hosting exists, and the feed links resolve.

---

## 5. Optional: e-paper PDFs from your Telegram channels

The brief already reads the **print-edition content** from the papers' own websites (The Hindu's *Today's Paper* hub, Indian Express front page + editorials), so the e-paper experience is covered without Telegram.

If you specifically want the PDFs you receive on Telegram used as the source (e.g. a channel whose edition differs from the website):

- Telegram bots cannot read a channel they are not an admin of, and a channel's files are not reachable by URL.
- Two viable paths: (a) forward the two PDFs to your bot each morning and the pipeline ingests the newest documents from the bot chat — keep the bot, no API keys needed; or (b) a one-time Telegram API login (`api_id`/`api_hash` from my.telegram.org) so the pipeline can read your channel directly.
- Tell me which one you prefer and the channel name, and I'll add `src/sources/telegram_pdf.py` for it.

---

## 6. Troubleshooting

| symptom | check |
|---|---|
| No push at 6:30 | `Get-Content logs\<date>.log -Tail 50`; PC on/asleep; run `schtasks /Run /TN "The Morning Brief"` |
| Push arrives but no attachments | audio/PDF build failed — grep the log for `audio:` / `pdf:` errors; the text still delivers |
| Audio is silent / weird | `out\<date>\brief.mp3` opens? `python tools/qa_report.py out\<date>\brief.json`; fallback voice is Windows SAPI, log shows provider |
| "all sources failed" | internet down or a site changed its HTML; run `python -m src.sources.livelaw --out out/probe/livelaw.json` etc. to isolate |
| Layout looks broken | re-render `python -m src.main --dry-run --skip-audio` then inspect `out\<date>\brief.pdf` |
| Duplicate stories | `src/core/rank.py` dedupe; tune `edition.sections` in config.json |
| Want it shorter | set `"read_minutes": 3` and `"sections": {"legal":4,"national":2,"editorial":2}`, then `python -m src.main --dry-run --skip-audio` |
| Want no audio one day | `python -m src.main --skip-audio` |
| Repeat today's delivery | `python -m src.main --test-push` |
