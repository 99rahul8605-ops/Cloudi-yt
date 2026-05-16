const TelegramBot = require('node-telegram-bot-api');
const fs = require('fs-extra');
const path = require('path');
const axios = require('axios');
const { exec } = require('child_process');
const util = require('util');
const http = require('http');
const { createReadStream } = require('fs');
const execPromise = util.promisify(exec);

// ---------- Config ----------
const BOT_TOKEN = process.env.BOT_TOKEN;
if (!BOT_TOKEN) throw new Error('Missing BOT_TOKEN env var');

// Optional: set LOCAL_BOT_API_URL to your custom Bot API server (e.g., http://localhost:8081) for 2GB uploads
const LOCAL_BOT_API_URL = process.env.LOCAL_BOT_API_URL || 'https://api.telegram.org';

const DOWNLOAD_DIR = path.join(__dirname, 'downloads');
const COOKIE_PATH = process.env.COOKIE_PATH || '/app/cookies.txt';
fs.ensureDirSync(DOWNLOAD_DIR);

const YTDLP_TIMEOUT = 120000; // 2 minutes

// ---------- Mandatory cookie check ----------
async function cookieExists() {
  try {
    await fs.access(COOKIE_PATH);
    return true;
  } catch {
    return false;
  }
}

async function getCookieArg() {
  const exists = await cookieExists();
  if (!exists) {
    throw new Error(`❌ Cookies are required but not found at ${COOKIE_PATH}. Please export cookies from a logged-in YouTube session (use "Get cookies.txt LOCALLY" extension) and place the file at ${COOKIE_PATH}.`);
  }
  return `--cookies "${COOKIE_PATH}"`;
}

// Startup validation – exit if cookies missing
(async () => {
  try {
    await getCookieArg();
    console.log(`✅ cookies.txt found at ${COOKIE_PATH}`);
  } catch (err) {
    console.error(err.message);
    process.exit(1);
  }
})();

// ---------- User settings and state ----------
const userSettings = new Map();
const defaultSettings = { quality: '720p', mode: 'manual', cleanupMinutes: 10 };
const cleanupRegistry = new Map();
const BOT_START_TIME = Date.now();
const pendingDownloads = new Map(); // chatId -> { messageId: { url, timestamp } }
const PENDING_TTL = 10 * 60 * 1000; // 10 minutes

// Clean up expired pending entries
setInterval(() => {
  const now = Date.now();
  for (const [chatId, entries] of pendingDownloads.entries()) {
    for (const [msgId, data] of Object.entries(entries)) {
      if (now - data.timestamp > PENDING_TTL) delete entries[msgId];
    }
    if (Object.keys(entries).length === 0) pendingDownloads.delete(chatId);
  }
}, 60000);

// ---------- Health server ----------
const PORT = process.env.PORT || 8080;
const healthServer = http.createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'text/plain' });
  res.end('OK');
});
healthServer.listen(PORT, () => console.log(`✅ Health server on port ${PORT}`));

// ---------- yt-dlp version ----------
let ytdlpVersion = null;
async function getYtdlpVersion() {
  if (ytdlpVersion) return ytdlpVersion;
  try {
    const { stdout } = await execPromise('yt-dlp --version', { timeout: 5000 });
    ytdlpVersion = stdout.trim();
    return ytdlpVersion;
  } catch {
    return 'unknown';
  }
}

// ---------- yt-dlp wrapper with all optimizations ----------
async function runYtdlp(args, timeout = YTDLP_TIMEOUT, verbose = false) {
  const cookieArg = await getCookieArg(); // will throw if missing
  const opts = [
    '--concurrent-fragments 50',
    '--geo-bypass',
    '--js-runtimes deno',
    '--remote-components ejs:github',
  ].join(' ');
  const verboseFlag = verbose ? '--verbose' : '';
  const fullArgs = `${cookieArg} ${opts} ${verboseFlag} ${args}`;
  console.log(`[yt-dlp] Running: yt-dlp ${fullArgs.substring(0, 200)}...`);
  try {
    const { stdout, stderr } = await execPromise(`yt-dlp ${fullArgs}`, { timeout });
    if (stderr) console.log(`[yt-dlp stderr]\n${stderr}`);
    if (stderr && !stderr.includes('WARNING') && !stderr.includes('[youtube]')) {
      throw new Error(stderr);
    }
    return stdout;
  } catch (err) {
    if (err.killed && err.signal === 'SIGTERM') {
      throw new Error('yt-dlp timed out after 2 minutes');
    }
    throw err;
  }
}

