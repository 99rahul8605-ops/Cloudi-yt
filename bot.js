/**
 * Telegram YouTube Downloader Bot — Node.js
 * Grammy + yt-dlp + FFmpeg
 *
 * Cookie bypass strategy:
 *   - cookies.txt auto-detected at startup (absolute path, next to bot.js)
 *   - With cookies    → web, android, ios clients (full resolution)
 *   - Without cookies → android_vr, ios, web, mweb, tv_embedded (no sign-in wall)
 */

"use strict";

const { Bot, InlineKeyboard, InputFile } = require("grammy");
const { execFile, spawn } = require("child_process");
const fs   = require("fs");
const fsp  = require("fs/promises");
const path = require("path");
const http = require("http");
const https = require("https");
const { promisify } = require("util");

const execFileAsync = promisify(execFile);

// ── Config ────────────────────────────────────────────────────────────────────
const BOT_TOKEN    = process.env.BOT_TOKEN;
const DOWNLOAD_DIR = path.resolve("downloads");
// FIX: absolute path so cookies.txt is always found next to bot.js,
// regardless of what directory the process is launched from (Render, Railway, etc.)
const COOKIES_FILE = path.resolve(__dirname, "cookies.txt");
const PORT         = parseInt(process.env.PORT || "8080", 10);

if (!BOT_TOKEN) {
  console.error("FATAL: BOT_TOKEN environment variable is not set.");
  process.exit(1);
}

fs.mkdirSync(DOWNLOAD_DIR, { recursive: true });

// ── State ─────────────────────────────────────────────────────────────────────
const userSettings    = new Map();
const cleanupRegistry = new Map();
const DEFAULT_SETTINGS = { quality: "720p", mode: "manual", cleanupMinutes: 10 };

function getSettings(uid) {
  if (!userSettings.has(uid)) userSettings.set(uid, { ...DEFAULT_SETTINGS });
  return userSettings.get(uid);
}

// ── Cookie helpers ────────────────────────────────────────────────────────────
function cookieStatus() {
  if (!fs.existsSync(COOKIES_FILE))
    return { ok: false, reason: `File not found at: ${COOKIES_FILE}` };

  const stat = fs.statSync(COOKIES_FILE);
  if (stat.size < 100)
    return { ok: false, reason: `File too small (${stat.size} bytes) — re-export it` };

  let text;
  try { text = fs.readFileSync(COOKIES_FILE, "utf8"); }
  catch (e) { return { ok: false, reason: `Cannot read file: ${e.message}` }; }

  // Normalize Windows line endings
  const lines = text.replace(/\r\n/g, "\n").split("\n");

  // Must be Netscape cookie format
  const hasHeader = lines.some(l => l.includes("Netscape HTTP Cookie File"));
  if (!hasHeader)
    return {
      ok: false,
      reason: "Not a valid Netscape cookie file — re-export using the 'Get cookies.txt LOCALLY' Chrome/Firefox extension",
    };

  const real = lines.filter(l => l.trim() && !l.startsWith("#"));
  const yt   = real.filter(l =>
    l.includes("youtube.com") || l.includes(".youtube.com") ||
    l.includes("google.com")  || l.includes(".google.com")
  );

  if (!real.length) return { ok: false, reason: "No cookie data (only comments/blank lines)" };
  if (!yt.length)   return { ok: false, reason: "No youtube.com/google.com cookies found — export while logged into youtube.com" };

  return {
    ok: true,
    size: stat.size,
    total: real.length,
    ytLines: yt.length,
    hasSAPISID: yt.some(l => l.includes("SAPISID")),
    hasSID: yt.some(l =>
      l.includes("\tSID\t") ||
      l.includes("\t__Secure-1PSID\t") ||
      l.includes("__Secure-3PSID")
    ),
    sample: yt[0]?.slice(0, 120) || "",
  };
}

