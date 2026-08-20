import asyncio
import gc
import math
import os
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
import imageio_ffmpeg
import yt_dlp
from pyrogram import Client, filters
from pyrogram.types import Message

# ==========================================================
# BOT CREDENTIALS & CONFIGURATION
# ==========================================================
API_ID = int(os.environ.get("API_ID", 30574823))
API_HASH = os.environ.get("API_HASH", "2815bb996f64421716844acaf2d51493")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client(
    "onex_uploader_session",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
DOWNLOAD_DIR = "downloads"
THUMB_DIR = "thumbnails"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

STOP_REQUESTS = {}
ACTIVE_SESSIONS = {}
ACTIVE_SUBPROCESSES = {}

# ==========================================================
# DUMMY HTTP SERVER (PORT KEEP-ALIVE)
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

threading.Thread(target=run_web_server, daemon=True).start()

# ==========================================================
# DYNAMIC HEADER RESOLVER
# ==========================================================
def get_headers_for_url(url: str, mode: str = "sway") -> dict:
    """Returns domain and platform-specific headers to avoid 403 Forbidden errors."""
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()

    if "classx.co.in" in domain or "futurekul" in domain or "stream-os" in domain or mode == "jrf":
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://classx.co.in/",
            "Origin": "https://classx.co.in"
        }
    elif "jrfadda" in domain:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.jrfadda.com/",
            "Origin": "https://www.jrfadda.com"
        }
    elif "akamai" in domain or "appx" in domain:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://player.akamai.net.in/",
            "Origin": "https://player.akamai.net.in"
        }

    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.selectionway.com/",
        "Origin": "https://www.selectionway.com/"
    }

