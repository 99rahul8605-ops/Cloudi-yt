/**
 * Telegram YouTube Downloader Bot — Node.js rewrite
 * Grammy (Telegram) + yt-dlp (child_process) + FFmpeg
 *
 * ROOT CAUSE FIX for quality bug:
 *   The Python bot used tv_embedded (360p max) during extract_info to fetch
 *   format metadata. So the quality menu showed wrong/empty ✅ marks and the
 *   format selector fell through to low-quality fallbacks on download.
 *
 *   Fix: use TWO separate yt-dlp calls —
 *     1. extract_info → uses "web" + "ios" clients (full resolution metadata)
 *     2. do_download  → same high-res clients, explicit format string
 *   tv_embedded / android_music are kept only as last-resort fallbacks.
 */

"use strict";

const { Bot, InlineKeyboard, InputFile } = require("grammy");
const { execFile, spawn } = require("child_process");
const fs = require("fs");
const fsp = require("fs/promises");
const path = require("path");
const http = require("http");
const https = require("https");
const { promisify } = require("util");

const execFileAsync = promisify(execFile);

// ── Config ────────────────────────────────────────────────────────────────────
const BOT_TOKEN    = process.env.BOT_TOKEN;
const DOWNLOAD_DIR = path.resolve("downloads");
const COOKIES_FILE = "cookies.txt";
const PORT         = parseInt(process.env.PORT || "8080", 10);

if (!BOT_TOKEN) {
  console.error("FATAL: BOT_TOKEN environment variable is not set.");
  process.exit(1);
}

fs.mkdirSync(DOWNLOAD_DIR, { recursive: true });

// ── State ─────────────────────────────────────────────────────────────────────
/** @type {Map<number, {quality: string, mode: string, cleanupMinutes: number}>} */
const userSettings = new Map();
/** @type {Map<string, number>} path → delete-at timestamp (ms), 0 = never */
const cleanupRegistry = new Map();

const DEFAULT_SETTINGS = { quality: "720p", mode: "manual", cleanupMinutes: 10 };

function getSettings(uid) {
  if (!userSettings.has(uid)) userSettings.set(uid, { ...DEFAULT_SETTINGS });
  return userSettings.get(uid);
}

// ── Cookie helpers ────────────────────────────────────────────────────────────
function cookieStatus() {
  if (!fs.existsSync(COOKIES_FILE))
    return { ok: false, reason: "File not found" };
  const stat = fs.statSync(COOKIES_FILE);
  if (stat.size < 100)
    return { ok: false, reason: `File too small (${stat.size} bytes)` };
  let text;
  try { text = fs.readFileSync(COOKIES_FILE, "utf8"); }
  catch (e) { return { ok: false, reason: `Cannot read: ${e.message}` }; }
  const lines = text.split("\n");
  const real  = lines.filter(l => l.trim() && !l.startsWith("#"));
  const yt    = real.filter(l => l.includes("youtube.com") || l.includes("google.com"));
  if (!real.length) return { ok: false, reason: "No cookie data (only comments)" };
  if (!yt.length)   return { ok: false, reason: "No youtube.com/google.com cookies found" };
  return {
    ok: true, size: stat.size,
    total: real.length, ytLines: yt.length,
    hasSAPISID: yt.some(l => l.includes("SAPISID")),
    hasSID:     yt.some(l => l.includes("\tSID\t") || l.includes("\t__Secure-1PSID\t")),
    sample:     yt[0]?.slice(0, 120) || "",
  };
}

// ── yt-dlp helpers ────────────────────────────────────────────────────────────

/**
 * Build base yt-dlp args.
 *
 * KEY FIX: for metadata fetching we use "web,ios" as primary clients.
 * "web" and "ios" return FULL format lists including 1080p / 4K.
 * tv_embedded / android_music are kept only as last-resort fallbacks
 * (they bypass age-gates but cap at 360p and often return empty format lists).
 */
