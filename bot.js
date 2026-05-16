const TelegramBot = require('node-telegram-bot-api');
const fs = require('fs-extra');
const path = require('path');
const axios = require('axios');
const { exec } = require('child_process');
const util = require('util');
const execPromise = util.promisify(exec);

// ---------- Config ----------
const BOT_TOKEN = process.env.BOT_TOKEN;
if (!BOT_TOKEN) throw new Error('Missing BOT_TOKEN env var');

const DOWNLOAD_DIR = path.join(__dirname, 'downloads');
const COOKIE_PATH = '/app/cookies.txt';
fs.ensureDirSync(DOWNLOAD_DIR);

// User settings (in-memory)
const userSettings = new Map();
const defaultSettings = { quality: '720p', mode: 'manual', cleanupMinutes: 10 };
const cleanupRegistry = new Map();

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
  const exists = await cookieExists();
  if (exists) {
    console.log('✅ cookies.txt found – using it for yt-dlp');
    return `--cookies "${COOKIE_PATH}"`;
  }
  console.warn('⚠️ cookies.txt not found – some videos may be limited to 360p');
  return '';
}

// ---------- yt-dlp wrapper with EJS fix ----------
async function runYtdlp(args) {
  const cookieArg = await getCookieArg();
  // Add remote components to fetch EJS challenge solver scripts from npm
  const remoteArg = '--remote-components ejs:npm';
  const cmd = `yt-dlp ${cookieArg} ${remoteArg} ${args}`;
  console.log(`Running: ${cmd}`);
  const { stdout, stderr } = await execPromise(cmd);
  if (stderr && !stderr.includes('WARNING') && !stderr.includes('[youtube]')) {
    throw new Error(stderr);
  }
  return stdout;
}

async function getAvailableQualities(url) {
  try {
    const stdout = await runYtdlp(`-J --flat-playlist "${url}"`);
    const data = JSON.parse(stdout);
    const heights = new Set();
    for (const f of data.formats || []) {
      if (f.height && f.vcodec !== 'none') heights.add(f.height);
    }
    const sorted = Array.from(heights).sort((a,b) => a-b);
    return sorted.length ? sorted : [360, 480, 720, 1080];
  } catch (err) {
    console.error(err);
    return [360, 480, 720, 1080];
  }
}

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
      formatSpec = `-f "bestvideo[height<=${target}][ext=mp4]+bestaudio[ext=m4a]/best[height<=${target}]"`;
    }
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
  if (!userSettings.has(userId)) {
    userSettings.set(userId, { ...defaultSettings });
  }
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

// ---------- Telegram bot ----------
const bot = new TelegramBot(BOT_TOKEN, { polling: true });

// Store pending downloads (chatId -> { messageId: url })
const pendingDownloads = new Map();

