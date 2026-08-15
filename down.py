import os
import sys
import subprocess

# ==========================================================
# AUTOMATED DEPENDENCY BOOTSTRAPPER (Zero Setup Needed)
# ==========================================================
REQUIRED_PACKAGES = {
    "aiohttp": "aiohttp>=3.9.0",
    "imageio_ffmpeg": "imageio-ffmpeg>=0.5.0",
    "requests": "requests"
}

def auto_install_dependencies():
    """Automatically detects and installs any missing Python packages on first run."""
    missing = []
    for mod_name, pkg_name in REQUIRED_PACKAGES.items():
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(pkg_name)

    if missing:
        print("\n" + "=" * 65)
        print("📦 FIRST-TIME SETUP: Installing required dependencies...")
        print(f"Missing packages: {', '.join(missing)}")
        print("⚡ Downloading & installing automatically... Please wait.")
        print("=" * 65 + "\n")
        try:
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + missing
            subprocess.check_call(cmd)
            print("\n✅ Setup complete! All dependencies are installed.\n")
        except Exception as e:
            print(f"\n❌ Automatic dependency installation failed: {e}")
            print(f"👉 Please run manually in terminal: pip install {' '.join(missing)}\n")
            sys.exit(1)

auto_install_dependencies()

# ==========================================================
# STANDARD IMPORTS (Guaranteed available after bootstrap)
# ==========================================================
import gc
import re
import time
import math
import asyncio
import urllib.parse
import aiohttp
import imageio_ffmpeg

# Standalone FFmpeg binary path (bundled inside imageio-ffmpeg)
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://transcoded-video.b-cdn.net/'
}

# ==========================================
# ULTRA-ROBUST UNIVERSAL TXT PARSER
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
            category = ' > '.join(prefix_parts[:video_tag_idx]) if video_tag_idx > 0 else 'General'
            title_parts = prefix_parts[video_tag_idx + 1:]
            title = ' - '.join(title_parts) if title_parts else f'Lecture {len(videos)+1}'
        else:
            category = prefix_parts[0] if len(prefix_parts) > 1 else 'General'
            title = ' - '.join(prefix_parts[1:]) if len(prefix_parts) > 1 else prefix_parts[0]

        videos.append({
            'index': len(videos) + 1,
            'category': category,
            'title': title,
            'url': url,
            'line': line_idx
        })

    return videos

def parse_txt_file(file_path: str):
    if not os.path.exists(file_path):
        return []
    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
        return parse_txt_content(f.read())

# ==========================================
# STREAM & QUALITY RESOLVER
# ==========================================
async def resolve_quality_url(session: aiohttp.ClientSession, original_url: str, desired_quality: str = "1080p") -> str:
    if not original_url.startswith("http"):
        return original_url

    if desired_quality in ["best", "original"]:
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
    if not stream_url or not stream_url.startswith("http"):
        return {"error": f"Invalid stream URL: {stream_url}"}

    try:
        async with session.get(stream_url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {"error": f"HTTP {resp.status} Forbidden/Not Found"}
            text = await resp.text()
    except Exception as e:
        return {"error": str(e)}

    segments = []
    total_duration = 0.0

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line.split(":")[1].split(",")[0])
                total_duration += dur
            except Exception:
                pass
        elif line and not line.startswith("#"):
            seg_url = urllib.parse.urljoin(stream_url, line)
            segments.append(seg_url)

    return {
        "stream_url": stream_url,
        "segments": segments,
        "segment_count": len(segments),
        "total_duration": total_duration
    }

def format_bytes(size_bytes):
    if size_bytes <= 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def make_progress_bar(current, total, bar_length=25):
    if total <= 0:
        return "░" * bar_length
    fraction = min(max(current / total, 0.0), 1.0)
    filled = int(fraction * bar_length)
    return "█" * filled + "░" * (bar_length - filled)

# ==========================================
# ULTRA-FAST ASYNC DOWNLOAD, MERGE & REMUX ENGINE
# ==========================================
async def download_single_chunk(session: aiohttp.ClientSession, index: int, url: str, sem: asyncio.Semaphore, retries: int = 3):
    async with sem:
        for _ in range(retries):
            try:
                async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status == 200:
                        content = await r.read()
                        if content:
                            return index, content
            except Exception:
                await asyncio.sleep(0.2)
        return index, None

