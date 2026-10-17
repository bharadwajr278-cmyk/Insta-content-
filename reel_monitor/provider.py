from __future__ import annotations

import logging
import re
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
            # Do not send placeholder cookies with empty values. Instagram treats
            # those as malformed browser state and may redirect otherwise valid
            # authenticated profile requests back to the login page (HTTP 302).
            # Instaloader requires the CSRF cookie key while restoring a session.
            # Keep only the two required cookies; empty placeholder values for
            # ds_user_id/mid/rur can make Instagram redirect profile requests.
            self.loader.load_session(
                username,
                {"sessionid": session_id, "csrftoken": ""},
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
        caller_user_id: str,
        api_version: str,
    ) -> None:
        if not access_token:
            raise RuntimeError("INSTAGRAM_GRAPH_ACCESS_TOKEN is required for graph provider")
        if not caller_user_id:
            raise RuntimeError(
                "INSTAGRAM_GRAPH_CALLER_USER_ID is required for graph provider"
            )
        self.http = http
        self.access_token = access_token
        self.caller_user_id = caller_user_id
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
        username = profile.instagram_username
        if not re.fullmatch(r"[A-Za-z0-9._]+", username):
            raise ValueError(f"Invalid Instagram username: {username!r}")
        url = f"https://graph.facebook.com/{self.api_version}/{self.caller_user_id}"
        fields = (
            f"business_discovery.username({username})"
            f"{{username,media.limit({min(limit, 100)})"
            "{id,caption,media_type,media_product_type,media_url,"
            "thumbnail_url,permalink,timestamp}}}"
        )
        response = self.http.request(
            "GET",
            url,
            params={
                "access_token": self.access_token,
                "fields": fields,
            },
        )
        if response is None:
            raise RuntimeError("Meta Graph discovery exhausted retries")
        response.raise_for_status()
        payload = response.json()
        discovery = payload.get("business_discovery")
        if not isinstance(discovery, dict):
            error = payload.get("error", {})
            detail = error.get("message", "target is not available to Business Discovery")
            raise RuntimeError(f"Meta Business Discovery failed for @{username}: {detail}")
        media = discovery.get("media", {})
        reels: list[Reel] = []
        for item in media.get("data", []):
            timestamp = str(item.get("timestamp", ""))
            if not timestamp:
                LOG.warning("Skipping Graph media %s: timestamp is missing", item.get("id"))
                continue
            published_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC)
            if since and published_at < since:
                continue
            product = str(item.get("media_product_type", "")).casefold()
            media_type = str(item.get("media_type", "")).casefold()
            permalink = str(item.get("permalink", ""))
            is_reel = product in {"reels", "clips"} or (
                media_type == "video" and "/reel/" in permalink
            )
            if not is_reel:
                continue
            video_url = str(item.get("media_url", ""))
            if not video_url:
                LOG.warning(
                    "Skipping Graph Reel %s from @%s: Meta supplied no downloadable media URL",
                    item.get("id"),
                    username,
                )
                continue
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
