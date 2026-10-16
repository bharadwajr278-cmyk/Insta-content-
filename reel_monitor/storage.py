from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote


class S3Storage:
    def __init__(self, s3: Any, bucket: str, public_base_url: str) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")

    def upload(self, source: Path, key: str, content_type: str) -> str:
        self.s3.upload_file(
            str(source),
            self.bucket,
            key,
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": "public, max-age=31536000, immutable",
                "ServerSideEncryption": "AES256",
            },
        )
        return f"{self.public_base_url}/{quote(key, safe='/')}"

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)