// ---------- Get all available video heights (including video-only) ----------
async function getAvailableQualities(url) {
  try {
    const stdout = await runYtdlp(`-J "${url}"`, YTDLP_TIMEOUT, false);
    const data = JSON.parse(stdout);
    const heights = new Set();
    for (const f of data.formats || []) {
      if (f.height && f.vcodec && f.vcodec !== 'none') {
        heights.add(f.height);
      }
    }
    const sorted = Array.from(heights).sort((a, b) => a - b);
    console.log(`Available qualities: ${sorted.join(', ')}`);
    return sorted.length ? sorted : [360, 480, 720, 1080];
  } catch (err) {
    console.error('getAvailableQualities error:', err.message);
    return [360, 480, 720, 1080];
  }
}

// ---------- Download video (separate video+audio, merge with ffmpeg) ----------
async function downloadVideo(url, quality) {
  const infoJson = await runYtdlp(`-J "${url}"`, YTDLP_TIMEOUT, false);
  const info = JSON.parse(infoJson);
  const title = info.title.replace(/[^\w\s]/gi, '');
  const videoId = info.id;
  const outputPath = path.join(DOWNLOAD_DIR, `${videoId}.mp4`);

  let formatArg = '';
  if (quality !== 'best') {
    const target = parseInt(quality);
    if (!isNaN(target)) {
      formatArg = `-f "bestvideo[height<=${target}]+bestaudio/best[height<=${target}]"`;
    }
  } else {
    formatArg = `-f "bestvideo+bestaudio/best"`;
  }
  const cmd = `${formatArg} -o "${outputPath}" --merge-output-format mp4 --verbose "${url}"`;
  console.log(`[yt-dlp] Download command: yt-dlp ${cmd}`);
  await runYtdlp(cmd, YTDLP_TIMEOUT, true);
  const stats = await fs.stat(outputPath);
  if (stats.size === 0) throw new Error('Downloaded file is empty');
  console.log(`Downloaded ${outputPath} (${stats.size} bytes)`);
  return { outputPath, title, videoId, info };
}

async function downloadAudio(url) {
  const infoJson = await runYtdlp(`-J "${url}"`, YTDLP_TIMEOUT, false);
  const info = JSON.parse(infoJson);
  const title = info.title.replace(/[^\w\s]/gi, '');
  const videoId = info.id;
  const mp3Path = path.join(DOWNLOAD_DIR, `${videoId}.mp3`);
  await runYtdlp(`-f bestaudio --extract-audio --audio-format mp3 --audio-quality 192K -o "${mp3Path}" "${url}"`, YTDLP_TIMEOUT, true);
  return { mp3Path, title, videoId };
}

async function downloadThumbnail(videoId) {
  const urls = [
    `https://img.youtube.com/vi/${videoId}/maxresdefault.jpg`,
    `https://img.youtube.com/vi/${videoId}/hqdefault.jpg`,
  ];
  for (const url of urls) {
    try {
      const response = await axios({ url, responseType: 'stream', timeout: 10000 });
      const thumbPath = path.join(DOWNLOAD_DIR, `${videoId}_thumb.jpg`);
      const writer = fs.createWriteStream(thumbPath);
      response.data.pipe(writer);
      await new Promise((resolve, reject) => {
        writer.on('finish', resolve);
        writer.on('error', reject);
      });
      return thumbPath;
    } catch {}
  }
  return null;
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
          await fs.remove(filePath);
          cleanupRegistry.delete(filePath);
        } catch {}
      }
    }
  }, 60000);
}

// ---------- Stats helpers ----------
async function getDownloadDirStats() {
  try {
    const files = await fs.readdir(DOWNLOAD_DIR);
    let totalSize = 0;
    for (const file of files) {
      const stat = await fs.stat(path.join(DOWNLOAD_DIR, file));
      if (stat.isFile()) totalSize += stat.size;
    }
    return { count: files.length, sizeMB: (totalSize / (1024 * 1024)).toFixed(2) };
  } catch {
    return { count: 0, sizeMB: '0.00' };
  }
}

function formatUptime(ms) {
  const seconds = Math.floor(ms / 1000);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (minutes) parts.push(`${minutes}m`);
  parts.push(`${secs}s`);
  return parts.join(' ');
}

// ---------- Telegram bot ----------
const bot = new TelegramBot(BOT_TOKEN, {
  polling: true,
  apiRoot: LOCAL_BOT_API_URL
});
console.log(`Bot using API root: ${LOCAL_BOT_API_URL}`);

