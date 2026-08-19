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
from pyrogram import Client, filters
from pyrogram.types import (
    Message, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery
)

# ==========================================================
# BOT CREDENTIALS & CONFIGURATION
# ==========================================================
API_ID = int(os.environ.get("API_ID", 30574823))
API_HASH = os.environ.get("API_HASH", "2815bb996f64421716844acaf2d51493")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8903665090:AAFAstADSgdnWi2_DXNxW4dOKg12ZsE6mrQ")

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
SWAY_SESSIONS = {}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://www.selectionway.com/',
    'Origin': 'https://www.selectionway.com/'
}

# ==========================================================
# DUMMY HTTP SERVER (Keeps Web Services Alive)
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

def get_thumbnail_path(user_id: int):
    path = os.path.join(THUMB_DIR, f"{user_id}.jpg")
    return path if os.path.exists(path) and os.path.getsize(path) > 0 else None

def get_video_metadata(video_path: str):
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
        
    final_thumb = thumb_path if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0 else None
    return final_thumb, width, height, int(duration)

# ==========================================================
# PARSER & RESOLVER ENGINE
# ==========================================================
def parse_txt_content(content: str):
    videos = []
    seen_urls = set()
    lines = content.splitlines()
    url_pattern = re.compile(r'https?://[^\s|<>"\']+')

    batch_name = "SelectionWay Batch"
    current_topic = "General Topic"

    for line_idx, raw_line in enumerate(lines, 1):
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

async def download_file_direct(url: str, file_path: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status == 200:
                    with open(file_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            f.write(chunk)
                    return os.path.exists(file_path) and os.path.getsize(file_path) > 0
    except Exception:
        pass
    return False

async def download_video_stream(url: str, file_path: str) -> bool:
    # 1. Try yt-dlp first
    cmd = [
        "yt-dlp",
        "--merge-output-format", "mp4",
        "--add-header", "Referer:https://www.selectionway.com/",
        "--add-header", "Origin:https://www.selectionway.com/",
        "--concurrent-fragments", "10",
        "-o", file_path,
        url,
        "--quiet", "--no-warnings"
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.communicate()
        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return True
    except Exception:
        pass

    # 2. Try ffmpeg fallback
    try:
        ffmpeg_cmd = [
            FFMPEG_EXE, "-y",
            "-headers", "Referer: https://www.selectionway.com/\r\nOrigin: https://www.selectionway.com/\r\n",
            "-i", url,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            file_path
        ]
        res = subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if res.returncode == 0 and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            return True
    except Exception:
        pass

    # 3. Direct file download fallback
    return await download_file_direct(url, file_path)

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
            InlineKeyboardButton("🚀 Best Available", callback_data=f"{callback_prefix}:best")
        ]
    ])

# ==========================================================
# BOT HANDLERS
# ==========================================================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    STOP_REQUESTS[message.from_user.id] = False
    await message.reply_text(
        "👋 **Welcome to SelectionWay Uploader Bot!** 🍃\n\n"
        "⚡ **Commands:**\n"
        "• `/sway` - Upload batch course from SelectionWay `.txt`\n"
        "• `/stop` - Cancel current task"
    )

@app.on_message(filters.command("stop") & filters.private)
async def stop_handler(client: Client, message: Message):
    user_id = message.from_user.id
    STOP_REQUESTS[user_id] = True
    await message.reply_text("🛑 **Stop request received! Ongoing downloads will terminate.**")

@app.on_message(filters.command("sway") & filters.private)
async def sway_command_handler(client: Client, message: Message):
    user_id = message.from_user.id
    STOP_REQUESTS[user_id] = False
    SWAY_SESSIONS[user_id] = {"step": "WAITING_TXT"}

    await client.send_message(
        message.chat.id,
        "📁 **Please send your SelectionWay `.txt` batch file:**"
    )

