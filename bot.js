import { Bot, InlineKeyboard, session } from "https://deno.land/x/grammy@v1.27.0/mod.ts";
import { ensureDir } from "https://deno.land/std@0.224.0/fs/mod.ts";
import { join } from "https://deno.land/std@0.224.0/path/mod.ts";
import { $ } from "https://deno.land/x/dax@0.39.0/mod.ts";

// ---------- Config ----------
const BOT_TOKEN = Deno.env.get("BOT_TOKEN");
if (!BOT_TOKEN) throw new Error("Missing BOT_TOKEN env var");

const DOWNLOAD_DIR = "./downloads";
const COOKIE_PATH = "/app/cookies.txt";
await ensureDir(DOWNLOAD_DIR);

// User settings (in-memory)
const userSettings = new Map();
const defaultSettings = { quality: "720p", mode: "manual", cleanupMinutes: 10 };
const cleanupRegistry = new Map();

// ---------- Cookie helper (no quotes) ----------
async function cookieExists() {
  try {
    await Deno.stat(COOKIE_PATH);
    return true;
  } catch {
    return false;
  }
}

async function getCookieArgs() {
  const exists = await cookieExists();
  if (exists) {
    console.log("✅ cookies.txt found – using it for yt-dlp");
    return ["--cookies", COOKIE_PATH];
  }
  console.warn("⚠️ cookies.txt not found – some videos may be limited to 360p");
  return [];
}

// ---------- yt-dlp version check ----------
async function logYtdlpVersion() {
  try {
    const { stdout } = await $`yt-dlp --version`;
    console.log(`yt-dlp version: ${stdout.trim()}`);
  } catch (err) {
    console.error("Failed to get yt-dlp version:", err.message);
  }
}

// ---------- Settings helpers ----------
function getSettings(userId) {
  if (!userSettings.has(userId)) userSettings.set(userId, { ...defaultSettings });
  return userSettings.get(userId);
}

function scheduleCleanup(filePath, minutes) {
  const expire = minutes === 0 ? 0 : Date.now() + minutes * 60 * 1000;
  cleanupRegistry.set(filePath, expire);
}

async function cleanupWorker() {
  setInterval(async () => {
    const now = Date.now();
    for (const [filePath, expire] of cleanupRegistry.entries()) {
      if (expire !== 0 && expire < now) {
        try {
          await Deno.remove(filePath);
          cleanupRegistry.delete(filePath);
        } catch {}
      }
    }
  }, 60000);
}

// ---------- yt-dlp wrappers (with proper argument splitting) ----------
async function getAvailableQualities(url) {
  const cookieArgs = await getCookieArgs();
  try {
    const { stdout } = await $`yt-dlp ${cookieArgs} -J --flat-playlist ${url}`;
    const data = JSON.parse(stdout);
    const heights = new Set();
    for (const f of data.formats || []) {
      if (f.height && f.vcodec !== "none") heights.add(f.height);
    }
    const sorted = Array.from(heights).sort((a, b) => a - b);
    return sorted.length ? sorted : [360, 480, 720, 1080];
  } catch {
    return [360, 480, 720, 1080];
  }
}

async function downloadVideo(url, quality) {
  const cookieArgs = await getCookieArgs();
  const { stdout: infoStdout } = await $`yt-dlp ${cookieArgs} -J ${url}`;
  const info = JSON.parse(infoStdout);
  const title = info.title.replace(/[^\w\s]/gi, "");
  const videoId = info.id;
  const outputPath = join(DOWNLOAD_DIR, `${videoId}.mp4`);

  let formatArgs = [];
  if (quality !== "best") {
    const target = parseInt(quality);
    if (!isNaN(target)) {
      const formatSpec = `bestvideo[height<=${target}][ext=mp4]+bestaudio[ext=m4a]/best[height<=${target}]`;
      formatArgs = ["-f", formatSpec];
    }
  }
  await $`yt-dlp ${cookieArgs} ${formatArgs} -o ${outputPath} --merge-output-format mp4 ${url}`;
  return { outputPath, title, videoId, info };
}

async function downloadAudio(url) {
  const cookieArgs = await getCookieArgs();
  const { stdout: infoStdout } = await $`yt-dlp ${cookieArgs} -J ${url}`;
  const info = JSON.parse(infoStdout);
  const title = info.title.replace(/[^\w\s]/gi, "");
  const videoId = info.id;
  const mp3Path = join(DOWNLOAD_DIR, `${videoId}.mp3`);
  await $`yt-dlp ${cookieArgs} -f bestaudio --extract-audio --audio-format mp3 --audio-quality 192K -o ${mp3Path} ${url}`;
  return { mp3Path, title, videoId };
}