// ---------- Inline keyboards ----------
function settingsKeyboard(userId) {
  const s = getSettings(userId);
  return {
    inline_keyboard: [
      [{ text: `🎬 Quality: ${s.quality.toUpperCase()}`, callback_data: 'set_quality' }],
      [{ text: `🔁 Mode: ${s.mode === 'fixed' ? 'Fixed ✅' : 'Manual 🎛'}`, callback_data: 'set_mode' }],
      [{ text: `🧹 Cleanup: ${s.cleanupMinutes === 0 ? '♾ Never' : `${s.cleanupMinutes} min`}`, callback_data: 'set_cleanup' }],
      [{ text: '❌ Close', callback_data: 'close_settings' }],
    ],
  };
}

function downloadTypeKeyboard() {
  return {
    inline_keyboard: [
      [{ text: '🎬 Video', callback_data: 'dl_video' }],
      [{ text: '🎵 Audio MP3', callback_data: 'dl_audio' }],
      [{ text: '🖼 Thumbnail', callback_data: 'dl_thumb' }],
      [{ text: '❌ Cancel', callback_data: 'cancel_download' }],
    ],
  };
}

function qualityKeyboard(qualities) {
  const buttons = qualities.map(h => [{ text: `${h}p`, callback_data: `quality_${h}p` }]);
  buttons.push([{ text: '⭐ Best', callback_data: 'quality_best' }]);
  buttons.push([{ text: '❌ Cancel', callback_data: 'cancel_download' }]);
  return { inline_keyboard: buttons };
}

// ---------- Command handlers ----------
bot.onText(/\/start/, (msg) => {
  bot.sendMessage(msg.chat.id,
    `👋 *Welcome to YT Downloader Bot (Cookies Required, FFmpeg Merge)*\n\n` +
    `Send me a YouTube URL.\n` +
    `⚙️ /settings – Preferences\n` +
    `📊 /stats – Bot statistics\n` +
    `🔍 /formats <url> – Debug available qualities\n` +
    `🍪 /checkcookie – Check cookie status`,
    { parse_mode: 'Markdown' }
  );
});

bot.onText(/\/checkcookie/, async (msg) => {
  const chatId = msg.chat.id;
  try {
    await getCookieArg(); // will throw if missing
    const stats = await fs.stat(COOKIE_PATH);
    await bot.sendMessage(chatId,
      `🍪 *Cookie file found*\n\n` +
      `📁 Path: \`${COOKIE_PATH}\`\n` +
      `📦 Size: ${stats.size} bytes\n` +
      `✅ Cookies are being used for all downloads.`,
      { parse_mode: 'Markdown' }
    );
  } catch (err) {
    await bot.sendMessage(chatId,
      `🍪 *Cookie file MISSING*\n\n` +
      `❌ ${err.message}\n\n` +
      `The bot requires cookies to work. Please place a valid \`cookies.txt\` at \`${COOKIE_PATH}\`.`,
      { parse_mode: 'Markdown' }
    );
  }
});

bot.onText(/\/formats (.+)/, async (msg, match) => {
  const chatId = msg.chat.id;
  const url = match[1];
  const processing = await bot.sendMessage(chatId, '🔍 Fetching formats...');
  try {
    const stdout = await runYtdlp(`-J "${url}"`, YTDLP_TIMEOUT, false);
    const data = JSON.parse(stdout);
    const formats = data.formats || [];
    const lines = [];
    for (const f of formats) {
      if (f.height && f.vcodec !== 'none') {
        lines.push(`${f.height}p (${f.ext}, vcodec: ${f.vcodec.split('.')[0]}, acodec: ${f.acodec || 'none'})`);
      }
    }
    if (lines.length === 0) {
      await bot.editMessageText('❌ No video formats found.', { chat_id: chatId, message_id: processing.message_id, parse_mode: 'Markdown' });
    } else {
      const text = `📺 *Available video heights:*\n${lines.join('\n')}`;
      await bot.editMessageText(text, { chat_id: chatId, message_id: processing.message_id, parse_mode: 'Markdown' });
    }
  } catch (err) {
    await bot.editMessageText(`❌ Error: \`${err.message}\``, { chat_id: chatId, message_id: processing.message_id, parse_mode: 'Markdown' });
  }
});

