import { Bot, InlineKeyboard, session, type Context } from "https://deno.land/x/grammy@v1.27.0/mod.ts";
import { type SessionFlavor } from "https://deno.land/x/grammy@v1.27.0/mod.ts";
import { ensureDir, exists } from "https://deno.land/std@0.224.0/fs/mod.ts";
import { join, basename } from "https://deno.land/std@0.224.0/path/mod.ts";
import { $ } from "https://deno.land/x/dax@0.39.0/mod.ts";

// ---------- Types ----------
interface SessionData {
  currentUrl?: string;
  currentInfo?: any;
  searchResults?: any[];
}
type MyContext = Context & SessionFlavor<SessionData>;

// ---------- Config ----------
const BOT_TOKEN = Deno.env.get("BOT_TOKEN");
if (!BOT_TOKEN) throw new Error("Missing BOT_TOKEN env var");

const DOWNLOAD_DIR = "./downloads";
await ensureDir(DOWNLOAD_DIR);

// User settings (in-memory)
const userSettings = new Map<number, any>();
const defaultSettings = { quality: "720p", mode: "manual", cleanupMinutes: 10 };

// Cleanup registry
const cleanupRegistry = new Map<string, number>();

// ---------- Helpers ----------
function getSettings(userId: number) {
  if (!userSettings.has(userId)) {
    userSettings.set(userId, { ...defaultSettings });
  }
  return userSettings.get(userId);
}

function scheduleCleanup(filePath: string, minutes: number) {
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
          console.log(`Cleaned: ${filePath}`);
        } catch (err) {
          console.error(`Cleanup error ${filePath}:`, err);
        }
      }
    }
  }, 60000);
}

// Get available qualities using yt-dlp
async function getAvailableQualities(url: string): Promise<number[]> {
  try {
    const cmd = await $`yt-dlp -J --flat-playlist ${url}`.text();
    const data = JSON.parse(cmd);
    const formats = data.formats || [];
    const heights = new Set<number>();
    for (const f of formats) {
      if (f.height && f.vcodec !== "none") {
        heights.add(f.height);
      }
    }
    const sorted = Array.from(heights).sort((a, b) => a - b);
    console.log("Qualities found:", sorted);
    return sorted.length ? sorted : [360, 480, 720, 1080];
  } catch (err) {
    console.error("getAvailableQualities error:", err);
    return [360, 480, 720, 1080];
  }
}

// Download video (video+audio) using yt-dlp
async function downloadVideo(url: string, quality: string): Promise<{ outputPath: string; title: string; videoId: string }> {
  // Get info first
  const infoJson = await $`yt-dlp -J ${url}`.text();
  const info = JSON.parse(infoJson);
  const title = info.title.replace(/[^\w\s]/gi, "");
  const videoId = info.id;
  const outputPath = join(DOWNLOAD_DIR, `${videoId}.mp4`);

  let formatSpec = "";
  if (quality !== "best") {
    const target = parseInt(quality);
    if (!isNaN(target)) {
      formatSpec = `-f "bestvideo[height<=${target}][ext=mp4]+bestaudio[ext=m4a]/best[height<=${target}]"`;
    }
  }
  const cmd = `yt-dlp ${formatSpec} -o "${outputPath}" --merge-output-format mp4 ${url}`;
  await $`bash -c ${cmd}`;
  return { outputPath, title, videoId };
}

// Download audio as MP3
async function downloadAudio(url: string): Promise<{ mp3Path: string; title: string; videoId: string }> {
  const infoJson = await $`yt-dlp -J ${url}`.text();
  const info = JSON.parse(infoJson);
  const title = info.title.replace(/[^\w\s]/gi, "");
  const videoId = info.id;
  const mp3Path = join(DOWNLOAD_DIR, `${videoId}.mp3`);
  await $`yt-dlp -f bestaudio --extract-audio --audio-format mp3 --audio-quality 192K -o "${mp3Path}" ${url}`;
  return { mp3Path, title, videoId };
}

// Download thumbnail
async function downloadThumbnail(videoId: string): Promise<string | null> {
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
    } catch {
      continue;
    }
  }
  return null;
}

// ---------- Bot Setup ----------
const bot = new Bot<MyContext>(BOT_TOKEN);
bot.use(session({ initial: () => ({}) }));

