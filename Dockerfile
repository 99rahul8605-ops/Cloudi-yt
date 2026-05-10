# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System dependencies (FFmpeg + curl for health checks) ────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── App directory ─────────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY bot.py .

# Optional: copy cookies file if present (Netscape format)
# COPY cookies.txt .

# ── Downloads directory ───────────────────────────────────────────────────────
RUN mkdir -p /app/downloads

# ── Expose health-check port ──────────────────────────────────────────────────
EXPOSE 8080

# ── Entrypoint ────────────────────────────────────────────────────────────────
CMD ["python", "bot.py"]
