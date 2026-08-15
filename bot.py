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

# ==========================================================
# ASYNCIO EVENT LOOP FIX (Python 3.10+ Compatibility)
# ==========================================================
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import aiohttp
import imageio_ffmpeg
from pyrogram import Client, filters
from pyrogram.types import Message

# ==========================================================
# BOT CREDENTIALS & CONFIGURATION
# ==========================================================
API_ID = 25105426
API_HASH = "d26c274c72a0cde1e7e157eec26f0226"
BOT_TOKEN = "8798719912:AAGnf0sLeE_BMZb_DEyIGtROJ8xZW7A60AQ"

app = Client("video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# User-specific active cancellation tracker: {user_id: bool}
STOP_REQUESTS = {}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://transcoded-video.b-cdn.net/'
}

# ==========================================================
# DUMMY HTTP SERVER (Keeps Render Web Service Alive)
# ==========================================================
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active and running!")

    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    server.serve_forever()

# ==========================================================
# UTILITY & FORMATTER FUNCTIONS
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

# ==========================================================
# PARSER & RESOLVER ENGINE
# ==========================================
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

        if video_tag_idx != -1:
            title_parts = prefix_parts[video_tag_idx + 1:]
            title = ' - '.join(title_parts) if title_parts else f'Lecture_{len(videos)+1}'
        else:
            title = ' - '.join(prefix_parts[1:]) if len(prefix_parts) > 1 else prefix_parts[0]

        videos.append({
            'index': len(videos) + 1,
            'title': title,
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
# ADVANCED DOWNLOAD & UPLOAD PIPELINE (WITH STOP SUPPORT)
# ==========================================================
async def process_video_download(client: Client, chat_id: int, user_id: int, status_msg: Message, stream_url: str, title: str) -> bool:
    if STOP_REQUESTS.get(user_id, False):
        await status_msg.edit_text("🛑 **Process was cancelled by user.**")
        return False

    clean_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_")).strip() or "video"
    output_mp4 = os.path.join(DOWNLOAD_DIR, f"{clean_title}_{int(time.time())}.mp4")
    temp_ts = output_mp4.replace(".mp4", ".ts")

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        resolved_url = await resolve_quality_url(session, stream_url, "720p")
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
                        f"🎬 **Title:** `{title}`\n\n"
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

        await client.send_video(
            chat_id=chat_id,
            video=output_mp4,
            caption=f"🎬 **{title}**\n💾 **Size:** `{format_bytes(file_size)}`",
            progress=upload_progress
        )

        if os.path.exists(output_mp4):
            os.remove(output_mp4)
        await status_msg.delete()
        return True

# ==========================================================
# BOT HANDLERS
# ==========================================================
@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message):
    STOP_REQUESTS[message.from_user.id] = False
    await message.reply_text(
        "👋 **Video & Stream Downloader Bot Active!**\n\n"
        "• Send any `.m3u8` / video link directly to download.\n"
        "• Send a `.txt` batch playlist file to download all lectures.\n"
        "• Send **/stop** anytime to cancel active downloading tasks."
    )

@app.on_message(filters.command("stop"))
async def stop_handler(client: Client, message: Message):
    STOP_REQUESTS[message.from_user.id] = True
    await message.reply_text("🛑 **Stopping and clearing all active tasks...**")

@app.on_message(filters.document)
async def doc_handler(client: Client, message: Message):
    if not message.document.file_name.endswith(".txt"):
        return

    user_id = message.from_user.id
    STOP_REQUESTS[user_id] = False

    status = await message.reply_text("📄 **Parsing .txt playlist file...**")
    file_path = await message.download()

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    os.remove(file_path)

    videos = parse_txt_content(content)
    if not videos:
        await status.edit_text("❌ No valid video links found in this file.")
        return

    await status.edit_text(f"✅ Found **{len(videos)}** videos! Starting batch process...\n*(Send /stop anytime to abort)*")
    
    for idx, v in enumerate(videos, 1):
        if STOP_REQUESTS.get(user_id, False):
            await message.reply_text("⏹️ **Batch process stopped completely.**")
            break

        task_status = await message.reply_text(f"🚀 Processing `{idx}/{len(videos)}`: **{v['title']}**")
        success = await process_video_download(client, message.chat.id, user_id, task_status, v["url"], v["title"])
        if not success and STOP_REQUESTS.get(user_id, False):
            await message.reply_text("⏹️ **Batch queue cancelled.**")
            break

@app.on_message(filters.text & filters.regex(r"https?://[^\s]+"))
async def url_handler(client: Client, message: Message):
    user_id = message.from_user.id
    STOP_REQUESTS[user_id] = False
    
    url = message.text.strip()
    status_msg = await message.reply_text("⚡ **Initializing task...**")
    title = f"Video_{int(time.time())}"
    await process_video_download(client, message.chat.id, user_id, status_msg, url, title)

# ==========================================================
# APP ENTRY POINT
# ==========================================================
if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    print("🚀 Bot starting with Pyrogram engine...")
    app.run()
