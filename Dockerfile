FROM node:20-slim

# Install system dependencies: ffmpeg, python3, pip, curl (for yt-dlp)
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp (pre-release)
RUN pip3 uninstall -y yt-dlp || true && \
    python3 -m pip install -U --pre "yt-dlp[default]"

WORKDIR /app

# Copy package files and install Node dependencies
COPY package*.json ./
RUN npm install --omit=dev

# Copy cookies.txt (must exist in build context)
COPY cookies.txt /app/cookies.txt

# Copy bot source
COPY bot.js .

RUN mkdir -p downloads

EXPOSE 8080

CMD ["node", "bot.js"]
