FROM python:3.13

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    git \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno >= 2.0.0 (required by bgutil-ytdlp-pot-provider)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
ENV PATH="/usr/local/bin:$PATH"
ENV DENO_INSTALL="/usr/local"
RUN deno --version

# Install bgutil server (PO Token provider for YouTube SABR bypass)
# This is the official yt-dlp recommended solution for 720p+ downloads
RUN git clone --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /bgutil && \
    cd /bgutil/server && \
    deno install --allow-scripts=npm:canvas --frozen && \
    deno --version

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install yt-dlp latest + bgutil plugin
RUN pip install --no-cache-dir --upgrade yt-dlp bgutil-ytdlp-pot-provider

# Copy app
COPY . .
RUN mkdir -p /app/downloads

EXPOSE 8080

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Start bgutil HTTP server (port 4416) in background, then run bot
CMD deno run --allow-all /bgutil/server/main.ts & sleep 3 && python3 bot.py
