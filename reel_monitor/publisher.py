from __future__ import annotations

import re

import httpx

from .http_client import ResilientHttpClient
from .models import MediaMetadata, PublishResult, Reel


class BrokketPublisher:
    def __init__(self, http: ResilientHttpClient, api_url: str, token: str) -> None:
        self.http = http
        self.api_url = api_url
        self.token = token

    @staticmethod
    def caption(reel: Reel) -> str:
        cleaned = " ".join(reel.caption.split())
        courtesy = f"Courtesy @{reel.profile.instagram_username}"
        return f"{courtesy}\n\n{cleaned}" if cleaned else courtesy

    def publish(
        self,
        reel: Reel,
        video_url: str,
        thumbnail_url: str,
        metadata: MediaMetadata,
    ) -> PublishResult:
        if not reel.profile.city_code:
            raise ValueError("cityCode is mandatory")
        text = self.caption(reel)
        payload = {
            "createdByUsername": reel.profile.app_username,
            "text": text,
            "clipMedia": {
                "url": video_url,
                "mediaType": "VIDEO",
                "thumbnailUrl": thumbnail_url,
                "durationSeconds": metadata.duration_seconds,
                "width": metadata.width,
                "height": metadata.height,
                "aspectRatio": metadata.width / metadata.height,
                "mimeType": metadata.mime_type,
                "fileSizeBytes": metadata.file_size_bytes,
            },
            "taggedUsernames": list(
                dict.fromkeys(re.findall(r"(?<![\w@])@([A-Za-z0-9._]+)", text))
            ),
            "taggedHashtags": list(
                dict.fromkeys(re.findall(r"(?<!\w)#([\w.]+)", text))
            ),
            "isLargeMultimedia": False,
            "makeLive": True,
            "cityCode": reel.profile.city_code,
            "sourceUrl": reel.source_url,
            "sourceMediaId": reel.media_id,
            "sourcePlatform": "instagram",
        }
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": f"instagram:{reel.media_id}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = self.http.request(
            "POST",
            self.api_url,
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(300.0, connect=30.0),
        )
        if response is None:
            raise RuntimeError("Brokket API request exhausted retries")
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = {}
        external_id = data.get("id") or data.get("postId") or data.get("_id")
        return PublishResult(
            external_id=str(external_id) if external_id is not None else None,
            response_status=response.status_code,
        )

    def existing_instagram_media_ids(self, max_pages: int = 100) -> set[str]:
        media_ids: set[str] = set()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        for page in range(max_pages):
            response = self.http.request(
                "GET",
                self.api_url,
                headers=headers,
                params={"page": page, "size": 100},
                timeout=httpx.Timeout(20.0, connect=5.0),
            )
            if response is None:
                break
            try:
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError):
                break
            for post in body.get("content", []):
                explicit = post.get("sourceMediaId")
                if explicit:
                    media_ids.add(str(explicit))
                for media in post.get("socialMultimediaList") or []:
                    match = re.search(
                        r"/instagram/[^/]+/([^/?#.]+)\.mp4(?:[?#]|$)",
                        str(media.get("url", "")),
                        re.IGNORECASE,
                    )
                    if match:
                        media_ids.add(match.group(1))
            if body.get("last", True):
                break
        return media_ids
