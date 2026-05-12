# 🎬 Advanced Telegram YouTube Downloader Bot

---

## 🚨 Fixing "YouTube is blocking this download"

This is the #1 issue when running on a cloud server. Here is every fix, in order.

---

### Step 1 — Run /cookiecheck first

Send `/cookiecheck` to the bot. It will tell you exactly what is wrong:

| Message | Meaning |
|---------|---------|
| File not found | cookies.txt was not copied into the container |
| File too small | You copied the placeholder, not real cookies |
| No youtube.com cookies | Exported from wrong site |
| Missing SAPISID | Cookies exported while not logged in |
| ✅ File looks valid | Cookies are present — may just be expired, re-export |

---

### Step 2 — Export cookies correctly

**The only method that works reliably:**

1. Open **Google Chrome** (not Firefox, not incognito)
2. Go to `https://www.youtube.com`
3. Make sure you are **logged into your Google account**
4. Install extension: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
5. Click the extension icon while on `youtube.com`
6. Click **Export** → save file as `cookies.txt`
7. Open the file — it should have 20–80+ lines of cookie data
8. Replace the `cookies.txt` in your project with this file
9. Redeploy on Render (push to GitHub → auto-deploys)

**Common mistakes that produce broken cookies:**
- ❌ Exporting in incognito mode (no cookies exist there)
- ❌ Exporting from google.com instead of youtube.com
- ❌ Using the placeholder file without replacing it
- ❌ Copying cookies.txt manually and introducing formatting errors
- ❌ Waiting too long — cookies expire in 1–2 weeks

---

### Step 3 — Verify the file looks right

A valid cookies.txt starts like this:
```
# Netscape HTTP Cookie File
# https://curl.haxx.se/rfc/cookie_spec.html
.youtube.com	TRUE	/	TRUE	1999999999	VISITOR_INFO1_LIVE	xxxxx
.youtube.com	TRUE	/	TRUE	1999999999	YSC	xxxxx
.youtube.com	TRUE	/	FALSE	1999999999	SAPISID	xxxxx
.google.com	TRUE	/	TRUE	1999999999	SID	xxxxx
```

Must contain: `SAPISID`, `SID`, `__Secure-1PSID`, `YSC`, `VISITOR_INFO1_LIVE`

---

### Step 4 — If cookies still don't work

The bot uses a client fallback chain even without cookies:
`tv_embedded → mweb → android_music → ios → web`

Some videos work without cookies via `android_music` or `tv_embedded` client.
If a specific video always fails, it may be:
- Region-blocked from Render's server location (US/EU)
- Requires account-level permission (members-only, etc.)

---

## 📁 File Structure

```
telegram-yt-bot/
├── bot.py            ← Main bot
├── requirements.txt
├── Dockerfile
├── render.yaml       ← Render deployment config
├── cookies.txt       ← Replace with your real exported cookies!
└── README.md
```

---

## ⚡ Local Setup

```bash
pip install -r requirements.txt
export BOT_TOKEN="your_token_here"
# Put your real cookies.txt in the same folder
python bot.py
```

FFmpeg must be installed: `apt install ffmpeg` / `brew install ffmpeg`

---

## 🚀 Deploy on Render

1. Push to GitHub
2. Render → New → Web Service → connect repo
3. Render auto-detects Dockerfile
4. Set env var: `BOT_TOKEN` = your token from @BotFather
5. Set env var: `PORT` = `8080`
6. Deploy

To prevent free-tier spin-down, use [UptimeRobot](https://uptimerobot.com)
to ping your Render URL every 5 minutes.

---

## ⚙️ Settings (/settings)

| Setting | Options | Default |
|---------|---------|---------|
| 🎬 Default Quality | 360p / 480p / 720p / 1080p / Best | 720p |
| 🔁 Download Mode | Fixed / Manual | Manual |
| 🧹 Cleanup Timer | 5 / 10 / 15 / 30 min / Never | 10 min |

---

## 🤖 Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Usage info |
| `/settings` | Preferences menu |
| `/cookiecheck` | 🆕 Diagnose cookie file issues |

