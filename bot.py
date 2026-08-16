import os
import sys
import gc
import re
import time
import math
import asyncio
import subprocess
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

import aiohttp
import imageio_ffmpeg
from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery
)

# ==========================================================
# BOT CREDENTIALS & CONFIGURATION
# ==========================================================
API_ID = 30574823
API_HASH = "2815bb996f64421716844acaf2d51493"
BOT_TOKEN = "8916680408:AAGNA6Y5VK68iibG5H18dr9aZj5r_mA5jEA"

app = Client("onex_video_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
DOWNLOAD_DIR = "downloads"
THUMB_DIR = "thumbnails"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

# State & Flood Prevention Locks
STOP_REQUESTS = {}
USER_PENDING_BATCH = {}
USER_PENDING_URL = {}
USER_QUALITY_PREF = {}
ACTIVE_USER_TASKS = set()
PROCESSED_MESSAGES = set()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://transcoded-video.b-cdn.net/'
}

# ==========================================================
# DUMMY HTTP SERVER (Keeps Render/Koyeb Web Services Alive)
# ==========================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ONeX Extractor Bot is running smoothly!")

    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()

# ==========================================================
# UTILITIES & VIDEO PROBING
# ==========================================================
def format_bytes(size_bytes):
    if size_bytes <= 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def time_formatter(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def make_progress_bar(current, total, bar_length=15):
    if total <= 0:
        return "░" * bar_length
    fraction = min(max(current / total, 0.0), 1.0)
    filled = int(fraction * bar_length)
    return "█" * filled + "░" * (bar_length - filled)

def get_thumbnail_path(user_id: int):
    path = os.path.join(THUMB_DIR, f"{user_id}.jpg")
    return path if os.path.exists(path) else None

def get_video_metadata(video_path: str):
    """Extracts width, height, duration and generates a clean snapshot thumbnail."""
    thumb_path = video_path + "_thumb.jpg"
    width, height, duration = 1280, 720, 0
    try:
        cmd = [
            FFMPEG_EXE, "-y",
            "-ss", "00:00:02",
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale=640:-1",
            thumb_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        
        cmd_probe = [FFMPEG_EXE, "-i", video_path]
        res = subprocess.run(cmd_probe, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if match := re.search(r"(\d{3,4})x(\d{3,4})", res.stderr):
            width = int(match.group(1))
            height = int(match.group(2))
            
        if dur_match := re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr):
            h, m, s = dur_match.groups()
            duration = int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        pass
        
    return thumb_path if os.path.exists(thumb_path) else None, width, height, duration

# ==========================================================
# PARSER & RESOLVER ENGINE
# ==========================================================
def parse_txt_content(content: str):
    videos = []
    lines = content.splitlines()
    url_pattern = re.compile(r'https?://[^\s|<>"\']+')

    for line_idx, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            continue

        parts = [p.strip() for p in line.split('|')]
        has_video_tag = any(p.upper() == 'VIDEO' for p in parts)
        url_match = url_pattern.search(line)
        if not url_match:
            continue

        url = url_match.group(0).rstrip('.,;')
        is_m3u8 = '.m3u8' in url.lower() or 'transcoded-video' in url.lower() or '.mp4' in url.lower()

        if not (has_video_tag or is_m3u8):
            continue
        if not is_m3u8 and any(p.upper() in ['LINK', 'IMAGE', 'DOCS', 'SLIDES', 'NOTES'] for p in parts):
            continue

        prefix = line[:url_match.start()].strip().rstrip('|').strip()
        prefix_parts = [p.strip() for p in prefix.split('|') if p.strip()]

        video_tag_idx = -1
        for i, p in enumerate(prefix_parts):
            if p.upper() == 'VIDEO':
                video_tag_idx = i
                break

        batch_name = prefix_parts[0] if len(prefix_parts) > 0 else "General Batch"
        topic_name = prefix_parts[1] if len(prefix_parts) > 1 else "Live Lecture"

        if video_tag_idx != -1:
            title_parts = prefix_parts[video_tag_idx + 1:]
            title = ' - '.join(title_parts) if title_parts else f'Lecture_{len(videos)+1}'
            if video_tag_idx > 1:
                topic_name = " → ".join(prefix_parts[1:video_tag_idx])
        else:
            title = ' - '.join(prefix_parts[2:]) if len(prefix_parts) > 2 else (prefix_parts[1] if len(prefix_parts) > 1 else prefix_parts[0])

        videos.append({
            'index': len(videos) + 1,
            'title': title,
            'topic': topic_name,
            'batch': batch_name,
            'url': url,
            'line': line_idx
        })

    return videos

async def resolve_quality_url(session: aiohttp.ClientSession, original_url: str, desired_quality: str = "720p") -> str:
    if not original_url.startswith("http") or desired_quality in ["best", "original"]:
        return original_url

    for q in ["1080p", "720p", "480p", "360p", "240p"]:
        if f"/{q}/" in original_url:
            candidate = original_url.replace(f"/{q}/", f"/{desired_quality}/")
            try:
                async with session.head(candidate, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=4)) as r:
                    if r.status == 200:
                        return candidate
            except Exception:
                pass
            break

    master_url = re.sub(r"/(1080p|720p|480p|360p)/playlist\.m3u8", "/master.m3u8", original_url)
    try:
        async with session.get(master_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                text = await r.text()
                lines = text.splitlines()
                for i, line in enumerate(lines):
                    if "RESOLUTION=" in line and desired_quality.replace("p", "") in line:
                        for nxt in lines[i+1:]:
                            nxt_s = nxt.strip()
                            if nxt_s and not nxt_s.startswith("#"):
                                return urllib.parse.urljoin(master_url, nxt_s)
    except Exception:
        pass

    return original_url

async def get_video_info_async(session: aiohttp.ClientSession, stream_url: str):
    try:
        async with session.get(stream_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {"error": f"HTTP {resp.status}"}
            text = await resp.text()
    except Exception as e:
        return {"error": str(e)}

    segments = []
    total_duration = 0.0
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                total_duration += float(line.split(":")[1].split(",")[0])
            except Exception:
                pass
        elif line and not line.startswith("#"):
            segments.append(urllib.parse.urljoin(stream_url, line))

    return {"segments": segments, "total_duration": total_duration}

async def download_single_chunk(session: aiohttp.ClientSession, index: int, url: str, sem: asyncio.Semaphore):
    async with sem:
        for _ in range(3):
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200:
                        return index, await r.read()
            except Exception:
                await asyncio.sleep(0.2)
        return index, None

def remux_ts_to_mp4(ts_path: str, mp4_path: str) -> bool:
    try:
        cmd = [
            FFMPEG_EXE, "-y",
            "-i", ts_path,
            "-c", "copy",
            "-movflags", "+faststart",
            mp4_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if res.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            return True

        cmd_fallback = [
            FFMPEG_EXE, "-y",
            "-i", ts_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-movflags", "+faststart",
            mp4_path
        ]
        res_fb = subprocess.run(cmd_fallback, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        return res_fb.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0
    except Exception:
        return False

# ==========================================================
# ADVANCED PIPELINE (WITH ANTI-BLACK PREVIEW & PROGRESS)
# ==========================================================
async def process_video_download(client: Client, chat_id: int, user_id: int, status_msg: Message, video_item: dict, quality: str = "720p") -> bool:
    if STOP_REQUESTS.get(user_id, False):
        await status_msg.edit_text("🛑 **Process was cancelled by user.**")
        return False

    title = video_item.get("title", "Lecture")
    stream_url = video_item.get("url", "")
    index = video_item.get("index", 1)
    topic = video_item.get("topic", "Lecture")
    batch = video_item.get("batch", "Batch 2026")

    clean_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).strip() or "video"
    output_mp4 = os.path.join(DOWNLOAD_DIR, f"{index}_{clean_title}_{int(time.time())}.mp4")
    temp_ts = output_mp4.replace(".mp4", ".ts")

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        resolved_url = await resolve_quality_url(session, stream_url, quality)
        video_info = await get_video_info_async(session, resolved_url)

        if "error" in video_info or not video_info.get("segments"):
            await status_msg.edit_text(f"❌ **Failed to fetch stream:** {video_info.get('error', 'No segments found')}")
            return False

        segments = video_info["segments"]
        total_segs = len(segments)
        sem = asyncio.Semaphore(35)
        downloaded_chunks = {}
        downloaded_bytes = 0
        completed_segs = 0
        last_update = time.time()
        start_time = time.time()

        tasks = [asyncio.create_task(download_single_chunk(session, idx, url, sem)) for idx, url in enumerate(segments)]

        for future in asyncio.as_completed(tasks):
            if STOP_REQUESTS.get(user_id, False):
                for t in tasks:
                    t.cancel()
                if os.path.exists(temp_ts):
                    os.remove(temp_ts)
                await status_msg.edit_text("🛑 **Download cancelled by user.**")
                return False

            idx, content = await future
            if content:
                downloaded_chunks[idx] = content
                downloaded_bytes += len(content)
            completed_segs += 1

            if time.time() - last_update > 3.5 or completed_segs == total_segs:
                pct = (completed_segs / total_segs) * 100
                bar = make_progress_bar(completed_segs, total_segs)
                elapsed = max(time.time() - start_time, 0.1)
                speed = downloaded_bytes / elapsed
                eta = (total_segs - completed_segs) / (completed_segs / elapsed) if completed_segs > 0 else 0
                est_total_size = (downloaded_bytes / max(completed_segs, 1)) * total_segs

                try:
                    await status_msg.edit_text(
                        f"📥 **DOWNLOADING LECTURE**\n"
                        f"🎬 **Title:** `{title}`\n"
                        f"⚙️ **Quality:** `{quality}`\n\n"
                        f"`[{bar}]` **{pct:.1f}%**\n\n"
                        f"📊 **Segments:** `{completed_segs}` / `{total_segs}`\n"
                        f"💾 **Size:** `{format_bytes(downloaded_bytes)}` / `~{format_bytes(est_total_size)}`\n"
                        f"⚡ **Speed:** `{format_bytes(speed)}/s`\n"
                        f"⏱️ **ETA:** `{time_formatter(eta)}` | ⏳ **Elapsed:** `{time_formatter(elapsed)}`\n\n"
                        f"🛑 *Send /stop to cancel the task.*"
                    )
                except Exception:
                    pass
                last_update = time.time()

        if STOP_REQUESTS.get(user_id, False):
            if os.path.exists(temp_ts):
                os.remove(temp_ts)
            await status_msg.edit_text("🛑 **Process stopped by user.**")
            return False

        await status_msg.edit_text("⚙️ **Merging & Remuxing stream to MP4...**")
        with open(temp_ts, "wb") as f:
            for idx in range(total_segs):
                if chunk := downloaded_chunks.get(idx):
                    f.write(chunk)
        downloaded_chunks.clear()
        gc.collect()

        remux_status = await asyncio.to_thread(remux_ts_to_mp4, temp_ts, output_mp4)

        if not remux_status or not os.path.exists(output_mp4) or os.path.getsize(output_mp4) == 0:
            if os.path.exists(temp_ts):
                if os.path.exists(output_mp4):
                    os.remove(output_mp4)
                os.rename(temp_ts, output_mp4)
        else:
            if os.path.exists(temp_ts):
                os.remove(temp_ts)

        if not os.path.exists(output_mp4) or os.path.getsize(output_mp4) == 0:
            await status_msg.edit_text("❌ **Download & Remuxing failed!**")
            return False

        file_size = os.path.getsize(output_mp4)
        upload_start = time.time()
        last_update = 0

        async def upload_progress(current, total):
            nonlocal last_update
            if time.time() - last_update > 3.5 or current == total:
                pct = (current / total) * 100
                bar = make_progress_bar(current, total)
                elapsed = max(time.time() - upload_start, 0.1)
                speed = current / elapsed
                eta = (total - current) / speed if speed > 0 else 0

                try:
                    await status_msg.edit_text(
                        f"📤 **UPLOADING TO TELEGRAM**\n"
                        f"🎬 **Title:** `{title}`\n\n"
                        f"`[{bar}]` **{pct:.1f}%**\n\n"
                        f"💾 **Size:** `{format_bytes(current)}` / `{format_bytes(total)}`\n"
                        f"⚡ **Speed:** `{format_bytes(speed)}/s`\n"
                        f"⏱️ **ETA:** `{time_formatter(eta)}` | ⏳ **Elapsed:** `{time_formatter(elapsed)}`"
                    )
                except Exception:
                    pass
                last_update = time.time()

        caption_text = (
            f"**Index:** `{index}`\n\n"
            f"**Title:** `{title}.mp4`\n\n"
            f"**Topic:** `{topic}`\n\n"
            f"**Batch:** `{batch}`\n\n"
            f"**Extracted By:** `O ɴ ᴇ 𝐗 🍃`"
        )

        auto_thumb, vid_w, vid_h, vid_dur = await asyncio.to_thread(get_video_metadata, output_mp4)
        custom_thumb = get_thumbnail_path(user_id)
        final_thumb = custom_thumb if custom_thumb else auto_thumb

        await client.send_video(
            chat_id=chat_id,
            video=output_mp4,
            caption=caption_text,
            thumb=final_thumb,
            width=vid_w,
            height=vid_h,
            duration=int(vid_dur) if vid_dur > 0 else None,
            supports_streaming=True,
            progress=upload_progress
        )

        if auto_thumb and os.path.exists(auto_thumb):
            os.remove(auto_thumb)
        if os.path.exists(output_mp4):
            os.remove(output_mp4)
            
        await status_msg.delete()
        return True

# ==========================================================
# INLINE KEYBOARDS
# ==========================================================
def get_quality_keyboard(callback_prefix: str = "qual"):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ 360p", callback_data=f"{callback_prefix}:360p"),
            InlineKeyboardButton("⚡ 480p", callback_data=f"{callback_prefix}:480p")
        ],
        [
            InlineKeyboardButton("⚡ 720p (HD)", callback_data=f"{callback_prefix}:720p"),
            InlineKeyboardButton("⚡ 1080p (FHD)", callback_data=f"{callback_prefix}:1080p")
        ],
        [
            InlineKeyboardButton("🚀 Best / Original", callback_data=f"{callback_prefix}:best")
        ]
    ])

# ==========================================================
# BOT HANDLERS & ANTI-SPAM ROUTERS
# ==========================================================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    if message.id in PROCESSED_MESSAGES:
        return
    PROCESSED_MESSAGES.add(message.id)
    
    STOP_REQUESTS[message.from_user.id] = False
    await message.reply_text(
        "👋 **Welcome to ONeX Extractor Bot!** 🍃\n\n"
        "⚡ **Features:**\n"
        "• Send any `.m3u8` or stream link directly.\n"
        "• Send `.txt` batch playlist file for course download.\n"
        "• Send an **Image** to set custom video thumbnail.\n"
        "• Commands: `/viewthumb`, `/delthumb`, `/stop`"
    )

@app.on_message(filters.command("stop") & filters.private)
async def stop_handler(client: Client, message: Message):
    user_id = message.from_user.id
    STOP_REQUESTS[user_id] = True
    USER_PENDING_BATCH.pop(user_id, None)
    USER_PENDING_URL.pop(user_id, None)
    ACTIVE_USER_TASKS.discard(user_id)
    await message.reply_text("🛑 **Stopping and clearing all active tasks...**")

@app.on_message(filters.photo & filters.private)
async def thumb_save_handler(client: Client, message: Message):
    thumb_path = os.path.join(THUMB_DIR, f"{message.from_user.id}.jpg")
    await message.download(file_name=thumb_path)
    await message.reply_text("✅ **Custom Thumbnail Saved Successfully!**")

@app.on_message(filters.command("viewthumb") & filters.private)
async def view_thumb_handler(client: Client, message: Message):
    thumb_path = get_thumbnail_path(message.from_user.id)
    if thumb_path:
        await message.reply_photo(photo=thumb_path, caption="🖼️ **Current Custom Thumbnail**")
    else:
        await message.reply_text("❌ No custom thumbnail set. Send any photo to set one.")

@app.on_message(filters.command("delthumb") & filters.private)
async def del_thumb_handler(client: Client, message: Message):
    thumb_path = get_thumbnail_path(message.from_user.id)
    if thumb_path:
        os.remove(thumb_path)
        await message.reply_text("🗑️ **Custom thumbnail deleted.**")
    else:
        await message.reply_text("❌ No custom thumbnail found to delete.")

@app.on_message(filters.document & filters.private)
async def doc_handler(client: Client, message: Message):
    if not message.document.file_name or not message.document.file_name.endswith(".txt") or message.id in PROCESSED_MESSAGES:
        return
    PROCESSED_MESSAGES.add(message.id)

    user_id = message.from_user.id
    if user_id in ACTIVE_USER_TASKS:
        await message.reply_text("⚠️ **A task is already running!** Send `/stop` first to cancel it.")
        return

    STOP_REQUESTS[user_id] = False
    status = await message.reply_text("📄 **Parsing .txt playlist file...**")
    file_path = await message.download()

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if os.path.exists(file_path):
        os.remove(file_path)

    videos = parse_txt_content(content)
    if not videos:
        await status.edit_text("❌ No valid video links found in this file.")
        return

    USER_PENDING_BATCH[user_id] = {
        'videos': videos,
        'chat_id': message.chat.id
    }

    first_batch = videos[0].get('batch', 'General Course')
    await status.edit_text(
        f"✅ **Playlist Loaded!**\n"
        f"📚 **Batch:** `{first_batch}`\n"
        f"📊 **Total Videos:** `{len(videos)}`\n\n"
        f"👉 **Select your desired video quality below:**",
        reply_markup=get_quality_keyboard("batch_qual")
    )

@app.on_message(filters.text & filters.regex(r"https?://[^\s]+") & filters.private)
async def url_handler(client: Client, message: Message):
    if message.id in PROCESSED_MESSAGES:
        return
    PROCESSED_MESSAGES.add(message.id)

    user_id = message.from_user.id
    if user_id in ACTIVE_USER_TASKS:
        await message.reply_text("⚠️ **A task is already running!** Send `/stop` first to cancel it.")
        return

    STOP_REQUESTS[user_id] = False
    url = message.text.strip()
    USER_PENDING_URL[user_id] = url

    await message.reply_text(
        f"🔗 **Direct URL Detected!**\n\n"
        f"👉 **Choose Download Quality:**",
        reply_markup=get_quality_keyboard("url_qual")
    )

@app.on_callback_query(filters.regex(r"^url_qual:"))
async def url_quality_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    quality = query.data.split(":")[1]
    url = USER_PENDING_URL.pop(user_id, None)

    if not url:
        await query.answer("❌ Task expired or already processed.", show_alert=True)
        return

    ACTIVE_USER_TASKS.add(user_id)
    await query.message.delete()
    status_msg = await query.message.reply_text("⚡ **Initializing download task...**")
    
    video_item = {
        'index': 1,
        'title': f"Lecture_{int(time.time())}",
        'topic': "Direct Stream",
        'batch': "Single Video Extraction",
        'url': url
    }
    try:
        await process_video_download(client, query.message.chat.id, user_id, status_msg, video_item, quality)
    finally:
        ACTIVE_USER_TASKS.discard(user_id)

@app.on_callback_query(filters.regex(r"^batch_qual:"))
async def batch_quality_callback(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    quality = query.data.split(":")[1]
    batch_data = USER_PENDING_BATCH.get(user_id)

    if not batch_data:
        await query.answer("❌ Session expired. Re-upload the .txt file.", show_alert=True)
        return

    USER_QUALITY_PREF[user_id] = quality
    total_count = len(batch_data['videos'])

    await query.message.edit_text(
        f"⚙️ **Quality Selected:** `{quality}`\n"
        f"📊 **Total Videos:** `{total_count}`\n\n"
        f"👉 **Reply with download range:**\n"
        f"• Send `all` to download all lectures.\n"
        f"• Send `1-10` to download lectures from 1 to 10.\n"
        f"• Send `5` to download only video #5."
    )

@app.on_message(filters.text & ~filters.command(["start", "stop", "viewthumb", "delthumb"]) & filters.private)
async def batch_range_handler(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in USER_PENDING_BATCH or user_id in ACTIVE_USER_TASKS:
        return

    batch_data = USER_PENDING_BATCH.pop(user_id)
    quality = USER_QUALITY_PREF.pop(user_id, "720p")
    videos = batch_data['videos']
    text = message.text.strip().lower()

    if text == "all":
        selected_videos = videos
    elif "-" in text:
        try:
            start_idx, end_idx = map(int, text.split("-"))
            selected_videos = videos[start_idx - 1:end_idx]
        except Exception:
            await message.reply_text("❌ Invalid range format. Please re-upload .txt file.")
            return
    elif text.isdigit():
        idx = int(text)
        if 1 <= idx <= len(videos):
            selected_videos = [videos[idx - 1]]
        else:
            await message.reply_text("❌ Index out of range. Re-upload .txt file.")
            return
    else:
        await message.reply_text("❌ Invalid selection. Re-upload .txt file.")
        return

    ACTIVE_USER_TASKS.add(user_id)
    await message.reply_text(f"🚀 Queued **{len(selected_videos)}** video(s) in `{quality}`!\n*(Send /stop anytime to cancel)*")

    try:
        for idx, v in enumerate(selected_videos, 1):
            if STOP_REQUESTS.get(user_id, False):
                await message.reply_text("⏹️ **Batch process stopped completely.**")
                break

            task_status = await message.reply_text(f"🚀 Processing `{idx}/{len(selected_videos)}`: **{v['title']}**")
            success = await process_video_download(client, message.chat.id, user_id, task_status, v, quality)
            if not success and STOP_REQUESTS.get(user_id, False):
                await message.reply_text("⏹️ **Batch queue aborted.**")
                break
    finally:
        ACTIVE_USER_TASKS.discard(user_id)

# ==========================================================
# MAIN EXECUTION
# ==========================================================
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    print("🚀 Starting ONeX Extractor Bot...")
    app.run()
