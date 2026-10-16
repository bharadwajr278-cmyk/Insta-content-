from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import boto3

from .config import Settings, load_profiles
from .database import Database
from .http_client import ResilientHttpClient
from .media import MediaProcessor
from .persistence import S3DatabaseStore
from .provider import InstagramProvider, MetaGraphProvider
from .publisher import BrokketPublisher
from .service import ReelSyncService
from .storage import S3Storage


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Synchronize authorized Instagram Reels")
    subcommands = result.add_subparsers(dest="command", required=True)
    sync = subcommands.add_parser("sync", help="Run one synchronization cycle")
    sync.add_argument("--mode", choices=("incremental", "backfill", "baseline"))
    sync.add_argument("--city-code")
    sync.add_argument("--max-publish", type=int)
    sync.add_argument("--backfill-since", help="ISO-8601 timestamp")
    sync.add_argument("--dry-run", action="store_true")
    subcommands.add_parser("validate-config", help="Validate profiles.json")
    return result


def _apply_args(args: argparse.Namespace) -> None:
    if getattr(args, "mode", None):
        os.environ["SYNC_MODE"] = args.mode
    if getattr(args, "city_code", None):
        os.environ["ONLY_CITY_CODE"] = args.city_code
    if getattr(args, "max_publish", None) is not None:
        os.environ["MAX_PUBLISH_PER_RUN"] = str(args.max_publish)
    if getattr(args, "backfill_since", None):
        os.environ["BACKFILL_SINCE"] = args.backfill_since
    if getattr(args, "dry_run", False):
        os.environ["DRY_RUN"] = "true"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parser().parse_args()
    _apply_args(args)
    profiles_path = Path(os.getenv("PROFILES_FILE", "profiles.json"))
    profiles = load_profiles(profiles_path)
    if args.command == "validate-config":
        print(f"Valid configuration: {len(profiles)} enabled profiles")
        return 0

    settings = Settings.from_env()
    s3 = boto3.client(
        "s3",
        region_name=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
    )
    store = S3DatabaseStore(
        s3,
        settings.s3_bucket,
        settings.database_object_key,
        settings.database_path,
        settings.legacy_state_object_key,
    )
    restored = store.restore()
    database = Database(settings.database_path)
    http = ResilientHttpClient(settings.request_attempts)
    try:
        database.sync_profiles(profiles)
        if not restored:
            store.import_legacy_state(database)
        provider = (
            MetaGraphProvider(
                http,
                settings.instagram_graph_access_token,
                settings.instagram_graph_api_version,
            )
            if settings.instagram_provider == "graph"
            else InstagramProvider(
                settings.instagram_session_username,
                settings.instagram_session_file,
            )
        )
        service = ReelSyncService(
            settings=settings,
            database=database,
            provider=provider,
            media=MediaProcessor(http, settings.max_video_bytes),
            storage=S3Storage(s3, settings.s3_bucket, settings.public_base_url),
            publisher=BrokketPublisher(http, settings.clip_api_url, settings.clip_api_token),
        )
        service.run(profiles)
        return 0
    finally:
        database.close()
        store.save()
        http.close()


if __name__ == "__main__":
    raise SystemExit(main())
