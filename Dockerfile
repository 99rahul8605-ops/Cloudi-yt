# ── Base ──────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System packages: FFmpeg + curl ────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── App directory ─────────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# Install pinned deps first (layer-cached), then force-upgrade yt-dlp to HEAD
# so the latest YouTube bot-detection patches are always present at build time.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir --upgrade yt-dlp

# ── Application code ──────────────────────────────────────────────────────────
COPY bot.py .

# ── Optional: cookies file (Netscape format) ──────────────────────────────────
# Uncomment the line below if you include cookies.txt in the build context.
# COPY cookies.txt .

# ── Temp downloads directory ──────────────────────────────────────────────────
RUN mkdir -p /app/downloads

# ── Health-check port ─────────────────────────────────────────────────────────
EXPOSE 8080

# ── Start bot ─────────────────────────────────────────────────────────────────
CMD ["python", "-u", "bot.py"]
