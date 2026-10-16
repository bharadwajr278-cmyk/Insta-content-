# Brokket Instagram Reel Automation

Production Python automation that monitors explicitly authorized Instagram profiles, discovers
new Reels, prevents duplicate media IDs, validates and normalizes videos, stores them in S3, and
publishes them to Brokket under the configured app username and city code.

## Guarantees

- A global SQLite primary key on the Instagram media ID plus the Brokket
  `Idempotency-Key: instagram:<media_id>` header prevents duplicate publishing.
- Each profile has an explicit `createdByUsername`, `cityCode`, `downloadAuthorized`, and
  `publishAuthorized` mapping. Missing authorization fails closed.
- Captions begin with `Courtesy @handle`, preserve the source caption, and include source metadata.
- A 48-hour overlapping lookback catches Reels after delayed or missed GitHub schedules.
- The durable SQLite ledger is restored from and saved to a private S3 object each run.
- First incremental scan creates a baseline instead of flooding the app with old posts.
- Historical import is an explicit `backfill` mode and remains duplicate-safe.
- One bad profile or Reel is logged and skipped without ending the complete run.
- Media requests retry; files are size-limited, transcoded to H.264/AAC MP4, probed, and rejected
  if empty, corrupt, unsupported, or missing a video stream.

## Project layout

```text
reel_monitor/
  cli.py           local/GitHub command entry point
  config.py        environment and profile validation
  database.py      SQLite schema, migrations, checkpoints, unique Reel ledger
  provider.py      authenticated Instagram metadata provider
  media.py         bounded download, FFmpeg normalization, ffprobe validation
  storage.py       encrypted S3 upload and public media URL construction
  publisher.py     Brokket payload and API idempotency
  persistence.py   S3 restore/save and legacy JSON-state import
  service.py       rotating discovery, lookback, round-robin processing, recovery
.github/workflows/monitor.yml   15-minute production workflow
.github/workflows/test.yml      lint and unit-test workflow
```

## Profile configuration

`profiles.json` is the source of truth. A profile is eligible only when both authorization flags
are true:

```json
{
  "profiles": [
    {
      "instagramUsername": "authorized.handle",
      "createdByUsername": "919999999999",
      "state": "Gujarat",
      "city": "Ahmedabad",
      "cityCode": "ahmedabad",
      "enabled": true,
      "downloadAuthorized": true,
      "publishAuthorized": true
    }
  ]
}
```

Only set the authorization flags when the content owner/provider permits download and
republishing. Private, inaccessible, challenged, or unsupported media is skipped.

## Required GitHub Actions secrets

Configure these at **Repository → Settings → Secrets and variables → Actions**:

| Secret | Purpose |
|---|---|
| `CLIP_API_URL` | Brokket clip publishing endpoint |
| `CLIP_API_TOKEN` | Optional bearer token |
| `S3_BUCKET` | Private media/state bucket name |
| `S3_REGION` | AWS region, for example `ap-south-1` |
| `S3_ENDPOINT_URL` | Optional S3-compatible endpoint |
| `PUBLIC_BASE_URL` | CDN/public base used by the Brokket app |
| `S3_ACCESS_KEY_ID` | Least-privilege bucket access key |
| `S3_SECRET_ACCESS_KEY` | Matching secret key |
| `INSTAGRAM_SESSION_USERNAME` | Instagram account that created the session |
| `INSTAGRAM_SESSION_B64` | Base64-encoded Instaloader session file |
| `INSTAGRAM_GRAPH_ACCESS_TOKEN` | Meta token when using the official Graph provider |

Never commit `.env`, session files, AWS CSV files, API tokens, or database files.

### Create the Instagram session secret

Run locally and complete any Instagram challenge interactively:

```powershell
python -m instaloader --login YOUR_INSTAGRAM_USERNAME
```

Copy the generated session file to `data/instagram-session`, test it locally, then encode it
without printing it:

```powershell
[Convert]::ToBase64String(
  [IO.File]::ReadAllBytes("data/instagram-session")
) | Set-Clipboard
```

Save the clipboard value as `INSTAGRAM_SESSION_B64`. Renew it whenever Instagram invalidates the
session. The workflow removes the decoded session at the end of every run.

For connected accounts, set repository variable `INSTAGRAM_PROVIDER=graph`, add
`instagramUserId` to each profile, and configure `INSTAGRAM_GRAPH_ACCESS_TOKEN`. Graph mode is the
preferred production path because it uses Meta's supported API and does not depend on web-session
HTML. Session mode remains available for content the authenticated account is permitted to access.

## Local setup

Requirements: Python 3.12+, FFmpeg/ffprobe, S3 credentials, and an authorized Instagram session.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

Load the environment variables using your preferred secret manager. Then:

```powershell
python -m reel_monitor.cli validate-config
python -m reel_monitor.cli sync --mode baseline --dry-run
python -m reel_monitor.cli sync --mode incremental
```

The compatibility command `python monitor.py` runs an incremental synchronization.

## Historical import

Backfill is explicit and uses the same unique media-ID ledger:

```powershell
python -m reel_monitor.cli sync `
  --mode backfill `
  --city-code ahemdabad `
  --backfill-since 2026-09-01T00:00:00+00:00 `
  --max-publish 20
```

Reels are queued oldest-first and round-robin across accounts, so one account cannot dominate a
batch. Previously published, reserved, or ambiguous API submissions are never reclaimed.

## GitHub Actions operation

The production workflow runs every 15 minutes and may also be started with **Run workflow**. Manual
inputs support city-only synchronization, maximum publish count, incremental/backfill/baseline
mode, a backfill timestamp, and dry-run.

GitHub schedules are best-effort. Synchronization does not depend on an exact start time: the last
seen timestamp is persisted and every incremental query overlaps it by `LOOKBACK_HOURS`.

## Brokket API contract

The publisher sends JSON with:

- `createdByUsername`: mapped Brokket app username
- `text`: `Courtesy @handle`, blank line, then original caption
- `clipMedia`: public video URL, thumbnail URL, dimensions, duration, MIME type, and size
- `cityCode`: normalized mapped city code
- `sourcePlatform`: `instagram`
- `sourceMediaId`: globally unique Instagram media ID
- `sourceUrl`: original Reel URL

The API should persist and uniquely index `sourceMediaId` or honor the `Idempotency-Key` header.
If an API response is lost after submission, the local record becomes `PUBLISH_UNKNOWN` and is not
blindly retried.

## Database states

- `BASELINE`: observed during first incremental scan; not automatically backfilled
- `DISCOVERED`: eligible new Reel
- `PROCESSING`: atomically reserved
- `STORED`: uploaded to the media bucket
- `PUBLISHED`: Brokket confirmed the request
- `FAILED_RETRYABLE`: failure occurred before API submission
- `PUBLISH_UNKNOWN`: API submission outcome was ambiguous; manual reconciliation required

## Tests

```powershell
python -m ruff check reel_monitor tests
python -m pytest -q
```

Tests cover configuration validation, city normalization, authorization, atomic duplicate claims,
terminal-state protection, Brokket payload fields, courtesy/caption formatting, and first-baseline
versus new-Reel behavior.