// Commands
bot.command("start", (ctx) => {
  ctx.reply(
    `👋 *Welcome to YT Downloader Bot (Deno + yt-dlp)!*\n\n` +
    `Send me a YouTube URL.\n` +
    `⚙️ /settings – Preferences\n` +
    `❓ /help – This message`,
    { parse_mode: "Markdown" }
  );
});

bot.command("help", (ctx) => ctx.reply("Send a YouTube URL. Use /settings to change quality."));

bot.command("settings", async (ctx) => {
  const userId = ctx.from!.id;
  const s = getSettings(userId);
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
  const uptime = Deno.env.get("DENO_START_TIME") ? (Date.now() - parseInt(Deno.env.get("DENO_START_TIME")!)) / 1000 : process.uptime();
  const hours = Math.floor(uptime / 3600);
  const minutes = Math.floor((uptime % 3600) / 60);
  const seconds = Math.floor(uptime % 60);
  ctx.reply(
    `📊 *Bot Stats*\nUptime: ${hours}h ${minutes}m ${seconds}s\nActive users: ${userSettings.size}\nDeno: ${Deno.version.deno}`,
    { parse_mode: "Markdown" }
  );
});

// Callback handlers
bot.callbackQuery("set_quality", async (ctx) => {
  const qualities = ["360p", "480p", "720p", "1080p", "1440p", "2160p", "best"];
  const keyboard = new InlineKeyboard();
  for (const q of qualities) keyboard.text(q, `set_quality_${q}`).row();
  keyboard.text("⬅️ Back", "back_settings");
  await ctx.editMessageText("🎬 *Select default quality:*", {
    parse_mode: "Markdown",
    reply_markup: keyboard,
  });
  await ctx.answerCallbackQuery();
});

bot.callbackQuery(/set_quality_(.+)/, async (ctx) => {
  const userId = ctx.from!.id;
  const quality = ctx.match![1];
  const s = getSettings(userId);
  s.quality = quality;
  userSettings.set(userId, s);
  await ctx.editMessageText(`✅ Default quality set to ${quality}.`, { parse_mode: "Markdown" });
  await ctx.answerCallbackQuery();
  setTimeout(async () => {
    await ctx.reply("⚙️ *Your Settings*", { parse_mode: "Markdown", reply_markup: settingsKeyboard(userId) });
  }, 1000);
});

bot.callbackQuery("set_mode", async (ctx) => {
  const keyboard = new InlineKeyboard()
    .text("Fixed ✅", "set_mode_fixed").row()
    .text("Manual 🎛", "set_mode_manual").row()
    .text("⬅️ Back", "back_settings");
  await ctx.editMessageText("🔁 *Download Mode:*\n• Fixed – always use default quality\n• Manual – choose per download", {
    parse_mode: "Markdown",
    reply_markup: keyboard,
  });
  await ctx.answerCallbackQuery();
});

bot.callbackQuery(/set_mode_(fixed|manual)/, async (ctx) => {
  const userId = ctx.from!.id;
  const mode = ctx.match![1];
  const s = getSettings(userId);
  s.mode = mode;
  userSettings.set(userId, s);
  await ctx.editMessageText(`✅ Mode set to ${mode}.`, { parse_mode: "Markdown" });
  await ctx.answerCallbackQuery();
  setTimeout(async () => {
    await ctx.reply("⚙️ *Your Settings*", { parse_mode: "Markdown", reply_markup: settingsKeyboard(userId) });
  }, 1000);
});

bot.callbackQuery("set_cleanup", async (ctx) => {
  const keyboard = new InlineKeyboard()
    .text("5 min", "set_cleanup_5").text("10 min", "set_cleanup_10").row()
    .text("15 min", "set_cleanup_15").text("30 min", "set_cleanup_30").row()
    .text("♾ Never", "set_cleanup_0").row()
    .text("⬅️ Back", "back_settings");
  await ctx.editMessageText("🧹 *Auto-Cleanup Timer:*", {
    parse_mode: "Markdown",
    reply_markup: keyboard,
  });
  await ctx.answerCallbackQuery();
});

