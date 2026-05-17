FROM python:3.13

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    git \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (JS runtime — used by some yt-dlp extractors)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always use the latest yt-dlp — YouTube extractor patches ship weekly
RUN pip install --no-cache-dir --upgrade yt-dlp

# Copy all bot source files
COPY . .

# Ensure downloads directory exists
RUN mkdir -p /app/downloads

EXPOSE 8080

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python3", "main.py"]
