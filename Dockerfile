FROM python:3.13

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    git \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno — required for yt-dlp PO token / BotGuard challenge solver.
# Without Deno, YouTube blocks adaptive streams and returns only 360p (format 18).
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Make sure deno is on PATH for yt-dlp to find it
ENV PATH="/usr/local/bin:$PATH"
ENV DENO_INSTALL="/usr/local"

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always upgrade yt-dlp to the absolute latest at build time.
# YouTube extractor patches ship WEEKLY — stale yt-dlp = bot detection fails.
RUN pip install --no-cache-dir --upgrade yt-dlp

# Verify deno is accessible
RUN deno --version

# Copy application code
COPY . .

RUN mkdir -p /app/downloads

EXPOSE 8080

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python3", "bot.py"]
