from datetime import UTC, datetime

import httpx

from reel_monitor.models import MediaMetadata, Profile, Reel
from reel_monitor.publisher import BrokketPublisher


class FakeHttp:
    def __init__(self) -> None:
        self.kwargs = None

    def request(self, method, url, **kwargs):
        self.kwargs = kwargs
        return httpx.Response(201, json={"id": "post-1"}, request=httpx.Request(method, url))


def test_payload_contains_mapping_credit_caption_city_and_idempotency() -> None:
    http = FakeHttp()
    publisher = BrokketPublisher(http, "https://api.example/clips", "secret")
    profile = Profile("agent", "919999999999", "State", "City", "city", True, True, True)
    reel = Reel(
        "12345",
        "ABC",
        profile,
        "https://www.instagram.com/reel/ABC/",
        "Original caption #home",
        datetime.now(UTC),
        "https://cdn.example/video.mp4",
    )
    result = publisher.publish(
        reel,
        "https://media.example/video.mp4",
        "https://media.example/thumb.jpg",
        MediaMetadata(12.5, 1080, 1920, 10000),
    )
    payload = http.kwargs["json"]
    assert payload["createdByUsername"] == "919999999999"
    assert payload["cityCode"] == "city"
    assert payload["text"].startswith("Courtesy @agent\n\nOriginal caption")
    assert payload["sourceMediaId"] == "12345"
    assert http.kwargs["headers"]["Idempotency-Key"] == "instagram:12345"
    assert result.external_id == "post-1"
