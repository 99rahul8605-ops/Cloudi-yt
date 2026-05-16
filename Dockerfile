FROM debian:bullseye-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    python3 \
    python3-pip \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp (pre-release) as you specified
RUN pip3 uninstall -y yt-dlp || true && \
    python3 -m pip install -U --pre "yt-dlp[default]"

# Install Deno system-wide (root shell wide)
RUN curl -fsSL https://deno.land/install.sh | sudo DENO_INSTALL=/usr/local sh

ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Copy bot source
COPY bot.js .

# Create downloads directory
RUN mkdir -p downloads

EXPOSE 8080

# Run the bot
CMD ["deno", "run", "--allow-net", "--allow-read", "--allow-write", "--allow-env", "--allow-run", "bot.js"]