bot.onText(/\/stats/, async (msg) => {
  const chatId = msg.chat.id;
  const userId = msg.from.id;
  try {
    await getCookieArg(); // just to verify cookies exist
    const [ytVersion, dirStats, uptime, activeUsers, nodeVersion, pendingCleanup, userSettingsObj] = await Promise.all([
      getYtdlpVersion(),
      getDownloadDirStats(),
      formatUptime(Date.now() - BOT_START_TIME),
      userSettings.size,
      process.version,
      cleanupRegistry.size,
      getSettings(userId)
    ]);
    const statsText =
      `📊 *Bot Statistics*\n\n` +
      `🔧 *yt-dlp version*: \`${ytVersion}\`\n` +
      `🍪 *Cookies*: ✅ Present (mandatory)\n` +
      `🚀 *Optimizations*: concurrent fragments=50, geo-bypass, deno runtime\n` +
      `📤 *Upload limit*: ${LOCAL_BOT_API_URL !== 'https://api.telegram.org' ? '2GB (custom API server)' : '50MB (public API)'}\n` +
      `🎞️ *FFmpeg merge*: Enabled\n` +
      `⏱ *Uptime*: ${uptime}\n` +
      `👥 *Active users*: ${activeUsers}\n` +
      `💻 *Node.js*: ${nodeVersion}\n` +
      `💾 *Download folder*: ${dirStats.count} files, ${dirStats.sizeMB} MB\n` +
      `🧹 *Pending cleanup*: ${pendingCleanup} files\n` +
      `⚙️ *Your settings*: Quality = ${userSettingsObj.quality}, Mode = ${userSettingsObj.mode}, Cleanup = ${userSettingsObj.cleanupMinutes === 0 ? 'Never' : userSettingsObj.cleanupMinutes + ' min'}`;
    await bot.sendMessage(chatId, statsText, { parse_mode: 'Markdown' });
  } catch (err) {
    await bot.sendMessage(chatId, `❌ Stats error: \`${err.message}\``, { parse_mode: 'Markdown' });
  }
});

bot.onText(/\/settings/, async (msg) => {
  await bot.sendMessage(msg.chat.id, '⚙️ *Your Settings*', {
    parse_mode: 'Markdown',
    reply_markup: settingsKeyboard(msg.from.id),
  });
});