// ── Startup cookie diagnostic ─────────────────────────────────────────────────
{
  const cs = cookieStatus();
  if (cs.ok) {
    console.log(`[startup] cookies.txt ✅  path=${COOKIES_FILE}  size=${cs.size}B  ytLines=${cs.ytLines}  SAPISID=${cs.hasSAPISID}  SID=${cs.hasSID}`);
  } else {
    console.warn(`[startup] cookies.txt ❌  reason="${cs.reason}"`);
    console.warn(`[startup] Expected path: ${COOKIES_FILE}`);
    console.warn("[startup] Bot will use android_vr bypass — some videos may still be blocked.");
  }
}

// ── yt-dlp helpers ────────────────────────────────────────────────────────────
function baseArgs() {
  const cs = cookieStatus();
  const args = [
    "--no-warnings",
    "--no-playlist",
    "--socket-timeout", "30",
    "--retries", "10",
    "--fragment-retries", "10",
    "--extractor-retries", "5",
    "--no-check-certificate",
    "--merge-output-format", "mp4",
    "--format-sort", "res,ext:mp4:m4a,codec:h264:aac,size",
    "--sleep-requests", "1",
    "--min-sleep-interval", "2",
    "--max-sleep-interval", "5",
    "--add-header", "Accept-Language:en-US,en;q=0.9",
    "--add-header", "Accept:text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "--add-header", "DNT:1",
    "--add-header", "Sec-Fetch-Mode:navigate",
  ];

  if (cs.ok) {
    args.push("--cookies", COOKIES_FILE);
    // With valid cookies, web client gives best quality and access
    args.push("--extractor-args", "youtube:player_client=web,android,ios,tv_embedded;skip=webpage");
    console.log(`[yt-dlp] cookies.txt OK — ${cs.ytLines} YT lines | SAPISID=${cs.hasSAPISID} | SID=${cs.hasSID}`);
  } else {
    // No cookies: android_vr bypasses sign-in wall without authentication
    args.push("--extractor-args", "youtube:player_client=android_vr,ios,web,mweb,tv_embedded;skip=webpage");
    console.log(`[yt-dlp] No cookies (${cs.reason}) — android_vr bypass active`);
  }

  return args;
}

function qualityToFormat(q) {
  const h = { "360p": 360, "480p": 480, "720p": 720, "1080p": 1080 }[q];
  if (h == null) {
    return (
      "bestvideo[ext=mp4]+bestaudio[ext=m4a]" +
      "/bestvideo[ext=mp4]+bestaudio[ext=webm]" +
      "/bestvideo[ext=webm]+bestaudio[ext=webm]" +
      "/bestvideo+bestaudio/best"
    );
  }
  const hUp = h + 360;
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
    "/bestvideo+bestaudio/best"
  );
}

async function extractInfo(url, extraArgs = []) {
  const args = [...baseArgs(), "--dump-json", "--no-download", ...extraArgs, url];
  const { stdout } = await execFileAsync("yt-dlp", args, { maxBuffer: 20 * 1024 * 1024 });
  return JSON.parse(stdout.trim().split("\n").pop());
}

function downloadVideo(url, format, onProgress) {
  return new Promise((resolve, reject) => {
    const args = [
      ...baseArgs(),
      "--format", format,
      "--output", path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
      "--newline",
      url,
    ];
    const proc = spawn("yt-dlp", args);
    let lastProgress = 0, stderr = "";
    proc.stdout.on("data", (chunk) => {
      for (const line of chunk.toString().split("\n")) {
        const m = line.match(/\[download\]\s+([\d.]+)%.*?at\s+(\S+)\s+ETA\s+(\S+)/);
        if (m && Date.now() - lastProgress > 3000) {
          lastProgress = Date.now();
          onProgress(m[1] + "%", m[2], m[3]);
        }
      }
    });
    proc.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    proc.on("close", (code) => code === 0 ? resolve() :
      reject(new Error(stderr.slice(-800) || `yt-dlp exited ${code}`)));
  });
}

