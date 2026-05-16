FROM debian:bullseye-slim

# Install system dependencies: ffmpeg, python3, pip, curl, and unzip (required by Deno installer)
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    python3 \
    python3-pip \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp (pre-release) as you specified
RUN pip3 uninstall -y yt-dlp || true && \
    python3 -m pip install -U --pre "yt-dlp[default]"

# Install Deno system‑wide (root shell) – unzip is now available
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Ensure Deno is in PATH
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Copy your bot source (the complete bot.js provided earlier)
COPY bot.js .

# Create downloads directory
RUN mkdir -p downloads

EXPOSE 8080

# Run the bot with all required permissions
CMD ["deno", "run", "--allow-net", "--allow-read", "--allow-write", "--allow-env", "--allow-run", "bot.js"]
