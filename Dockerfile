FROM node:20-slim

# Install system dependencies: ffmpeg, curl, unzip (required for Deno installer)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Download yt-dlp binary (standalone, no Python needed)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

# Install Deno system-wide (using the official install script)
# The script automatically uses `unzip` (already installed) and respects DENO_INSTALL
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

# Ensure Deno is in PATH
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Copy package.json and install Node dependencies
COPY package*.json ./
RUN npm install --omit=dev

# Copy cookies.txt (optional – if missing, bot will warn)
COPY cookies.txt /app/cookies.txt

# Copy bot source (the version with health server, stats, and EJS fix)
COPY bot.js .

RUN mkdir -p downloads

EXPOSE 8080

CMD ["node", "bot.js"]