async function findDownloadedFile(videoId) {
  if (!videoId) return null;
  const files = await fsp.readdir(DOWNLOAD_DIR);
  for (const ext of ["mp4", "mkv", "webm"]) {
    if (files.includes(`${videoId}.${ext}`))
      return path.join(DOWNLOAD_DIR, `${videoId}.${ext}`);
  }
  const match = files.find(f => f.startsWith(videoId + "."));
  return match ? path.join(DOWNLOAD_DIR, match) : null;
}

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

function fetchBuffer(url) {
  return new Promise((resolve, reject) => {
    const proto = url.startsWith("https") ? https : http;
    proto.get(url, { timeout: 10_000 }, (res) => {
      const chunks = [];
      res.on("data", c => chunks.push(c));
      res.on("end", () => resolve(Buffer.concat(chunks)));
    }).on("error", reject);
  });
}

function friendlyError(err) {
  const msg = (err?.message || String(err)).toLowerCase();
  if (msg.includes("sign in") || msg.includes("login") || msg.includes("not a bot") ||
      msg.includes("confirm your age") || msg.includes("this video is unavailable")) {
    const cs = cookieStatus();
    const hint = cs.ok
      ? "Cookies are loaded but may be *expired* — re-export from a fresh YouTube session and redeploy."
      : `Cookie problem: _${cs.reason}_\nRun /cookiecheck for details.`;
    return `🔒 *YouTube blocked this video.*\n\n🍪 ${hint}`;
  }
  if (msg.includes("private"))     return "🔒 This video is *private*.";
  if (msg.includes("unavailable")) return "❌ Video *unavailable* — region-blocked or removed.";
  if (msg.includes("age"))         return "🔞 *Age-restricted.* Add cookies from a verified account.";
  if (msg.includes("copyright") || msg.includes("blocked"))
                                   return "⛔ Blocked due to *copyright restrictions*.";
  if (msg.includes("ffmpeg"))      return "⚙️ *FFmpeg error.* Try a lower quality.";
  if (msg.includes("fragment"))    return "🌐 *Network error* on fragments. Please retry.";
  if (msg.includes("requested format") || msg.includes("not available"))
    return "❌ *Requested format not available.* Retrying with best…";
  return `❌ Download failed:\n\`${String(err).slice(0, 400)}\``;
}

function isYouTubeUrl(text) {
  return /^(https?:\/\/)?(www\.)?(youtube\.com|youtu\.be)\/.+/.test(text.trim());
}
function formatDuration(s) {
  if (!s) return "?";
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}
function escMd(text) {
  return String(text).replace(/[_*[\]()~`>#+\-=|{}.!\\]/g, "\\$&");
}

// ── Cleanup ───────────────────────────────────────────────────────────────────
function registerCleanup(filePath, minutes) {
  cleanupRegistry.set(filePath, minutes === 0 ? 0 : Date.now() + minutes * 60_000);
}
setInterval(() => {
  const now = Date.now();
  for (const [p, t] of cleanupRegistry)
    if (t !== 0 && t < now) { fs.unlink(p, () => {}); cleanupRegistry.delete(p); }
}, 60_000);

// ── Health server ─────────────────────────────────────────────────────────────
http.createServer((_, res) => res.end("OK")).listen(PORT, () =>
  console.log(`Health server :${PORT}`)
);

// ── Session ───────────────────────────────────────────────────────────────────
const session = new Map();
function sess(chatId) {
  if (!session.has(chatId)) session.set(chatId, {});
  return session.get(chatId);
}

// ── Bot ───────────────────────────────────────────────────────────────────────
const bot = new Bot(BOT_TOKEN);

bot.command(["start", "help"], async (ctx) => {
  await ctx.reply(
    "👋 *Welcome to YT Downloader Bot\\!*\n\n" +
    "Send me:\n• A *YouTube URL* → video / audio / thumbnail\n" +
    "• A *song or video name* → search \\(top 5\\)\n\n" +
    "⚙️ /settings – Preferences\n🍪 /cookiecheck – Cookie status\n❓ /help – Help",
    { parse_mode: "MarkdownV2" }
  );
});

bot.command("cookiecheck", async (ctx) => {
  const cs = cookieStatus();
  let msg;
  if (!cs.ok) {
    msg =
      "🍪 *Cookie Check — ❌ PROBLEM*\n\n" +
      `📁 Path: \`${COOKIES_FILE}\`\n` +
      `❗ Issue: \`${cs.reason}\`\n\n` +
      "*How to fix:*\n1\\. Log into YouTube in Chrome/Firefox \\(not incognito\\)\n" +
      "2\\. Install *'Get cookies\\.txt LOCALLY'* extension\n" +
      "3\\. Export `cookies.txt` from youtube\\.com\n" +
      "4\\. Place it next to `bot\\.js` and redeploy\n\n" +
      "_ℹ️ Bot still works without cookies via android\\_vr bypass\\._";
  } else {
    msg =
      "🍪 *Cookie Check — ✅ Valid*\n\n" +
      `📁 Path: \`${COOKIES_FILE}\`\n` +
      `📦 Size: \`${cs.size} bytes\`\n` +
      `🎯 YouTube/Google lines: \`${cs.ytLines}\`\n` +
      `🔑 SAPISID: ${cs.hasSAPISID ? "✅" : "⚠️ Missing"}\n` +
      `🔑 SID: ${cs.hasSID ? "✅" : "⚠️ Missing"}\n\n` +
      ((!cs.hasSAPISID || !cs.hasSID)
        ? "_⚠️ Some auth cookies missing — re\\-export while fully logged into YouTube\\._"
        : "_✅ All key auth cookies present\\._");
  }
  await ctx.reply(msg, { parse_mode: "MarkdownV2" });
});

