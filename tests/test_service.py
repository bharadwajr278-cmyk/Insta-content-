from datetime import UTC, datetime, timedelta
from pathlib import Path

from reel_monitor.config import Settings
from reel_monitor.database import Database
from reel_monitor.models import MediaMetadata, Profile, PublishResult, Reel
from reel_monitor.service import ReelSyncService


class FakeProvider:
    def __init__(self, batches):
        self.batches = iter(batches)

    def discover(self, profile, *, since, limit):
        return next(self.batches)


class FakeMedia:
    def download(self, url: str, destination: Path) -> None:
        destination.write_bytes(b"source")

    def normalize(self, source: Path, destination: Path) -> None:
        destination.write_bytes(b"normalized")

    def thumbnail(self, video: Path, destination: Path) -> None:
        destination.write_bytes(b"jpeg")

    def validate(self, video: Path) -> MediaMetadata:
        return MediaMetadata(10, 1080, 1920, video.stat().st_size)


class FakeStorage:
    def upload(self, source, key, content_type):
        return f"https://media.example/{key}"


class FakePublisher:
    def __init__(self):
        self.published = []

    def publish(self, reel, video_url, thumbnail_url, metadata):
        self.published.append(reel.media_id)
        return PublishResult("post-1", 201)

    def existing_instagram_media_ids(self):
        return set()


def settings(tmp_path) -> Settings:
    return Settings(
        tmp_path / "profiles.json",
        tmp_path / "monitor.db",
        "monitor.db",
        "state.json",
        "https://api.example/clips",
        "",
        "bucket",
        "region",
        None,
        "https://media.example",
        "session-user",
        tmp_path / "session",
        "instaloader",
        "",
        "v24.0",
        30,
        20,
        2,
        20,
        48,
        1000000,
        4,
        "",
        "incremental",
        "",
        False,
    )


def reel(profile, media_id, shortcode, published_at):
    return Reel(
        media_id,
        shortcode,
        profile,
        f"https://www.instagram.com/reel/{shortcode}/",
        "caption",
        published_at,
        f"https://cdn.example/{media_id}.mp4",
    )


def test_first_scan_baselines_then_only_new_unique_reel_is_published(tmp_path) -> None:
    profile = Profile("agent", "9199", "State", "City", "city", True, True, True)
    now = datetime.now(UTC)
    old = reel(profile, "1", "OLD", now - timedelta(hours=1))
    new = reel(profile, "2", "NEW", now)
    database = Database(tmp_path / "monitor.db")
    publisher = FakePublisher()
    first = ReelSyncService(
        settings(tmp_path), database, FakeProvider([[old]]), FakeMedia(), FakeStorage(), publisher
    )
    assert first.run([profile])["published_reels"] == 0
    second = ReelSyncService(
        settings(tmp_path),
        database,
        FakeProvider([[new, old]]),
        FakeMedia(),
        FakeStorage(),
        publisher,
    )
    assert second.run([profile])["published_reels"] == 1
    assert publisher.published == ["2"]
    database.close()
