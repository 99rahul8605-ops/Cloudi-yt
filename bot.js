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

const DOWNLOAD_DIR = path.join(__dirname, 'downloads');
const COOKIE_PATH = '/app/cookies.txt';
fs.ensureDirSync(DOWNLOAD_DIR);

const YTDLP_TIMEOUT = 60000;

// User settings
const userSettings = new Map();
const defaultSettings = { quality: '720p', mode: 'manual', cleanupMinutes: 10 };
const cleanupRegistry = new Map();
const BOT_START_TIME = Date.now();

// ---------- Pending downloads (with TTL) ----------
const pendingDownloads = new Map();
const PENDING_TTL = 10 * 60 * 1000;
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

// ---------- yt-dlp version cache ----------
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

// ---------- Cookie helper ----------
async function cookieExists() {
  try {
    await fs.access(COOKIE_PATH);
    return true;
  } catch {
    return false;
  }
}
async function getCookieArg() {
  return (await cookieExists()) ? `--cookies "${COOKIE_PATH}"` : '';
}

// ---------- yt-dlp wrapper with all optimizations ----------
async function runYtdlp(args, timeout = YTDLP_TIMEOUT) {
  const cookieArg = await getCookieArg();
  const opts = [
    '--concurrent-fragments 50',
    '--geo-bypass',
    '--js-runtimes deno',
    '--remote-components ejs:github',
  ].join(' ');
  const fullArgs = `${cookieArg} ${opts} ${args}`;
  console.log(`[yt-dlp] ${fullArgs.substring(0, 150)}...`);
  try {
    const { stdout, stderr } = await execPromise(`yt-dlp ${fullArgs}`, { timeout });
    if (stderr && !stderr.includes('WARNING') && !stderr.includes('[youtube]')) {
      throw new Error(stderr);
    }
    return stdout;
  } catch (err) {
    if (err.killed && err.signal === 'SIGTERM') {
      throw new Error('yt-dlp timed out after 60 seconds');
    }
    throw err;
  }
}

// ---------- Core functions ----------
async function getAvailableQualities(url) {
  try {
    const stdout = await runYtdlp(`-J --flat-playlist "${url}"`);
    const data = JSON.parse(stdout);
    const heights = new Set();
    for (const f of data.formats || []) {
      // Include any format that has video (vcodec not 'none')
      if (f.height && f.vcodec !== 'none') heights.add(f.height);
    }
    const sorted = Array.from(heights).sort((a, b) => a - b);
    return sorted.length ? sorted : [360, 480, 720, 1080];
  } catch (err) {
    console.error('getAvailableQualities error:', err.message);
    return [360, 480, 720, 1080];
  }
}

// CRITICAL FIX: Use bestvideo+bestaudio and let yt-dlp merge them
async function downloadVideo(url, quality) {
  const infoJson = await runYtdlp(`-J "${url}"`);
  const info = JSON.parse(infoJson);
  const title = info.title.replace(/[^\w\s]/gi, '');
  const videoId = info.id;
  const outputPath = path.join(DOWNLOAD_DIR, `${videoId}.mp4`);

  let formatSpec = '';
  if (quality !== 'best') {
    const target = parseInt(quality);
    if (!isNaN(target)) {
      // Request video up to target height + best audio, then merge to mp4
      formatSpec = `-f "bestvideo[height<=${target}]+bestaudio/best[height<=${target}]"`;
    }
  } else {
    formatSpec = '-f "bestvideo+bestaudio/best"';
  }
  await runYtdlp(`${formatSpec} -o "${outputPath}" --merge-output-format mp4 "${url}"`);
  return { outputPath, title, videoId, info };
}

async function downloadAudio(url) {
  const infoJson = await runYtdlp(`-J "${url}"`);
  const info = JSON.parse(infoJson);
  const title = info.title.replace(/[^\w\s]/gi, '');
  const videoId = info.id;
  const mp3Path = path.join(DOWNLOAD_DIR, `${videoId}.mp3`);
  await runYtdlp(`-f bestaudio --extract-audio --audio-format mp3 --audio-quality 192K -o "${mp3Path}" "${url}"`);
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
const bot = new TelegramBot(BOT_TOKEN, { polling: true });

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
    `👋 *Welcome to YT Downloader Bot (Optimized)*\n\n` +
    `Send me a YouTube URL.\n` +
    `⚙️ /settings – Preferences\n` +
    `📊 /stats – Bot statistics`,
    { parse_mode: 'Markdown' }
  );
});

