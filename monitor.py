from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit, urlunsplit

import boto3
import httpx
import instaloader
from botocore.exceptions import ClientError
from playwright.sync_api import sync_playwright

LOG = logging.getLogger("reel-monitor")
COURTESY = "Courtesy"
HTTP_CLIENT = httpx.Client(
    timeout=httpx.Timeout(60.0, connect=30.0),
    transport=httpx.HTTPTransport(retries=3),
    follow_redirects=True,
)


def redacted_url(url: str) -> str:
    """Return a log-safe URL without query parameters or fragments."""
    try:
        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return "<redacted-url>"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except (TypeError, ValueError):
        return "<redacted-url>"


def safe_request(method: str, url: str, **kwargs: Any) -> httpx.Response | None:
    """Make an HTTP request without allowing transient connection failures to stop a run."""
    retry_delays = (5, 10, 15)
    log_url = redacted_url(url)
    for attempt in range(4):
        try:
            return HTTP_CLIENT.request(method, url, **kwargs)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
            LOG.warning(
                "HTTP %s %s failed (attempt %d/4): %s",
                method.upper(),
                log_url,
                attempt + 1,
                exc,
            )
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
    LOG.error("HTTP %s %s failed after 3 retries; skipping", method.upper(), log_url)
    return None


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def run_command(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-1500:]}")


@dataclass(frozen=True)
class Profile:
    instagram: str
    app_user: str
    state: str
    city: str
    city_code: str


@dataclass(frozen=True)
class Reel:
    reel_id: str
    profile: Profile
    url: str
    caption: str
    published_at: str
    video_url: str
    audio_url: str | None
    thumbnail_url: str


class StateStore:
    def __init__(self, s3: Any, bucket: str, key: str) -> None:
        self.s3, self.bucket, self.key = s3, bucket, key

    def load(self) -> dict[str, Any] | None:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return json.loads(response["Body"].read())

    def save(self, state: dict[str, Any]) -> None:
        state["updatedAt"] = datetime.now(UTC).isoformat()
        self.s3.put_object(
            Bucket=self.bucket,
            Key=self.key,
            Body=json.dumps(state, indent=2, sort_keys=True).encode(),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )


