FROM debian:bullseye-slim

# Install system dependencies: ffmpeg, python3, pip, curl, unzip
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    python3 \
    python3-pip \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp (pre-release)
RUN pip3 uninstall -y yt-dlp || true && \
    python3 -m pip install -U --pre "yt-dlp[default]"

# Install Deno system-wide
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Copy your bot.js (the complete code)
COPY bot.js .

RUN mkdir -p downloads

EXPOSE 8080

# Run with all permissions (simplest)
CMD ["deno", "run", "--allow-all", "bot.js"]
