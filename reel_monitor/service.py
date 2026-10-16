from __future__ import annotations

import logging
import tempfile
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .database import Database
from .media import MediaProcessor
from .models import Profile, Reel
from .provider import ReelProvider
from .publisher import BrokketPublisher
from .storage import S3Storage

LOG = logging.getLogger(__name__)


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value)


class ReelSyncService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        provider: ReelProvider,
        media: MediaProcessor,
        storage: S3Storage,
        publisher: BrokketPublisher,
    ) -> None:
        self.settings = settings
        self.database = database
        self.provider = provider
        self.media = media
        self.storage = storage
        self.publisher = publisher

    def _selected_profiles(self, profiles: list[Profile]) -> list[Profile]:
        if self.settings.only_city_code:
            selected = [
                profile
                for profile in profiles
                if profile.city_code == self.settings.only_city_code
            ]
            if not selected:
                raise RuntimeError(
                    f"No enabled profiles for cityCode={self.settings.only_city_code}"
                )
            return selected
        cursor = int(self.database.get_checkpoint("profile_cursor", "0"))
        size = min(self.settings.accounts_per_run, len(profiles))
        selected = [profiles[(cursor + index) % len(profiles)] for index in range(size)]
        self.database.set_checkpoint("profile_cursor", str((cursor + size) % len(profiles)))
        return selected

    def _since_for(self, profile: Profile) -> datetime | None:
        if self.settings.mode == "backfill":
            if not self.settings.backfill_since:
                return None
            return datetime.fromisoformat(self.settings.backfill_since).astimezone(UTC)
        last_seen = self.database.profile_last_seen(profile.instagram_username)
        if not last_seen:
            return None
        return datetime.fromisoformat(last_seen).astimezone(UTC) - timedelta(
            hours=self.settings.lookback_hours
        )

    def _process(self, reel: Reel) -> bool:
        if not self.database.claim(reel.media_id):
            LOG.info("Skipping known or reserved Reel %s", reel.media_id)
            return False
        before_publish = True
        with tempfile.TemporaryDirectory(prefix="instagram-reel-") as folder:
            work = Path(folder)
            source = work / reel.filename
            normalized = work / f"normalized_{reel.filename}"
            thumbnail = work / f"{reel.profile.instagram_username}_{reel.media_id}.jpg"
            try:
                self.media.download(reel.video_url, source)
                self.media.normalize(source, normalized)
                metadata = self.media.validate(normalized)
                self.media.thumbnail(normalized, thumbnail)
                video_key = (
                    f"instagram/{_safe(reel.profile.instagram_username)}/"
                    f"{_safe(reel.filename)}"
                )
                thumbnail_key = (
                    f"instagram/{_safe(reel.profile.instagram_username)}/"
                    f"{_safe(reel.profile.instagram_username)}_{_safe(reel.media_id)}.jpg"
                )
                video_url = self.storage.upload(normalized, video_key, "video/mp4")
                thumbnail_url = self.storage.upload(thumbnail, thumbnail_key, "image/jpeg")
                self.database.set_status(
                    reel.media_id,
                    "STORED",
                    storage_video_key=video_key,
                    storage_thumbnail_key=thumbnail_key,
                    error=None,
                )
                before_publish = False
                result = self.publisher.publish(
                    reel, video_url, thumbnail_url, metadata
                )
                self.database.set_status(
                    reel.media_id,
                    "PUBLISHED",
                    brokket_external_id=result.external_id,
                    error=None,
                )
                LOG.info(
                    "Published media_id=%s shortcode=%s profile=@%s app_user=%s cityCode=%s",
                    reel.media_id,
                    reel.shortcode,
                    reel.profile.instagram_username,
                    reel.profile.app_username,
                    reel.profile.city_code,
                )
                return True
            except Exception as exc:
                status = "FAILED_RETRYABLE" if before_publish else "PUBLISH_UNKNOWN"
                self.database.set_status(
                    reel.media_id, status, error=str(exc)[:1000]
                )
                LOG.exception(
                    "Failed Reel %s from @%s; status=%s",
                    reel.media_id,
                    reel.profile.instagram_username,
                    status,
                )
                return False

    def run(self, profiles: list[Profile]) -> dict[str, int]:
        if not profiles:
            raise RuntimeError("No enabled profiles are configured")
        self.database.sync_profiles(profiles)
        existing_ids = self.publisher.existing_instagram_media_ids()
        imported = self.database.import_external_media_ids(existing_ids)
        LOG.info(
            "Reconciled %d existing Brokket Reel IDs (%d newly imported)",
            len(existing_ids),
            imported,
        )
        selected = self._selected_profiles(profiles)
        run_id = self.database.begin_run(self.settings.mode)
        summary = {
            "scanned_profiles": 0,
            "discovered_reels": 0,
            "published_reels": 0,
            "failed_reels": 0,
        }
        queues: list[deque[Reel]] = []
        try:
            for profile in selected:
                if not profile.authorized:
                    LOG.warning(
                        "Authorization missing for @%s; skipped",
                        profile.instagram_username,
                    )
                    continue
                first_scan = self.database.profile_last_seen(profile.instagram_username) is None
                try:
                    reels = self.provider.discover(
                        profile,
                        since=self._since_for(profile),
                        limit=self.settings.scan_limit,
                    )
                    summary["scanned_profiles"] += 1
                    latest = max((reel.published_at for reel in reels), default=None)
                    queue: list[Reel] = []
                    for reel in sorted(reels, key=lambda item: item.published_at):
                        initial_status = (
                            "BASELINE"
                            if first_scan and self.settings.mode == "incremental"
                            else "DISCOVERED"
                        )
                        inserted = self.database.record_discovered(reel, initial_status)
                        status = self.database.reel_status(reel.media_id)
                        if inserted:
                            summary["discovered_reels"] += 1
                        if status in {"DISCOVERED", "FAILED_RETRYABLE"}:
                            queue.append(reel)
                    if self.settings.mode == "baseline":
                        queue = []
                    if queue:
                        queues.append(deque(queue[-self.settings.max_new_per_profile :]))
                    self.database.mark_profile_checked(
                        profile.instagram_username,
                        latest.isoformat() if latest else None,
                    )
                except Exception as exc:
                    summary["failed_reels"] += 1
                    self.database.mark_profile_checked(
                        profile.instagram_username, None, str(exc)[:1000]
                    )
                    LOG.exception("Discovery failed for @%s", profile.instagram_username)

            if self.settings.dry_run:
                LOG.info("Dry run complete; no media uploaded or published")
                return summary

            published = 0
            while queues:
                next_round: list[deque[Reel]] = []
                for queue in queues:
                    limit_reached = (
                        self.settings.max_publish_per_run
                        and published >= self.settings.max_publish_per_run
                    )
                    if limit_reached:
                        break
                    reel = queue.popleft()
                    if self._process(reel):
                        published += 1
                        summary["published_reels"] += 1
                    else:
                        summary["failed_reels"] += 1
                    if queue:
                        next_round.append(queue)
                queues = next_round
                limit_reached = (
                    self.settings.max_publish_per_run
                    and published >= self.settings.max_publish_per_run
                )
                if limit_reached:
                    break
            return summary
        finally:
            self.database.finish_run(run_id, **summary)
            LOG.info("Run summary: %s", summary)
