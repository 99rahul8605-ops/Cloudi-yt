# 🎬 Advanced Telegram YouTube Downloader Bot

---

## ⚡ Quick start (local)

```bash
pip install -r requirements.txt
export BOT_TOKEN="your_token_here"
python bot.py
```

FFmpeg must be installed and on PATH: `ffmpeg -version`

---

## 🔒 Fixing "Sign in to confirm you're not a bot"

YouTube increasingly blocks server IP ranges. Three layers of protection are
built into the bot, applied in this order:

### Layer 1 — android_music / ios client (automatic, no action needed)
yt-dlp tries the `android_music` and `ios` internal clients before the web
client. These APIs do **not** require a sign-in and bypass the bot-check gate
for most videos.

### Layer 2 — cookies.txt (recommended, fixes the rest)

1. In Chrome/Firefox, **log into your YouTube account**.
2. Install the **"Get cookies.txt LOCALLY"** extension:
   - Chrome: https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
   - Firefox: https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/
3. Go to `https://www.youtube.com`
4. Click the extension icon → **Export** → save as `cookies.txt`
5. Place `cookies.txt` in the same directory as `bot.py`
6. Restart the bot (or redeploy on Render)

> The bot auto-detects `cookies.txt` when it is >200 bytes. No code change needed.

### Layer 3 — redeploy to refresh IP (last resort)
Render assigns a new IP on each deploy. If a video is still blocked, trigger
a manual redeploy from the Render dashboard — this often resolves it.

---

## 🚀 Deploy to Render

1. Push all files to a GitHub repo:
```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/you/yt-bot.git
git push -u origin main
```

2. Go to https://dashboard.render.com → **New → Web Service**
3. Connect your repo — Render detects the `Dockerfile` automatically
4. Under **Environment Variables** add:

| Key | Value |
|-----|-------|
| `BOT_TOKEN` | Your token from @BotFather |
| `PORT` | `8080` |

5. Click **Deploy**

Render pings `GET /` on port 8080 — the built-in health server returns `200 OK`.

> **Free-tier tip:** use https://uptimerobot.com (free) to ping your service
> URL every 5 minutes so Render doesn't spin it down.

---

## ⚙️ Settings

| Setting | Options | Default |
|---------|---------|---------|
| 🎬 Default Quality | 360p / 480p / **720p** / 1080p / Best | 720p |
| 🔁 Download Mode | **Manual** / Fixed | Manual |
| 🧹 Cleanup Timer | 5 / **10** / 15 / 30 min / Never | 10 min |

---

## 📋 Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BOT_TOKEN` | ✅ | Telegram token from @BotFather |
| `PORT` | Render only | HTTP health port (default 8080) |

---

## 🗒 Notes

- All files sent as **documents** (no Telegram compression).
- User settings are **in-memory** — they reset on restart. Swap `user_settings`
  dict for SQLite/Redis for persistence.
- The Dockerfile always upgrades yt-dlp to the latest release at build time,
  which is important because YouTube frequently updates its bot-detection.
