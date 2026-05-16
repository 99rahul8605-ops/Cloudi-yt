FROM debian:bullseye-slim

# Install system dependencies: Deno, yt-dlp, ffmpeg, Python (for yt-dlp), curl
RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install Deno
RUN curl -fsSL https://deno.land/x/install/install.sh | sh
ENV DENO_INSTALL="/root/.deno"
ENV PATH="${DENO_INSTALL}/bin:${PATH}"

# Install yt-dlp via pip (or direct binary)
RUN pip3 install yt-dlp

WORKDIR /app

# Copy bot source
COPY bot.ts .

# Pre-cache Deno dependencies
RUN deno cache bot.ts --reload

# Create downloads directory
RUN mkdir -p downloads

EXPOSE 8080

# Run the bot
CMD ["deno", "run", "--allow-net", "--allow-read", "--allow-write", "--allow-env", "--allow-run", "bot.ts"]