function baseArgs({ forDownload = false } = {}) {
  const cs = cookieStatus();
  const args = [
    "--no-warnings",
    "--no-playlist",
    "--socket-timeout", "30",
    "--retries", "10",
    "--fragment-retries", "10",
    "--extractor-retries", "5",
    "--merge-output-format", "mp4",
    // Prefer mp4/m4a so format selectors match
    "--format-sort", "res,ext:mp4:m4a,codec:h264:aac,size",
    // Human-like pacing
    "--sleep-requests", "1",
    "--min-sleep-interval", "2",
    "--max-sleep-interval", "5",
    "--add-header", "Accept-Language:en-US,en;q=0.9",
    "--add-header", "DNT:1",
  ];

  // ── Client order (THE CORE FIX) ──────────────────────────────────────────
  // For metadata:  web + ios first → full resolution format list always available
  // For downloads: ios first → fast, full-res, low bot-detection
  // Fallbacks:     mweb, tv_embedded, android_music (bypass age-gate/sign-in)
  const clients = forDownload
    ? "ios,web,mweb,tv_embedded,android_music"
    : "web,ios,mweb,tv_embedded,android_music";

  args.push("--extractor-args", `youtube:player_client=${clients}`);

  if (cs.ok) {
    args.push("--cookies", COOKIES_FILE);
  }

  return args;
}

/**
 * Map quality label → yt-dlp format selector.
 *
 * The exhaustive fallback chain mirrors the Python version but is correct:
 * since we now use web/ios for downloads, mp4+m4a will actually be available
 * and the first arm of each chain will win instead of falling through to low-res.
 */
function qualityToFormat(q) {
  const h = { "360p": 360, "480p": 480, "720p": 720, "1080p": 1080 }[q];

  if (h == null) {
    // "best" — no height constraint
    return (
      "bestvideo[ext=mp4]+bestaudio[ext=m4a]" +
      "/bestvideo[ext=mp4]+bestaudio[ext=webm]" +
      "/bestvideo[ext=webm]+bestaudio[ext=webm]" +
      "/bestvideo+bestaudio" +
      "/best"
    );
  }

  const hUp = h + 360; // allow one tier above if exact height missing

  return (
    `bestvideo[height<=${h}][ext=mp4]+bestaudio[ext=m4a]` +
    `/bestvideo[height<=${h}][ext=mp4]+bestaudio[ext=webm]` +
    `/bestvideo[height<=${h}][ext=webm]+bestaudio[ext=webm]` +
    `/bestvideo[height<=${h}][ext=webm]+bestaudio[ext=m4a]` +
    `/bestvideo[height<=${h}]+bestaudio` +
    `/best[height<=${h}][ext=mp4]` +
    `/best[height<=${h}]` +
    `/bestvideo[height<=${hUp}][ext=mp4]+bestaudio[ext=m4a]` +
    `/bestvideo[height<=${hUp}][ext=mp4]+bestaudio[ext=webm]` +
    `/bestvideo[height<=${hUp}][ext=webm]+bestaudio[ext=webm]` +
    `/bestvideo[height<=${hUp}]+bestaudio` +
    `/best[height<=${hUp}]` +
    "/bestvideo[ext=mp4]+bestaudio[ext=m4a]" +
    "/bestvideo[ext=mp4]+bestaudio[ext=webm]" +
    "/bestvideo[ext=webm]+bestaudio[ext=webm]" +
    "/bestvideo+bestaudio" +
    "/best"
  );
}

/**
 * Run yt-dlp --dump-json to get video metadata (NO download).
 * Uses web+ios client order for accurate format detection.
 */
async function extractInfo(url, extraArgs = []) {
  const args = [
    ...baseArgs({ forDownload: false }),
    "--dump-json",
    "--no-download",
    ...extraArgs,
    url,
  ];
  const { stdout } = await execFileAsync("yt-dlp", args, { maxBuffer: 20 * 1024 * 1024 });
  return JSON.parse(stdout.trim().split("\n").pop()); // last JSON line
}