# ==========================================================
# UTILITIES & PROBING
# ==========================================================
def format_bytes(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def get_thumbnail_path(user_id: int):
    path = os.path.join(THUMB_DIR, f"{user_id}.jpg")
    return path if os.path.exists(path) and os.path.getsize(path) > 0 else None

def get_video_metadata(video_path: str):
    thumb_path = video_path + "_thumb.jpg"
    width, height, duration = 1280, 720, 0
    try:
        cmd = [
            FFMPEG_EXE, "-y",
            "-ss", "00:00:03",
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

    final_thumb = thumb_path if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0 else None
    return final_thumb, width, height, int(duration)

# ==========================================================
# TXT PARSER
# ==========================================================
def parse_txt_content(content: str, default_mode: str = "sway"):
    videos = []
    seen_urls = set()
    lines = content.splitlines()
    url_pattern = re.compile(r'https?://[^\s|<>"\']+')

    batch_name = "JRFAdda Batch" if default_mode == "jrf" else "SelectionWay Batch"
    current_topic = "General Topic"

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("BATCH:"):
            batch_name = line.replace("BATCH:", "").strip()
            continue
        elif "BATCH :" in line:
            batch_name = line.split("BATCH :")[1].strip()
            continue

        if line.startswith("---") and line.endswith("---"):
            current_topic = line.strip("- \t")
            continue
        elif line.startswith("TOPIC:"):
            current_topic = line.replace("TOPIC:", "").strip()
            continue

        url_match = url_pattern.search(line)
        if not url_match:
            continue

        url = url_match.group(0).rstrip('.,;')
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title_part = line[:url_match.start()].strip().rstrip(':|-').strip()
        title = title_part if title_part else f"Lecture_{len(videos)+1}"

        videos.append({
            'index': len(videos) + 1,
            'title': title,
            'topic': current_topic,
            'batch': batch_name,
            'url': url,
            'is_pdf': ".pdf" in url.lower() or "pdf" in title.lower()
        })

    return videos, batch_name

# ==========================================================
# DOWNLOAD ENGINES
# ==========================================================
async def download_file_direct(url: str, file_path: str, user_id: int, mode: str = "sway") -> bool:
    headers = get_headers_for_url(url, mode)
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status == 200:
                    with open(file_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            if STOP_REQUESTS.get(user_id, False):
                                return False
                            f.write(chunk)
                    return os.path.exists(file_path) and os.path.getsize(file_path) > 0
    except Exception:
        pass
    return False

def _ytdlp_download_sync(url: str, file_path: str, quality: str = "720", mode: str = "sway") -> bool:
    """Integrated yt-dlp downloader execution with custom fragment & retry logic."""
    out_template = file_path.rsplit(".", 1)[0] + ".%(ext)s"
    headers = get_headers_for_url(url, mode)

    q_val = quality.replace("p", "")
    q_filter = "bestvideo+bestaudio/best" if quality in ["best", "1080"] else f"bestvideo[height<={q_val}]+bestaudio/best[height<={q_val}]/best"

    ydl_opts = {
        "outtmpl": out_template,
        "format": q_filter,
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
            "Referer": headers.get("Referer", "https://classx.co.in/"),
            "Origin": headers.get("Origin", "https://classx.co.in"),
        },
        "concurrent_fragment_downloads": 8,
        "retries": 10,
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return os.path.exists(file_path) and os.path.getsize(file_path) > 0
    except Exception as e:
        print(f"yt-dlp download error on {url}: {e}")
        return False

async def download_video_stream(url: str, file_path: str, user_id: int, quality: str = "720", mode: str = "sway") -> bool:
    """Executes the yt-dlp download asynchronously without blocking the event loop."""
    success = await asyncio.to_thread(_ytdlp_download_sync, url, file_path, quality, mode)

    if STOP_REQUESTS.get(user_id, False):
        if os.path.exists(file_path):
            os.remove(file_path)
        return False

    if success:
        return True

    # Fallback to direct HTTP download if yt-dlp cannot parse the stream
    return await download_file_direct(url, file_path, user_id, mode)

# ==========================================================
# BOT HANDLERS & ROUTING
# ==========================================================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    STOP_REQUESTS[message.from_user.id] = False
    await message.reply_text(
        "👋 **Welcome to the Multi-Source Extractor & Uploader Bot!** 🍃\n\n"
        "⚡ **Commands:**\n"
        "• `/sway` - Upload batch course from SelectionWay `.txt`\n"
        "• `/jrf`  - Upload batch course from JRFAdda / ClassX `.txt`\n"
        "• `/stop` - Immediately cancel ongoing download and upload\n\n"
        "_Tip: You can also send any `.txt` file directly to auto-start._"
    )

@app.on_message(filters.command("stop") & filters.private)
async def stop_handler(client: Client, message: Message):
    user_id = message.from_user.id
    STOP_REQUESTS[user_id] = True
    ACTIVE_SESSIONS.pop(user_id, None)

    proc = ACTIVE_SUBPROCESSES.get(user_id)
    if proc:
        try:
            proc.kill()
        except Exception:
            pass
        ACTIVE_SUBPROCESSES.pop(user_id, None)

    await message.reply_text("🛑 **Process stopped! Any active tasks have been terminated.**")

@app.on_message(filters.command(["sway", "jrf"]) & filters.private)
async def platform_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    cmd = message.command[0].lower()
    STOP_REQUESTS[user_id] = False

    platform_name = "JRFAdda / ClassX" if cmd == "jrf" else "SelectionWay"
    ACTIVE_SESSIONS[user_id] = {
        "step": "WAITING_TXT",
        "mode": cmd
    }

    await client.send_message(
        message.chat.id,
        f"📁 **Please send your {platform_name} `.txt` batch file:**"
    )

@app.on_message(filters.document & filters.private)
async def doc_handler(client: Client, message: Message):
    user_id = message.from_user.id
    session = ACTIVE_SESSIONS.get(user_id, {})
    mode = session.get("mode", "sway")

    if not message.document.file_name.endswith(".txt"):
        await message.reply_text("❌ Please send a valid `.txt` file.")
        return

    status = await message.reply_text("📥 **Downloading and parsing .txt file...**")
    txt_path = await message.download()

    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Auto-detect mode if not explicitly set via command
    if "classx" in content.lower() or "jrf" in content.lower():
        mode = "jrf"

    items, batch_name = parse_txt_content(content, default_mode=mode)
    if not items:
        await status.edit_text("❌ **No video or PDF links found in this file.**")
        if os.path.exists(txt_path):
            os.remove(txt_path)
        return

    ACTIVE_SESSIONS[user_id] = {
        "items": items,
        "batch_name": batch_name,
        "txt_path": txt_path,
        "mode": mode,
        "step": "WAITING_START_INDEX"
    }

    await status.edit_text(
        f"✅ **Loaded {len(items)} items from:** `{batch_name}`\n"
        f"🏷 **Engine Mode:** `{mode.upper()}`\n\n"
        f"🔢 **From where to start?**\n"
        f"Send start index number (e.g. `1` or `15`):"
    )

@app.on_message(filters.text & filters.private)
async def text_step_handler(client: Client, message: Message):
    user_id = message.from_user.id
    session = ACTIVE_SESSIONS.get(user_id)
    if not session:
        return

    # Step 1: Start Index
    if session.get("step") == "WAITING_START_INDEX":
        text = message.text.strip()
        start_idx = int(text) if text.isdigit() else 1
        session["start_index"] = max(1, start_idx)
        session["step"] = "WAITING_QUALITY"

        await message.reply_text(
            "🎬 **Send Video Quality to Download:**\n"
            "(e.g. `360`, `480`, `720`, `1080`, or `best`)"
        )
        return

    # Step 2: Video Quality
    if session.get("step") == "WAITING_QUALITY":
        quality_input = message.text.strip().lower()
        session["quality"] = quality_input if quality_input in ["360", "480", "720", "1080", "best"] else "720"
        session["step"] = "WAITING_CREDIT"

        await message.reply_text(
            "✍️ **Send Extracted By Name / Credit for Captions (or send `None`):**"
        )
        return

    # Step 3: Credit Name
    if session.get("step") == "WAITING_CREDIT":
        credit_input = message.text.strip()
        session["credit"] = "O ɴ ᴇ 𝐗 🍃" if credit_input.lower() == "none" else credit_input
        session["step"] = "WAITING_CHANNEL"

        await message.reply_text(
            "📢 **Send Target Channel ID to upload into (e.g., `-100xxxxxxxxxx` or `me` for Saved Messages):**"
        )
        return

    # Step 4: Channel ID & Run Batch Processing
    if session.get("step") == "WAITING_CHANNEL":
        channel_input = message.text.strip()
        try:
            target_chat_id = message.chat.id if channel_input.lower() == "me" else int(channel_input)
        except ValueError:
            await message.reply_text("❌ Invalid Channel ID format. Example: `-1003991146605`")
            return

        all_items = session["items"]
        start_index = session["start_index"]
        quality = session["quality"]
        credit_name = session["credit"]
        default_batch_name = session.get("batch_name", "Batch")
        txt_path = session["txt_path"]
        mode = session.get("mode", "sway")

        items = all_items[start_index - 1:] if start_index <= len(all_items) else []
        if not items:
            await message.reply_text("❌ Start index is higher than the total number of items.")
            ACTIVE_SESSIONS.pop(user_id, None)
            return

        ACTIVE_SESSIONS.pop(user_id, None)
        status_msg = await message.reply_text(f"🚀 **Starting batch processing from index {start_index} ({len(items)} files)...**")

        for idx, item in enumerate(items, start_index):
            if STOP_REQUESTS.get(user_id, False):
                await status_msg.edit_text("🛑 **Batch task was cancelled via /stop.**")
                break

            title = item["title"]
            url = item["url"]
            topic = item.get("topic", "Lecture")
            batch = item.get("batch", default_batch_name)
            is_pdf = item["is_pdf"]

            clean_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_", "(", ")", ".")).strip() or f"file_{idx}"
            file_ext = ".pdf" if is_pdf else ".mp4"
            file_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{idx}_{clean_title[:30]}{file_ext}")

            await status_msg.edit_text(f"⏳ `[{idx:03d}/{len(all_items):03d}]` **Downloading:** `{title}`\n\n_Send /stop to cancel._")

            # Download routine
            if is_pdf:
                success = await download_file_direct(url, file_path, user_id, mode)
            else:
                success = await download_video_stream(url, file_path, user_id, quality, mode)

            if STOP_REQUESTS.get(user_id, False):
                if os.path.exists(file_path):
                    os.remove(file_path)
                await status_msg.edit_text("🛑 **Batch task was cancelled via /stop.**")
                break

            if not success or not os.path.exists(file_path):
                await message.reply_text(f"⚠️ **Failed/Skipped:** `{title}`")
                continue

            await status_msg.edit_text(f"📤 `[{idx:03d}/{len(all_items):03d}]` **Uploading:** `{title}`")

            caption_text = (
                f"Index: {idx:03d}\n\n"
                f"Title: {title}{file_ext}\n\n"
                f"Topic: {topic}\n\n"
                f"Batch: {batch}\n\n"
                f"Extracted By: {credit_name}"
            )

            try:
                if is_pdf:
                    await client.send_document(
                        chat_id=target_chat_id,
                        document=file_path,
                        caption=caption_text
                    )
                else:
                    auto_thumb, vid_w, vid_h, vid_dur = await asyncio.to_thread(get_video_metadata, file_path)
                    await client.send_video(
                        chat_id=target_chat_id,
                        video=file_path,
                        caption=caption_text,
                        thumb=auto_thumb,
                        width=vid_w or 1280,
                        height=vid_h or 720,
                        duration=int(vid_dur) or 0,
                        supports_streaming=True
                    )
                    if auto_thumb and os.path.exists(auto_thumb):
                        os.remove(auto_thumb)
            except Exception as e:
                await message.reply_text(f"❌ **Upload Error on `{title}`:** {e}")
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)

            await asyncio.sleep(2)

        if not STOP_REQUESTS.get(user_id, False):
            await status_msg.edit_text("🎉 **All batch files downloaded and uploaded successfully!**")

        if os.path.exists(txt_path):
            os.remove(txt_path)

if __name__ == "__main__":
    app.run()
