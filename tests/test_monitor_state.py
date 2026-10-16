from monitor import Monitor, Profile


def test_new_reel_is_publishable() -> None:
    assert Monitor.is_publishable_record(None, backfill=True)


def test_baseline_reel_is_publishable_only_during_backfill() -> None:
    record = {"status": "BASELINE"}
    assert Monitor.is_publishable_record(record, backfill=True)
    assert not Monitor.is_publishable_record(record, backfill=False)


def test_terminal_and_ambiguous_reels_are_never_republished() -> None:
    for status in ("PUBLISHED", "PUBLISHING", "PUBLISH_UNKNOWN"):
        assert not Monitor.is_publishable_record({"status": status}, backfill=True)


def test_profile_sharding_is_stable_and_exclusive() -> None:
    profile = Profile("some.handle", "919999999999", "State", "City", "city")
    matches = []
    for index in range(4):
        monitor = object.__new__(Monitor)
        monitor.profile_shard_count = 4
        monitor.profile_shard_index = index
        matches.append(monitor.in_configured_shard(profile))
    assert matches.count(True) == 1