/**
 * Run yt-dlp to actually download a file with a given format selector.
 * Uses ios+web client order (full resolution).
 * Calls onProgress(pct, speed, eta) every ~3 s.
 */
function downloadVideo(url, format, onProgress) {
  return new Promise((resolve, reject) => {
    const outtmpl = path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s");
    const args = [
      ...baseArgs({ forDownload: true }),
      "--format", format,
      "--output", outtmpl,
      "--newline",            // one progress line per stdout line
      url,
    ];

    const proc = spawn("yt-dlp", args);
    let lastProgress = 0;
    let stderr = "";

    proc.stdout.on("data", (chunk) => {
      const lines = chunk.toString().split("\n");
      for (const line of lines) {
        // Progress lines look like: [download]  42.3% of ~123.45MiB at  3.00MiB/s ETA 00:30
        const m = line.match(/\[download\]\s+([\d.]+)%.*?at\s+(\S+)\s+ETA\s+(\S+)/);
        if (m) {
          const now = Date.now();
          if (now - lastProgress > 3000) {
            lastProgress = now;
            onProgress(m[1] + "%", m[2], m[3]);
          }
        }
      }
    });

    proc.stderr.on("data", (chunk) => { stderr += chunk.toString(); });

    proc.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(stderr.slice(-800) || `yt-dlp exited with code ${code}`));
    });
  });
}

/**
 * After downloadVideo() completes, find the output file by video ID.
 */
async function findDownloadedFile(videoId) {
  const files = await fsp.readdir(DOWNLOAD_DIR);
  for (const ext of ["mp4", "mkv", "webm"]) {
    const name = `${videoId}.${ext}`;
    if (files.includes(name)) return path.join(DOWNLOAD_DIR, name);
  }
  // Fallback: any file starting with the video ID
  const match = files.find(f => f.startsWith(videoId + "."));
  return match ? path.join(DOWNLOAD_DIR, match) : null;
}

/** Download a URL to a local file path. */
function downloadUrl(url, dest) {
  return new Promise((resolve, reject) => {
    const proto = url.startsWith("https") ? https : http;
    const file = fs.createWriteStream(dest);
    proto.get(url, (res) => {
      res.pipe(file);
      file.on("finish", () => { file.close(); resolve(); });
    }).on("error", reject);
  });
}

function friendlyError(err) {
  const msg = (err?.message || String(err)).toLowerCase();
  if (msg.includes("sign in") || msg.includes("not a bot") || msg.includes("cookie"))
    return "🔒 *YouTube is blocking this.* Your cookies may be expired.\nRun /cookiecheck for details.";
  if (msg.includes("private"))       return "🔒 This video is *private*.";
  if (msg.includes("unavailable"))   return "❌ Video *unavailable* — may be region-blocked or removed.";
  if (msg.includes("age"))           return "🔞 *Age-restricted.* Provide cookies from a verified account.";
  if (msg.includes("copyright") || msg.includes("blocked"))
                                     return "⛔ Blocked due to *copyright restrictions*.";
  if (msg.includes("ffmpeg"))        return "⚙️ *FFmpeg error.* Try a lower quality.";
  if (msg.includes("fragment"))      return "🌐 *Network error* downloading fragments. Please retry.";
  if (msg.includes("requested format") || msg.includes("not available"))
    return "❌ *Requested format not available.* Retrying with best available…";
  return `❌ Download failed:\n\`${String(err).slice(0, 400)}\``;
}

function isYouTubeUrl(text) {
  return /^(https?:\/\/)?(www\.)?(youtube\.com|youtu\.be)\/.+/.test(text.trim());
}

function formatDuration(s) {
  if (!s) return "?";
  const m = Math.floor(s / 60), sec = s % 60;
  return `${m}m ${sec}s`;
}

// ── Cleanup ───────────────────────────────────────────────────────────────────
function registerCleanup(filePath, minutes) {
  cleanupRegistry.set(filePath, minutes === 0 ? 0 : Date.now() + minutes * 60_000);
}