function settingsKeyboard(uid) {
  const s = getSettings(uid);
  return new InlineKeyboard()
    .text(`🎬 Quality: ${s.quality.toUpperCase()}`, "s:quality").row()
    .text(`🔁 Mode: ${s.mode === "fixed" ? "Fixed ✅" : "Manual 🎛"}`, "s:mode").row()
    .text(`🧹 Cleanup: ${s.cleanupMinutes === 0 ? "♾ Never" : s.cleanupMinutes + " min"}`, "s:cleanup").row()
    .text("❌ Close", "s:close");
}

bot.command("settings", async (ctx) => {
  await ctx.reply("⚙️ *Your Settings*\nTap an option to change it:",
    { parse_mode: "Markdown", reply_markup: settingsKeyboard(ctx.from.id) });
});

bot.callbackQuery(/^s:/, async (ctx) => {
  await ctx.answerCallbackQuery();
  const uid = ctx.from.id, parts = ctx.callbackQuery.data.split(":");
  if (parts[1] === "close") { await ctx.deleteMessage(); return; }
  if (parts[1] === "back") {
    await ctx.editMessageText("⚙️ *Your Settings*",
      { parse_mode: "Markdown", reply_markup: settingsKeyboard(uid) }); return;
  }
  if (parts[1] === "quality" && parts.length === 2) {
    await ctx.editMessageText("🎬 *Select Default Quality:*", { parse_mode: "Markdown",
      reply_markup: new InlineKeyboard()
        .text("360p","s:set:quality:360p").text("480p","s:set:quality:480p").row()
        .text("720p","s:set:quality:720p").text("1080p","s:set:quality:1080p").row()
        .text("⭐ Best Available","s:set:quality:best").row().text("⬅️ Back","s:back") }); return;
  }
  if (parts[1] === "mode" && parts.length === 2) {
    await ctx.editMessageText(
      "🔁 *Download Mode:*\n\n• *Fixed* – always use default quality\n• *Manual* – choose per download",
      { parse_mode: "Markdown", reply_markup: new InlineKeyboard()
        .text("✅ Fixed Quality","s:set:mode:fixed").row()
        .text("🎛 Manual Selection","s:set:mode:manual").row()
        .text("⬅️ Back","s:back") }); return;
  }
  if (parts[1] === "cleanup" && parts.length === 2) {
    await ctx.editMessageText("🧹 *Auto-Cleanup Timer:*", { parse_mode: "Markdown",
      reply_markup: new InlineKeyboard()
        .text("5 min","s:set:cleanup:5").text("10 min","s:set:cleanup:10").row()
        .text("15 min","s:set:cleanup:15").text("30 min","s:set:cleanup:30").row()
        .text("♾ Never","s:set:cleanup:0").row().text("⬅️ Back","s:back") }); return;
  }
  if (parts[1] === "set" && parts.length === 4) {
    const [,,key,value] = parts, s = getSettings(uid);
    if (key === "quality")  s.quality        = value;
    if (key === "mode")     s.mode           = value;
    if (key === "cleanup")  s.cleanupMinutes = parseInt(value, 10);
    await ctx.editMessageText("✅ *Setting saved!*",
      { parse_mode: "Markdown", reply_markup: settingsKeyboard(uid) });
  }
});

