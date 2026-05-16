FROM debian:bullseye-slim

RUN apt-get update && apt-get install -y curl ffmpeg python3 python3-pip unzip && rm -rf /var/lib/apt/lists/*

RUN pip3 uninstall -y yt-dlp || true && python3 -m pip install -U --pre "yt-dlp[default]"

RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app
COPY bot.js .
RUN mkdir -p downloads
EXPOSE 8080
CMD ["deno", "run", "--allow-all", "bot.js"]