@app.on_message(filters.document & filters.private)
async def doc_handler(client: Client, message: Message):
    user_id = message.from_user.id
    session = SWAY_SESSIONS.get(user_id)

    if session and session.get("step") == "WAITING_TXT":
        if not message.document.file_name.endswith(".txt"):
            await message.reply_text("❌ Please send a valid `.txt` file.")
            return

        status = await message.reply_text("📥 **Downloading and parsing .txt file...**")
        txt_path = await message.download()

        with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        items, batch_name = parse_txt_content(content)
        if not items:
            await status.edit_text("❌ **No video/PDF links found in this file.**")
            if os.path.exists(txt_path):
                os.remove(txt_path)
            return

        session["items"] = items
        session["batch_name"] = batch_name
        session["txt_path"] = txt_path
        session["step"] = "WAITING_QUALITY"

        await status.edit_text(
            f"✅ **Loaded {len(items)} items from:** `{batch_name}`\n\n🎬 **Select Video Quality to Download:**",
            reply_markup=get_quality_keyboard("swayqual")
        )

@app.on_callback_query(filters.regex(r"^swayqual:"))
async def sway_quality_callback(client: Client, callback: CallbackQuery):
    user_id = callback.from_user.id
    session = SWAY_SESSIONS.get(user_id)

    if not session or session.get("step") != "WAITING_QUALITY":
        await callback.answer("Session expired. Please send /sway again.", show_alert=True)
        return

    quality = callback.data.split(":")[1]
    session["quality"] = quality
    session["step"] = "WAITING_CREDIT"

    await callback.message.delete()
    await client.send_message(
        callback.message.chat.id,
        "✍️ **Send Extracted By Name / Credit for Captions (or send `None`):**"
    )
    await callback.answer()

@app.on_message(filters.text & filters.private)
async def text_step_handler(client: Client, message: Message):
    user_id = message.from_user.id
    session = SWAY_SESSIONS.get(user_id)
    if not session:
        return

    if session.get("step") == "WAITING_CREDIT":
        credit_input = message.text.strip()
        session["credit"] = "O ɴ ᴇ 𝐗 🍃" if credit_input.lower() == "none" else credit_input
        session["step"] = "WAITING_CHANNEL"

        await message.reply_text(
            "📢 **Send Target Channel ID to upload into (e.g., `-100xxxxxxxxxx` or `me` for Saved Messages):**"
        )
        return

    if session.get("step") == "WAITING_CHANNEL":
        channel_input = message.text.strip()
        try:
            target_chat_id = message.chat.id if channel_input.lower() == "me" else int(channel_input)
        except ValueError:
            await message.reply_text("❌ Invalid Channel ID format. Example: `-1003991146605`")
            return

        items = session["items"]
        credit_name = session["credit"]
        default_batch_name = session.get("batch_name", "Batch")
        txt_path = session["txt_path"]

        SWAY_SESSIONS.pop(user_id, None)
        status_msg = await message.reply_text(f"🚀 **Starting batch processing for {len(items)} files...**")

        for idx, item in enumerate(items, 1):
            if STOP_REQUESTS.get(user_id, False):
                await status_msg.edit_text("🛑 **Process stopped by user.**")
                break

            title = item["title"]
            url = item["url"]
            topic = item.get("topic", "Live Lecture")
            batch = item.get("batch", default_batch_name)
            is_pdf = item["is_pdf"]

            clean_title = "".join(c for c in title if c.isalnum() or c in (" ", "-", "_", "(", ")", ".")).strip() or f"file_{idx}"
            file_ext = ".pdf" if is_pdf else ".mp4"
            file_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{idx}_{clean_title[:30]}{file_ext}")

            await status_msg.edit_text(f"⏳ `[{idx:03d}/{len(items):03d}]` **Downloading:** `{title}`")

            if is_pdf:
                success = await download_file_direct(url, file_path)
            else:
                success = await download_video_stream(url, file_path)

            if not success or not os.path.exists(file_path):
                await message.reply_text(f"⚠️ **Failed/Skipped:** `{title}`")
                continue

            await status_msg.edit_text(f"📤 `[{idx:03d}/{len(items):03d}]` **Uploading:** `{title}`")

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

        await status_msg.edit_text("🎉 **All batch files downloaded and uploaded successfully!**")
        if os.path.exists(txt_path):
            os.remove(txt_path)

if __name__ == "__main__":
    app.run()
