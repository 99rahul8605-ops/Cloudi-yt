# 🎬 Advanced Telegram YouTube Downloader Bot

---

## 🚀 AWS EC2 Deploy Guide

### Prerequisites
- AWS EC2 instance (Ubuntu 22/24)
- SSH access
- GitHub repo access

---

### Step 1 — Connect to EC2

```bash
ssh -i mybot.pem ubuntu@<EC2-IP>
```

---

### Step 2 — Install Dependencies

```bash
# System packages
sudo apt-get update
sudo apt-get install -y ffmpeg git unzip curl

# Deno (required for yt-dlp)
curl -fsSL https://deno.land/install.sh | sudo DENO_INSTALL=/usr/local sh

# Clone repo
cd ~
git clone https://github.com/99rahul8605-ops/Cloudi-yt.git
cd Cloudi-yt

# Python packages
pip install -r requirements.txt --break-system-packages
pip install python-dotenv --break-system-packages
pip install --upgrade yt-dlp --break-system-packages
```

---

### Step 3 — Setup .env File

```bash
nano .env
```

Fill in your values:

```
BOT_TOKEN=your_bot_token_here
API_ID=your_api_id
API_HASH=your_api_hash
```

Save: `Ctrl+X` → `Y` → Enter

---

### Step 4 — Fix dotenv (One Time)

```bash
sed -i '1s/^/from dotenv import load_dotenv\nload_dotenv()\n/' main.py
```

---

### Step 5 — Run in Screen

```bash
screen -dmS cloudi bash -c 'cd ~/Cloudi-yt && python3 main.py'
screen -ls
```

---

### Step 6 — Restart Script

Create `start3.sh` for easy restart:

```bash
cat > ~/start3.sh << 'EOF'
#!/bin/bash

screen -X -S cloudi quit 2>/dev/null

cd ~/Cloudi-yt
git fetch --all
git reset --hard origin/main

# Restore dotenv fix
sed -i '1s/^/from dotenv import load_dotenv\nload_dotenv()\n/' main.py

# Start bot
screen -dmS cloudi bash -c 'cd ~/Cloudi-yt && python3 main.py'
echo "Cloudi bot start ho gaya!"
screen -ls
EOF
chmod +x ~/start3.sh
```

Restart karne ke liye:
```bash
~/start3.sh
```

---

## 🍪 Fixing "YouTube is blocking this download"

### Step 1 — Run /cookiecheck first

Send `/cookiecheck` to the bot:

| Message | Meaning |
|---------|---------|
| File not found | cookies.txt missing |
| File too small | Placeholder copied, not real cookies |
| No youtube.com cookies | Exported from wrong site |
| Missing SAPISID | Exported while not logged in |
| ✅ File looks valid | Cookies ok — may be expired, re-export |

---

### Step 2 — Export Cookies Correctly

1. Open **Google Chrome** (not Firefox, not incognito)
2. Go to `https://www.youtube.com`
3. Make sure you are **logged into your Google account**
4. Install: [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
5. Click extension → **Export** → save as `cookies.txt`
6. Upload to server:
```bash
scp -i mybot.pem cookies.txt ubuntu@<EC2-IP>:~/Cloudi-yt/
```
7. Restart bot: `~/start3.sh`

**Common mistakes:**
- Exporting in incognito mode
- Exporting from google.com instead of youtube.com
- Cookies expire in 1–2 weeks — re-export regularly

---

### Step 3 — Valid cookies.txt looks like this:

```
# Netscape HTTP Cookie File
.youtube.com	TRUE	/	TRUE	1999999999	SAPISID	xxxxx
.youtube.com	TRUE	/	TRUE	1999999999	YSC	xxxxx
.google.com	TRUE	/	TRUE	1999999999	SID	xxxxx
```

Must contain: `SAPISID`, `SID`, `__Secure-1PSID`, `YSC`, `VISITOR_INFO1_LIVE`

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
| `/cookiecheck` | Diagnose cookie file issues |

---

## 📁 File Structure

```
Cloudi-yt/
├── main.py           ← Entry point
├── requirements.txt
├── Dockerfile
├── cookies.txt       ← Replace with real exported cookies!
├── .env              ← Your secrets (never commit this!)
└── README.md
```

---

## 🖥️ All Bots Running on EC2

| Screen Name | Bot | Port |
|-------------|-----|------|
| bot + tunnel | Ca-Inter-lecture | 3000 |
| bot2 + tunnel2 | Edu-app | 3001 |
| devgagan | Save Restricted Bot | — |
| cloudi | Cloudi-yt | — |

Check all screens: `screen -ls`