bot.on("message:text", async (ctx) => {
  const text = ctx.message.text.trim();
  if (isYouTubeUrl(text)) await handleYouTubeUrl(ctx, text);
  else                    await handleSearch(ctx, text);
});

async function handleYouTubeUrl(ctx, url) {
  const msg = await ctx.reply("🔍 Fetching video info…");
  let info;
  try { info = await extractInfo(url); }
  catch (e) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id,
      friendlyError(e), { parse_mode: "Markdown" }); return;
  }
  sess(ctx.chat.id).url = url;
  sess(ctx.chat.id).info = info;
  await ctx.api.editMessageText(ctx.chat.id, msg.message_id,
    `📹 *${escMd(info.title || "Unknown")}*\n⏱ \`${formatDuration(info.duration)}\`\n\nWhat would you like?`,
    { parse_mode: "MarkdownV2", reply_markup: new InlineKeyboard()
      .text("🎬 Video","dl:video").row().text("🎵 Audio MP3","dl:audio").row()
      .text("🖼 Thumbnail","dl:thumb").row().text("❌ Cancel","dl:cancel") });
}

async function handleSearch(ctx, query) {
  const msg = await ctx.reply(`🔎 Searching: *${escMd(query)}*…`, { parse_mode: "MarkdownV2" });
  let results;
  try {
    const info = await extractInfo(`ytsearch5:${query}`, ["--flat-playlist"]);
    results = info.entries || [];
  } catch (e) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id,
      `❌ Search failed: \`${String(e.message).slice(0,200)}\``, { parse_mode: "Markdown" }); return;
  }
  if (!results.length) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, "😕 No results found."); return;
  }
  sess(ctx.chat.id).searchResults = results;
  const kb = new InlineKeyboard();
  results.slice(0,5).forEach((entry, i) => {
    const title = (entry.title || "Unknown").slice(0,52);
    const dur = entry.duration || 0;
    const ds = dur ? `${Math.floor(dur/60)}:${String(dur%60).padStart(2,"0")}` : "?";
    kb.text(`${i+1}. ${title} [${ds}]`, `dl:search:${i}`).row();
  });
  kb.text("❌ Cancel","dl:cancel");
  await ctx.api.editMessageText(ctx.chat.id, msg.message_id,
    "🎵 *Top results — tap to select:*", { parse_mode: "Markdown", reply_markup: kb });
}

