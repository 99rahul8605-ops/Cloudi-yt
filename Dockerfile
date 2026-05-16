FROM node:20-slim

WORKDIR /app

# Install system dependencies: ffmpeg, yt-dlp, python3 (yt-dlp needs it)
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Install latest yt-dlp binary directly (faster and always up-to-date)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

# Install Node dependencies
COPY package*.json ./
RUN npm ci --omit=dev

# Copy app
COPY bot.js .

# Downloads directory
RUN mkdir -p /app/downloads

EXPOSE 8080

ENV NODE_ENV=production

CMD ["node", "bot.js"]
