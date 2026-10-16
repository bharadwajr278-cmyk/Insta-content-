from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from .database import Database

LOG = logging.getLogger(__name__)


class S3DatabaseStore:
    def __init__(
        self,
        s3: Any,
        bucket: str,
        key: str,
        local_path: Path,
        legacy_key: str = "",
    ) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.key = key
        self.local_path = local_path
        self.legacy_key = legacy_key

    def restore(self) -> bool:
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.s3.download_file(self.bucket, self.key, str(self.local_path))
            LOG.info("Restored durable database from s3://%s/%s", self.bucket, self.key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404"}:
                raise
            LOG.info("No durable database exists yet; creating a new one")
            return False

    def import_legacy_state(self, database: Database) -> int:
        if not self.legacy_key:
            return 0
        directory, _, filename = self.legacy_key.rpartition("/")
        stem = filename.removesuffix(".json")
        prefix = f"{directory}/{stem}" if directory else stem
        response = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        keys = {
            row["Key"]
            for row in response.get("Contents", [])
            if row["Key"] == self.legacy_key
            or (
                row["Key"].startswith(f"{prefix}-")
                and row["Key"].endswith(".json")
            )
        }
        imported = 0
        for key in sorted(keys):
            state_object = self.s3.get_object(Bucket=self.bucket, Key=key)
            state = json.loads(state_object["Body"].read())
            for media_id in state.get("reels", {}):
                imported += database.import_external_media_ids({str(media_id)})
        LOG.info("Imported %d Reel IDs from legacy state", imported)
        return imported

    def save(self) -> None:
        self.s3.upload_file(
            str(self.local_path),
            self.bucket,
            self.key,
            ExtraArgs={
                "ContentType": "application/vnd.sqlite3",
                "ServerSideEncryption": "AES256",
                "CacheControl": "no-store",
            },
        )
        LOG.info("Saved durable database to s3://%s/%s", self.bucket, self.key)