bot.callbackQuery(/^dl:/, async (ctx) => {
  await ctx.answerCallbackQuery();
  const uid = ctx.from.id, parts = ctx.callbackQuery.data.split(":");
  if (parts[1] === "cancel") { await ctx.editMessageText("❌ Download cancelled."); return; }
  if (parts[1] === "thumb")  { await doThumbnail(ctx, uid); return; }
  if (parts[1] === "audio")  { await doAudio(ctx, uid); return; }
  if (parts[1] === "video") {
    const s = getSettings(uid);
    if (s.mode === "fixed") await doVideo(ctx, uid, s.quality);
    else                    await showQualityMenu(ctx);
    return;
  }
  if (parts[1] === "quality" && parts.length === 3) { await doVideo(ctx, uid, parts[2]); return; }
  if (parts[1] === "search" && parts.length === 3) {
    const results = sess(ctx.chat.id).searchResults || [];
    const entry = results[parseInt(parts[2], 10)];
    if (!entry) return;
    sess(ctx.chat.id).url  = entry.webpage_url || entry.url || "";
    sess(ctx.chat.id).info = entry;
    await ctx.editMessageText(
      `🎵 *${escMd(entry.title || "?")}*\n\nChoose download type:`,
      { parse_mode: "MarkdownV2", reply_markup: new InlineKeyboard()
        .text("🎬 Video","dl:video").row().text("🎵 Audio MP3","dl:audio").row()
        .text("🖼 Thumbnail","dl:thumb").row().text("❌ Cancel","dl:cancel") });
  }
});

async function showQualityMenu(ctx) {
  const formats = (sess(ctx.chat.id).info || {}).formats || [];
  const detected = new Set(
    formats.filter(f => f.height > 0 && f.vcodec && f.vcodec !== "none")
           .map(f => Math.round(f.height))
  );
  const kb = new InlineKeyboard();
  [[360,480],[720,1080]].forEach(pair => {
    pair.forEach(h => kb.text(detected.size > 0 && detected.has(h) ? `✅ ${h}p` : `${h}p`, `dl:quality:${h}p`));
    kb.row();
  });
  kb.text("⭐ Best Available","dl:quality:best").row().text("❌ Cancel","dl:cancel");
  await ctx.editMessageText(
    `🎬 *Select video quality:*${detected.size === 0 ? "\n_ℹ️ Format list unavailable — all qualities will be attempted._" : ""}`,
    { parse_mode: "Markdown", reply_markup: kb }
  );
}

async function doVideo(ctx, uid, quality) {
  const { url } = sess(ctx.chat.id);
  if (!url) { await ctx.editMessageText("❌ No URL stored. Please resend the link."); return; }
  const statusMsg = await ctx.editMessageText(`⬇️ *Downloading (${quality})…*`, { parse_mode: "Markdown" });
  const msgId = statusMsg.message_id, chatId = ctx.chat.id;
  let lastEdit = 0;
  const onProgress = (pct, speed, eta) => {
    if (Date.now() - lastEdit < 3000) return;
    lastEdit = Date.now();
    ctx.api.editMessageText(chatId, msgId,
      `⬇️ *Downloading…*\n\`${pct}\` | 🚀 \`${speed}\` | ⏱ ETA \`${eta}\``,
      { parse_mode: "Markdown" }).catch(() => {});
  };
  let info = sess(chatId).info;
  try {
    await downloadVideo(url, qualityToFormat(quality), onProgress);
  } catch (e) {
    if (String(e).toLowerCase().includes("requested format") || String(e).toLowerCase().includes("not available")) {
      await ctx.api.editMessageText(chatId, msgId,
        `⚠️ *${quality} unavailable — retrying with best quality…*`, { parse_mode: "Markdown" });
      try { await downloadVideo(url, qualityToFormat("best"), onProgress); }
      catch (e2) {
        await ctx.api.editMessageText(chatId, msgId, friendlyError(e2), { parse_mode: "Markdown" }); return;
      }
    } else {
      await ctx.api.editMessageText(chatId, msgId, friendlyError(e), { parse_mode: "Markdown" }); return;
    }
  }
  if (!info?.id) { try { info = await extractInfo(url); } catch (_) {} }
  const filepath = await findDownloadedFile(info?.id);
  if (!filepath) {
    await ctx.api.editMessageText(chatId, msgId, "❌ File not found after download."); return;
  }
  await ctx.api.editMessageText(chatId, msgId, "📤 *Uploading…*", { parse_mode: "Markdown" });
  let thumbBuffer = null;
  if (info?.thumbnail) { try { thumbBuffer = await fetchBuffer(info.thumbnail); } catch (_) {} }
  try {
    await ctx.api.sendVideo(chatId, new InputFile(filepath), {
      caption: `🎬 ${info?.title || ""} [${quality}]`,
      supports_streaming: true, width: info?.width, height: info?.height, duration: info?.duration,
      thumbnail: thumbBuffer ? new InputFile(thumbBuffer, "thumb.jpg") : undefined,
    });
    await ctx.api.deleteMessage(chatId, msgId);
  } catch (e) {
    await ctx.api.editMessageText(chatId, msgId,
      `❌ Upload failed: \`${e.message?.slice(0,200)}\``, { parse_mode: "Markdown" }); return;
  }
  registerCleanup(filepath, getSettings(uid).cleanupMinutes);
}

