from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from monitor import Monitor, Profile, Reel, safe


LOG = logging.getLogger("url-batch")


def download_reel(url: str, work: Path) -> tuple[Path, dict]:
    output = work / "source.%(ext)s"
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-warnings",
        "--write-info-json",
        "--no-write-comments",
        "-f",
        "bestvideo*+bestaudio/best",
        "--merge-output-format",
        "mp4",
        "-o",
        str(output),
        url,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"yt-dlp failed: {result.stderr[-1500:]}")
    info_files = list(work.glob("source.info.json"))
    if not info_files:
        raise RuntimeError("yt-dlp did not produce metadata")
    info = json.loads(info_files[0].read_text(encoding="utf-8"))
    candidates = [p for p in work.glob("source.*") if p.suffix.casefold() in {".mp4", ".mkv", ".webm"}]
    if not candidates:
        raise RuntimeError("yt-dlp did not produce a video")
    return max(candidates, key=lambda p: p.stat().st_size), info


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = json.loads(Path(os.environ["URL_BATCH_FILE"]).read_text(encoding="utf-8"))
    monitor = Monitor()
    state = monitor.state_store.load()
    if state is None:
        raise RuntimeError("Monitor state is missing")
    published = failed = 0
    force_republish = os.getenv("FORCE_REPUBLISH", "").strip().casefold() in {
        "1",
        "true",
        "yes",
    }
    skipped_ids = {
        item.strip()
        for item in os.getenv("SKIP_REEL_IDS", "").split(",")
        if item.strip()
    }
    only_ids = {
        item.strip()
        for item in os.getenv("ONLY_REEL_IDS", "").split(",")
        if item.strip()
    }
    rows = config["profiles"]
    for position in range(config.get("reelsPerProfile", 5)):
        for row in rows:
            links = row.get("reels", [])
            if position >= len(links):
                continue
            url = links[position]
            reel_id = url.rstrip("/").split("/")[-1]
            if only_ids and reel_id not in only_ids:
                continue
            if reel_id in skipped_ids:
                LOG.info("Skipping explicitly excluded Reel %s", reel_id)
                continue
            if not force_republish and reel_id in state.get("reels", {}):
                LOG.info("Skipping known Reel %s", reel_id)
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="url-reel-") as folder:
                    work = Path(folder)
                    source, info = download_reel(url, work)
                    video = work / "video.mp4"
                    image = work / "thumbnail.jpg"
                    monitor.normalize(source, video)
                    monitor.thumbnail(video, image)
                    meta = monitor.metadata(video)
                    profile = Profile(
                        instagram=row["instagramUsername"],
                        app_user=config["appUser"],
                        state=config["state"],
                        city=config["city"],
                        city_code=config["cityCode"],
                    )
                    timestamp = info.get("timestamp")
                    published_at = (
                        datetime.fromtimestamp(timestamp, UTC).isoformat()
                        if timestamp
                        else datetime.now(UTC).isoformat()
                    )
                    reel = Reel(
                        reel_id=reel_id,
                        profile=profile,
                        url=url,
                        caption=info.get("description") or "",
                        published_at=published_at,
                        video_url="",
                        audio_url=None,
                        thumbnail_url=info.get("thumbnail") or "",
                    )
                    base = f"instagram/{safe(profile.instagram)}/{safe(reel_id)}"
                    video_url = monitor.upload(video, f"{base}.mp4", "video/mp4")
                    image_url = monitor.upload(image, f"{base}.jpg", "image/jpeg")
                state["reels"][reel_id] = {
                    "profile": profile.instagram,
                    "publishedAt": published_at,
                    "status": "PUBLISHING",
                }
                monitor.state_store.save(state)
                monitor.publish(reel, video_url, image_url, meta)
                state["reels"][reel_id]["status"] = "PUBLISHED"
                monitor.state_store.save(state)
                published += 1
                LOG.info("Published %s from @%s", reel_id, profile.instagram)
            except Exception:
                failed += 1
                LOG.exception("Failed %s from @%s", reel_id, row["instagramUsername"])
    LOG.info("URL batch complete: published=%d failed=%d", published, failed)
    if failed:
        raise RuntimeError(f"URL batch completed with {failed} failures")


if __name__ == "__main__":
    main()
