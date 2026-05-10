# 🎬 Advanced Telegram YouTube Downloader Bot

A production-ready Telegram bot built with Python 3.11+, python-telegram-bot v21,
yt-dlp, and FFmpeg. Fully async, document-only uploads, per-user settings, and
hosted on Render with a health-check HTTP server.

---

## 📁 File Structure

```
telegram-yt-bot/
├── bot.py            # Main bot code
├── requirements.txt  # Python dependencies
├── Dockerfile        # Container definition
├── render.yaml       # Render deployment config
├── cookies.txt       # (Optional) YouTube cookies – Netscape format
└── README.md         # This file
```

---

## ⚡ Quick Local Setup

### 1. Prerequisites

| Tool    | Version  | Install |
|---------|----------|---------|
| Python  | 3.10+    | https://python.org |
| FFmpeg  | Any      | `apt install ffmpeg` / `brew install ffmpeg` / https://ffmpeg.org |
| pip     | latest   | `pip install --upgrade pip` |

### 2. Clone & install

```bash
git clone https://github.com/yourname/yt-bot.git
cd yt-bot
pip install -r requirements.txt
```

### 3. Set environment variable

**Linux / macOS**
```bash
export BOT_TOKEN="123456:ABCDefgh..."
```

**Windows CMD**
```cmd
set BOT_TOKEN=123456:ABCDefgh...
```

**Windows PowerShell**
```powershell
$env:BOT_TOKEN="123456:ABCDefgh..."
```

### 4. Run

```bash
python bot.py
```

---

## 🍪 Cookies (Solving Login / Age-restriction Issues)

Some YouTube videos require authentication (age-restricted, members-only).
Export your browser cookies in **Netscape format** and save as `cookies.txt`
in the same directory as `bot.py`.

### Export with browser extension

1. Install **"Get cookies.txt LOCALLY"** for Chrome/Firefox
2. Go to youtube.com while logged in
3. Click the extension → Export → save as `cookies.txt`
4. Place `cookies.txt` next to `bot.py`

The bot automatically detects and uses the file when present.

---

## 🚀 Deploy on Render

### Step 1 – Push code to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/yourname/yt-bot.git
git push -u origin main
```

### Step 2 – Create a new Web Service on Render

1. Go to https://dashboard.render.com → **New → Web Service**
2. Connect your GitHub repo
3. Render auto-detects the `Dockerfile`
4. Set the following:
   - **Name**: `yt-downloader-bot`
   - **Region**: closest to you
   - **Instance Type**: Free or Starter

### Step 3 – Set Environment Variables

In the Render dashboard → your service → **Environment**:

| Key        | Value                  |
|------------|------------------------|
| `BOT_TOKEN` | Your Telegram bot token |
| `PORT`      | `8080`                 |

### Step 4 – Deploy

Click **Deploy** (or push to GitHub – auto-deploy is enabled in `render.yaml`).

### Step 5 – Health check

Render pings `GET /` on port 8080. The bot's built-in HTTP server returns `200 OK`.
This keeps the service alive on the free tier (with standard spin-down caveats).

> **Tip**: To prevent Render free-tier spin-down, use a free uptime monitor like
> UptimeRobot to ping your service URL every 5 minutes.

---

## ⚙️ Settings System

Send `/settings` to access the inline settings menu:

| Setting | Options | Default |
|---------|---------|---------|
| 🎬 Default Video Quality | 360p, 480p, **720p**, 1080p, Best | 720p |
| 🔁 Download Mode | Fixed ✅, **Manual 🎛** | Manual |
| 🧹 Cleanup Timer | 5, **10**, 15, 30 min, ♾ Never | 10 min |

- **Fixed mode** → skips quality menu, uses your default every time
- **Manual mode** → shows available resolutions extracted from yt-dlp per video
- **Cleanup timer** → auto-deletes temporary files from the server after the chosen delay

---

## 🤖 Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Show usage info |
| `/settings` | Open settings menu |

---

## 📤 Download Flow

1. Send a **YouTube URL** → bot shows: Video / Audio MP3 / Thumbnail / Cancel
2. Send **song name** → bot searches YouTube, shows top 5 results → pick one → continue

All files are sent as **documents** (no Telegram compression).

---

## 🔧 FFmpeg Note

FFmpeg is **required** for:
- Merging separate video+audio streams (needed for 1080p+)
- MP3 audio extraction

The `Dockerfile` installs FFmpeg automatically via `apt-get`.
For local runs, install FFmpeg manually and ensure it's in your `PATH`.

Verify: `ffmpeg -version`

---

## 🛡 Error Handling

The bot gracefully handles:
- Invalid / non-YouTube URLs
- Private or deleted videos
- Geo-restricted content
- Age-restricted videos (use cookies.txt to bypass)
- No available formats
- FFmpeg merge failures
- Network timeouts
- Upload failures

---

## 📝 Environment Variables Reference

| Variable   | Required | Description |
|------------|----------|-------------|
| `BOT_TOKEN` | ✅ Yes  | Telegram bot token from @BotFather |
| `PORT`      | Render   | HTTP port for health server (default: 8080) |

---

## 🗒 Notes

- **User settings are in-memory** – they reset on bot restart.
  For persistence across restarts, swap the `user_settings` dict for
  a SQLite / Redis backend (straightforward extension).
- The bot uses **polling** (not webhooks), which works fine on Render.
- Downloaded files are stored in `/app/downloads/` inside the container
  and cleaned up automatically per user settings.