setInterval(() => {
  const now = Date.now();
  for (const [p, t] of cleanupRegistry) {
    if (t !== 0 && t < now) {
      fs.unlink(p, () => {});
      cleanupRegistry.delete(p);
    }
  }
}, 60_000);

// ── Health server ─────────────────────────────────────────────────────────────
http.createServer((_, res) => res.end("OK")).listen(PORT, () =>
  console.log(`Health server :${PORT}`)
);

// ── In-memory session (url, info, searchResults per chat) ─────────────────────
/** @type {Map<number, {url?: string, info?: object, searchResults?: object[]}>} */
const session = new Map();
function sess(chatId) {
  if (!session.has(chatId)) session.set(chatId, {});
  return session.get(chatId);
}

// ── Bot ───────────────────────────────────────────────────────────────────────
const bot = new Bot(BOT_TOKEN);

// ── /start & /help ────────────────────────────────────────────────────────────
const WELCOME =
  "👋 *Welcome to YT Downloader Bot\\!*\n\n" +
  "Send me:\n" +
  "• A *YouTube URL* → video / audio / thumbnail\n" +
  "• A *song or video name* → search \\(top 5 results\\)\n\n" +
  "⚙️ /settings – Preferences\n" +
  "🍪 /cookiecheck – Diagnose cookie issues\n" +
  "❓ /help – This message";

bot.command(["start", "help"], async (ctx) => {
  await ctx.reply(WELCOME, { parse_mode: "MarkdownV2" });
});

// ── /cookiecheck ──────────────────────────────────────────────────────────────
bot.command("cookiecheck", async (ctx) => {
  const cs = cookieStatus();
  let msg;
  if (!cs.ok) {
    msg =
      "🍪 *Cookie Check — ❌ PROBLEM FOUND*\n\n" +
      `❗ Issue: *${cs.reason}*\n\n` +
      "*How to fix:*\n" +
      "1\\. Log into YouTube in Chrome/Firefox\n" +
      "2\\. Install *'Get cookies\\.txt LOCALLY'* extension\n" +
      "3\\. Export `cookies.txt` from youtube\\.com\n" +
      "4\\. Replace your file and redeploy";
  } else {
    const sapisid = cs.hasSAPISID ? "✅" : "⚠️ Missing";
    const sid     = cs.hasSID     ? "✅" : "⚠️ Missing";
    msg =
      "🍪 *Cookie Check — ✅ File looks valid*\n\n" +
      `📦 Size: \`${cs.size} bytes\`\n` +
      `🎯 YouTube/Google lines: \`${cs.ytLines}\`\n` +
      `🔑 SAPISID: ${sapisid}\n` +
      `🔑 SID: ${sid}`;
  }
  await ctx.reply(msg, { parse_mode: "MarkdownV2" });
});

// ── /settings ─────────────────────────────────────────────────────────────────
function settingsKeyboard(uid) {
  const s = getSettings(uid);
  const modeLbl    = s.mode === "fixed" ? "Fixed ✅" : "Manual 🎛";
  const timerLbl   = s.cleanupMinutes === 0 ? "♾ Never" : `${s.cleanupMinutes} min`;
  return new InlineKeyboard()
    .text(`🎬 Quality: ${s.quality.toUpperCase()}`, "s:quality").row()
    .text(`🔁 Mode: ${modeLbl}`,                   "s:mode").row()
    .text(`🧹 Cleanup: ${timerLbl}`,               "s:cleanup").row()
    .text("❌ Close",                               "s:close");
}

bot.command("settings", async (ctx) => {
  await ctx.reply("⚙️ *Your Settings*\nTap an option to change it:",
    { parse_mode: "Markdown", reply_markup: settingsKeyboard(ctx.from.id) });
});