async function downloadThumbnail(videoId) {
  const urls = [
    `https://img.youtube.com/vi/${videoId}/maxresdefault.jpg`,
    `https://img.youtube.com/vi/${videoId}/hqdefault.jpg`,
  ];
  for (const url of urls) {
    try {
      const resp = await fetch(url);
      if (resp.ok) {
        const thumbPath = join(DOWNLOAD_DIR, `${videoId}_thumb.jpg`);
        const file = await Deno.open(thumbPath, { write: true, create: true });
        await resp.body?.pipeTo(file.writable);
        file.close();
        return thumbPath;
      }
    } catch {}
  }
  return null;
}

// ---------- Bot setup ----------
const bot = new Bot(BOT_TOKEN);
bot.use(session({ initial: () => ({}) }));

// Commands
bot.command("start", (ctx) => {
  ctx.reply(
    `👋 *Welcome to YT Downloader Bot (Deno + yt-dlp + cookies)!*\n\n` +
    `Send me a YouTube URL.\n` +
    `⚙️ /settings – Preferences\n` +
    `❓ /help – This message`,
    { parse_mode: "Markdown" }
  );
});

bot.command("help", (ctx) => ctx.reply("Send a YouTube URL. Use /settings to change quality."));

bot.command("settings", async (ctx) => {
  const s = getSettings(ctx.from.id);
  const modeLabel = s.mode === "fixed" ? "Fixed ✅" : "Manual 🎛";
  const timerLabel = s.cleanupMinutes === 0 ? "♾ Never" : `${s.cleanupMinutes} min`;
  const keyboard = new InlineKeyboard()
    .text(`🎬 Quality: ${s.quality.toUpperCase()}`, "set_quality")
    .row()
    .text(`🔁 Mode: ${modeLabel}`, "set_mode")
    .row()
    .text(`🧹 Cleanup: ${timerLabel}`, "set_cleanup")
    .row()
    .text("❌ Close", "close_settings");
  await ctx.reply("⚙️ *Your Settings*", { parse_mode: "Markdown", reply_markup: keyboard });
});

bot.command("stats", (ctx) => {
  const uptime = Deno.env.get("DENO_START_TIME") ? (Date.now() - parseInt(Deno.env.get("DENO_START_TIME"))) / 1000 : 0;
  const hours = Math.floor(uptime / 3600);
  const minutes = Math.floor((uptime % 3600) / 60);
  const seconds = Math.floor(uptime % 60);
  ctx.reply(
    `📊 *Bot Stats*\nUptime: ${hours}h ${minutes}m ${seconds}s\nActive users: ${userSettings.size}\nDeno: ${Deno.version.deno}`,
    { parse_mode: "Markdown" }
  );
});

// ---------- Settings callbacks ----------
bot.callbackQuery("set_quality", async (ctx) => {
  const qualities = ["360p", "480p", "720p", "1080p", "1440p", "2160p", "best"];
  const keyboard = new InlineKeyboard();
  for (const q of qualities) keyboard.text(q, `set_quality_${q}`).row();
  keyboard.text("⬅️ Back", "back_settings");
  await ctx.editMessageText("🎬 *Select default quality:*", { parse_mode: "Markdown", reply_markup: keyboard });
  await ctx.answerCallbackQuery();
});

bot.callbackQuery(/set_quality_(.+)/, async (ctx) => {
  const s = getSettings(ctx.from.id);
  s.quality = ctx.match[1];
  userSettings.set(ctx.from.id, s);
  await ctx.editMessageText(`✅ Default quality set to ${ctx.match[1]}.`, { parse_mode: "Markdown" });
  await ctx.answerCallbackQuery();
  setTimeout(async () => {
    await ctx.reply("⚙️ *Your Settings*", { parse_mode: "Markdown", reply_markup: settingsKeyboard(ctx.from.id) });
  }, 1000);
});

bot.callbackQuery("set_mode", async (ctx) => {
  const keyboard = new InlineKeyboard()
    .text("Fixed ✅", "set_mode_fixed").row()
    .text("Manual 🎛", "set_mode_manual").row()
    .text("⬅️ Back", "back_settings");
  await ctx.editMessageText("🔁 *Download Mode:*", { parse_mode: "Markdown", reply_markup: keyboard });
  await ctx.answerCallbackQuery();
});