def remux_ts_to_mp4(ts_path: str, mp4_path: str) -> bool:
    try:
        cmd = [
            FFMPEG_EXE,
            "-y",
            "-i", ts_path,
            "-c", "copy",
            "-movflags", "+faststart",
            "-bsf:a", "aac_adtstoasc",
            mp4_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return res.returncode == 0 and os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0
    except Exception:
        return False

async def download_video_fast(stream_url: str, output_mp4_path: str, title: str = "Video", concurrency: int = 40):
    connector = aiohttp.TCPConnector(limit=concurrency + 15, ttl_dns_cache=300, enable_cleanup_closed=True)
    timeout = aiohttp.ClientTimeout(total=None)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        print(f"\n🔍 Resolving stream manifest...")
        video_info = await get_video_info_async(session, stream_url)
        if "error" in video_info or not video_info.get("segments"):
            print(f"❌ Error: {video_info.get('error', 'No segments found')}")
            return False

        segments = video_info.get("segments", [])
        total_segs = len(segments)
        duration_m = video_info.get("total_duration", 0) / 60
        print(f"🎬 Title: {title}")
        print(f"📊 Segments: {total_segs} (~{duration_m:.1f} minutes)")
        print(f"⚡ Concurrency: 40 Parallel TCP Workers\n")

        temp_ts_path = output_mp4_path.replace(".mp4", ".ts")
        downloaded_chunks = {}
        downloaded_bytes = 0
        completed_segs = 0
        start_time = time.time()
        sem = asyncio.Semaphore(concurrency)

        tasks = [
            asyncio.create_task(download_single_chunk(session, idx, url, sem))
            for idx, url in enumerate(segments)
        ]

        for future in asyncio.as_completed(tasks):
            idx, content = await future
            if content:
                downloaded_chunks[idx] = content
                downloaded_bytes += len(content)
            completed_segs += 1

            pct = (completed_segs / total_segs) * 100
            bar = make_progress_bar(completed_segs, total_segs)
            elapsed = max(time.time() - start_time, 0.1)
            speed = downloaded_bytes / elapsed
            speed_str = f"{format_bytes(speed)}/s"
            eta = (total_segs - completed_segs) / (completed_segs / elapsed) if completed_segs > 0 else 0
            eta_str = f"{int(eta)}s" if eta < 3600 else f"{int(eta//60)}m {int(eta%60)}s"

            sys.stdout.write(f"\r📥 Downloading: [{bar}] {pct:5.1f}% ({completed_segs}/{total_segs}) | {format_bytes(downloaded_bytes)} | ⚡ {speed_str} | ETA: {eta_str}")
            sys.stdout.flush()

        print("\n\n⚙️ Merging stream segments into TS container...")
        with open(temp_ts_path, "wb") as f:
            for idx in range(total_segs):
                chunk = downloaded_chunks.get(idx)
                if chunk:
                    f.write(chunk)

        downloaded_chunks.clear()
        gc.collect()

        print("⚡ Lossless Remuxing to Native MP4 (+faststart index)...")
        remux_ok = await asyncio.to_thread(remux_ts_to_mp4, temp_ts_path, output_mp4_path)

        if remux_ok and os.path.exists(output_mp4_path):
            if os.path.exists(temp_ts_path):
                os.remove(temp_ts_path)
        else:
            if os.path.exists(temp_ts_path):
                if os.path.exists(output_mp4_path):
                    os.remove(output_mp4_path)
                os.rename(temp_ts_path, output_mp4_path)

        file_size = os.path.getsize(output_mp4_path)
        total_time = time.time() - start_time
        print(f"\n🎉 Download Complete!")
        print(f"📁 Saved to: {os.path.abspath(output_mp4_path)}")
        print(f"💾 File Size: {format_bytes(file_size)}")
        print(f"⏱️ Total Time: {total_time:.1f}s (Avg Speed: {format_bytes(file_size/total_time)}/s)\n")
        return True

# ==========================================
# MAIN INTERACTIVE CLI
# ==========================================
async def main_async():
    print("=" * 65)
    print("🚀 TURBO HIGH-SPEED M3U8 VIDEO DOWNLOADER (STANDALONE)")
    print("=" * 65)

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Auto-detect any .txt playlist file in the current directory or parent
    txt_files = [f for f in os.listdir(script_dir) if f.endswith(".txt")]

    playlist_path = None
    if len(txt_files) == 1:
        playlist_path = os.path.join(script_dir, txt_files[0])
        print(f"📁 Auto-detected playlist file: {txt_files[0]}")
    elif len(txt_files) > 1:
        print("\nFound multiple playlist files:")
        for i, tf in enumerate(txt_files, 1):
            print(f"  [{i}] {tf}")
        sel = input(f"Select playlist file (1-{len(txt_files)}) or press Enter to input path: ").strip()
        if sel.isdigit() and 1 <= int(sel) <= len(txt_files):
            playlist_path = os.path.join(script_dir, txt_files[int(sel) - 1])

    if not playlist_path or not os.path.exists(playlist_path):
        user_input = input("\n📁 Enter path to your course .txt file OR paste direct stream URL: ").strip().strip('"').strip("'")
        if user_input.startswith("http"):
            out_name = input("Enter output filename (default: video.mp4): ").strip() or "video.mp4"
            if not out_name.endswith(".mp4"):
                out_name += ".mp4"
            out_path = os.path.join(script_dir, out_name)
            await download_video_fast(user_input, out_path, title="Direct URL")
            return
        playlist_path = user_input

    videos = parse_txt_file(playlist_path)
    if not videos:
        print("❌ Could not parse any valid video entries from the file.")
        return

    print(f"\n✅ Loaded {len(videos)} verified video lectures!")
    first_cat = videos[0]["category"].split(">")[0].strip()
    print(f"🏷️ Course Batch: {first_cat}\n")

    print("Options:")
    print("  • Enter a single number (e.g. 5) to download that lecture")
    print("  • Enter a range (e.g. 1-10) to batch download multiple lectures")
    print("  • Enter 'list' to browse all lecture titles")
    print("  • Enter 'all' to download the entire course")

    choice = input("\n👉 Enter your choice: ").strip()

    if choice.lower() == "list":
        for v in videos:
            print(f"#{v['index']:03d}: {v['title']} ({v['category'].split('>')[-1].strip()})")
        choice = input("\n👉 Enter lecture number or range to download: ").strip()

    quality = input("Enter quality (1080p, 720p, 480p, 360p, best) [default: 1080p]: ").strip() or "1080p"
    output_dir = os.path.join(script_dir, "Downloads")
    os.makedirs(output_dir, exist_ok=True)

    if choice.lower() == "all":
        selected_videos = videos
    elif "-" in choice:
        try:
            s_idx, e_idx = map(int, choice.split("-"))
            selected_videos = videos[s_idx - 1:e_idx]
        except Exception:
            print("❌ Invalid range format.")
            return
    elif choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(videos):
            selected_videos = [videos[idx - 1]]
        else:
            print("❌ Number out of range.")
            return
    else:
        print("❌ Invalid selection.")
        return

    print(f"\n🚀 Queued {len(selected_videos)} video(s) for download in {quality}!\n")

    async with aiohttp.ClientSession() as session:
        for i, v in enumerate(selected_videos, 1):
            print(f"\n[{i}/{len(selected_videos)}] Processing #{v['index']:03d}: {v['title']}")
            clean_title = "".join(c for c in v["title"] if c.isalnum() or c in (" ", "-", "_")).strip()
            filename = f"{v['index']:03d}_{clean_title}.mp4"
            filepath = os.path.join(output_dir, filename)

            stream_url = await resolve_quality_url(session, v["url"], quality)
            await download_video_fast(stream_url, filepath, title=v["title"])

    print("\n🎉 ALL SELECTED VIDEOS DOWNLOADED SUCCESSFULLY!")
    print(f"📂 Output Folder: {os.path.abspath(output_dir)}\n")

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