bot.onText(/\/stats/, async (msg) => {
  const chatId = msg.chat.id;
  const userId = msg.from.id;
  try {
    const [ytVersion, cookiePresent, dirStats, uptime, activeUsers, nodeVersion, pendingCleanup, userSettingsObj] = await Promise.all([
      getYtdlpVersion(),
      cookieExists(),
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
      `🍪 *Cookies*: ${cookiePresent ? '✅ Present' : '❌ Missing'}\n` +
      `🚀 *Optimizations*: concurrent fragments=50, geo-bypass, deno runtime\n` +
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

// ---------- Callback queries (fixed pending entry handling) ----------
bot.on('callback_query', async (callbackQuery) => {
  const chatId = callbackQuery.message.chat.id;
  const messageId = callbackQuery.message.message_id;
  const userId = callbackQuery.from.id;
  const data = callbackQuery.data;

  await bot.answerCallbackQuery(callbackQuery.id);

  function removePendingEntry() {
    const chatPending = pendingDownloads.get(chatId);
    if (chatPending && chatPending[messageId]) {
      delete chatPending[messageId];
      if (Object.keys(chatPending).length === 0) pendingDownloads.delete(chatId);
    }
  }

  // ---------- Settings callbacks (unchanged) ----------
  if (data === 'close_settings') {
    await bot.deleteMessage(chatId, messageId);
    return;
  }
  if (data === 'set_quality') {
    const qualities = ['360p', '480p', '720p', '1080p', '1440p', '2160p', 'best'];
    const buttons = qualities.map(q => [{ text: q, callback_data: `set_quality_${q}` }]);
    buttons.push([{ text: '⬅️ Back', callback_data: 'back_settings' }]);
    await bot.editMessageText('🎬 *Select default quality:*', {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
      reply_markup: { inline_keyboard: buttons },
    });
    return;
  }
  if (data.startsWith('set_quality_')) {
    const quality = data.replace('set_quality_', '');
    const s = getSettings(userId);
    s.quality = quality;
    userSettings.set(userId, s);
    await bot.editMessageText(`✅ Default quality set to ${quality}.`, {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
    });
    setTimeout(async () => {
      await bot.editMessageText('⚙️ *Your Settings*', {
        chat_id: chatId,
        message_id: messageId,
        parse_mode: 'Markdown',
        reply_markup: settingsKeyboard(userId),
      });
    }, 1000);
    return;
  }
  if (data === 'set_mode') {
    const buttons = [
      [{ text: 'Fixed ✅', callback_data: 'set_mode_fixed' }],
      [{ text: 'Manual 🎛', callback_data: 'set_mode_manual' }],
      [{ text: '⬅️ Back', callback_data: 'back_settings' }],
    ];
    await bot.editMessageText('🔁 *Download Mode:*', {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
      reply_markup: { inline_keyboard: buttons },
    });
    return;
  }
  if (data === 'set_mode_fixed') {
    const s = getSettings(userId);
    s.mode = 'fixed';
    userSettings.set(userId, s);
    await bot.editMessageText('✅ Mode set to fixed.', {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
    });
    setTimeout(async () => {
      await bot.editMessageText('⚙️ *Your Settings*', {
        chat_id: chatId,
        message_id: messageId,
        parse_mode: 'Markdown',
        reply_markup: settingsKeyboard(userId),
      });
    }, 1000);
    return;
  }
  if (data === 'set_mode_manual') {
    const s = getSettings(userId);
    s.mode = 'manual';
    userSettings.set(userId, s);
    await bot.editMessageText('✅ Mode set to manual.', {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
    });
    setTimeout(async () => {
      await bot.editMessageText('⚙️ *Your Settings*', {
        chat_id: chatId,
        message_id: messageId,
        parse_mode: 'Markdown',
        reply_markup: settingsKeyboard(userId),
      });
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
    await bot.editMessageText('🧹 *Auto-Cleanup Timer:*', {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
      reply_markup: { inline_keyboard: buttons },
    });
    return;
  }
  if (data.startsWith('set_cleanup_')) {
    const minutes = parseInt(data.replace('set_cleanup_', ''));
    const s = getSettings(userId);
    s.cleanupMinutes = minutes;
    userSettings.set(userId, s);
    await bot.editMessageText(`✅ Cleanup set to ${minutes === 0 ? 'Never' : minutes + ' min'}.`, {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
    });
    setTimeout(async () => {
      await bot.editMessageText('⚙️ *Your Settings*', {
        chat_id: chatId,
        message_id: messageId,
        parse_mode: 'Markdown',
        reply_markup: settingsKeyboard(userId),
      });
    }, 1000);
    return;
  }
  if (data === 'back_settings') {
    await bot.editMessageText('⚙️ *Your Settings*', {
      chat_id: chatId,
      message_id: messageId,
      parse_mode: 'Markdown',
      reply_markup: settingsKeyboard(userId),
    });
    return;
  }

  // ---------- Download callbacks ----------
  if (data === 'dl_video') {
    const url = pendingDownloads.get(chatId)?.[messageId]?.url;
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
      await bot.editMessageText('🎬 *Select video quality:*', {
        chat_id: chatId,
        message_id: messageId,
        parse_mode: 'Markdown',
        reply_markup: qualityKeyboard(heights),
      });
    }
    return;
  }
  if (data === 'dl_audio') {
    const url = pendingDownloads.get(chatId)?.[messageId]?.url;
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
    const url = pendingDownloads.get(chatId)?.[messageId]?.url;
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
    await bot.deleteMessage(chatId, messageId);
    return;
  }
  if (data.startsWith('quality_')) {
    const quality = data.replace('quality_', '');
    const url = pendingDownloads.get(chatId)?.[messageId]?.url;
    if (!url) {
      await bot.sendMessage(chatId, 'Session expired. Please send URL again.');
      return;
    }
    removePendingEntry();
    const statusMsg = await bot.sendMessage(chatId, `⬇️ *Downloading (${quality})…*`, { parse_mode: 'Markdown' });
    await startVideoDownload(chatId, userId, url, quality, statusMsg.message_id);
    await bot.deleteMessage(chatId, messageId);
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
    });
    const videoStream = createReadStream(outputPath);
    const options = { caption: `🎬 ${title}\n[${quality}]`, supports_streaming: true };
    if (thumb) options.thumbnail = thumb;
    await bot.sendVideo(chatId, videoStream, options);
    await bot.deleteMessage(chatId, statusMsgId);
    const minutes = getSettings(userId).cleanupMinutes;
    scheduleCleanup(outputPath, minutes);
    if (thumb) scheduleCleanup(thumb, minutes);
  } catch (err) {
    await bot.editMessageText(`❌ Download failed: \`${err.message}\``, {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    });
  }
}

async function startAudioDownload(chatId, userId, url, statusMsgId) {
  try {
    const { mp3Path, title } = await downloadAudio(url);
    await bot.editMessageText('📤 *Uploading MP3…*', {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    });
    const audioStream = createReadStream(mp3Path);
    await bot.sendDocument(chatId, audioStream, {
      caption: `🎵 ${title}`,
      filename: `${title}.mp3`,
    });
    await bot.deleteMessage(chatId, statusMsgId);
    scheduleCleanup(mp3Path, getSettings(userId).cleanupMinutes);
  } catch (err) {
    await bot.editMessageText(`❌ Audio failed: \`${err.message}\``, {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    });
  }
}

async function startThumbnailDownload(chatId, userId, url, statusMsgId) {
  try {
    const infoJson = await runYtdlp(`-J "${url}"`);
    const info = JSON.parse(infoJson);
    const thumb = await downloadThumbnail(info.id);
    if (!thumb) throw new Error('No thumbnail');
    const thumbStream = createReadStream(thumb);
    await bot.sendPhoto(chatId, thumbStream, { caption: `🖼 ${info.title}` });
    await bot.deleteMessage(chatId, statusMsgId);
    scheduleCleanup(thumb, getSettings(userId).cleanupMinutes);
  } catch (err) {
    await bot.editMessageText(`❌ Thumbnail failed: \`${err.message}\``, {
      chat_id: chatId,
      message_id: statusMsgId,
      parse_mode: 'Markdown',
    });
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
    const infoJson = await runYtdlp(`-J "${url}"`);
    const info = JSON.parse(infoJson);
    const dur = info.duration ? `${Math.floor(info.duration / 60)}m ${info.duration % 60}s` : '?';
    const sent = await bot.sendMessage(chatId,
      `📹 *${info.title}*\n⏱ \`${dur}\`\n\nWhat would you like?`,
      { parse_mode: 'Markdown', reply_markup: downloadTypeKeyboard() }
    );
    if (!pendingDownloads.has(chatId)) pendingDownloads.set(chatId, {});
    pendingDownloads.get(chatId)[sent.message_id] = { url, timestamp: Date.now() };
    await bot.deleteMessage(chatId, processingMsg.message_id);
  } catch (err) {
    await bot.editMessageText(`❌ Failed to fetch video info: \`${err.message}\``, {
      chat_id: chatId,
      message_id: processingMsg.message_id,
      parse_mode: 'Markdown',
    });
  }
});

// ---------- Start cleanup worker and bot ----------
cleanupWorker();
console.log('✅ Bot started with fixed format selection (bestvideo+bestaudio)');