class Monitor:
    def __init__(self) -> None:
        self.api_url = required("CLIP_API_URL")
        self.api_token = os.getenv("CLIP_API_TOKEN", "").strip()
        self.bucket = required("S3_BUCKET")
        self.public_base = required("PUBLIC_BASE_URL").rstrip("/")
        self.scan_limit = int(os.getenv("SCAN_LIMIT", "5"))
        self.browser_scan_limit = min(
            self.scan_limit, int(os.getenv("BROWSER_SCAN_LIMIT", "5"))
        )
        self.max_new = int(os.getenv("MAX_NEW_REELS_PER_PROFILE", "2"))
        self.backfill_existing = os.getenv("BACKFILL_EXISTING_REELS", "true").casefold() in {
            "1",
            "true",
            "yes",
        }
        self.max_publish_per_run = int(os.getenv("MAX_PUBLISH_PER_RUN", "0"))
        self.publish_baseline_reels = os.getenv("PUBLISH_BASELINE_REELS", "").casefold() in {
            "1",
            "true",
            "yes",
        }
        self.profile_batch_size = int(os.getenv("PROFILE_BATCH_SIZE", "5"))
        self.session_username = required("INSTAGRAM_SESSION_USERNAME")
        self.session_file = Path(required("INSTAGRAM_SESSION_FILE"))
        endpoint = os.getenv("S3_ENDPOINT_URL", "").strip() or None
        self.s3 = boto3.client(
            "s3", region_name=os.getenv("S3_REGION", "ap-south-1"), endpoint_url=endpoint
        )
        self.state_store = StateStore(
            self.s3,
            self.bucket,
            os.getenv("STATE_OBJECT_KEY", "automation/instagram-reel-monitor/state.json"),
        )
        self.loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
            max_connection_attempts=2,
            request_timeout=45,
            fatal_status_codes=[401, 403, 429],
        )
        self.loader.load_session_from_file(self.session_username, self.session_file)
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)
        self.browser_context = self.browser.new_context()
        self.browser_context.add_cookies(
            [
                {
                    "name": name,
                    "value": str(value),
                    "domain": ".instagram.com",
                    "path": "/",
                }
                for name, value in self.loader.context._session.cookies.get_dict().items()
            ]
        )

    @staticmethod
    def load_profiles() -> list[Profile]:
        data = json.loads(Path("profiles.json").read_text(encoding="utf-8"))
        profiles: list[Profile] = []
        seen: set[str] = set()
        for raw in data["profiles"]:
            if not raw.get("enabled", True):
                continue
            username = raw["instagramUsername"].strip().lstrip("@")
            key = username.casefold()
            if key in seen:
                raise ValueError(f"Duplicate enabled Instagram profile: {username}")
            seen.add(key)
            profiles.append(
                Profile(
                    instagram=username,
                    app_user=str(raw["createdByUsername"]),
                    state=raw.get("state", ""),
                    city=raw.get("city", ""),
                    city_code=raw.get("cityCode") or slug(raw.get("city", "")),
                )
            )
        return profiles

    @staticmethod
    def optional_attribute(page: Any, selector: str, attribute: str) -> str | None:
        locator = page.locator(selector)
        if locator.count() == 0:
            return None
        return locator.first.get_attribute(attribute, timeout=3_000)

    @staticmethod
    def embedded_media_urls(page: Any) -> tuple[str | None, str | None]:
        direct = Monitor.optional_attribute(page, "video[src]", "src")
        video_url = direct if direct and direct.startswith("http") else None
        html = page.content()
        if not video_url:
            for pattern in (
                r'"video_url"\s*:\s*"(https:[^"]+)"',
                r'"contentUrl"\s*:\s*"(https:[^"]+)"',
            ):
                match = re.search(pattern, html)
                if match:
                    video_url = match.group(1).replace(r"\u0026", "&").replace(r"\/", "/")
                    break
        resources = page.evaluate("() => performance.getEntriesByType('resource').map(e => e.name)")
        audio_url = None
        for url in resources:
            if not isinstance(url, str) or not any(
                host in url.lower() for host in ("fbcdn", "cdninstagram")
            ):
                continue
            descriptor = ""
            encoded = parse_qs(urlsplit(url).query).get("efg", [""])[0]
            if encoded:
                try:
                    descriptor = base64.b64decode(encoded + "===").decode(errors="ignore").lower()
                except Exception:
                    pass
            lower_url = url.lower()
            is_image = any(
                marker in descriptor for marker in ("cover", "image", "thumbnail")
            ) or any(
                lower_url.split("?", 1)[0].endswith(ext)
                for ext in (".jpg", ".jpeg", ".png", ".webp")
            )
            if "audio" in descriptor and audio_url is None:
                audio_url = url
            elif (
                not is_image
                and ("dash" in descriptor or "video" in descriptor or ".mp4" in lower_url)
                and video_url is None
            ):
                video_url = url
        return video_url, audio_url

    @staticmethod
    def looks_like_video_url(url: str | None) -> bool:
        if not url or not url.startswith("http"):
            return False
        path = urlsplit(url).path.casefold()
        return not path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))

    @staticmethod
    def browser_timestamp(page: Any) -> str | None:
        """Read a Reel timestamp from DOM, Open Graph, or embedded JSON."""
        for selector, attribute in (
            ("time[datetime]", "datetime"),
            ("meta[property='article:published_time']", "content"),
            ("meta[itemprop='uploadDate']", "content"),
        ):
            value = Monitor.optional_attribute(page, selector, attribute)
            if value:
                return value
        html = page.content()
        for pattern in (
            r'"uploadDate"\s*:\s*"([^"]+)"',
            r'"datePublished"\s*:\s*"([^"]+)"',
            r'"taken_at_timestamp"\s*:\s*(\d+)',
            r'"taken_at"\s*:\s*(\d+)',
        ):
            match = re.search(pattern, html)
            if not match:
                continue
            value = match.group(1)
            if value.isdigit():
                return datetime.fromtimestamp(int(value), UTC).isoformat()
            return value.replace(r"\u002B", "+")
        return None

    @staticmethod
    def contains_video(post: Any) -> bool:
        if bool(getattr(post, "is_video", False)):
            return True
        if getattr(post, "typename", "") == "GraphSidecar":
            return any(node.is_video for node in post.get_sidecar_nodes())
        return False

    def discover_instaloader(self, profile: Profile) -> list[Reel]:
        """Use Instagram's authenticated metadata API before scraping changing HTML."""
        remote = instaloader.Profile.from_username(self.loader.context, profile.instagram)
        if remote.is_private and not remote.followed_by_viewer:
            raise PermissionError(f"@{profile.instagram} is private")

        candidates: dict[str, Any] = {}
        errors: list[Exception] = []
        for iterator_factory in (remote.get_reels, remote.get_posts):
            try:
                for post in islice(iterator_factory(), self.scan_limit):
                    if self.contains_video(post):
                        candidates.setdefault(post.shortcode, post)
            except Exception as exc:
                errors.append(exc)

        reels: list[Reel] = []
        for post in candidates.values():
            try:
                video_url = str(post.video_url)
                if not video_url.startswith("http"):
                    continue
                published = post.date_utc.replace(tzinfo=UTC).isoformat()
                reels.append(
                    Reel(
                        reel_id=post.shortcode,
                        profile=profile,
                        url=f"https://www.instagram.com/reel/{post.shortcode}/",
                        caption=post.caption or "",
                        published_at=published,
                        video_url=video_url,
                        audio_url=None,
                        thumbnail_url=str(post.url) if post.url else "",
                    )
                )
            except Exception as exc:
                LOG.warning(
                    "Skipping unreadable metadata Reel %s from @%s: %s",
                    getattr(post, "shortcode", "unknown"),
                    profile.instagram,
                    exc,
                )
        if not reels:
            detail = f": {errors[-1]}" if errors else ""
            raise RuntimeError(f"No readable metadata reels found for @{profile.instagram}{detail}")
        return sorted(reels, key=lambda item: item.published_at, reverse=True)

    @staticmethod
    def full_media_url(url: str) -> str:
        parts = urlsplit(url)
        query = [
            (name, value)
            for name, value in parse_qsl(parts.query, keep_blank_values=True)
            if name.casefold() not in {"bytestart", "byteend"}
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    def discover_browser(self, profile: Profile) -> list[Reel]:
        page = self.browser_context.new_page()
        try:
            page.goto(
                f"https://www.instagram.com/{profile.instagram}/reels/",
                wait_until="domcontentloaded",
                timeout=45_000,
            )
            body = page.locator("body").inner_text().lower()
            if "this account is private" in body:
                raise PermissionError(f"@{profile.instagram} is private")
            if "challenge" in page.url or "verify your identity" in body or "captcha" in body:
                raise RuntimeError("Instagram login challenge requires manual session renewal")
            page.wait_for_selector("a[href*='/reel/']", timeout=30_000)
            hrefs = page.locator("a[href*='/reel/']").evaluate_all(
                "els => [...new Set(els.map(e => e.href))]"
            )
        finally:
            page.close()

        reels: list[Reel] = []
        # Browser fallback is intentionally shallow: opening every historical
        # Reel page would exceed a workflow run. Authenticated metadata remains
        # the primary deep backfill path.
        for url in hrefs[: self.browser_scan_limit]:
            match = re.search(r"/reel/([^/?#]+)/?", url)
            if not match:
                continue
            detail = self.browser_context.new_page()
            try:
                try:
                    detail.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    detail.wait_for_timeout(3_000)
                    videos = detail.locator("video")
                    if videos.count():
                        videos.first.evaluate(
                            "v => { v.muted = true; return v.play().catch(() => null); }"
                        )
                        detail.wait_for_timeout(2_000)
                    published = self.browser_timestamp(detail)
                    resource_video_url, audio_url = self.embedded_media_urls(detail)
                    video_url = (
                        self.optional_attribute(detail, "meta[property='og:video']", "content")
                        or resource_video_url
                    )
                    thumbnail_url = self.optional_attribute(
                        detail, "meta[property='og:image']", "content"
                    )
                    description = self.optional_attribute(
                        detail, "meta[property='og:description']", "content"
                    )
                    title = self.optional_attribute(detail, "meta[property='og:title']", "content")
                    if not published or not self.looks_like_video_url(video_url):
                        raise RuntimeError("timestamp or downloadable video URL is unavailable")
                    caption = ""
                    for value in (title, description):
                        if not value:
                            continue
                        caption_match = re.search(
                            r'(?:on Instagram|\d{4}):\s*["“](.*?)["”]\.?(?:\s|$)',
                            value,
                            re.DOTALL,
                        )
                        if caption_match:
                            caption = caption_match.group(1).strip()
                            break
                    reels.append(
                        Reel(
                            reel_id=match.group(1),
                            profile=profile,
                            url=f"https://www.instagram.com/reel/{match.group(1)}/",
                            caption=caption,
                            published_at=datetime.fromisoformat(published)
                            .astimezone(UTC)
                            .isoformat(),
                            video_url=video_url,
                            audio_url=audio_url,
                            thumbnail_url=thumbnail_url or "",
                        )
                    )
                except Exception as exc:
                    LOG.warning(
                        "Skipping unreadable Reel %s from @%s: %s",
                        match.group(1),
                        profile.instagram,
                        exc,
                    )
            finally:
                detail.close()
        if not reels:
            raise RuntimeError(f"No readable reels found for @{profile.instagram}")
        return sorted(reels, key=lambda item: item.published_at, reverse=True)

    def discover(self, profile: Profile) -> list[Reel]:
        metadata_error: Exception | None = None
        try:
            return self.discover_instaloader(profile)
        except Exception as exc:
            metadata_error = exc
            LOG.warning("Metadata discovery failed for @%s: %s", profile.instagram, exc)
        try:
            return self.discover_browser(profile)
        except Exception as browser_error:
            raise RuntimeError(
                f"Metadata and browser discovery failed for @{profile.instagram}. "
                f"Metadata: {metadata_error}; browser: {browser_error}"
            ) from browser_error

    def download(self, url: str, destination: Path) -> bool:
        url = self.full_media_url(url)
        headers = {
            "User-Agent": self.loader.context.user_agent,
            "Referer": "https://www.instagram.com/",
            "Range": "bytes=0-",
        }
        cookies = self.loader.context._session.cookies.get_dict()
        response = safe_request("GET", url, headers=headers, cookies=cookies)
        if response is None:
            LOG.error("Media download failed for %s; skipping reel", redacted_url(url))
            return False
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").casefold()
        if destination.suffix.casefold() == ".mp4" and content_type.startswith("image/"):
            raise RuntimeError(f"Instagram returned {content_type} instead of video data")
        destination.write_bytes(response.content)
        if destination.stat().st_size < 10_000:
            raise RuntimeError("Instagram media download is unexpectedly small")
        return True

    @staticmethod
    def normalize(source: Path, destination: Path, audio_source: Path | None = None) -> None:
        inputs = ["-i", str(source)]
        audio_map = ["-map", "1:a:0?"] if audio_source else ["-map", "0:a:0?"]
        if audio_source:
            inputs.extend(["-i", str(audio_source)])
        run_command(
            [
                "ffmpeg",
                "-y",
                *inputs,
                "-map",
                "0:v:0",
                *audio_map,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-profile:v",
                "high",
                "-level:v",
                "4.1",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(destination),
            ]
        )

    @staticmethod
    def thumbnail(video: Path, destination: Path) -> None:
        run_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                "0.5",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(1080,iw)':-2",
                "-q:v",
                "2",
                str(destination),
            ]
        )

    @staticmethod
    def metadata(video: Path) -> dict[str, Any]:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of",
                "json",
                str(video),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        data = json.loads(result.stdout)
        stream = next(item for item in data["streams"] if item["codec_type"] == "video")
        if not any(item.get("codec_type") == "audio" for item in data["streams"]):
            raise RuntimeError("Final MP4 has no audio stream; refusing to publish a silent Reel")
        width, height = int(stream["width"]), int(stream["height"])
        return {
            "durationSeconds": float(data["format"]["duration"]),
            "width": width,
            "height": height,
            "aspectRatio": width / height,
            "mimeType": "video/mp4",
            "fileSizeBytes": video.stat().st_size,
        }

    def upload(self, path: Path, key: str, content_type: str) -> str:
        self.s3.upload_file(
            str(path),
            self.bucket,
            key,
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": "public, max-age=31536000, immutable",
            },
        )
        return f"{self.public_base}/{quote(key, safe='/')}"

    def publish(self, reel: Reel, video_url: str, thumbnail_url: str, meta: dict[str, Any]) -> bool:
        cleaned = " ".join(reel.caption.split())
        courtesy = f"{COURTESY} @{reel.profile.instagram}"
        text = f"{courtesy}\n\n{cleaned}" if cleaned else courtesy
        payload = {
            "createdByUsername": reel.profile.app_user,
            "text": text,
            "clipMedia": {
                "url": video_url,
                "mediaType": "VIDEO",
                "thumbnailUrl": thumbnail_url,
                **meta,
            },
            "taggedUsernames": list(
                dict.fromkeys(re.findall(r"(?<![\w@])@([A-Za-z0-9._]+)", cleaned))
            ),
            "taggedHashtags": list(dict.fromkeys(re.findall(r"(?<!\w)#([\w.]+)", cleaned))),
            "isLargeMultimedia": False,
            "makeLive": True,
            "cityCode": reel.profile.city_code,
        }
        idempotency_suffix = os.getenv("IDEMPOTENCY_SUFFIX", "").strip()
        # Match the key used by the manual publisher so the API can reject a
        # Reel that was already sent before this cloud automation saw it.
        idempotency_key = (
            f"instagram:{reel.profile.instagram.casefold()}:{reel.reel_id}"
        )
        if idempotency_suffix:
            idempotency_key = f"{idempotency_key}:{idempotency_suffix}"
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        # Publishing can take several minutes while the app validates the S3 media.
        # Keep the shared client defaults for discovery/download requests, but allow
        # this long-running API operation to finish instead of timing out at 60s.
        response = safe_request(
            "POST",
            self.api_url,
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(1200.0, connect=30.0),
        )
        if response is None:
            LOG.error("API publish failed for Reel %s; skipping", reel.reel_id)
            return False
        response.raise_for_status()
        return True

    def process(self, reel: Reel, state: dict[str, Any]) -> bool:
        with tempfile.TemporaryDirectory(prefix="reel-") as folder:
            work = Path(folder)
            source = work / "source.mp4"
            audio = work / "audio.mp4"
            video = work / "video.mp4"
            image = work / "thumbnail.jpg"
            if not self.download(reel.video_url, source):
                return False
            audio_source = None
            if reel.audio_url:
                if not self.download(reel.audio_url, audio):
                    return False
                audio_source = audio
            self.normalize(source, video, audio_source)
            self.thumbnail(video, image)
            if not image.exists() or image.stat().st_size < 1_000:
                if not reel.thumbnail_url:
                    raise RuntimeError("Thumbnail generation produced no image")
                if not self.download(reel.thumbnail_url, image):
                    return False
            meta = self.metadata(video)
            base = f"instagram/{safe(reel.profile.instagram)}/{safe(reel.reel_id)}"
            video_url = self.upload(video, f"{base}.mp4", "video/mp4")
            image_url = self.upload(image, f"{base}.jpg", "image/jpeg")

        # Reserve before POST. If the network response is lost, future runs must not duplicate it.
        state["reels"][reel.reel_id] = {
            "profile": reel.profile.instagram,
            "publishedAt": reel.published_at,
            "status": "PUBLISHING",
        }
        self.state_store.save(state)
        try:
            if not self.publish(reel, video_url, image_url, meta):
                del state["reels"][reel.reel_id]
                self.state_store.save(state)
                return False
        except Exception as exc:
            state["reels"][reel.reel_id]["status"] = "PUBLISH_UNKNOWN"
            state["reels"][reel.reel_id]["error"] = str(exc)[:500]
            self.state_store.save(state)
            raise
        state["reels"][reel.reel_id]["status"] = "PUBLISHED"
        self.state_store.save(state)
        LOG.info("Published %s from @%s", reel.reel_id, reel.profile.instagram)
        return True

    def migrate_state(self, state: dict[str, Any]) -> bool:
        """Recover post-deployment reels incorrectly absorbed by the staggered first baseline."""
        if int(state.get("version", 1)) >= 2:
            return False
        cutoff = datetime.fromisoformat(state["initializedAt"])
        priority = set(state.get("priorityProfiles", []))
        recovered = 0
        for reel_id, record in list(state.get("reels", {}).items()):
            published = record.get("publishedAt")
            if (
                record.get("status") == "BASELINE"
                and published
                and datetime.fromisoformat(published) > cutoff
            ):
                priority.add(str(record.get("profile", "")).casefold())
                del state["reels"][reel_id]
                recovered += 1
        state["version"] = 2
        state["priorityProfiles"] = sorted(item for item in priority if item)
        LOG.info("State migration recovered %d post-deployment Reel IDs", recovered)
        return True

    @staticmethod
    def is_publishable_record(record: dict[str, Any] | None, backfill: bool) -> bool:
        """Return whether a discovered Reel still needs publishing.

        BASELINE means the Reel was only observed, not sent to the app.  When
        backfill is enabled those records must remain eligible. All terminal or
        ambiguous publish states are treated as known so a retry can never
        create a duplicate.
        """
        if record is None:
            return True
        return backfill and record.get("status") == "BASELINE"

    def execute(self) -> None:
        profiles = self.load_profiles()
        only_city_code = os.getenv("ONLY_CITY_CODE", "").strip().casefold()
        if only_city_code:
            profiles = [
                profile for profile in profiles if profile.city_code.casefold() == only_city_code
            ]
            if not profiles:
                raise RuntimeError(f"No enabled profiles found for city code: {only_city_code}")
            LOG.info("Manual city filter active: %s (%d profiles)", only_city_code, len(profiles))
        state = self.state_store.load()
        if state is not None and self.migrate_state(state):
            self.state_store.save(state)
        cursor = int((state or {}).get("profileCursor", 0)) % len(profiles)
        batch_size = min(self.profile_batch_size, len(profiles))
        profile_by_name = {profile.instagram.casefold(): profile for profile in profiles}
        priority_names = list((state or {}).get("priorityProfiles", []))
        if priority_names:
            selected = [
                profile_by_name[name]
                for name in priority_names[:batch_size]
                if name in profile_by_name
            ]
        else:
            selected = [profiles[(cursor + offset) % len(profiles)] for offset in range(batch_size)]
        discovered: list[tuple[Profile, list[Reel]]] = []
        for profile in selected:
            try:
                discovered.append((profile, self.discover(profile)))
            except Exception as exc:
                LOG.error("Discovery failed for @%s: %s", profile.instagram, exc)
            time.sleep(2)

        if not discovered:
            raise RuntimeError("Instagram discovery failed for every configured profile")

        if state is None:
            state = {
                "version": 2,
                "initializedAt": datetime.now(UTC).isoformat(),
                "profilesBaselined": [],
                "profileCursor": (cursor + batch_size) % len(profiles),
                "reels": {},
            }
            for _, reels in discovered:
                for reel in reels:
                    state["reels"][reel.reel_id] = {
                        "profile": reel.profile.instagram,
                        "publishedAt": reel.published_at,
                        "status": "BASELINE",
                    }
            state["profilesBaselined"] = sorted(
                profile.instagram.casefold() for profile, _ in discovered
            )
            self.state_store.save(state)
            LOG.info(
                "Initialized baseline with %d Reel IDs", len(state["reels"])
            )
            if not self.backfill_existing:
                LOG.info("Backfill disabled; no baseline reels published")
                return

        known = set(state.get("reels", {}))
        if not priority_names:
            state["profileCursor"] = (cursor + batch_size) % len(profiles)
        baselined = set(state.get("profilesBaselined", []))
        queues: list[list[Reel]] = []
        state_changed = False
        deployment_cutoff = datetime.fromisoformat(state["initializedAt"])
        for profile, reels in discovered:
            profile_key = profile.instagram.casefold()
            if profile_key not in baselined:
                pending = []
                for reel in reels:
                    if datetime.fromisoformat(reel.published_at) <= deployment_cutoff:
                        state["reels"][reel.reel_id] = {
                            "profile": reel.profile.instagram,
                            "publishedAt": reel.published_at,
                            "status": "BASELINE",
                        }
                        known.add(reel.reel_id)
                        if self.backfill_existing:
                            pending.append(reel)
                    elif reel.reel_id not in known:
                        pending.append(reel)
                baselined.add(profile_key)
                state_changed = True
                LOG.info("Baselined newly added profile @%s", profile.instagram)
                if pending:
                    queues.append(pending[: self.max_new])
                continue
            pending = []
            for reel in reels:
                record = state.get("reels", {}).get(reel.reel_id)
                if self.is_publishable_record(
                    record, self.backfill_existing or self.publish_baseline_reels
                ):
                    pending.append(reel)
            if pending:
                queues.append(pending[: self.max_new])

        if state_changed:
            state["profilesBaselined"] = sorted(baselined)
            self.state_store.save(state)
        else:
            self.state_store.save(state)

        published = 0
        for index in range(max((len(queue) for queue in queues), default=0)):
            for queue in queues:
                if self.max_publish_per_run and published >= self.max_publish_per_run:
                    break
                if index >= len(queue):
                    continue
                reel = queue[index]
                try:
                    if self.process(reel, state):
                        published += 1
                except Exception as exc:
                    LOG.error(
                        "Processing failed for Reel %s from @%s; continuing: %s",
                        reel.reel_id,
                        reel.profile.instagram,
                        exc,
                        exc_info=True,
                    )
            if self.max_publish_per_run and published >= self.max_publish_per_run:
                break
        if priority_names:
            successful = {profile.instagram.casefold() for profile, _ in discovered}
            state["priorityProfiles"] = [name for name in priority_names if name not in successful]
            self.state_store.save(state)
        LOG.info(
            "Run finished; published=%d scanned=%d total_profiles=%d",
            published,
            len(selected),
            len(profiles),
        )

    def close(self) -> None:
        self.browser_context.close()
        self.browser.close()
        self.playwright.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    monitor = Monitor()
    try:
        monitor.execute()
    finally:
        monitor.close()
        HTTP_CLIENT.close()
