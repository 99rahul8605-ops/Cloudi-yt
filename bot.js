const { Telegraf, Markup } = require('telegraf');
const ytdl = require('@distube/ytdl-core');
const fs = require('fs-extra');
const path = require('path');
const axios = require('axios');

// ---------- Config ----------
const BOT_TOKEN = process.env.BOT_TOKEN;
if (!BOT_TOKEN) throw new Error('BOT_TOKEN env var required');

const DOWNLOAD_DIR = path.join(__dirname, 'downloads');
fs.ensureDirSync(DOWNLOAD_DIR);

// User settings store (in-memory)
const userSettings = new Map();
const defaultSettings = { quality: '720p', mode: 'manual', cleanupMinutes: 10 };

// Cleanup registry
const cleanupRegistry = new Map();

// ---------- Helper functions ----------
function getSettings(userId) {
    if (!userSettings.has(userId)) {
        userSettings.set(userId, { ...defaultSettings });
    }
    return userSettings.get(userId);
}

async function cleanupWorker() {
    setInterval(async () => {
        const now = Date.now();
        for (const [filePath, expireTime] of cleanupRegistry.entries()) {
            if (expireTime !== 0 && expireTime < now) {
                try {
                    await fs.remove(filePath);
                    cleanupRegistry.delete(filePath);
                    console.log(`Cleaned: ${filePath}`);
                } catch (err) {
                    console.error(`Cleanup error ${filePath}:`, err);
                }
            }
        }
    }, 60000);
}

function scheduleCleanup(filePath, minutes) {
    const expire = minutes === 0 ? 0 : Date.now() + minutes * 60 * 1000;
    cleanupRegistry.set(filePath, expire);
}

// Get all available video heights (quality options)
async function getAvailableQualities(url) {
    try {
        const info = await ytdl.getInfo(url);
        const heights = new Set();
        // Check both formats and adaptiveFormats
        for (const f of info.formats) {
            if (f.hasVideo && f.height && f.height > 0) heights.add(f.height);
        }
        if (info.adaptiveFormats) {
            for (const f of info.adaptiveFormats) {
                if (f.hasVideo && f.height && f.height > 0) heights.add(f.height);
            }
        }
        const sorted = Array.from(heights).sort((a, b) => a - b);
        console.log(`Qualities found: ${sorted.join(', ')}`);
        return sorted;
    } catch (err) {
        console.error('getAvailableQualities error:', err);
        return [360, 480, 720, 1080]; // fallback
    }
}

// Download video (video+audio combined)
async function downloadVideo(url, targetQuality, progressCallback) {
    const info = await ytdl.getInfo(url);
    const title = info.videoDetails.title.replace(/[^\w\s]/gi, '');
    const videoId = info.videoDetails.videoId;
    const outputPath = path.join(DOWNLOAD_DIR, `${videoId}.mp4`);

    // Choose format
    let qualityFilter = null;
    if (targetQuality !== 'best') {
        const targetHeight = parseInt(targetQuality);
        if (!isNaN(targetHeight)) {
            // Prefer exact match, then <= target
            qualityFilter = (format) => {
                return format.hasVideo && format.hasAudio && format.height && format.height <= targetHeight;
            };
        }
    }

    const stream = ytdl(url, {
        quality: qualityFilter || 'highest',
        filter: 'audioandvideo'
    });

    const writeStream = fs.createWriteStream(outputPath);
    let lastPercent = 0;
    stream.on('progress', (chunkLength, downloaded, total) => {
        const percent = (downloaded / total) * 100;
        if (percent - lastPercent >= 5) {
            lastPercent = percent;
            progressCallback(percent.toFixed(1));
        }
    });

    await new Promise((resolve, reject) => {
        stream.pipe(writeStream);
        writeStream.on('finish', resolve);
        writeStream.on('error', reject);
        stream.on('error', reject);
    });

    return { outputPath, title, videoId, info };
}

