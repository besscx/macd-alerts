# Run the MACD alerts on GitHub Actions (free, no computer of mine needed)

Everything is prepared — you just need a GitHub account and ~10 minutes.

## Cost
Free. Make the repo **Public** → GitHub Actions minutes are unlimited.
(Private repos only get 2,000 min/month; this setup would sit right around
that limit, so public is the safe free choice. Your bot token is stored in
encrypted Secrets — it never appears in the code, even in a public repo.)

## Steps

### 1. Create a free GitHub account
Go to https://github.com/signup and sign up (email + password).

### 2. Create the repository
- Go to https://github.com/new
- Repository name: `macd-alerts`
- Visibility: **Public** (for unlimited free Actions minutes)
- Leave "Add a README file" **unchecked**
- Click **Create repository**

### 3. Upload the files
On your new repo page, click the **"uploading an existing file"** link.
Drag in these 4 files (keep the folder structure!):

| Your computer | Goes to repo as |
|---|---|
| `macd_alert.py` | `macd_alert.py` |
| `requirements.txt` | `requirements.txt` |
| `.github/workflows/macd-30m.yml` | `.github/workflows/macd-30m.yml` |
| `.github/workflows/macd-4h.yml` | `.github/workflows/macd-4h.yml` |

⚠️ The two `.yml` files must land inside `.github/workflows/`. If your
computer hides the `.github` folder, zip the whole folder structure and
upload the zip's contents, or create the folders in GitHub's web UI first
(Add file → Create new file → type `.github/workflows/macd-30m.yml` and
paste the contents).

Then click **Commit changes**.

### 4. Add your Telegram secrets
- In the repo, go to **Settings** → **Secrets and variables** → **Actions**
- Click **New repository secret**, add:
  - Name: `TELEGRAM_BOT_TOKEN`, Value: your bot token
  - Name: `TELEGRAM_CHAT_ID`, Value: `8635033657`
- Secrets are encrypted — nobody browsing the repo can see them.

### 4b. Add Bark (China channel, added 2026-11-21)
Dual-send: every DDMM signal and Fear & Greed alert goes to Telegram
AND to Bark. Telegram is unchanged; Bark is best-effort (a Bark
failure is logged but never blocks Telegram or the state cursor).
- Install **Bark – Customed Notifications** on your iPhone, open it,
  copy the key from the address it shows (`https://api.day.app/<key>`
  — the key is only the part after `api.day.app/`).
- In the repo, **Settings** → **Secrets and variables** → **Actions**
  → **New repository secret**: Name `BARK_KEY`, Value = that key only.
  Never paste the key in chat or in any file — it is the credential
  that lets anyone push to your phone.
- Upload the updated `macd_alert.py` and all 4 workflow files
  (`.github/workflows/macd-15m.yml`, `macd-30m.yml`, `macd-4h.yml`,
  `fear-greed.yml`) — the workflows pass `BARK_KEY` through as an env
  var. If `BARK_KEY` is not set, the script just skips Bark.
- Test: Actions → run any one workflow manually, then check the run
  log for `alert sent` (only if a fresh signal just closed) — or
  temporarily test Bark from your phone in the Bark app first
  ("send test" in the app), which proves the key + APNs path works
  without waiting for a signal.

### 5. Test it
- Go to the **Actions** tab → click **MACD 30m check** in the left sidebar
- Click **Run workflow** → **Run workflow** (green button)
- Wait ~1–2 min, click the run, and check it ends green ✅
- You won't get a Telegram message unless a fresh crossover just happened —
  green run = the pipeline works.

### 6. Done — it's automatic from here
- **MACD 30m check**: every 30 min, Mon–Fri
- **MACD 4h check**: every 4 hours, Mon–Fri
- After each run, the workflow commits its `state-*.json` back to the repo,
  so every crossover alerts exactly once even though runners are ephemeral.

## Two quirks worth knowing
- GitHub starts scheduled runs a few minutes late sometimes. Normal.
- If the repo has zero activity for 60 days, GitHub pauses scheduled
  workflows. Any commit (even editing a file) re-enables them.
- Schedules run in UTC and only on the repo's default branch (`main`).

## What changed on my side
Per your request, the monitor no longer runs on my computer — the local
schedule is disabled and your bot token was deleted from my machine.

## CNN Fear & Greed check (added 2026-10-21)
- Extra workflow: `.github/workflows/fear-greed.yml` — runs
  `python macd_alert.py --fear-greed` every hour, every day (UTC).
- It reads CNN's Fear & Greed Index (no API key) and sends ONE Telegram
  alert when the rating ENTERS Extreme Fear (score ≤ 24). While it stays
  in Extreme Fear it stays silent; it re-arms automatically once the
  rating exits Extreme Fear, so a later re-entry alerts again.
- State lives in `state-fear-greed.json`, committed back by the workflow
  the same way as the `state-*.json` files. Upload it with the other
  files (same folder structure); no new secrets needed — it reuses
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
- After uploading, test it once manually: Actions → "CNN Fear & Greed
  check" → Run workflow, then check the run log shows a real score
  line (`[fear-greed] CNN Fear & Greed: ...`). That log line is the
  proof the GitHub runner can reach CNN's endpoint.
