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
records a baseline. With `BACKFILL_EXISTING_REELS=true`, baseline IDs are then published gradually
in profile rotation; otherwise they are only recorded. Later runs publish only unseen Reel IDs.

### Privacy and compliance

- The automation runs on an ephemeral GitHub-hosted runner. The laptop may be switched off, but
  GitHub and destination services can still see the runner's network address.
- It does not use a VPN, proxy rotation, browser fingerprint spoofing, or anti-detection behavior.
- Credentials remain in encrypted GitHub Actions secrets and the restored Instagram session file
  is restricted to the workflow process on the temporary runner.
- Signed media URL query strings and fragments are removed from logs so temporary access tokens
  are not exposed in workflow output.
- Browser contexts and downloaded media live only for the duration of a run; temporary working
  directories are deleted after each reel.

## Safety behavior

- One scheduled run at a time through workflow concurrency.
- Global Reel-ID deduplication across every profile.
- Existing baseline reels are eligible for controlled backfill, so older reels inside the configured
  scan window are not hidden by a newer Reel ID that has already been published.
- Reel ID is reserved in S3 before the API POST. An ambiguous API response is never blindly
  retried, preventing duplicate posts.
- Profiles are scanned in rotating batches of five to reduce Instagram rate-limit pressure.
- Every enabled profile in `profiles.json` is covered by that rotation; with the current schedule,
  each profile is revisited automatically without requiring the laptop to remain online.
- Maximum two new reels per profile per run, published round-robin across profiles.
- Caption starts with `Courtesy @InstagramHandle` and includes the source caption.
- `cityCode` is always sent as a lowercase city slug.
- Private profiles and Instagram challenge pages fail closed.