bot.callbackQuery(/set_mode_(fixed|manual)/, async (ctx) => {
  const s = getSettings(ctx.from.id);
  s.mode = ctx.match[1];
  userSettings.set(ctx.from.id, s);
  await ctx.editMessageText(`✅ Mode set to ${ctx.match[1]}.`, { parse_mode: "Markdown" });
  await ctx.answerCallbackQuery();
  setTimeout(async () => {
    await ctx.reply("⚙️ *Your Settings*", { parse_mode: "Markdown", reply_markup: settingsKeyboard(ctx.from.id) });
  }, 1000);
});

bot.callbackQuery("set_cleanup", async (ctx) => {
  const keyboard = new InlineKeyboard()
    .text("5 min", "set_cleanup_5").text("10 min", "set_cleanup_10").row()
    .text("15 min", "set_cleanup_15").text("30 min", "set_cleanup_30").row()
    .text("♾ Never", "set_cleanup_0").row()
    .text("⬅️ Back", "back_settings");
  await ctx.editMessageText("🧹 *Auto-Cleanup Timer:*", { parse_mode: "Markdown", reply_markup: keyboard });
  await ctx.answerCallbackQuery();
});

bot.callbackQuery(/set_cleanup_(\d+)/, async (ctx) => {
  const s = getSettings(ctx.from.id);
  s.cleanupMinutes = parseInt(ctx.match[1]);
  userSettings.set(ctx.from.id, s);
  await ctx.editMessageText(`✅ Cleanup set to ${ctx.match[1] === "0" ? "Never" : ctx.match[1] + " min"}.`, { parse_mode: "Markdown" });
  await ctx.answerCallbackQuery();
  setTimeout(async () => {
    await ctx.reply("⚙️ *Your Settings*", { parse_mode: "Markdown", reply_markup: settingsKeyboard(ctx.from.id) });
  }, 1000);
});

bot.callbackQuery("back_settings", async (ctx) => {
  await ctx.editMessageText("⚙️ *Your Settings*", { parse_mode: "Markdown", reply_markup: settingsKeyboard(ctx.from.id) });
  await ctx.answerCallbackQuery();
});

bot.callbackQuery("close_settings", async (ctx) => {
  await ctx.deleteMessage();
  await ctx.answerCallbackQuery();
});

// ---------- Download callbacks ----------
bot.callbackQuery("dl:video", async (ctx) => {
  const url = ctx.session.currentUrl;
  if (!url) return ctx.reply("No URL found.");
  const s = getSettings(ctx.from.id);
  if (s.mode === "fixed") {
    await handleVideoDownload(ctx, url, s.quality);
  } else {
    const heights = await getAvailableQualities(url);
    const keyboard = new InlineKeyboard();
    for (const h of heights) keyboard.text(`${h}p`, `dl:quality:${h}p`).row();
    keyboard.text("⭐ Best", "dl:quality:best").row().text("❌ Cancel", "dl:cancel");
    await ctx.editMessageText("🎬 *Select video quality:*", { parse_mode: "Markdown", reply_markup: keyboard });
  }
  await ctx.answerCallbackQuery();
});

bot.callbackQuery(/dl:quality:(.+)/, async (ctx) => {
  const url = ctx.session.currentUrl;
  if (!url) return ctx.reply("No URL.");
  await handleVideoDownload(ctx, url, ctx.match[1]);
  await ctx.answerCallbackQuery();
});

bot.callbackQuery("dl:audio", async (ctx) => {
  const url = ctx.session.currentUrl;
  if (!url) return ctx.reply("No URL.");
  await handleAudioDownload(ctx, url);
  await ctx.answerCallbackQuery();
});

bot.callbackQuery("dl:thumb", async (ctx) => {
  const url = ctx.session.currentUrl;
  if (!url) return ctx.reply("No URL.");
  await handleThumbnail(ctx, url);
  await ctx.answerCallbackQuery();
});

bot.callbackQuery("dl:cancel", async (ctx) => {
  await ctx.editMessageText("❌ Cancelled.");
  await ctx.answerCallbackQuery();
});

