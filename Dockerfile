FROM node:20-slim

# Install ffmpeg, curl, and QuickJS
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    make \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Build QuickJS from source (latest)
WORKDIR /tmp
RUN curl -L https://bellard.org/quickjs/quickjs-2024-01-13.tar.xz | tar -xJ && \
    cd quickjs-2024-01-13 && \
    make && \
    cp qjs /usr/local/bin/ && \
    cd / && rm -rf /tmp/quickjs-2024-01-13

# Download yt-dlp binary
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

WORKDIR /app
COPY package*.json ./
RUN npm install --omit=dev
COPY cookies.txt /app/cookies.txt
COPY bot.js .
RUN mkdir -p downloads
EXPOSE 8080
CMD ["node", "bot.js"]
