FROM node:20-slim

# Install system dependencies: ffmpeg, curl, unzip (for yt-dlp and Deno)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp binary
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

# Install Deno (required for yt-dlp JavaScript runtime)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

# Copy package files and install Node dependencies
COPY package*.json ./
RUN npm install --omit=dev

# Copy bot source
COPY bot.js .

# Optional: copy cookies.txt if you have one
# COPY cookies.txt /app/cookies.txt
COPY cookies.txt /app/cookies.txt

RUN mkdir -p downloads

EXPOSE 8080

CMD ["node", "bot.js"]
