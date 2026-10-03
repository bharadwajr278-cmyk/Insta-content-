from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
import httpx
import instaloader
from botocore.exceptions import ClientError
from playwright.sync_api import sync_playwright

LOG = logging.getLogger("reel-monitor")
COURTESY = "Courtesy"


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
        self.max_new = int(os.getenv("MAX_NEW_REELS_PER_PROFILE", "2"))
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
    def embedded_video_url(page: Any) -> str | None:
        direct = Monitor.optional_attribute(page, "video[src]", "src")
        if direct and direct.startswith("http"):
            return direct
        html = page.content()
        for pattern in (
            r'"video_url"\s*:\s*"(https:[^"]+)"',
            r'"contentUrl"\s*:\s*"(https:[^"]+)"',
        ):
            match = re.search(pattern, html)
            if match:
                return match.group(1).replace(r"\u0026", "&").replace(r"\/", "/")
        resources = page.evaluate(
            "() => performance.getEntriesByType('resource').map(e => e.name)"
        )
        return next(
            (
                url
                for url in resources
                if isinstance(url, str)
                and (".mp4" in url.lower() or "fbcdn.net" in url.lower() and "video" in url.lower())
            ),
            None,
        )

    def discover(self, profile: Profile) -> list[Reel]:
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
        for url in hrefs[: self.scan_limit]:
            match = re.search(r"/reel/([^/?#]+)/?", url)
            if not match:
                continue
            detail = self.browser_context.new_page()
            try:
                try:
                    detail.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    detail.wait_for_selector("time[datetime]", timeout=25_000)
                    published = detail.locator("time[datetime]").first.get_attribute("datetime")
                    video_url = self.optional_attribute(
                        detail, "meta[property='og:video']", "content"
                    ) or self.embedded_video_url(detail)
                    thumbnail_url = self.optional_attribute(
                        detail, "meta[property='og:image']", "content"
                    )
                    description = self.optional_attribute(
                        detail, "meta[property='og:description']", "content"
                    )
                    title = self.optional_attribute(
                        detail, "meta[property='og:title']", "content"
                    )
                    if not published or not video_url:
                        raise RuntimeError("timestamp or downloadable video URL is unavailable")
                    caption = ""
                    for value in (title, description):
                        if not value:
                            continue
                        caption_match = re.search(
                            r'(?:on Instagram|\d{4}):\s*["“](.*?)["”]\.?(?:\s|$)',
                            value,
                            re.S,
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

    def download(self, url: str, destination: Path) -> None:
        headers = {
            "User-Agent": self.loader.context.user_agent,
            "Referer": "https://www.instagram.com/",
        }
        cookies = self.loader.context._session.cookies.get_dict()
        with httpx.Client(timeout=180, follow_redirects=True, headers=headers, cookies=cookies) as c:
            with c.stream("GET", url) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        if destination.stat().st_size < 10_000:
            raise RuntimeError("Instagram media download is unexpectedly small")

    @staticmethod
    def normalize(source: Path, destination: Path) -> None:
        run_command(
            [
                "ffmpeg", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt",
                "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                str(destination),
            ]
        )

    @staticmethod
    def thumbnail(video: Path, destination: Path) -> None:
        run_command(
            [
                "ffmpeg", "-y", "-ss", "0.5", "-i", str(video), "-frames:v", "1",
                "-vf", "scale='min(1080,iw)':-2", "-q:v", "2", str(destination),
            ]
        )

    @staticmethod
    def metadata(video: Path) -> dict[str, Any]:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height", "-of", "json", str(video),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        data = json.loads(result.stdout)
        stream = next(item for item in data["streams"] if item["codec_type"] == "video")
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
            str(path), self.bucket, key,
            ExtraArgs={"ContentType": content_type, "CacheControl": "public, max-age=31536000, immutable"},
        )
        return f"{self.public_base}/{quote(key, safe='/')}"

    def publish(self, reel: Reel, video_url: str, thumbnail_url: str, meta: dict[str, Any]) -> None:
        cleaned = " ".join(reel.caption.split())
        courtesy = f"{COURTESY} @{reel.profile.instagram}"
        text = f"{courtesy}\n\n{cleaned}" if cleaned else courtesy
        payload = {
            "createdByUsername": reel.profile.app_user,
            "text": text,
            "clipMedia": {"url": video_url, "mediaType": "VIDEO", "thumbnailUrl": thumbnail_url, **meta},
            "taggedUsernames": list(dict.fromkeys(re.findall(r"(?<![\w@])@([A-Za-z0-9._]+)", cleaned))),
            "taggedHashtags": list(dict.fromkeys(re.findall(r"(?<!\w)#([\w.]+)", cleaned))),
            "isLargeMultimedia": False,
            "makeLive": True,
            "cityCode": reel.profile.city_code,
        }
        headers = {"Content-Type": "application/json", "Idempotency-Key": f"instagram:{reel.reel_id}"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        response = httpx.post(self.api_url, json=payload, headers=headers, timeout=90)
        response.raise_for_status()

    def process(self, reel: Reel, state: dict[str, Any]) -> None:
        with tempfile.TemporaryDirectory(prefix="reel-") as folder:
            work = Path(folder)
            source, video, image = work / "source.mp4", work / "video.mp4", work / "thumbnail.jpg"
            self.download(reel.video_url, source)
            self.normalize(source, video)
            self.thumbnail(video, image)
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
            self.publish(reel, video_url, image_url, meta)
        except Exception as exc:
            state["reels"][reel.reel_id]["status"] = "PUBLISH_UNKNOWN"
            state["reels"][reel.reel_id]["error"] = str(exc)[:500]
            self.state_store.save(state)
            raise
        state["reels"][reel.reel_id]["status"] = "PUBLISHED"
        self.state_store.save(state)
        LOG.info("Published %s from @%s", reel.reel_id, reel.profile.instagram)

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

    def execute(self) -> None:
        profiles = self.load_profiles()
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
                "version": 1,
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
            LOG.info("Initialized baseline with %d Reel IDs; no old reels published", len(state["reels"]))
            return

        known = set(state.get("reels", {}))
        if priority_names:
            successful = {profile.instagram.casefold() for profile, _ in discovered}
            state["priorityProfiles"] = [
                name for name in priority_names if name not in successful
            ]
        else:
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
                if reel.reel_id in known:
                    break
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
                if index >= len(queue):
                    continue
                self.process(queue[index], state)
                published += 1
        LOG.info("Run finished; published=%d scanned=%d total_profiles=%d", published, len(selected), len(profiles))

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