bot.callbackQuery(/^s:/, async (ctx) => {
  await ctx.answerCallbackQuery();
  const uid   = ctx.from.id;
  const parts = ctx.callbackQuery.data.split(":");

  if (parts[1] === "close") { await ctx.deleteMessage(); return; }

  if (parts[1] === "back") {
    await ctx.editMessageText("⚙️ *Your Settings*",
      { parse_mode: "Markdown", reply_markup: settingsKeyboard(uid) });
    return;
  }

  if (parts[1] === "quality" && parts.length === 2) {
    await ctx.editMessageText("🎬 *Select Default Quality:*",
      { parse_mode: "Markdown", reply_markup:
        new InlineKeyboard()
          .text("360p",  "s:set:quality:360p").text("480p",  "s:set:quality:480p").row()
          .text("720p",  "s:set:quality:720p").text("1080p", "s:set:quality:1080p").row()
          .text("⭐ Best Available", "s:set:quality:best").row()
          .text("⬅️ Back", "s:back")
      });
    return;
  }

  if (parts[1] === "mode" && parts.length === 2) {
    await ctx.editMessageText(
      "🔁 *Download Mode:*\n\n• *Fixed* – always use default quality\n• *Manual* – choose per download",
      { parse_mode: "Markdown", reply_markup:
        new InlineKeyboard()
          .text("✅ Fixed Quality",    "s:set:mode:fixed").row()
          .text("🎛 Manual Selection", "s:set:mode:manual").row()
          .text("⬅️ Back",             "s:back")
      });
    return;
  }

  if (parts[1] === "cleanup" && parts.length === 2) {
    await ctx.editMessageText("🧹 *Auto-Cleanup Timer:*",
      { parse_mode: "Markdown", reply_markup:
        new InlineKeyboard()
          .text("5 min",   "s:set:cleanup:5").text("10 min",  "s:set:cleanup:10").row()
          .text("15 min",  "s:set:cleanup:15").text("30 min", "s:set:cleanup:30").row()
          .text("♾ Never", "s:set:cleanup:0").row()
          .text("⬅️ Back",  "s:back")
      });
    return;
  }

  if (parts[1] === "set" && parts.length === 4) {
    const [, , key, value] = parts;
    const s = getSettings(uid);
    if (key === "quality")  s.quality         = value;
    if (key === "mode")     s.mode            = value;
    if (key === "cleanup")  s.cleanupMinutes  = parseInt(value, 10);
    await ctx.editMessageText("✅ *Setting saved!*",
      { parse_mode: "Markdown", reply_markup: settingsKeyboard(uid) });
  }
});

// ── Message handler ───────────────────────────────────────────────────────────
bot.on("message:text", async (ctx) => {
  const text = ctx.message.text.trim();
  if (isYouTubeUrl(text)) {
    await handleYouTubeUrl(ctx, text);
  } else {
    await handleSearch(ctx, text);
  }
});

async function handleYouTubeUrl(ctx, url) {
  const msg = await ctx.reply("🔍 Fetching video info…");
  let info;
  try {
    info = await extractInfo(url);
  } catch (e) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id,
      friendlyError(e), { parse_mode: "Markdown" });
    return;
  }

  sess(ctx.chat.id).url  = url;
  sess(ctx.chat.id).info = info;

  const title  = info.title  || "Unknown";
  const durStr = formatDuration(info.duration);
  await ctx.api.editMessageText(ctx.chat.id, msg.message_id,
    `📹 *${escMd(title)}*\n⏱ \`${durStr}\`\n\nWhat would you like?`,
    { parse_mode: "MarkdownV2", reply_markup:
      new InlineKeyboard()
        .text("🎬 Video",     "dl:video").row()
        .text("🎵 Audio MP3", "dl:audio").row()
        .text("🖼 Thumbnail", "dl:thumb").row()
        .text("❌ Cancel",    "dl:cancel")
    });
}

