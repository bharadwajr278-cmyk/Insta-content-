from monitor import Monitor


def test_new_reel_is_publishable() -> None:
    assert Monitor.is_publishable_record(None, backfill=True)


def test_baseline_reel_is_publishable_only_during_backfill() -> None:
    record = {"status": "BASELINE"}
    assert Monitor.is_publishable_record(record, backfill=True)
    assert not Monitor.is_publishable_record(record, backfill=False)


def test_terminal_and_ambiguous_reels_are_never_republished() -> None:
    for status in ("PUBLISHED", "PUBLISHING", "PUBLISH_UNKNOWN"):
        assert not Monitor.is_publishable_record({"status": status}, backfill=True)