// Helper to create inline keyboards
function settingsKeyboard(userId) {
  const s = getSettings(userId);
  const modeLabel = s.mode === 'fixed' ? 'Fixed ✅' : 'Manual 🎛';
  const timerLabel = s.cleanupMinutes === 0 ? '♾ Never' : `${s.cleanupMinutes} min`;
  return {
    inline_keyboard: [
      [{ text: `🎬 Quality: ${s.quality.toUpperCase()}`, callback_data: 'set_quality' }],
      [{ text: `🔁 Mode: ${modeLabel}`, callback_data: 'set_mode' }],
      [{ text: `🧹 Cleanup: ${timerLabel}`, callback_data: 'set_cleanup' }],
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
  const chatId = msg.chat.id;
  bot.sendMessage(chatId,
    `👋 *Welcome to YT Downloader Bot (Node.js + yt-dlp + EJS fix)!*\n\n` +
    `Send me a YouTube URL.\n` +
    `⚙️ /settings – Preferences`,
    { parse_mode: 'Markdown' }
  );
});

bot.onText(/\/settings/, async (msg) => {
  const chatId = msg.chat.id;
  const userId = msg.from.id;
  const sent = await bot.sendMessage(chatId, '⚙️ *Your Settings*', {
    parse_mode: 'Markdown',
    reply_markup: settingsKeyboard(userId),
  });
});

// ---------- Callback queries ----------
bot.on('callback_query', async (callbackQuery) => {
  const chatId = callbackQuery.message.chat.id;
  const messageId = callbackQuery.message.message_id;
  const userId = callbackQuery.from.id;
  const data = callbackQuery.data;

  await bot.answerCallbackQuery(callbackQuery.id);

  // --- Settings callbacks ---
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

  // --- Download callbacks ---
  if (data === 'dl_video') {
    const url = pendingDownloads.get(chatId)?.[messageId];
    if (!url) {
      await bot.sendMessage(chatId, 'No URL found. Please send again.');
      return;
    }
    const s = getSettings(userId);
    if (s.mode === 'fixed') {
      const statusMsg = await bot.sendMessage(chatId, `⬇️ *Downloading (${s.quality})…*`, { parse_mode: 'Markdown' });
      await startVideoDownload(chatId, userId, url, s.quality, statusMsg.message_id);
    } else {
      const heights = await getAvailableQualities(url);
      const keyboard = qualityKeyboard(heights);
      await bot.editMessageText('🎬 *Select video quality:*', {
        chat_id: chatId,
        message_id: messageId,
        parse_mode: 'Markdown',
        reply_markup: keyboard,
      });
    }
    return;
  }
  if (data === 'dl_audio') {
    const url = pendingDownloads.get(chatId)?.[messageId];
    if (!url) {
      await bot.sendMessage(chatId, 'No URL found. Please send again.');
      return;
    }
    const statusMsg = await bot.sendMessage(chatId, '⬇️ *Extracting audio…*', { parse_mode: 'Markdown' });
    await startAudioDownload(chatId, userId, url, statusMsg.message_id);
    return;
  }
  if (data === 'dl_thumb') {
    const url = pendingDownloads.get(chatId)?.[messageId];
    if (!url) {
      await bot.sendMessage(chatId, 'No URL found. Please send again.');
      return;
    }
    const statusMsg = await bot.sendMessage(chatId, '🖼 *Downloading thumbnail…*', { parse_mode: 'Markdown' });
    await startThumbnailDownload(chatId, userId, url, statusMsg.message_id);
    return;
  }
  if (data === 'cancel_download') {
    await bot.deleteMessage(chatId, messageId);
    return;
  }
  if (data.startsWith('quality_')) {
    const quality = data.replace('quality_', '');
    const url = pendingDownloads.get(chatId)?.[messageId];
    if (!url) {
      await bot.sendMessage(chatId, 'Session expired. Please send URL again.');
      return;
    }
    const statusMsg = await bot.sendMessage(chatId, `⬇️ *Downloading (${quality})…*`, { parse_mode: 'Markdown' });
    await startVideoDownload(chatId, userId, url, quality, statusMsg.message_id);
    await bot.deleteMessage(chatId, messageId); // remove quality menu
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
    await bot.sendVideo(chatId, outputPath, {
      caption: `🎬 ${title}\n[${quality}]`,
      thumbnail: thumb,
      supports_streaming: true,
    });
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
    await bot.sendDocument(chatId, mp3Path, {
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
    await bot.sendPhoto(chatId, thumb, { caption: `🖼 ${info.title}` });
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
  if (!text) return;
  if (text.startsWith('/')) return;

  const urlMatch = text.match(/(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/);
  if (!urlMatch) {
    await bot.sendMessage(chatId, 'Please send a valid YouTube URL.');
    return;
  }
  const url = urlMatch[0];
  try {
    const infoJson = await runYtdlp(`-J "${url}"`);
    const info = JSON.parse(infoJson);
    const dur = info.duration ? `${Math.floor(info.duration/60)}m ${info.duration%60}s` : '?';
    const sent = await bot.sendMessage(chatId,
      `📹 *${info.title}*\n⏱ \`${dur}\`\n\nWhat would you like?`,
      {
        parse_mode: 'Markdown',
        reply_markup: downloadTypeKeyboard(),
      }
    );
    // store URL for callback
    if (!pendingDownloads.has(chatId)) pendingDownloads.set(chatId, {});
    pendingDownloads.get(chatId)[sent.message_id] = url;
  } catch (err) {
    await bot.sendMessage(chatId, `❌ Failed to fetch video info: \`${err.message}\``, { parse_mode: 'Markdown' });
  }
});

// ---------- Start cleanup worker ----------
cleanupWorker();
console.log('Bot started (Node.js + yt-dlp + EJS fix applied)');