bot.callbackQuery(/set_cleanup_(\d+)/, async (ctx) => {
  const userId = ctx.from!.id;
  const minutes = parseInt(ctx.match![1]);
  const s = getSettings(userId);
  s.cleanupMinutes = minutes;
  userSettings.set(userId, s);
  await ctx.editMessageText(`✅ Cleanup set to ${minutes === 0 ? "Never" : minutes + " min"}.`, { parse_mode: "Markdown" });
  await ctx.answerCallbackQuery();
  setTimeout(async () => {
    await ctx.reply("⚙️ *Your Settings*", { parse_mode: "Markdown", reply_markup: settingsKeyboard(userId) });
  }, 1000);
});

bot.callbackQuery("back_settings", async (ctx) => {
  const userId = ctx.from!.id;
  await ctx.editMessageText("⚙️ *Your Settings*", {
    parse_mode: "Markdown",
    reply_markup: settingsKeyboard(userId),
  });
  await ctx.answerCallbackQuery();
});

bot.callbackQuery("close_settings", async (ctx) => {
  await ctx.deleteMessage();
  await ctx.answerCallbackQuery();
});

// Download actions
bot.callbackQuery("dl:video", async (ctx) => {
  const userId = ctx.from!.id;
  const url = ctx.session.currentUrl;
  if (!url) {
    await ctx.reply("No URL found. Please send again.");
    return;
  }
  const s = getSettings(userId);
  if (s.mode === "fixed") {
    await handleVideoDownload(ctx, url, s.quality);
  } else {
    const qualities = await getAvailableQualities(url);
    const keyboard = new InlineKeyboard();
    for (const h of qualities) keyboard.text(`${h}p`, `dl:quality:${h}p`).row();
    keyboard.text("⭐ Best", "dl:quality:best").row().text("❌ Cancel", "dl:cancel");
    await ctx.editMessageText("🎬 *Select video quality:*", {
      parse_mode: "Markdown",
      reply_markup: keyboard,
    });
  }
  await ctx.answerCallbackQuery();
});

bot.callbackQuery(/dl:quality:(.+)/, async (ctx) => {
  const url = ctx.session.currentUrl;
  if (!url) {
    await ctx.reply("No URL. Please send again.");
    return;
  }
  await handleVideoDownload(ctx, url, ctx.match![1]);
  await ctx.answerCallbackQuery();
});

bot.callbackQuery("dl:audio", async (ctx) => {
  const url = ctx.session.currentUrl;
  if (!url) {
    await ctx.reply("No URL. Please send again.");
    return;
  }
  await handleAudioDownload(ctx, url);
  await ctx.answerCallbackQuery();
});

bot.callbackQuery("dl:thumb", async (ctx) => {
  const url = ctx.session.currentUrl;
  if (!url) {
    await ctx.reply("No URL. Please send again.");
    return;
  }
  await handleThumbnail(ctx, url);
  await ctx.answerCallbackQuery();
});

bot.callbackQuery("dl:cancel", async (ctx) => {
  await ctx.editMessageText("❌ Cancelled.");
  await ctx.answerCallbackQuery();
});

