from __future__ import annotations

import logging
import os
import time
from dataclasses import replace

from monitor import Monitor


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    requested = [
        item.strip().lstrip("@").casefold()
        for item in os.environ["BATCH_PROFILES"].split(",")
        if item.strip()
    ]
    limit = int(os.getenv("BATCH_REELS_PER_PROFILE", "6"))
    app_user_override = os.getenv("BATCH_APP_USER", "").strip()
    city_code_override = os.getenv("BATCH_CITY_CODE", "").strip()
    monitor = Monitor()
    try:
        configured = {
            profile.instagram.casefold(): profile for profile in monitor.load_profiles()
        }
        missing = [name for name in requested if name not in configured]
        if missing:
            raise RuntimeError(f"Profiles are not configured: {', '.join(missing)}")

        state = monitor.state_store.load()
        if state is None:
            raise RuntimeError("Monitor state is missing")
        known = set(state.get("reels", {}))
        queues = []
        for name in requested:
            profile = configured[name]
            if app_user_override or city_code_override:
                profile = replace(
                    profile,
                    app_user=app_user_override or profile.app_user,
                    city_code=city_code_override or profile.city_code,
                )
            reels = monitor.discover(profile)
            unique = [reel for reel in reels if reel.reel_id not in known][:limit]
            queues.append(unique)
            logging.info("@%s: queued %d unique reels", profile.instagram, len(unique))
            time.sleep(2)

        published = 0
        failed = 0
        for index in range(limit):
            for queue in queues:
                if index >= len(queue):
                    continue
                reel = queue[index]
                try:
                    monitor.process(reel, state)
                    known.add(reel.reel_id)
                    published += 1
                except Exception:
                    failed += 1
                    logging.exception(
                        "Failed to publish %s from @%s",
                        reel.reel_id,
                        reel.profile.instagram,
                    )

        baselined = set(state.get("profilesBaselined", []))
        baselined.update(requested)
        state["profilesBaselined"] = sorted(baselined)
        monitor.state_store.save(state)
        logging.info("Batch complete: published=%d failed=%d", published, failed)
        if failed:
            raise RuntimeError(f"Batch completed with {failed} failed reels")
    finally:
        monitor.close()


if __name__ == "__main__":
    main()
