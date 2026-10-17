from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Profile


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def _integer(name: str, default: int, minimum: int = 0) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().casefold() in {"1", "true", "yes", "on"}


def city_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


@dataclass(frozen=True, slots=True)
class Settings:
    profiles_path: Path
    database_path: Path
    database_object_key: str
    legacy_state_object_key: str
    clip_api_url: str
    clip_api_token: str
    s3_bucket: str
    s3_region: str
    s3_endpoint_url: str | None
    public_base_url: str
    instagram_session_username: str
    instagram_session_file: Path
    instagram_session_id: str
    instagram_provider: str
    instagram_graph_access_token: str
    instagram_graph_caller_user_id: str
    instagram_graph_api_version: str
    scan_limit: int
    accounts_per_run: int
    max_new_per_profile: int
    max_publish_per_run: int
    lookback_hours: int
    max_video_bytes: int
    request_attempts: int
    only_city_code: str
    mode: str
    backfill_since: str
    dry_run: bool

    @classmethod
    def from_env(cls) -> Settings:
        mode = os.getenv("SYNC_MODE", "incremental").strip().casefold()
        if mode not in {"incremental", "backfill", "baseline"}:
            raise ValueError("SYNC_MODE must be incremental, backfill, or baseline")
        provider = os.getenv("INSTAGRAM_PROVIDER", "instaloader").strip().casefold()
        if provider not in {"instaloader", "graph"}:
            raise ValueError("INSTAGRAM_PROVIDER must be instaloader or graph")
        session_username = os.getenv("INSTAGRAM_SESSION_USERNAME", "").strip()
        session_file = Path(os.getenv("INSTAGRAM_SESSION_FILE", "data/instagram-session"))
        if provider == "instaloader" and not session_username:
            raise RuntimeError(
                "Required environment variable is missing: INSTAGRAM_SESSION_USERNAME"
            )
        return cls(
            profiles_path=Path(os.getenv("PROFILES_FILE", "profiles.json")),
            database_path=Path(os.getenv("DATABASE_PATH", "data/monitor.db")),
            database_object_key=os.getenv(
                "DATABASE_OBJECT_KEY", "automation/instagram-reel-monitor/monitor.db"
            ).strip(),
            legacy_state_object_key=os.getenv(
                "LEGACY_STATE_OBJECT_KEY", "automation/instagram-reel-monitor/state.json"
            ).strip(),
            clip_api_url=_required("CLIP_API_URL"),
            clip_api_token=os.getenv("CLIP_API_TOKEN", "").strip(),
            s3_bucket=_required("S3_BUCKET"),
            s3_region=os.getenv("S3_REGION", "ap-south-1").strip(),
            s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", "").strip() or None,
            public_base_url=_required("PUBLIC_BASE_URL").rstrip("/"),
            instagram_session_username=session_username,
            instagram_session_file=session_file,
            instagram_session_id=os.getenv("INSTAGRAM_SESSION_ID", "").strip(),
            instagram_provider=provider,
            instagram_graph_access_token=os.getenv(
                "INSTAGRAM_GRAPH_ACCESS_TOKEN", ""
            ).strip(),
            instagram_graph_caller_user_id=os.getenv(
                "INSTAGRAM_GRAPH_CALLER_USER_ID", ""
            ).strip(),
            instagram_graph_api_version=os.getenv(
                "INSTAGRAM_GRAPH_API_VERSION", "v24.0"
            ).strip(),
            scan_limit=_integer("SCAN_LIMIT", 30, 1),
            accounts_per_run=_integer("ACCOUNTS_PER_RUN", 20, 1),
            max_new_per_profile=_integer("MAX_NEW_REELS_PER_PROFILE", 2, 1),
            max_publish_per_run=_integer("MAX_PUBLISH_PER_RUN", 20, 0),
            lookback_hours=_integer("LOOKBACK_HOURS", 48, 1),
            max_video_bytes=_integer("MAX_VIDEO_BYTES", 250 * 1024 * 1024, 1),
            request_attempts=_integer("REQUEST_ATTEMPTS", 4, 1),
            only_city_code=city_slug(os.getenv("ONLY_CITY_CODE", "")),
            mode=mode,
            backfill_since=os.getenv("BACKFILL_SINCE", "").strip(),
            dry_run=_boolean("DRY_RUN"),
        )


def load_profiles(path: Path) -> list[Profile]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    profiles: list[Profile] = []
    seen: set[str] = set()
    for row in payload.get("profiles", []):
        username = str(row.get("instagramUsername", "")).strip().lstrip("@")
        key = username.casefold()
        if not username or not row.get("enabled", True):
            continue
        if key in seen:
            raise ValueError(f"Duplicate enabled Instagram profile: {username}")
        city = str(row.get("city", "")).strip()
        city_code = city_slug(str(row.get("cityCode") or city))
        app_username = str(row.get("createdByUsername", "")).strip()
        if not app_username or not city_code:
            raise ValueError(f"Profile @{username} requires createdByUsername and cityCode")
        profiles.append(
            Profile(
                instagram_username=username,
                app_username=app_username,
                state=str(row.get("state", "")).strip(),
                city=city,
                city_code=city_code,
                enabled=True,
                download_authorized=bool(row.get("downloadAuthorized", False)),
                publish_authorized=bool(row.get("publishAuthorized", False)),
                instagram_user_id=str(row.get("instagramUserId", "")).strip(),
            )
        )
        seen.add(key)
    return profiles
