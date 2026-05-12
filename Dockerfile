FROM python:3.11-slim

# FFmpeg required for audio extraction and video merging
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always upgrade yt-dlp to the absolute latest at build time.
# YouTube extractor patches ship WEEKLY — stale yt-dlp = bot detection fails.
RUN pip install --no-cache-dir --upgrade yt-dlp

COPY bot.py .

# cookies.txt — replace placeholder with your real exported cookies.
# If the file is missing the COPY will fail, so we always keep the placeholder.
COPY cookies.txt .

RUN mkdir -p /app/downloads

EXPOSE 8080

CMD ["python", "bot.py"]
