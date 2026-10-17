from datetime import UTC, datetime
from pathlib import Path

import instaloader

from reel_monitor.models import Profile
from reel_monitor.provider import InstagramProvider, MetaGraphProvider


def test_instagram_provider_accepts_session_id_without_file(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_load_session(self, username, session_data):
        captured["username"] = username
        captured["session_data"] = session_data

    monkeypatch.setattr(instaloader.Instaloader, "load_session", fake_load_session)
    monkeypatch.setattr(instaloader.Instaloader, "test_login", lambda self: "leo22122026")

    InstagramProvider("leo22122026", Path("missing-session"), "secret-session-id")

    assert captured["username"] == "leo22122026"
    assert captured["session_data"]["sessionid"] == "secret-session-id"


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "business_discovery": {
                "media": {
                    "data": [
                        {
                            "id": "12345",
                            "caption": "New listing",
                            "media_type": "VIDEO",
                            "media_product_type": "REELS",
                            "media_url": "https://cdn.example/reel.mp4",
                            "thumbnail_url": "https://cdn.example/reel.jpg",
                            "permalink": "https://www.instagram.com/reel/ABC123/",
                            "timestamp": "2026-10-17T08:00:00+0000",
                        }
                    ]
                }
            }
        }


class FakeHttp:
    def __init__(self) -> None:
        self.url = ""
        self.params: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs):
        assert method == "GET"
        self.url = url
        self.params = kwargs["params"]
        return FakeResponse()


def test_graph_provider_uses_business_discovery_without_target_id() -> None:
    http = FakeHttp()
    provider = MetaGraphProvider(http, "token", "caller-id", "v24.0")
    profile = Profile(
        instagram_username="target.handle",
        app_username="919999999999",
        state="Gujarat",
        city="Ahmedabad",
        city_code="ahmedabad",
        enabled=True,
        download_authorized=True,
        publish_authorized=True,
    )

    reels = provider.discover(
        profile,
        since=datetime(2026, 10, 16, tzinfo=UTC),
        limit=5,
    )

    assert http.url.endswith("/caller-id")
    assert "business_discovery.username(target.handle)" in http.params["fields"]
    assert reels[0].media_id == "12345"
    assert reels[0].profile.city_code == "ahmedabad"
