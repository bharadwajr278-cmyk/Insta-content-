"""Backward-compatible entry point for existing deployments."""

from __future__ import annotations

import sys

from reel_monitor.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("sync")
    raise SystemExit(main())
