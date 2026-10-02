# Instagram Reel Monitor

A GitHub Actions worker that runs every 15 minutes, monitors configured Instagram profiles, rejects duplicate
Reel IDs, converts videos to mobile-safe MP4, uploads media to S3, and publishes clips to the app
API. It runs in GitHub's cloud, so the laptop can be switched off.

## Security

No credential is stored in this repository. Add these encrypted GitHub Actions secrets under
**Settings → Secrets and variables → Actions**:

- `CLIP_API_URL`
- `CLIP_API_TOKEN` (optional)
- `S3_BUCKET`
- `S3_REGION`
- `S3_ENDPOINT_URL` (optional for AWS S3)
- `PUBLIC_BASE_URL`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `INSTAGRAM_SESSION_USERNAME`
- `INSTAGRAM_SESSION_B64`

Create the last value locally without printing it:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("data/instagram-session")) |
  Set-Clipboard
```

The worker stores its durable deduplication state at
`s3://<bucket>/automation/instagram-reel-monitor/state.json`. On the first successful run it
records a baseline without publishing old reels. Later runs publish only IDs not in that state.

## Safety behavior

- One scheduled run at a time through workflow concurrency.
- Global Reel-ID deduplication across every profile.
- Reel ID is reserved in S3 before the API POST. An ambiguous API response is never blindly
  retried, preventing duplicate posts.
- Profiles are scanned in rotating batches of five to reduce Instagram rate-limit pressure.
- Maximum two new reels per profile per run, published round-robin across profiles.
- Caption starts with `Courtesy @InstagramHandle` and includes the source caption.
- `cityCode` is always sent as a lowercase city slug.
- Private profiles and Instagram challenge pages fail closed.