// Download audio as MP3
async function downloadAudio(url, progressCallback) {
    const info = await ytdl.getInfo(url);
    const title = info.videoDetails.title.replace(/[^\w\s]/gi, '');
    const videoId = info.videoDetails.videoId;
    const mp3Path = path.join(DOWNLOAD_DIR, `${videoId}.mp3`);

    const stream = ytdl(url, { quality: 'highestaudio', filter: 'audioonly' });
    const writeStream = fs.createWriteStream(mp3Path);
    let lastPercent = 0;
    stream.on('progress', (chunkLength, downloaded, total) => {
        const percent = (downloaded / total) * 100;
        if (percent - lastPercent >= 5) {
            lastPercent = percent;
            progressCallback(percent.toFixed(1));
        }
    });

    await new Promise((resolve, reject) => {
        stream.pipe(writeStream);
        writeStream.on('finish', resolve);
        writeStream.on('error', reject);
        stream.on('error', reject);
    });

    return { mp3Path, title, videoId, info };
}

// Download thumbnail
async function downloadThumbnail(videoId) {
    const urls = [
        `https://img.youtube.com/vi/${videoId}/maxresdefault.jpg`,
        `https://img.youtube.com/vi/${videoId}/hqdefault.jpg`
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
        } catch (err) {
            continue;
        }
    }
    return null;
}

// ---------- Bot Setup ----------
const bot = new Telegraf(BOT_TOKEN);

// Commands
bot.start((ctx) => {
    ctx.replyWithMarkdown(
        `👋 *Welcome to YT Downloader Bot (Node.js)!*\n\n` +
        `Send me a YouTube URL or search query.\n` +
        `⚙️ /settings – Preferences\n` +
        `❓ /help – This message`
    );
});

bot.help((ctx) => ctx.reply('Send a YouTube link. Use /settings to change default quality.'));

bot.command('settings', async (ctx) => {
    const userId = ctx.from.id;
    const s = getSettings(userId);
    const modeLabel = s.mode === 'fixed' ? 'Fixed ✅' : 'Manual 🎛';
    const timerLabel = s.cleanupMinutes === 0 ? '♾ Never' : `${s.cleanupMinutes} min`;
    const keyboard = Markup.inlineKeyboard([
        [Markup.button.callback(`🎬 Quality: ${s.quality.toUpperCase()}`, 'set_quality')],
        [Markup.button.callback(`🔁 Mode: ${modeLabel}`, 'set_mode')],
        [Markup.button.callback(`🧹 Cleanup: ${timerLabel}`, 'set_cleanup')],
        [Markup.button.callback('❌ Close', 'close_settings')],
    ]);
    await ctx.replyWithMarkdown('⚙️ *Your Settings*', keyboard);
});

bot.command('cookiecheck', (ctx) => {
    ctx.replyWithMarkdown(
        `🍪 *Cookie Check*\n` +
        `ytdl-core does not require cookies for most videos.\n` +
        `✅ No cookies needed.`
    );
});

bot.command('stats', (ctx) => {
    const uptime = process.uptime();
    const hours = Math.floor(uptime / 3600);
    const minutes = Math.floor((uptime % 3600) / 60);
    const seconds = Math.floor(uptime % 60);
    ctx.replyWithMarkdown(
        `📊 *Bot Stats*\n` +
        `Uptime: ${hours}h ${minutes}m ${seconds}s\n` +
        `Active users: ${userSettings.size}\n` +
        `Node.js: ${process.version}`
    );
});

// Callback handlers
bot.action('set_quality', async (ctx) => {
    const qualities = ['360p', '480p', '720p', '1080p', '1440p', '2160p', 'best'];
    const buttons = qualities.map(q => [Markup.button.callback(q, `set_quality_${q}`)]);
    buttons.push([Markup.button.callback('⬅️ Back', 'back_settings')]);
    await ctx.editMessageText('🎬 *Select default quality:*', {
        parse_mode: 'Markdown',
        ...Markup.inlineKeyboard(buttons)
    });
    await ctx.answerCbQuery();
});

