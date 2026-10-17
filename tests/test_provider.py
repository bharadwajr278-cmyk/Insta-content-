from pathlib import Path

import instaloader

from reel_monitor.provider import InstagramProvider


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