async function handleSearch(ctx, query) {
  const msg = await ctx.reply(`🔎 Searching: *${escMd(query)}*…`, { parse_mode: "MarkdownV2" });
  let results;
  try {
    const info = await extractInfo(`ytsearch5:${query}`, ["--flat-playlist"]);
    results = info.entries || [];
  } catch (e) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id,
      `❌ Search failed: \`${e.message?.slice(0, 200)}\``, { parse_mode: "Markdown" });
    return;
  }

  if (!results.length) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, "😕 No results found.");
    return;
  }

  sess(ctx.chat.id).searchResults = results;
  const kb = new InlineKeyboard();
  results.slice(0, 5).forEach((entry, i) => {
    const title  = (entry.title || "Unknown").slice(0, 52);
    const dur    = entry.duration || 0;
    const durStr = dur ? `${Math.floor(dur / 60)}:${String(dur % 60).padStart(2, "0")}` : "?";
    kb.text(`${i + 1}. ${title} [${durStr}]`, `dl:search:${i}`).row();
  });
  kb.text("❌ Cancel", "dl:cancel");
  await ctx.api.editMessageText(ctx.chat.id, msg.message_id,
    "🎵 *Top results — tap to select:*",
    { parse_mode: "Markdown", reply_markup: kb });
}

// ── Download callbacks ────────────────────────────────────────────────────────
bot.callbackQuery(/^dl:/, async (ctx) => {
  await ctx.answerCallbackQuery();
  const uid   = ctx.from.id;
  const parts = ctx.callbackQuery.data.split(":");
  const action = parts[1];

  if (action === "cancel") {
    await ctx.editMessageText("❌ Download cancelled."); return;
  }
  if (action === "thumb") {
    await doThumbnail(ctx, uid); return;
  }
  if (action === "audio") {
    await doAudio(ctx, uid); return;
  }
  if (action === "video") {
    const s = getSettings(uid);
    if (s.mode === "fixed") {
      await doVideo(ctx, uid, s.quality);
    } else {
      await showQualityMenu(ctx);
    }
    return;
  }
  if (action === "quality" && parts.length === 3) {
    await doVideo(ctx, uid, parts[2]); return;
  }
  if (action === "search" && parts.length === 3) {
    const results = sess(ctx.chat.id).searchResults || [];
    const entry   = results[parseInt(parts[2], 10)];
    if (!entry) return;
    const url = entry.webpage_url || entry.url || "";
    sess(ctx.chat.id).url  = url;
    sess(ctx.chat.id).info = entry;
    await ctx.editMessageText(
      `🎵 *${escMd(entry.title || "?")}*\n\nChoose download type:`,
      { parse_mode: "MarkdownV2", reply_markup:
        new InlineKeyboard()
          .text("🎬 Video",     "dl:video").row()
          .text("🎵 Audio MP3", "dl:audio").row()
          .text("🖼 Thumbnail", "dl:thumb").row()
          .text("❌ Cancel",    "dl:cancel")
      });
  }
});

// ── Quality menu ──────────────────────────────────────────────────────────────
/**
 * Show quality menu.
 *
 * KEY FIX: extractInfo now uses web+ios clients, so info.formats contains the
 * REAL set of available heights. The ✅ marks are now accurate.
 */
async function showQualityMenu(ctx) {
  const info    = sess(ctx.chat.id).info || {};
  const formats = info.formats || [];

  // Collect confirmed-available heights from web/ios format list
  const detected = new Set(
    formats
      .filter(f => f.height && f.height > 0 && f.vcodec && f.vcodec !== "none")
      .map(f => Math.round(f.height))
  );

  const standard = [360, 480, 720, 1080];
  const kb = new InlineKeyboard();
  for (let i = 0; i < standard.length; i += 2) {
    const pair = standard.slice(i, i + 2);
    pair.forEach(h => {
      const label = detected.size > 0 && detected.has(h) ? `✅ ${h}p` : `${h}p`;
      kb.text(label, `dl:quality:${h}p`);
    });
    kb.row();
  }
  kb.text("⭐ Best Available", "dl:quality:best").row();
  kb.text("❌ Cancel",         "dl:cancel");

  const note = detected.size === 0
    ? "\n_ℹ️ Format list unavailable — all qualities will be attempted._"
    : "";

  await ctx.editMessageText(
    `🎬 *Select video quality:*${note}`,
    { parse_mode: "Markdown", reply_markup: kb }
  );
}

