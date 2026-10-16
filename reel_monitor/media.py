from __future__ import annotations

import json
import logging
import mimetypes
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .http_client import ResilientHttpClient
from .models import MediaMetadata

LOG = logging.getLogger(__name__)


class MediaProcessor:
    SUPPORTED_CONTENT_TYPES = {
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "application/octet-stream",
    }

    def __init__(self, http: ResilientHttpClient, max_bytes: int) -> None:
        self.http = http
        self.max_bytes = max_bytes

    def download(self, url: str, destination: Path) -> None:
        response = self.http.request(
            "GET",
            url,
            headers={"Accept": "video/*,application/octet-stream;q=0.8"},
            timeout=120.0,
        )
        if response is None:
            raise RuntimeError("Video download exhausted retries")
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
        if (
            content_type not in self.SUPPORTED_CONTENT_TYPES
            and not content_type.startswith("video/")
        ):
            guessed = mimetypes.guess_type(urlsplit(url).path)[0]
            if guessed not in self.SUPPORTED_CONTENT_TYPES:
                raise ValueError(f"Unsupported media content type: {content_type or 'missing'}")
        declared = int(response.headers.get("content-length", "0") or 0)
        if declared > self.max_bytes:
            raise ValueError(f"Video exceeds MAX_VIDEO_BYTES ({declared} bytes)")
        data = response.content
        if not data:
            raise ValueError("Downloaded video is empty")
        if len(data) > self.max_bytes:
            raise ValueError(f"Video exceeds MAX_VIDEO_BYTES ({len(data)} bytes)")
        destination.write_bytes(data)

    @staticmethod
    def _run(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{args[0]} failed: {result.stderr[-1500:]}")
        return result

    def normalize(self, source: Path, destination: Path) -> None:
        self._run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                str(destination),
            ]
        )

    def thumbnail(self, video: Path, destination: Path) -> None:
        self._run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "0.5",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(destination),
            ],
            timeout=120,
        )

    def validate(self, video: Path) -> MediaMetadata:
        if not video.exists() or video.stat().st_size < 1024:
            raise ValueError("Normalized video is missing or too small")
        result = self._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_type,width,height",
                "-of",
                "json",
                str(video),
            ],
            timeout=60,
        )
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        visual = next((row for row in streams if row.get("codec_type") == "video"), None)
        if not visual:
            raise ValueError("Downloaded file contains no video stream")
        duration = float(payload.get("format", {}).get("duration", 0))
        if duration <= 0:
            raise ValueError("Video duration is invalid")
        width = int(visual.get("width", 0))
        height = int(visual.get("height", 0))
        if width <= 0 or height <= 0:
            raise ValueError("Video dimensions are invalid")
        return MediaMetadata(
            duration_seconds=duration,
            width=width,
            height=height,
            file_size_bytes=video.stat().st_size,
        )
