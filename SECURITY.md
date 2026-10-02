# Security policy

Do not commit credentials, session cookies, access tokens, `.env` files, database files, or AWS
access-key CSV files. Runtime credentials belong only in encrypted GitHub Actions secrets.

If a credential is ever committed, deleting the file is not enough: immediately revoke/rotate the
credential and remove it from Git history.

The recommended AWS identity is a dedicated least-privilege IAM user restricted to this bucket and
the `instagram/*` plus `automation/instagram-reel-monitor/*` prefixes. Public read access should be
provided through the configured media delivery URL, not by exposing AWS credentials.
