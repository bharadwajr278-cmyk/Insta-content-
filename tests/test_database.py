from datetime import UTC, datetime

from reel_monitor.database import Database
from reel_monitor.models import Profile, Reel


def _profile() -> Profile:
    return Profile("handle", "919999999999", "State", "City", "city", True, True, True)


def _reel(media_id: str = "123") -> Reel:
    return Reel(
        media_id,
        "ABC",
        _profile(),
        "https://www.instagram.com/reel/ABC/",
        "caption",
        datetime.now(UTC),
        "https://cdn.example/video.mp4",
    )


def test_media_id_is_globally_unique_and_claim_is_atomic(tmp_path) -> None:
    database = Database(tmp_path / "monitor.db")
    database.sync_profiles([_profile()])
    assert database.record_discovered(_reel())
    assert not database.record_discovered(_reel())
    assert database.claim("123")
    assert not database.claim("123")
    database.close()


def test_published_reel_cannot_be_reclaimed(tmp_path) -> None:
    database = Database(tmp_path / "monitor.db")
    database.sync_profiles([_profile()])
    database.record_discovered(_reel())
    assert database.claim("123")
    database.set_status("123", "PUBLISHED", brokket_external_id="post-1")
    assert not database.claim("123")
    database.close()
