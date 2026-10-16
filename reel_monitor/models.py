from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Profile:
    instagram_username: str
    app_username: str
    state: str
    city: str
    city_code: str
    enabled: bool = True
    download_authorized: bool = False
    publish_authorized: bool = False
    instagram_user_id: str = ""

    @property
    def authorized(self) -> bool:
        return self.download_authorized and self.publish_authorized


@dataclass(frozen=True, slots=True)
class Reel:
    media_id: str
    shortcode: str
    profile: Profile
    source_url: str
    caption: str
    published_at: datetime
    video_url: str
    thumbnail_url: str | None = None

    @property
    def filename(self) -> str:
        return f"{self.profile.instagram_username}_{self.media_id}.mp4"


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    duration_seconds: float
    width: int
    height: int
    file_size_bytes: int
    mime_type: str = "video/mp4"


@dataclass(frozen=True, slots=True)
class PublishResult:
    external_id: str | None
    response_status: int