// ---------- Callback queries (fully implemented) ----------
bot.on('callback_query', async (callbackQuery) => {
  const chatId = callbackQuery.message.chat.id;
  const messageId = callbackQuery.message.message_id;
  const userId = callbackQuery.from.id;
  const data = callbackQuery.data;

  // Always answer first to avoid "query too old"
  await bot.answerCallbackQuery(callbackQuery.id);

  function removePendingEntry() {
    const chatPending = pendingDownloads.get(chatId);
    if (chatPending && chatPending[messageId]) {
      delete chatPending[messageId];
      if (Object.keys(chatPending).length === 0) pendingDownloads.delete(chatId);
    }
  }

  async function safeEdit(text, replyMarkup = null) {
    try {
      const options = { chat_id: chatId, message_id: messageId, parse_mode: 'Markdown' };
      if (replyMarkup) options.reply_markup = replyMarkup;
      await bot.editMessageText(text, options);
    } catch (err) {
      console.log(`Edit error (ignored): ${err.message}`);
    }
  }

  // Settings callbacks
  if (data === 'close_settings') {
    await safeEdit('Settings closed.');
    await bot.deleteMessage(chatId, messageId).catch(() => {});
    return;
  }
  if (data === 'set_quality') {
    const qualities = ['360p', '480p', '720p', '1080p', '1440p', '2160p', 'best'];
    const buttons = qualities.map(q => [{ text: q, callback_data: `set_quality_${q}` }]);
    buttons.push([{ text: '⬅️ Back', callback_data: 'back_settings' }]);
    await safeEdit('🎬 *Select default quality:*', { inline_keyboard: buttons });
    return;
  }
  if (data.startsWith('set_quality_')) {
    const quality = data.replace('set_quality_', '');
    const s = getSettings(userId);
    s.quality = quality;
    userSettings.set(userId, s);
    await safeEdit(`✅ Default quality set to ${quality}.`);
    setTimeout(async () => {
      await safeEdit('⚙️ *Your Settings*', settingsKeyboard(userId));
    }, 1000);
    return;
  }
  if (data === 'set_mode') {
    const buttons = [
      [{ text: 'Fixed ✅', callback_data: 'set_mode_fixed' }],
      [{ text: 'Manual 🎛', callback_data: 'set_mode_manual' }],
      [{ text: '⬅️ Back', callback_data: 'back_settings' }],
    ];
    await safeEdit('🔁 *Download Mode:*', { inline_keyboard: buttons });
    return;
  }
  if (data === 'set_mode_fixed') {
    const s = getSettings(userId);
    s.mode = 'fixed';
    userSettings.set(userId, s);
    await safeEdit('✅ Mode set to fixed.');
    setTimeout(async () => {
      await safeEdit('⚙️ *Your Settings*', settingsKeyboard(userId));
    }, 1000);
    return;
  }
  if (data === 'set_mode_manual') {
    const s = getSettings(userId);
    s.mode = 'manual';
    userSettings.set(userId, s);
    await safeEdit('✅ Mode set to manual.');
    setTimeout(async () => {
      await safeEdit('⚙️ *Your Settings*', settingsKeyboard(userId));
    }, 1000);
    return;
  }
  if (data === 'set_cleanup') {
    const buttons = [
      [{ text: '5 min', callback_data: 'set_cleanup_5' }, { text: '10 min', callback_data: 'set_cleanup_10' }],
      [{ text: '15 min', callback_data: 'set_cleanup_15' }, { text: '30 min', callback_data: 'set_cleanup_30' }],
      [{ text: '♾ Never', callback_data: 'set_cleanup_0' }],
      [{ text: '⬅️ Back', callback_data: 'back_settings' }],
    ];
    await safeEdit('🧹 *Auto-Cleanup Timer:*', { inline_keyboard: buttons });
    return;
  }
  if (data.startsWith('set_cleanup_')) {
    const minutes = parseInt(data.replace('set_cleanup_', ''));
    const s = getSettings(userId);
    s.cleanupMinutes = minutes;
    userSettings.set(userId, s);
    await safeEdit(`✅ Cleanup set to ${minutes === 0 ? 'Never' : minutes + ' min'}.`);
    setTimeout(async () => {
      await safeEdit('⚙️ *Your Settings*', settingsKeyboard(userId));
    }, 1000);
    return;
  }
  if (data === 'back_settings') {
    await safeEdit('⚙️ *Your Settings*', settingsKeyboard(userId));
    return;
  }

  // Download callbacks
  if (data === 'dl_video') {
    const entry = pendingDownloads.get(chatId)?.[messageId];
    const url = entry?.url;
    if (!url) {
      await bot.sendMessage(chatId, 'No URL found. Please send again.');
      return;
    }
    const s = getSettings(userId);
    if (s.mode === 'fixed') {
      removePendingEntry();
      const statusMsg = await bot.sendMessage(chatId, `⬇️ *Downloading (${s.quality})…*`, { parse_mode: 'Markdown' });
      await startVideoDownload(chatId, userId, url, s.quality, statusMsg.message_id);
    } else {
      const heights = await getAvailableQualities(url);
      await safeEdit('🎬 *Select video quality:*', qualityKeyboard(heights));
    }
    return;
  }
  if (data === 'dl_audio') {
    const entry = pendingDownloads.get(chatId)?.[messageId];
    const url = entry?.url;
    if (!url) {
      await bot.sendMessage(chatId, 'No URL found.');
      return;
    }
    removePendingEntry();
    const statusMsg = await bot.sendMessage(chatId, '⬇️ *Extracting audio…*', { parse_mode: 'Markdown' });
    await startAudioDownload(chatId, userId, url, statusMsg.message_id);
    return;
  }
  if (data === 'dl_thumb') {
    const entry = pendingDownloads.get(chatId)?.[messageId];
    const url = entry?.url;
    if (!url) {
      await bot.sendMessage(chatId, 'No URL found.');
      return;
    }
    removePendingEntry();
    const statusMsg = await bot.sendMessage(chatId, '🖼 *Downloading thumbnail…*', { parse_mode: 'Markdown' });
    await startThumbnailDownload(chatId, userId, url, statusMsg.message_id);
    return;
  }
  if (data === 'cancel_download') {
    removePendingEntry();
    await bot.deleteMessage(chatId, messageId).catch(() => {});
    return;
  }
  if (data.startsWith('quality_')) {
    const quality = data.replace('quality_', '');
    const entry = pendingDownloads.get(chatId)?.[messageId];
    const url = entry?.url;
    if (!url) {
      await bot.sendMessage(chatId, 'Session expired. Please send URL again.');
      return;
    }
    removePendingEntry();
    const statusMsg = await bot.sendMessage(chatId, `⬇️ *Downloading (${quality})…*`, { parse_mode: 'Markdown' });
    await startVideoDownload(chatId, userId, url, quality, statusMsg.message_id);
    await bot.deleteMessage(chatId, messageId).catch(() => {});
    return;
  }
});