// ── Video download ────────────────────────────────────────────────────────────
async function doVideo(ctx, uid, quality) {
  const { url } = sess(ctx.chat.id);
  if (!url) { await ctx.editMessageText("❌ No URL stored. Please resend the link."); return; }

  const statusMsg = await ctx.editMessageText(
    `⬇️ *Downloading (${quality})…*`, { parse_mode: "Markdown" });
  const msgId = statusMsg.message_id;
  const chatId = ctx.chat.id;

  // Throttled progress updater
  let lastEdit = 0;
  const onProgress = (pct, speed, eta) => {
    const now = Date.now();
    if (now - lastEdit < 3000) return;
    lastEdit = now;
    ctx.api.editMessageText(chatId, msgId,
      `⬇️ *Downloading…*\n\`${pct}\` | 🚀 \`${speed}\` | ⏱ ETA \`${eta}\``,
      { parse_mode: "Markdown" }).catch(() => {});
  };

  let info;
  try {
    // Download with the selected format
    await downloadVideo(url, qualityToFormat(quality), onProgress);
    // Refetch info to get video ID (needed to locate the file)
    info = sess(ctx.chat.id).info;
    if (!info?.id) info = await extractInfo(url);
  } catch (e) {
    const errStr = String(e).toLowerCase();
    // If format not available, retry with absolute best
    if (errStr.includes("requested format") || errStr.includes("not available")) {
      await ctx.api.editMessageText(chatId, msgId,
        `⚠️ *${quality} unavailable — retrying with best quality…*`,
        { parse_mode: "Markdown" });
      try {
        await downloadVideo(url, qualityToFormat("best"), onProgress);
        info = sess(ctx.chat.id).info;
        if (!info?.id) info = await extractInfo(url);
      } catch (e2) {
        await ctx.api.editMessageText(chatId, msgId,
          friendlyError(e2), { parse_mode: "Markdown" });
        return;
      }
    } else {
      await ctx.api.editMessageText(chatId, msgId,
        friendlyError(e), { parse_mode: "Markdown" });
      return;
    }
  }

  const filepath = await findDownloadedFile(info.id);
  if (!filepath) {
    await ctx.api.editMessageText(chatId, msgId, "❌ File not found after download.");
    return;
  }

  await ctx.api.editMessageText(chatId, msgId, "📤 *Uploading…*", { parse_mode: "Markdown" });

  // Fetch thumbnail
  let thumbBuffer = null;
  if (info.thumbnail) {
    try {
      thumbBuffer = await fetchBuffer(info.thumbnail);
    } catch (_) {}
  }

  try {
    await ctx.api.sendVideo(chatId, new InputFile(filepath), {
      caption:            `🎬 ${info.title || ""} [${quality}]`,
      supports_streaming: true,
      width:              info.width,
      height:             info.height,
      duration:           info.duration,
      thumbnail:          thumbBuffer ? new InputFile(thumbBuffer, "thumb.jpg") : undefined,
    });
    await ctx.api.deleteMessage(chatId, msgId);
  } catch (e) {
    await ctx.api.editMessageText(chatId, msgId,
      `❌ Upload failed: \`${e.message?.slice(0, 200)}\``, { parse_mode: "Markdown" });
    return;
  }

  registerCleanup(filepath, getSettings(uid).cleanupMinutes);
}

