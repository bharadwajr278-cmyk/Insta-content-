from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import instaloader

from .http_client import ResilientHttpClient
from .models import Profile, Reel

LOG = logging.getLogger(__name__)


class ReelProvider(Protocol):
    def discover(
        self, profile: Profile, *, since: datetime | None, limit: int
    ) -> list[Reel]: ...


class InstagramProvider:
    """Authenticated provider for metadata Instagram makes available to the account."""

    def __init__(self, username: str, session_file: Path, session_id: str = "") -> None:
        self.loader = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
            max_connection_attempts=3,
            request_timeout=30,
            fatal_status_codes=[401, 403, 429],
        )
        if session_id:
            self.loader.load_session(
                username,
                {
                    "sessionid": session_id,
                    "csrftoken": "",
                    "ds_user_id": "",
                    "mid": "",
                    "ig_pr": "1",
                    "ig_vw": "1920",
                    "s_network": "",
                },
            )
        else:
            self.loader.load_session_from_file(username, session_file)
        if not self.loader.test_login():
            source = "INSTAGRAM_SESSION_ID" if session_id else "INSTAGRAM_SESSION_B64"
            raise RuntimeError(f"Instagram session from {source} is invalid or expired")

    def discover(
        self,
        profile: Profile,
        *,
        since: datetime | None,
        limit: int,
    ) -> list[Reel]:
        if not profile.authorized:
            LOG.warning(
                "Skipping @%s: downloadAuthorized and publishAuthorized must both be true",
                profile.instagram_username,
            )
            return []
        remote = instaloader.Profile.from_username(
            self.loader.context, profile.instagram_username
        )
        reels: list[Reel] = []
        for post in remote.get_posts():
            if len(reels) >= limit:
                break
            published_at = post.date_utc.replace(tzinfo=UTC)
            if since and published_at < since:
                break
            if not post.is_video:
                continue
            product_type = str(getattr(post, "product_type", "")).casefold()
            typename = str(getattr(post, "typename", "")).casefold()
            if product_type not in {"clips", "reels", ""} and typename != "graphvideo":
                continue
            video_url = str(post.video_url or "")
            if not video_url:
                LOG.warning("Skipping %s: provider supplied no downloadable video", post.shortcode)
                continue
            reels.append(
                Reel(
                    media_id=str(post.mediaid),
                    shortcode=post.shortcode,
                    profile=profile,
                    source_url=f"https://www.instagram.com/reel/{post.shortcode}/",
                    caption=post.caption or "",
                    published_at=published_at,
                    video_url=video_url,
                    thumbnail_url=str(post.url or "") or None,
                )
            )
        return reels


class MetaGraphProvider:
    """Official Meta Graph API provider for connected, authorized Instagram accounts."""

    def __init__(
        self,
        http: ResilientHttpClient,
        access_token: str,
        api_version: str,
    ) -> None:
        if not access_token:
            raise RuntimeError("INSTAGRAM_GRAPH_ACCESS_TOKEN is required for graph provider")
        self.http = http
        self.access_token = access_token
        self.api_version = api_version

    def discover(
        self,
        profile: Profile,
        *,
        since: datetime | None,
        limit: int,
    ) -> list[Reel]:
        if not profile.authorized:
            return []
        if not profile.instagram_user_id:
            raise ValueError(
                f"@{profile.instagram_username} requires instagramUserId for graph provider"
            )
        url = (
            f"https://graph.facebook.com/{self.api_version}/"
            f"{profile.instagram_user_id}/media"
        )
        response = self.http.request(
            "GET",
            url,
            params={
                "access_token": self.access_token,
                "fields": (
                    "id,caption,media_type,media_product_type,media_url,"
                    "thumbnail_url,permalink,timestamp"
                ),
                "limit": min(limit, 100),
            },
        )
        if response is None:
            raise RuntimeError("Meta Graph discovery exhausted retries")
        response.raise_for_status()
        reels: list[Reel] = []
        for item in response.json().get("data", []):
            published_at = datetime.fromisoformat(
                str(item["timestamp"]).replace("Z", "+00:00")
            ).astimezone(UTC)
            if since and published_at < since:
                continue
            product = str(item.get("media_product_type", "")).casefold()
            if product not in {"reels", "clips"}:
                continue
            video_url = str(item.get("media_url", ""))
            if not video_url:
                continue
            permalink = str(item.get("permalink", ""))
            shortcode = permalink.rstrip("/").split("/")[-1]
            reels.append(
                Reel(
                    media_id=str(item["id"]),
                    shortcode=shortcode,
                    profile=profile,
                    source_url=permalink,
                    caption=str(item.get("caption", "")),
                    published_at=published_at,
                    video_url=video_url,
                    thumbnail_url=item.get("thumbnail_url"),
                )
            )
        return reels