// ---------- Message handler ----------
bot.on("message:text", async (ctx) => {
  const text = ctx.message.text;
  const match = text.match(/(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/);
  if (!match) return ctx.reply("Please send a valid YouTube URL.");
  const url = match[0];
  ctx.session.currentUrl = url;
  try {
    const cookieArgs = await getCookieArgs();
    const { stdout } = await $`yt-dlp ${cookieArgs} -J ${url}`;
    const info = JSON.parse(stdout);
    const dur = info.duration ? `${Math.floor(info.duration / 60)}m ${info.duration % 60}s` : "?";
    const keyboard = new InlineKeyboard()
      .text("🎬 Video", "dl:video").row()
      .text("🎵 Audio MP3", "dl:audio").row()
      .text("🖼 Thumbnail", "dl:thumb").row()
      .text("❌ Cancel", "dl:cancel");
    await ctx.reply(`📹 *${info.title}*\n⏱ \`${dur}\`\n\nWhat would you like?`, {
      parse_mode: "Markdown",
      reply_markup: keyboard,
    });
  } catch (err) {
    await ctx.reply(`❌ Failed to fetch video info: \`${err.message}\``, { parse_mode: "Markdown" });
  }
});

// ---------- Helper keyboard ----------
function settingsKeyboard(userId) {
  const s = getSettings(userId);
  const modeLabel = s.mode === "fixed" ? "Fixed ✅" : "Manual 🎛";
  const timerLabel = s.cleanupMinutes === 0 ? "♾ Never" : `${s.cleanupMinutes} min`;
  return new InlineKeyboard()
    .text(`🎬 Quality: ${s.quality.toUpperCase()}`, "set_quality").row()
    .text(`🔁 Mode: ${modeLabel}`, "set_mode").row()
    .text(`🧹 Cleanup: ${timerLabel}`, "set_cleanup").row()
    .text("❌ Close", "close_settings");
}

// ---------- Download handlers ----------
async function handleVideoDownload(ctx, url, quality) {
  const msg = await ctx.reply(`⬇️ *Downloading (${quality})…*`, { parse_mode: "Markdown" });
  try {
    const { outputPath, title, videoId } = await downloadVideo(url, quality);
    const thumb = await downloadThumbnail(videoId);
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, `📤 *Uploading video…*`, { parse_mode: "Markdown" });
    await ctx.replyWithVideo(new Blob([await Deno.readFile(outputPath)]), {
      caption: `🎬 ${title}\n[${quality}]`,
      thumbnail: thumb ? new Blob([await Deno.readFile(thumb)]) : undefined,
      supports_streaming: true,
    });
    await ctx.api.deleteMessage(ctx.chat.id, msg.message_id);
    const minutes = getSettings(ctx.from.id).cleanupMinutes;
    scheduleCleanup(outputPath, minutes);
    if (thumb) scheduleCleanup(thumb, minutes);
  } catch (err) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, `❌ Download failed: \`${err.message}\``, { parse_mode: "Markdown" });
  }
}

async function handleAudioDownload(ctx, url) {
  const msg = await ctx.reply(`⬇️ *Extracting audio…*`, { parse_mode: "Markdown" });
  try {
    const { mp3Path, title } = await downloadAudio(url);
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, `📤 *Uploading MP3…*`, { parse_mode: "Markdown" });
    await ctx.replyWithDocument(new Blob([await Deno.readFile(mp3Path)]), {
      caption: `🎵 ${title}`,
      filename: `${title}.mp3`,
    });
    await ctx.api.deleteMessage(ctx.chat.id, msg.message_id);
    scheduleCleanup(mp3Path, getSettings(ctx.from.id).cleanupMinutes);
  } catch (err) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, `❌ Audio failed: \`${err.message}\``, { parse_mode: "Markdown" });
  }
}

async function handleThumbnail(ctx, url) {
  const msg = await ctx.reply(`🖼 *Downloading thumbnail…*`, { parse_mode: "Markdown" });
  try {
    const cookieArgs = await getCookieArgs();
    const { stdout } = await $`yt-dlp ${cookieArgs} -J ${url}`;
    const info = JSON.parse(stdout);
    const thumb = await downloadThumbnail(info.id);
    if (!thumb) throw new Error("No thumbnail available");
    await ctx.replyWithPhoto(new Blob([await Deno.readFile(thumb)]), { caption: `🖼 ${info.title}` });
    await ctx.api.deleteMessage(ctx.chat.id, msg.message_id);
    scheduleCleanup(thumb, getSettings(ctx.from.id).cleanupMinutes);
  } catch (err) {
    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, `❌ Thumbnail failed: \`${err.message}\``, { parse_mode: "Markdown" });
  }
}

// ---------- Start bot ----------
await logYtdlpVersion();
cleanupWorker();
bot.start();
console.log("Bot started (Deno + fixed cookie support)");