async function doAudio(ctx, uid) {
  const { url } = sess(ctx.chat.id);
  if (!url) { await ctx.editMessageText("❌ No URL stored."); return; }
  const statusMsg = await ctx.editMessageText("⬇️ *Extracting audio…*", { parse_mode: "Markdown" });
  const msgId = statusMsg.message_id, chatId = ctx.chat.id;
  const args = [
    ...baseArgs(),
    "--format", "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    "--extract-audio", "--audio-format", "mp3", "--audio-quality", "192K",
    "--output", path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"), url,
  ];
  let stderr = "";
  try {
    await new Promise((resolve, reject) => {
      const proc = spawn("yt-dlp", args);
      proc.stderr.on("data", d => { stderr += d.toString(); });
      proc.on("close", code => code === 0 ? resolve() : reject(new Error(stderr.slice(-600))));
    });
  } catch (e) {
    await ctx.api.editMessageText(chatId, msgId, friendlyError(e), { parse_mode: "Markdown" }); return;
  }
  const info = sess(chatId).info;
  const filepath = await findDownloadedFile(info?.id || "");
  if (!filepath) {
    await ctx.api.editMessageText(chatId, msgId, "❌ Audio file not found."); return;
  }
  await ctx.api.editMessageText(chatId, msgId, "📤 *Uploading MP3…*", { parse_mode: "Markdown" });
  try {
    await ctx.api.sendDocument(chatId, new InputFile(filepath), {
      filename: `${info?.title || "audio"}.mp3`, caption: `🎵 ${info?.title || ""}`,
    });
    await ctx.api.deleteMessage(chatId, msgId);
  } catch (e) {
    await ctx.api.editMessageText(chatId, msgId,
      `❌ Upload failed: \`${e.message?.slice(0,200)}\``, { parse_mode: "Markdown" }); return;
  }
  registerCleanup(filepath, getSettings(uid).cleanupMinutes);
}

async function doThumbnail(ctx, uid) {
  const info = sess(ctx.chat.id).info || {};
  if (!info.thumbnail) { await ctx.editMessageText("❌ No thumbnail found."); return; }
  const statusMsg = await ctx.editMessageText("🖼 *Downloading thumbnail…*", { parse_mode: "Markdown" });
  const msgId = statusMsg.message_id, chatId = ctx.chat.id;
  const outPath = path.join(DOWNLOAD_DIR, `${info.id || "thumb"}_thumb.jpg`);
  try { await downloadUrl(info.thumbnail, outPath); }
  catch (e) {
    await ctx.api.editMessageText(chatId, msgId,
      `❌ Thumbnail fetch failed: \`${e.message}\``, { parse_mode: "Markdown" }); return;
  }
  try {
    await ctx.api.sendDocument(chatId, new InputFile(outPath), {
      filename: `${info.title || "thumbnail"}.jpg`, caption: `🖼 ${info.title || ""}`,
    });
    await ctx.api.deleteMessage(chatId, msgId);
  } catch (e) {
    await ctx.api.editMessageText(chatId, msgId,
      `❌ Upload failed: \`${e.message?.slice(0,200)}\``, { parse_mode: "Markdown" }); return;
  }
  registerCleanup(outPath, getSettings(uid).cleanupMinutes);
}

bot.catch((err) => console.error("Bot error:", err));
bot.start();
console.log("Bot started — polling");
