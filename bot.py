import os
import re
import gc
import time
import math
import asyncio
import subprocess
import urllib.parse
import aiohttp
import imageio_ffmpeg
from pyrogram import Client, filters
from pyrogram.types import Message

# ==========================================
# BOT CONFIGURATION
# ==========================================
API_ID = int(os.environ.get("API_ID", "25105426"))
API_HASH = os.environ.get("API_HASH", "d26c274c72a0cde1e7e157eec26f0226")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8798719912:AAGnf0sLeE_BMZb_DEyIGtROJ8xZW7A60AQ")

app = Client("video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://transcoded-video.b-cdn.net/'
}

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def format_bytes(size_bytes):
    if size_bytes <= 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def make_progress_bar(current, total, bar_length=15):
    if total <= 0:
        return "░" * bar_length
    fraction = min(max(current / total, 0.0), 1.0)
    filled = int(fraction * bar_length)
    return "█" * filled + "░" * (bar_length - filled)

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
        cmd = [FFMPEG_EXE, "-y", "-i", ts_path, "-c", "copy", "-movflags", "+faststart", "-bsf:a", "aac_adtstoasc", mp4_path]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return res.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0
    except Exception:
        return False

# ==========================================
# ASYNC DOWNLOAD ENGINE (WITH TG PROGRESS)
# ==========================================
async def download_and_upload(client: Client, message: Message, stream_url: str, title: str):
    status_msg = await message.reply_text(f"🔍 **Resolving Stream...**\n`{title}`")
    output_mp4 = os.path.join(DOWNLOAD_DIR, f"{int(time.time())}.mp4")
    temp_ts = output_mp4.replace(".mp4", ".ts")

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        resolved_url = await resolve_quality_url(session, stream_url, "720p")
        video_info = await get_video_info_async(session, resolved_url)

        if "error" in video_info or not video_info.get("segments"):
            await status_msg.edit_text(f"❌ Failed to fetch manifest: {video_info.get('error', 'No segments')}")
            return

        segments = video_info["segments"]
        total_segs = len(segments)
        sem = asyncio.Semaphore(30)
        downloaded_chunks = {}
        completed_segs = 0
        last_update = time.time()
        start_time = time.time()

        tasks = [asyncio.create_task(download_single_chunk(session, idx, url, sem)) for idx, url in enumerate(segments)]

        for future in asyncio.as_completed(tasks):
            idx, content = await future
            if content:
                downloaded_chunks[idx] = content
            completed_segs += 1

            if time.time() - last_update > 4 or completed_segs == total_segs:
                pct = (completed_segs / total_segs) * 100
                bar = make_progress_bar(completed_segs, total_segs)
                try:
                    await status_msg.edit_text(
                        f"📥 **Downloading:** `{title}`\n"
                        f"[{bar}] `{pct:.1f}%` ({completed_segs}/{total_segs})\n"
                        f"⏱ ETA: `{int((time.time() - start_time) / max(completed_segs, 1) * (total_segs - completed_segs))}s`"
                    )
                except Exception:
                    pass
                last_update = time.time()

        await status_msg.edit_text("⚙️ **Remuxing to MP4...**")
        with open(temp_ts, "wb") as f:
            for idx in range(total_segs):
                if chunk := downloaded_chunks.get(idx):
                    f.write(chunk)
        downloaded_chunks.clear()
        gc.collect()

        await asyncio.to_thread(remux_ts_to_mp4, temp_ts, output_mp4)
        if os.path.exists(temp_ts):
            os.remove(temp_ts)

        if not os.path.exists(output_mp4):
            await status_msg.edit_text("❌ Remuxing failed!")
            return

        # Upload to Telegram
        await status_msg.edit_text("📤 **Uploading video to Telegram...**")
        
        async def upload_progress(current, total):
            nonlocal last_update
            if time.time() - last_update > 4:
                pct = (current / total) * 100
                bar = make_progress_bar(current, total)
                try:
                    await status_msg.edit_text(f"📤 **Uploading:**\n[{bar}] `{pct:.1f}%` ({format_bytes(current)}/{format_bytes(total)})")
                except Exception:
                    pass
                last_update = time.time()

        await client.send_video(
            chat_id=message.chat.id,
            video=output_mp4,
            caption=f"🎬 **{title}**\n💾 Size: `{format_bytes(os.path.getsize(output_mp4))}`",
            progress=upload_progress
        )

        if os.path.exists(output_mp4):
            os.remove(output_mp4)
        await status_msg.delete()

# ==========================================
# BOT HANDLERS
# ==========================================
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply_text(
        "👋 **Welcome to High-Speed Stream Downloader!**\n\n"
        "Send me a `.m3u8` link or forward a `.txt` batch file to start downloading."
    )

@app.on_message(filters.text & filters.regex(r"https?://[^\s]+"))
async def handle_url(client, message):
    url = message.text.strip()
    title = f"Video_{int(time.time())}"
    await download_and_upload(client, message, url, title)

if __name__ == "__main__":
    app.run()
