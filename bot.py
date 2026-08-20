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
# PROGRESS & STATS FORMATTERS
# ==========================================================
def format_bytes(size_bytes: int) -> str:
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def format_time(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def make_progress_bar(percentage: float) -> str:
    filled = int(percentage // 10)
    empty = 10 - filled
    return "█" * filled + "░" * empty

async def upload_progress_callback(current, total, status_msg: Message, title: str, start_time: float, last_update: list):
    """Live progress callback for Pyrogram send_video / send_document."""
    now = time.time()
    if now - last_update[0] < 3.5 and current != total:
        return

    last_update[0] = now
    elapsed = max(1, int(now - start_time))
    speed = current / elapsed
    percent = round((current / total) * 100, 1) if total > 0 else 0
    eta = int((total - current) / speed) if speed > 0 else 0
    bar = make_progress_bar(percent)

    text = (
        f"📤 **Uploading:** `{title}`\n\n"
        f"**Progress:** [{bar}] `{percent}%`\n"
        f"⚡ **Speed:** `{format_bytes(int(speed))}/s`\n"
        f"📊 **Size:** `{format_bytes(current)}` / `{format_bytes(total)}`\n"
        f"⏳ **ETA:** `{format_time(eta)}`"
    )
    try:
        await status_msg.edit_text(text)
    except Exception:
        pass

# ==========================================================
# DYNAMIC HEADERS RESOLVER
# ==========================================================
def get_headers_for_url(url: str, mode: str = "jrf") -> dict:
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.lower()

    if any(k in domain for k in ["classx", "stream-os", "transcoded", "futurekul"]) or mode == "jrf":
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
# METADATA & PROBING ENGINE
# ==========================================================
def get_video_metadata(video_path: str):
    thumb_path = video_path + "_thumb.jpg"
    width, height, duration = 1280, 720, 0
    try:
        cmd_probe = [FFMPEG_EXE, "-i", video_path]
        res = subprocess.run(cmd_probe, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        
        if dur_match := re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", res.stderr):
            h, m, s = dur_match.groups()
            duration = int(h) * 3600 + int(m) * 60 + float(s)
            
        if dim_match := re.search(r",\s*(\d{3,4})x(\d{3,4})", res.stderr):
            width = int(dim_match.group(1))
            height = int(dim_match.group(2))

        frame_time = min(3, max(0, int(duration) - 1))
        cmd_thumb = [
            FFMPEG_EXE, "-y",
            "-ss", str(frame_time),
            "-i", video_path,
            "-vframes", "1",
            "-vf", "scale=640:-1",
            "-q:v", "2",
            thumb_path
        ]
        subprocess.run(cmd_thumb, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except Exception as e:
        print(f"Metadata extraction error: {e}")

    final_thumb = thumb_path if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0 else None
    return final_thumb, width, height, int(duration)

# ==========================================================
# TXT PARSER
# ==========================================================
def parse_txt_content(content: str, default_mode: str = "jrf"):
    videos = []
    seen_urls = set()
    lines = content.splitlines()

    batch_name = "JRFAdda Batch" if default_mode == "jrf" else "SelectionWay Batch"
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

        url_match = re.search(r"https?://\S+", line)
        if not url_match:
            continue

        url = url_match.group(0).rstrip('.,;)"\'')
        if url in seen_urls:
            continue
        seen_urls.add(url)

        prefix = line[:url_match.start()].strip()
        clean_prefix = re.sub(r'[:|\-]+$', '', prefix).strip()
        clean_title = re.sub(r'^(Free Classes|Paid Classes|All Classes|All Notes|Free Class)\s*', '', clean_prefix, flags=re.I).strip()
        
        if not clean_title or clean_title.lower() in ["video", "pdf", "file"]:
            clean_title = f"Lecture_{len(videos)+1}"

        is_pdf = ".pdf" in url.lower() or "pdf" in clean_title.lower()

        videos.append({
            'index': len(videos) + 1,
            'title': clean_title,
            'topic': current_topic,
            'batch': batch_name,
            'url': url,
            'is_pdf': is_pdf
        })

    return videos, batch_name

# ==========================================================
# DOWNLOAD ENGINES WITH LIVE STATS
# ==========================================================
async def download_file_direct(url: str, file_path: str, user_id: int, status_msg: Message, title: str, mode: str = "jrf") -> bool:
    headers = get_headers_for_url(url, mode)
    start_time = time.time()
    last_update = [0.0]

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=600)) as resp:
                if resp.status == 200:
                    total_size = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    with open(file_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            if STOP_REQUESTS.get(user_id, False):
                                return False
                            f.write(chunk)
                            downloaded += len(chunk)

                            # Update progress stats
                            now = time.time()
                            if now - last_update[0] > 3.5:
                                last_update[0] = now
                                elapsed = max(1, int(now - start_time))
                                speed = downloaded / elapsed
                                percent = round((downloaded / total_size) * 100, 1) if total_size > 0 else 0
                                bar = make_progress_bar(percent)
                                try:
                                    await status_msg.edit_text(
                                        f"📥 **Downloading PDF:** `{title}`\n\n"
                                        f"**Progress:** [{bar}] `{percent}%`\n"
                                        f"⚡ **Speed:** `{format_bytes(int(speed))}/s`\n"
                                        f"📊 **Size:** `{format_bytes(downloaded)}` / `{format_bytes(total_size)}`"
                                    )
                                except Exception:
                                    pass

                    return os.path.exists(file_path) and os.path.getsize(file_path) > 0
    except Exception as e:
        print(f"Direct download error on {url}: {e}")
    return False

def _ytdlp_download_sync(url: str, file_path: str, quality: str, mode: str, status_msg: Message, title: str, loop: asyncio.AbstractEventLoop) -> bool:
    out_template = file_path.rsplit(".", 1)[0] + ".%(ext)s"
    headers = get_headers_for_url(url, mode)

    q_val = quality.replace("p", "")
    q_filter = "bestvideo+bestaudio/best" if quality in ["best", "1080"] else f"bestvideo[height<={q_val}]+bestaudio/best[height<={q_val}]/best"

    last_update = [0.0]

    def ytdlp_hook(d):
        if d.get("status") == "downloading":
            now = time.time()
            if now - last_update[0] > 3.5:
                last_update[0] = now
                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                speed = d.get("speed") or 0
                eta = d.get("eta") or 0
                percent = round((downloaded / total) * 100, 1) if total > 0 else 0
                bar = make_progress_bar(percent)

                text = (
                    f"📥 **Downloading Stream:** `{title}`\n\n"
                    f"**Progress:** [{bar}] `{percent}%`\n"
                    f"⚡ **Speed:** `{format_bytes(int(speed))}/s`\n"
                    f"📊 **Size:** `{format_bytes(downloaded)}` / `{format_bytes(total)}`\n"
                    f"⏳ **ETA:** `{format_time(eta)}`"
                )
                asyncio.run_coroutine_threadsafe(status_msg.edit_text(text), loop)

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
        "progress_hooks": [ytdlp_hook],
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

async def download_video_stream(url: str, file_path: str, user_id: int, status_msg: Message, title: str, quality: str = "720", mode: str = "jrf") -> bool:
    loop = asyncio.get_event_loop()
    success = await asyncio.to_thread(_ytdlp_download_sync, url, file_path, quality, mode, status_msg, title, loop)

    if STOP_REQUESTS.get(user_id, False):
        if os.path.exists(file_path):
            os.remove(file_path)
        return False

    if success:
        return True

    return await download_file_direct(url, file_path, user_id, status_msg, title, mode)

# ==========================================================
# BOT HANDLERS & ROUTING
# ==========================================================
@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    STOP_REQUESTS[message.from_user.id] = False
    await message.reply_text(
        "👋 **Welcome to the Course Uploader Bot!** 🍃\n\n"
        "⚡ **Commands:**\n"
        "• `/jrf`  - Upload batch course from JRFAdda / ClassX `.txt`\n"
        "• `/sway` - Upload batch course from SelectionWay `.txt`\n"
        "• `/stop` - Immediately cancel ongoing download and upload\n\n"
        "_Tip: Send any `.txt` batch file to start automatically._"
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
    mode = session.get("mode", "jrf")

    if not message.document.file_name.endswith(".txt"):
        await message.reply_text("❌ Please send a valid `.txt` file.")
        return

    status = await message.reply_text("📥 **Downloading and parsing .txt file...**")
    txt_path = await message.download()

    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    if "selectionway" in content.lower():
        mode = "sway"
    else:
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

    # Step 4: Channel ID & Execution
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
        mode = session.get("mode", "jrf")

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

            clean_filename = re.sub(r'[\\/*?:"<>|]', "", title).strip() or f"file_{idx}"
            file_ext = ".pdf" if is_pdf else ".mp4"
            file_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_{idx}_{clean_filename[:35]}{file_ext}")

            await status_msg.edit_text(f"⏳ `[{idx:03d}/{len(all_items):03d}]` **Preparing Download:** `{title}`")

            # 1. Download
            if is_pdf:
                success = await download_file_direct(url, file_path, user_id, status_msg, title, mode)
            else:
                success = await download_video_stream(url, file_path, user_id, status_msg, title, quality, mode)

            if STOP_REQUESTS.get(user_id, False):
                if os.path.exists(file_path):
                    os.remove(file_path)
                await status_msg.edit_text("🛑 **Batch task was cancelled via /stop.**")
                break

            if not success or not os.path.exists(file_path):
                await message.reply_text(f"⚠️ **Failed/Skipped:** `{title}`")
                continue

            caption_text = (
                f"Index: {idx:03d}\n\n"
                f"Title: {title}{file_ext}\n\n"
                f"Topic: {topic}\n\n"
                f"Batch: {batch}\n\n"
                f"Extracted By: {credit_name}"
            )

            # 2. Upload with Live Stats
            upload_start = time.time()
            last_upload_update = [0.0]

            try:
                if is_pdf:
                    await client.send_document(
                        chat_id=target_chat_id,
                        document=file_path,
                        caption=caption_text,
                        progress=upload_progress_callback,
                        progress_args=(status_msg, title, upload_start, last_upload_update)
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
                        supports_streaming=True,
                        progress=upload_progress_callback,
                        progress_args=(status_msg, title, upload_start, last_upload_update)
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