// Message handler
bot.on("message:text", async (ctx) => {
  const text = ctx.message.text;
  const urlMatch = text.match(/(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/);
  if (urlMatch) {
    const url = urlMatch[0];
    ctx.session.currentUrl = url;
    try {
      const infoJson = await $`yt-dlp -J ${url}`.text();
      const info = JSON.parse(infoJson);
      const title = info.title;
      const duration = info.duration;
      const durStr = duration ? `${Math.floor(duration / 60)}m ${duration % 60}s` : "?";
      const keyboard = new InlineKeyboard()
        .text("🎬 Video", "dl:video").row()
        .text("🎵 Audio MP3", "dl:audio").row()
        .text("🖼 Thumbnail", "dl:thumb").row()
        .text("❌ Cancel", "dl:cancel");
      await ctx.reply(`📹 *${title}*\n⏱ \`${durStr}\`\n\nWhat would you like?`, {
        parse_mode: "Markdown",
        reply_markup: keyboard,
      });
    } catch (err) {
      await ctx.reply(`❌ Failed to fetch video info: \`${err.message}\``, { parse_mode: "Markdown" });
    }
  } else {
    await ctx.reply("Please send a valid YouTube URL (e.g., https://youtube.com/watch?v=...).");
  }
});

// Helper to generate settings keyboard
function settingsKeyboard(userId: number) {
  const s = getSettings(userId);
  const modeLabel = s.mode === "fixed" ? "Fixed ✅" : "Manual 🎛";
  const timerLabel = s.cleanupMinutes === 0 ? "♾ Never" : `${s.cleanupMinutes} min`;
  return new InlineKeyboard()
    .text(`🎬 Quality: ${s.quality.toUpperCase()}`, "set_quality").row()
    .text(`🔁 Mode: ${modeLabel}`, "set_mode").row()
    .text(`🧹 Cleanup: ${timerLabel}`, "set_cleanup").row()
    .text("❌ Close", "close_settings");
}

// Download handlers
async function handleVideoDownload(ctx: MyContext, url: string, quality: string) {
  const statusMsg = await ctx.reply(`⬇️ *Downloading (${quality})…*`, { parse_mode: "Markdown" });
  try {
    const { outputPath, title, videoId } = await downloadVideo(url, quality);
    const thumbPath = await downloadThumbnail(videoId);
    await ctx.api.editMessageText(ctx.chat!.id, statusMsg.message_id, `📤 *Uploading video…*`, { parse_mode: "Markdown" });
    await ctx.replyWithVideo(new Blob([await Deno.readFile(outputPath)]), {
      caption: `🎬 ${title}\n[${quality}]`,
      thumbnail: thumbPath ? new Blob([await Deno.readFile(thumbPath)]) : undefined,
      supports_streaming: true,
    });
    await ctx.api.deleteMessage(ctx.chat!.id, statusMsg.message_id);
    const userId = ctx.from!.id;
    const minutes = getSettings(userId).cleanupMinutes;
    scheduleCleanup(outputPath, minutes);
    if (thumbPath) scheduleCleanup(thumbPath, minutes);
  } catch (err) {
    console.error(err);
    await ctx.api.editMessageText(ctx.chat!.id, statusMsg.message_id, `❌ Download failed: \`${err.message}\``, { parse_mode: "Markdown" });
  }
}

async function handleAudioDownload(ctx: MyContext, url: string) {
  const statusMsg = await ctx.reply(`⬇️ *Extracting audio…*`, { parse_mode: "Markdown" });
  try {
    const { mp3Path, title } = await downloadAudio(url);
    await ctx.api.editMessageText(ctx.chat!.id, statusMsg.message_id, `📤 *Uploading MP3…*`, { parse_mode: "Markdown" });
    await ctx.replyWithDocument(new Blob([await Deno.readFile(mp3Path)]), {
      caption: `🎵 ${title}`,
      filename: `${title}.mp3`,
    });
    await ctx.api.deleteMessage(ctx.chat!.id, statusMsg.message_id);
    const userId = ctx.from!.id;
    const minutes = getSettings(userId).cleanupMinutes;
    scheduleCleanup(mp3Path, minutes);
  } catch (err) {
    console.error(err);
    await ctx.api.editMessageText(ctx.chat!.id, statusMsg.message_id, `❌ Audio failed: \`${err.message}\``, { parse_mode: "Markdown" });
  }
}

async function handleThumbnail(ctx: MyContext, url: string) {
  const statusMsg = await ctx.reply(`🖼 *Downloading thumbnail…*`, { parse_mode: "Markdown" });
  try {
    const infoJson = await $`yt-dlp -J ${url}`.text();
    const info = JSON.parse(infoJson);
    const videoId = info.id;
    const thumbPath = await downloadThumbnail(videoId);
    if (!thumbPath) throw new Error("No thumbnail available");
    await ctx.replyWithPhoto(new Blob([await Deno.readFile(thumbPath)]), { caption: `🖼 ${info.title}` });
    await ctx.api.deleteMessage(ctx.chat!.id, statusMsg.message_id);
    const userId = ctx.from!.id;
    const minutes = getSettings(userId).cleanupMinutes;
    scheduleCleanup(thumbPath, minutes);
  } catch (err) {
    console.error(err);
    await ctx.api.editMessageText(ctx.chat!.id, statusMsg.message_id, `❌ Thumbnail failed: \`${err.message}\``, { parse_mode: "Markdown" });
  }
}

// Start bot
cleanupWorker();
bot.start();
console.log("Bot started");