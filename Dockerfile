FROM python:3.13

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    git \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install deno (JS runtime for yt-dlp EJS challenge solver)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always upgrade yt-dlp to the absolute latest at build time.
# YouTube extractor patches ship WEEKLY â€” stale yt-dlp = bot detection fails.
RUN pip install --no-cache-dir --upgrade yt-dlp

# Copy application code
COPY . .

# Ensure downloads directory exists
RUN mkdir -p /app/downloads

# Expose port (Health server runs on port 8080)
EXPOSE 8080

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Default command
CMD ["python3", "bot.py"]
