import json

import pytest

from reel_monitor.config import load_profiles


def test_profile_requires_unique_handle_and_normalizes_city(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "instagramUsername": "Example.Handle",
                        "createdByUsername": "919999999999",
                        "city": "New Delhi",
                        "enabled": True,
                        "downloadAuthorized": True,
                        "publishAuthorized": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    profile = load_profiles(path)[0]
    assert profile.city_code == "new_delhi"
    assert profile.authorized


def test_duplicate_handle_is_rejected(tmp_path) -> None:
    path = tmp_path / "profiles.json"
    row = {
        "instagramUsername": "same.handle",
        "createdByUsername": "1",
        "city": "City",
    }
    path.write_text(json.dumps({"profiles": [row, {**row, "instagramUsername": "SAME.HANDLE"}]}))
    with pytest.raises(ValueError, match="Duplicate enabled"):
        load_profiles(path)
