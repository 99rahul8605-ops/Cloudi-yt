FROM node:20-slim

# Install ffmpeg and curl (curl is needed to download yt-dlp)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Download yt-dlp binary (standalone, no Python needed)
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

WORKDIR /app

# Copy package.json and install Node dependencies
COPY package*.json ./
RUN npm install --omit=dev

# Copy cookies.txt (optional – if missing, bot will warn)
COPY cookies.txt /app/cookies.txt

# Copy bot source (the updated version with health server and EJS fix)
COPY bot.js .

RUN mkdir -p downloads

EXPOSE 8080

CMD ["node", "bot.js"]