bot.action(/set_quality_(.+)/, async (ctx) => {
    const userId = ctx.from.id;
    const quality = ctx.match[1];
    const s = getSettings(userId);
    s.quality = quality;
    userSettings.set(userId, s);
    await ctx.editMessageText(`✅ Default quality set to ${quality}.`, { parse_mode: 'Markdown' });
    await ctx.answerCbQuery();
    setTimeout(async () => {
        await ctx.replyWithMarkdown('⚙️ *Your Settings*', settingsKeyboard(userId));
    }, 1000);
});

bot.action('set_mode', async (ctx) => {
    const buttons = [
        [Markup.button.callback('Fixed ✅', 'set_mode_fixed')],
        [Markup.button.callback('Manual 🎛', 'set_mode_manual')],
        [Markup.button.callback('⬅️ Back', 'back_settings')],
    ];
    await ctx.editMessageText('🔁 *Download Mode:*\n• Fixed – always use default quality\n• Manual – choose per download', {
        parse_mode: 'Markdown',
        ...Markup.inlineKeyboard(buttons)
    });
    await ctx.answerCbQuery();
});

bot.action(/set_mode_(fixed|manual)/, async (ctx) => {
    const userId = ctx.from.id;
    const mode = ctx.match[1];
    const s = getSettings(userId);
    s.mode = mode;
    userSettings.set(userId, s);
    await ctx.editMessageText(`✅ Mode set to ${mode}.`, { parse_mode: 'Markdown' });
    await ctx.answerCbQuery();
    setTimeout(async () => {
        await ctx.replyWithMarkdown('⚙️ *Your Settings*', settingsKeyboard(userId));
    }, 1000);
});

bot.action('set_cleanup', async (ctx) => {
    const buttons = [
        [Markup.button.callback('5 min', 'set_cleanup_5'), Markup.button.callback('10 min', 'set_cleanup_10')],
        [Markup.button.callback('15 min', 'set_cleanup_15'), Markup.button.callback('30 min', 'set_cleanup_30')],
        [Markup.button.callback('♾ Never', 'set_cleanup_0')],
        [Markup.button.callback('⬅️ Back', 'back_settings')],
    ];
    await ctx.editMessageText('🧹 *Auto-Cleanup Timer:*', {
        parse_mode: 'Markdown',
        ...Markup.inlineKeyboard(buttons)
    });
    await ctx.answerCbQuery();
});

bot.action(/set_cleanup_(\d+)/, async (ctx) => {
    const userId = ctx.from.id;
    const minutes = parseInt(ctx.match[1]);
    const s = getSettings(userId);
    s.cleanupMinutes = minutes;
    userSettings.set(userId, s);
    await ctx.editMessageText(`✅ Cleanup set to ${minutes === 0 ? 'Never' : minutes+' min'}.`, { parse_mode: 'Markdown' });
    await ctx.answerCbQuery();
    setTimeout(async () => {
        await ctx.replyWithMarkdown('⚙️ *Your Settings*', settingsKeyboard(userId));
    }, 1000);
});

bot.action('back_settings', async (ctx) => {
    const userId = ctx.from.id;
    await ctx.editMessageText('⚙️ *Your Settings*', { parse_mode: 'Markdown', ...settingsKeyboard(userId) });
    await ctx.answerCbQuery();
});

bot.action('close_settings', async (ctx) => {
    await ctx.deleteMessage();
    await ctx.answerCbQuery();
});

// Download actions
bot.action('dl:video', async (ctx) => {
    const userId = ctx.from.id;
    const url = ctx.session?.currentUrl;
    if (!url) {
        await ctx.reply('No URL found. Please send again.');
        return;
    }
    const s = getSettings(userId);
    if (s.mode === 'fixed') {
        await handleVideoDownload(ctx, url, s.quality);
    } else {
        const qualities = await getAvailableQualities(url);
        const buttons = qualities.map(h => [Markup.button.callback(`${h}p`, `dl:quality:${h}p`)]);
        buttons.push([Markup.button.callback('⭐ Best', 'dl:quality:best')]);
        buttons.push([Markup.button.callback('❌ Cancel', 'dl:cancel')]);
        await ctx.editMessageText('🎬 *Select video quality:*', {
            parse_mode: 'Markdown',
            ...Markup.inlineKeyboard(buttons)
        });
    }
    await ctx.answerCbQuery();
});

