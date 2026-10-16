from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import Profile, Reel

SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profiles (
                instagram_username TEXT PRIMARY KEY COLLATE NOCASE,
                app_username TEXT NOT NULL,
                state TEXT NOT NULL,
                city TEXT NOT NULL,
                city_code TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                download_authorized INTEGER NOT NULL,
                publish_authorized INTEGER NOT NULL,
                last_checked_at TEXT,
                last_seen_published_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reels (
                media_id TEXT PRIMARY KEY,
                shortcode TEXT NOT NULL,
                instagram_username TEXT NOT NULL COLLATE NOCASE,
                source_url TEXT NOT NULL,
                caption TEXT NOT NULL,
                published_at TEXT NOT NULL,
                status TEXT NOT NULL,
                storage_video_key TEXT,
                storage_thumbnail_key TEXT,
                brokket_external_id TEXT,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(instagram_username) REFERENCES profiles(instagram_username)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS reels_shortcode_unique ON reels(shortcode);
            CREATE INDEX IF NOT EXISTS reels_status_idx ON reels(status, published_at);
            CREATE TABLE IF NOT EXISTS checkpoints (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS external_media_ids (
                media_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                mode TEXT NOT NULL,
                scanned_profiles INTEGER NOT NULL DEFAULT 0,
                discovered_reels INTEGER NOT NULL DEFAULT 0,
                published_reels INTEGER NOT NULL DEFAULT 0,
                failed_reels INTEGER NOT NULL DEFAULT 0,
                summary TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, datetime.now(UTC).isoformat()),
        )
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def sync_profiles(self, profiles: list[Profile]) -> None:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as db:
            for profile in profiles:
                db.execute(
                    """
                    INSERT INTO profiles(instagram_username, app_username, state, city,
                        city_code, enabled, download_authorized, publish_authorized, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instagram_username) DO UPDATE SET
                        app_username=excluded.app_username, state=excluded.state,
                        city=excluded.city, city_code=excluded.city_code,
                        enabled=excluded.enabled,
                        download_authorized=excluded.download_authorized,
                        publish_authorized=excluded.publish_authorized,
                        updated_at=excluded.updated_at
                    """,
                    (
                        profile.instagram_username,
                        profile.app_username,
                        profile.state,
                        profile.city,
                        profile.city_code,
                        int(profile.enabled),
                        int(profile.download_authorized),
                        int(profile.publish_authorized),
                        now,
                    ),
                )

    def get_checkpoint(self, name: str, default: str = "") -> str:
        row = self.connection.execute(
            "SELECT value FROM checkpoints WHERE name=?", (name,)
        ).fetchone()
        return str(row["value"]) if row else default

    def set_checkpoint(self, name: str, value: str) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """INSERT INTO checkpoints(name, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (name, value, now),
        )
        self.connection.commit()

    def record_discovered(self, reel: Reel, status: str = "DISCOVERED") -> bool:
        if self.already_known(reel.media_id):
            return False
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO reels(media_id, shortcode, instagram_username,
                source_url, caption, published_at, status, discovered_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reel.media_id,
                reel.shortcode,
                reel.profile.instagram_username,
                reel.source_url,
                reel.caption,
                reel.published_at.isoformat(),
                status,
                now,
                now,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def import_known_reel(
        self,
        media_id: str,
        instagram_username: str,
        published_at: str,
        status: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO reels(media_id, shortcode, instagram_username,
                source_url, caption, published_at, status, discovered_at, updated_at)
            VALUES (?, ?, ?, ?, '', ?, ?, ?, ?)
            """,
            (
                media_id,
                media_id,
                instagram_username,
                f"https://www.instagram.com/reel/{media_id}/",
                published_at or now,
                status,
                now,
                now,
            ),
        )
        self.connection.commit()

    def profile_last_seen(self, username: str) -> str | None:
        row = self.connection.execute(
            "SELECT last_seen_published_at FROM profiles WHERE instagram_username=?",
            (username,),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def mark_profile_checked(
        self,
        username: str,
        last_seen_published_at: str | None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.connection.execute(
            """UPDATE profiles SET last_checked_at=?,
            last_seen_published_at=COALESCE(?, last_seen_published_at),
            last_error=?, updated_at=? WHERE instagram_username=?""",
            (now, last_seen_published_at, error, now, username),
        )
        self.connection.commit()

    def claim(self, media_id: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE reels SET status='PROCESSING', attempts=attempts+1, updated_at=?
                WHERE media_id=? AND status IN ('DISCOVERED', 'FAILED_RETRYABLE')""",
                (now, media_id),
            )
            return cursor.rowcount == 1

    def set_status(self, media_id: str, status: str, **fields: str | None) -> None:
        allowed = {
            "storage_video_key",
            "storage_thumbnail_key",
            "brokket_external_id",
            "error",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Unsupported reel fields: {sorted(invalid)}")
        assignments = ["status=?", "updated_at=?"] + [f"{name}=?" for name in fields]
        values = [status, datetime.now(UTC).isoformat(), *fields.values(), media_id]
        self.connection.execute(
            f"UPDATE reels SET {', '.join(assignments)} WHERE media_id=?", values
        )
        self.connection.commit()

    def already_known(self, media_id: str) -> bool:
        reel = self.connection.execute(
            "SELECT 1 FROM reels WHERE media_id=?", (media_id,)
        ).fetchone()
        external = self.connection.execute(
            "SELECT 1 FROM external_media_ids WHERE media_id=?", (media_id,)
        ).fetchone()
        return reel is not None or external is not None

    def import_external_media_ids(self, media_ids: set[str]) -> int:
        now = datetime.now(UTC).isoformat()
        before = self.connection.total_changes
        self.connection.executemany(
            "INSERT OR IGNORE INTO external_media_ids(media_id, observed_at) VALUES (?, ?)",
            ((media_id, now) for media_id in media_ids),
        )
        self.connection.commit()
        return self.connection.total_changes - before

    def reel_status(self, media_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM reels WHERE media_id=?", (media_id,)
        ).fetchone()
        return str(row[0]) if row else None

    def begin_run(self, mode: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(started_at, mode) VALUES (?, ?)",
            (datetime.now(UTC).isoformat(), mode),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, **summary: int) -> None:
        self.connection.execute(
            """UPDATE runs SET finished_at=?, scanned_profiles=?, discovered_reels=?,
            published_reels=?, failed_reels=?, summary=? WHERE id=?""",
            (
                datetime.now(UTC).isoformat(),
                summary.get("scanned_profiles", 0),
                summary.get("discovered_reels", 0),
                summary.get("published_reels", 0),
                summary.get("failed_reels", 0),
                json.dumps(summary, sort_keys=True),
                run_id,
            ),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.connection.close()