// ---------- Download implementations ----------
async function startVideoDownload(chatId, userId, url, quality, statusMsgId) {
  try {
    const { outputPath, title, videoId } = await downloadVideo(url, quality);
    const thumb = await downloadThumbnail(videoId);
    await bot.editMessageText('📤 *Uploading video…*', {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    }).catch(() => {});
    const videoStream = createReadStream(outputPath);
    const options = { caption: `🎬 ${title}\n[${quality}]`, supports_streaming: true };
    if (thumb) options.thumbnail = thumb;
    await bot.sendVideo(chatId, videoStream, options);
    await bot.deleteMessage(chatId, statusMsgId).catch(() => {});
    const minutes = getSettings(userId).cleanupMinutes;
    scheduleCleanup(outputPath, minutes);
    if (thumb) scheduleCleanup(thumb, minutes);
  } catch (err) {
    console.error(`Download error: ${err.message}`);
    await bot.editMessageText(`❌ Download failed: \`${err.message}\``, {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    }).catch(() => {});
  }
}

async function startAudioDownload(chatId, userId, url, statusMsgId) {
  try {
    const { mp3Path, title } = await downloadAudio(url);
    await bot.editMessageText('📤 *Uploading MP3…*', {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    }).catch(() => {});
    const audioStream = createReadStream(mp3Path);
    await bot.sendDocument(chatId, audioStream, {
      caption: `🎵 ${title}`,
      filename: `${title}.mp3`,
    });
    await bot.deleteMessage(chatId, statusMsgId).catch(() => {});
    scheduleCleanup(mp3Path, getSettings(userId).cleanupMinutes);
  } catch (err) {
    await bot.editMessageText(`❌ Audio failed: \`${err.message}\``, {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    }).catch(() => {});
  }
}

async function startThumbnailDownload(chatId, userId, url, statusMsgId) {
  try {
    const infoJson = await runYtdlp(`-J "${url}"`, YTDLP_TIMEOUT, false);
    const info = JSON.parse(infoJson);
    const thumb = await downloadThumbnail(info.id);
    if (!thumb) throw new Error('No thumbnail');
    const thumbStream = createReadStream(thumb);
    await bot.sendPhoto(chatId, thumbStream, { caption: `🖼 ${info.title}` });
    await bot.deleteMessage(chatId, statusMsgId).catch(() => {});
    scheduleCleanup(thumb, getSettings(userId).cleanupMinutes);
  } catch (err) {
    await bot.editMessageText(`❌ Thumbnail failed: \`${err.message}\``, {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    }).catch(() => {});
  }
}

// ---------- Message handler for URLs ----------
bot.on('message', async (msg) => {
  const chatId = msg.chat.id;
  const text = msg.text;
  if (!text || text.startsWith('/')) return;

  const urlMatch = text.match(/(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/);
  if (!urlMatch) {
    await bot.sendMessage(chatId, 'Please send a valid YouTube URL.');
    return;
  }
  const url = urlMatch[0];
  const processingMsg = await bot.sendMessage(chatId, '⏳ *Fetching video info...*', { parse_mode: 'Markdown' });

  try {
    const infoJson = await runYtdlp(`-J "${url}"`, YTDLP_TIMEOUT, false);
    const info = JSON.parse(infoJson);
    const dur = info.duration ? `${Math.floor(info.duration / 60)}m ${info.duration % 60}s` : '?';
    const sent = await bot.sendMessage(chatId,
      `📹 *${info.title}*\n⏱ \`${dur}\`\n\nWhat would you like?`,
      { parse_mode: 'Markdown', reply_markup: downloadTypeKeyboard() }
    );
    if (!pendingDownloads.has(chatId)) pendingDownloads.set(chatId, {});
    pendingDownloads.get(chatId)[sent.message_id] = { url, timestamp: Date.now() };
    await bot.deleteMessage(chatId, processingMsg.message_id).catch(() => {});
  } catch (err) {
    await bot.editMessageText(`❌ Failed to fetch video info: \`${err.message}\``, {
      chat_id: chatId,
      message_id: processingMsg.message_id,
      parse_mode: 'Markdown',
    }).catch(() => {});
  }
});

// ---------- Start cleanup worker and bot ----------
cleanupWorker();
console.log('✅ Bot started with mandatory cookies, FFmpeg merge, and 2GB upload support');