bot.action(/dl:quality:(.+)/, async (ctx) => {
    const url = ctx.session?.currentUrl;
    if (!url) {
        await ctx.reply('No URL. Please send again.');
        return;
    }
    await handleVideoDownload(ctx, url, ctx.match[1]);
    await ctx.answerCbQuery();
});

bot.action('dl:audio', async (ctx) => {
    const url = ctx.session?.currentUrl;
    if (!url) {
        await ctx.reply('No URL. Please send again.');
        return;
    }
    await handleAudioDownload(ctx, url);
    await ctx.answerCbQuery();
});

bot.action('dl:thumb', async (ctx) => {
    const url = ctx.session?.currentUrl;
    if (!url) {
        await ctx.reply('No URL. Please send again.');
        return;
    }
    await handleThumbnail(ctx, url);
    await ctx.answerCbQuery();
});

bot.action('dl:cancel', async (ctx) => {
    await ctx.editMessageText('❌ Cancelled.');
    await ctx.answerCbQuery();
});

// Search callback (simplified – direct URL only)
bot.action(/dl:search:(\d+)/, async (ctx) => {
    const idx = parseInt(ctx.match[1]);
    const results = ctx.session?.searchResults;
    if (!results || idx >= results.length) {
        await ctx.reply('Search expired. Please search again.');
        return;
    }
    const entry = results[idx];
    ctx.session.currentUrl = entry.url;
    ctx.session.currentInfo = entry;
    const title = entry.title || 'Unknown';
    const duration = entry.duration ? `${Math.floor(entry.duration/60)}m ${entry.duration%60}s` : '?';
    await ctx.editMessageText(
        `📹 *${title}*\n⏱ \`${duration}\`\n\nWhat would you like?`,
        {
            parse_mode: 'Markdown',
            ...Markup.inlineKeyboard([
                [Markup.button.callback('🎬 Video', 'dl:video')],
                [Markup.button.callback('🎵 Audio MP3', 'dl:audio')],
                [Markup.button.callback('🖼 Thumbnail', 'dl:thumb')],
                [Markup.button.callback('❌ Cancel', 'dl:cancel')],
            ])
        }
    );
    await ctx.answerCbQuery();
});

// Message handler
bot.on('text', async (ctx) => {
    const text = ctx.message.text.trim();
    const urlRegex = /(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]+)/;
    if (urlRegex.test(text)) {
        const url = text.match(urlRegex)[0];
        ctx.session = ctx.session || {};
        ctx.session.currentUrl = url;
        try {
            const info = await ytdl.getInfo(url);
            const title = info.videoDetails.title;
            const duration = parseInt(info.videoDetails.lengthSeconds);
            const durStr = duration ? `${Math.floor(duration/60)}m ${duration%60}s` : '?';
            await ctx.replyWithMarkdown(
                `📹 *${title}*\n⏱ \`${durStr}\`\n\nWhat would you like?`,
                Markup.inlineKeyboard([
                    [Markup.button.callback('🎬 Video', 'dl:video')],
                    [Markup.button.callback('🎵 Audio MP3', 'dl:audio')],
                    [Markup.button.callback('🖼 Thumbnail', 'dl:thumb')],
                    [Markup.button.callback('❌ Cancel', 'dl:cancel')],
                ])
            );
        } catch (err) {
            ctx.replyWithMarkdown(`❌ Failed to fetch video info: \`${err.message}\``);
        }
    } else {
        // Search: use ytsearch via ytdl-core? Not directly. We'll suggest sending a direct URL.
        ctx.reply('Please send a valid YouTube URL (e.g., https://youtube.com/watch?v=...). Search is not implemented in this version.');
    }
});

