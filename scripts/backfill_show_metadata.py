#!/usr/bin/env python3
"""
Backfill show/movie metadata to R2 without re-uploading video.

Writes tvshow.nfo / movie.nfo + poster.jpg + fanart.jpg via write_show_nfo().

Usage (inside MoviePilot container or with matching env):
  python3 scripts/backfill_show_metadata.py 239385 259837

Required env / plugin config:
  - R2 credentials (CF token auto-config or manual keys)
  - TMDB token (or MoviePilot TMDB_API_KEY)
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plugins.v2", "clouduploader"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from uploader.runtime_config import settings  # noqa: E402
from uploader.register import write_show_nfo  # noqa: E402


def configure_from_env() -> list[str]:
    settings.configure(
        r2_account_id=os.environ.get("R2_ACCOUNT_ID", ""),
        r2_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
        r2_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        r2_bucket=os.environ.get("R2_BUCKET", "flix-800-assets"),
        api_base=os.environ.get("API_BASE", "https://aeun.cn"),
        api_admin_key=os.environ.get("API_ADMIN_KEY", ""),
        api_username=os.environ.get("API_USERNAME", ""),
        api_password=os.environ.get("API_PASSWORD", ""),
        tmdb_token=os.environ.get("TMDB_TOKEN", os.environ.get("TMDB_API_KEY", "")),
        hls_output_dir=os.environ.get("HLS_OUTPUT_DIR", "/tmp/hls-output"),
    )
    return settings.validate()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill R2 show metadata (NFO + posters)")
    parser.add_argument("tmdb_ids", nargs="+", type=int, help="TMDB TV/movie IDs")
    parser.add_argument("--type", choices=("tv", "movie"), default="tv")
    args = parser.parse_args()

    missing = configure_from_env()
    if missing:
        print("配置缺失:", "、".join(missing), file=sys.stderr)
        return 1

    for tmdb_id in args.tmdb_ids:
        print(f"→ tmdb:{tmdb_id}")
        write_show_nfo(tmdb_id, args.type, print_fn=print)

    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