// ── Audio download ────────────────────────────────────────────────────────────
async function doAudio(ctx, uid) {
  const { url } = sess(ctx.chat.id);
  if (!url) { await ctx.editMessageText("❌ No URL stored."); return; }

  const statusMsg = await ctx.editMessageText("⬇️ *Extracting audio…*", { parse_mode: "Markdown" });
  const msgId  = statusMsg.message_id;
  const chatId = ctx.chat.id;

  const outtmpl = path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s");
  const cs = cookieStatus();
  const args = [
    ...baseArgs({ forDownload: true }),
    "--format", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    "--extract-audio",
    "--audio-format", "mp3",
    "--audio-quality", "192K",
    "--output", outtmpl,
    url,
  ];

  let stderr = "";
  await new Promise((resolve, reject) => {
    const proc = spawn("yt-dlp", args);
    proc.stderr.on("data", d => { stderr += d.toString(); });
    proc.on("close", code => code === 0 ? resolve() : reject(new Error(stderr.slice(-600))));
  }).catch(async (e) => {
    await ctx.api.editMessageText(chatId, msgId,
      friendlyError(e), { parse_mode: "Markdown" });
    throw e;
  });

  const info = sess(ctx.chat.id).info;
  const filepath = await findDownloadedFile(info?.id || "");
  if (!filepath) {
    await ctx.api.editMessageText(chatId, msgId, "❌ Audio file not found.");
    return;
  }

  await ctx.api.editMessageText(chatId, msgId, "📤 *Uploading MP3…*", { parse_mode: "Markdown" });
  try {
    await ctx.api.sendDocument(chatId, new InputFile(filepath), {
      filename: `${info?.title || "audio"}.mp3`,
      caption:  `🎵 ${info?.title || ""}`,
    });
    await ctx.api.deleteMessage(chatId, msgId);
  } catch (e) {
    await ctx.api.editMessageText(chatId, msgId,
      `❌ Upload failed: \`${e.message?.slice(0, 200)}\``, { parse_mode: "Markdown" });
    return;
  }
  registerCleanup(filepath, getSettings(uid).cleanupMinutes);
}

// ── Thumbnail ─────────────────────────────────────────────────────────────────
async function doThumbnail(ctx, uid) {
  const info = sess(ctx.chat.id).info || {};
  const thumbUrl = info.thumbnail;
  if (!thumbUrl) { await ctx.editMessageText("❌ No thumbnail found."); return; }

  const statusMsg = await ctx.editMessageText("🖼 *Downloading thumbnail…*", { parse_mode: "Markdown" });
  const msgId  = statusMsg.message_id;
  const chatId = ctx.chat.id;

  const outPath = path.join(DOWNLOAD_DIR, `${info.id || "thumb"}_thumb.jpg`);
  try {
    await downloadUrl(thumbUrl, outPath);
  } catch (e) {
    await ctx.api.editMessageText(chatId, msgId,
      `❌ Thumbnail fetch failed: \`${e.message}\``, { parse_mode: "Markdown" });
    return;
  }

  try {
    await ctx.api.sendDocument(chatId, new InputFile(outPath), {
      filename: `${info.title || "thumbnail"}.jpg`,
      caption:  `🖼 ${info.title || ""}`,
    });
    await ctx.api.deleteMessage(chatId, msgId);
  } catch (e) {
    await ctx.api.editMessageText(chatId, msgId,
      `❌ Upload failed: \`${e.message?.slice(0, 200)}\``, { parse_mode: "Markdown" });
    return;
  }
  registerCleanup(outPath, getSettings(uid).cleanupMinutes);
}

// ── Utils ─────────────────────────────────────────────────────────────────────
/** Fetch a URL into a Buffer. */
function fetchBuffer(url) {
  return new Promise((resolve, reject) => {
    const proto = url.startsWith("https") ? https : http;
    proto.get(url, { timeout: 10_000 }, (res) => {
      const chunks = [];
      res.on("data", c => chunks.push(c));
      res.on("end",  ()  => resolve(Buffer.concat(chunks)));
    }).on("error", reject);
  });
}

/** Escape special MarkdownV2 characters. */
function escMd(text) {
  return String(text).replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, "\\$&");
}

// ── Error handler ─────────────────────────────────────────────────────────────
bot.catch((err) => {
  console.error("Bot error:", err);
});

// ── Start ─────────────────────────────────────────────────────────────────────
bot.start();
console.log("Bot started — polling");