// Helper to generate settings keyboard
function settingsKeyboard(userId) {
    const s = getSettings(userId);
    const modeLabel = s.mode === 'fixed' ? 'Fixed ✅' : 'Manual 🎛';
    const timerLabel = s.cleanupMinutes === 0 ? '♾ Never' : `${s.cleanupMinutes} min`;
    return Markup.inlineKeyboard([
        [Markup.button.callback(`🎬 Quality: ${s.quality.toUpperCase()}`, 'set_quality')],
        [Markup.button.callback(`🔁 Mode: ${modeLabel}`, 'set_mode')],
        [Markup.button.callback(`🧹 Cleanup: ${timerLabel}`, 'set_cleanup')],
        [Markup.button.callback('❌ Close', 'close_settings')],
    ]);
}

// Download handlers
async function handleVideoDownload(ctx, url, quality) {
    const statusMsg = await ctx.replyWithMarkdown(`⬇️ *Downloading (${quality})…*`);
    try {
        const progress = (pct) => {
            ctx.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, null,
                `⬇️ *Downloading…* \`${pct}%\``, { parse_mode: 'Markdown' }).catch(()=>{});
        };
        const { outputPath, title, videoId } = await downloadVideo(url, quality, progress);
        const thumbPath = await downloadThumbnail(videoId);
        await ctx.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, null, `📤 *Uploading video…*`, { parse_mode: 'Markdown' });
        await ctx.replyWithVideo(
            { source: outputPath, thumbnail: thumbPath ? { source: thumbPath } : undefined },
            { caption: `🎬 ${title}\n[${quality}]`, supports_streaming: true }
        );
        await ctx.telegram.deleteMessage(ctx.chat.id, statusMsg.message_id);
        const userId = ctx.from.id;
        const minutes = getSettings(userId).cleanupMinutes;
        scheduleCleanup(outputPath, minutes);
        if (thumbPath) scheduleCleanup(thumbPath, minutes);
    } catch (err) {
        console.error(err);
        await ctx.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, null,
            `❌ Download failed: \`${err.message}\``, { parse_mode: 'Markdown' });
    }
}

async function handleAudioDownload(ctx, url) {
    const statusMsg = await ctx.replyWithMarkdown(`⬇️ *Extracting audio…*`);
    try {
        const progress = (pct) => {
            ctx.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, null,
                `⬇️ *Downloading…* \`${pct}%\``, { parse_mode: 'Markdown' }).catch(()=>{});
        };
        const { mp3Path, title } = await downloadAudio(url, progress);
        await ctx.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, null, `📤 *Uploading MP3…*`, { parse_mode: 'Markdown' });
        await ctx.replyWithDocument({ source: mp3Path, filename: `${title}.mp3` }, { caption: `🎵 ${title}` });
        await ctx.telegram.deleteMessage(ctx.chat.id, statusMsg.message_id);
        const userId = ctx.from.id;
        const minutes = getSettings(userId).cleanupMinutes;
        scheduleCleanup(mp3Path, minutes);
    } catch (err) {
        console.error(err);
        await ctx.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, null,
            `❌ Audio failed: \`${err.message}\``, { parse_mode: 'Markdown' });
    }
}

async function handleThumbnail(ctx, url) {
    const statusMsg = await ctx.replyWithMarkdown(`🖼 *Downloading thumbnail…*`);
    try {
        const info = await ytdl.getInfo(url);
        const videoId = info.videoDetails.videoId;
        const thumbPath = await downloadThumbnail(videoId);
        if (!thumbPath) throw new Error('No thumbnail available');
        await ctx.replyWithPhoto({ source: thumbPath }, { caption: `🖼 ${info.videoDetails.title}` });
        await ctx.telegram.deleteMessage(ctx.chat.id, statusMsg.message_id);
        const userId = ctx.from.id;
        const minutes = getSettings(userId).cleanupMinutes;
        scheduleCleanup(thumbPath, minutes);
    } catch (err) {
        console.error(err);
        await ctx.telegram.editMessageText(ctx.chat.id, statusMsg.message_id, null,
            `❌ Thumbnail failed: \`${err.message}\``, { parse_mode: 'Markdown' });
    }
}

// Session middleware
const session = require('telegraf/session');
bot.use(session());

// Start bot
cleanupWorker();
bot.launch().then(() => console.log('Bot started'));
process.once('SIGINT', () => bot.stop('SIGINT'));
process.once('SIGTERM', () => bot.stop('SIGTERM'));