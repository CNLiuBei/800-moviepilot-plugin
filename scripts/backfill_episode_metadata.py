#!/usr/bin/env python3
"""
Backfill episode.nfo + tmdb/t/p still mirrors for existing R2 uploads.

Usage (inside MoviePilot container or with matching env):
  python3 scripts/backfill_episode_metadata.py 239385 259837
  python3 scripts/backfill_episode_metadata.py 239385 --season 1 --episode 1
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plugins.v2", "clouduploader"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from uploader.runtime_config import settings  # noqa: E402
from uploader.register import write_episode_nfo  # noqa: E402

_HTTPX = {"trust_env": False, "timeout": 30}


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


def fetch_meta_episodes(api_base: str, tmdb_id: int) -> list[tuple[int, int]]:
    url = f"{api_base.rstrip('/')}/addon/meta/series/tmdb%3A{tmdb_id}.json"
    response = httpx.get(url, **_HTTPX)
    response.raise_for_status()
    videos = response.json().get("meta", {}).get("videos") or []
    rows: list[tuple[int, int]] = []
    for video in videos:
        season = video.get("season")
        episode = video.get("episode")
        if season is None or episode is None:
            continue
        rows.append((int(season), int(episode)))
    return sorted(set(rows))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill R2 episode metadata (NFO + still mirrors)")
    parser.add_argument("tmdb_ids", nargs="+", type=int, help="TMDB TV IDs")
    parser.add_argument("--season", type=int, help="Only backfill this season")
    parser.add_argument("--episode", type=int, help="Only backfill this episode (requires --season)")
    args = parser.parse_args()

    if args.episode is not None and args.season is None:
        print("使用 --episode 时必须同时指定 --season", file=sys.stderr)
        return 1

    missing = configure_from_env()
    if missing:
        print("配置缺失:", "、".join(missing), file=sys.stderr)
        return 1

    api_base = settings.API_BASE or "https://aeun.cn"

    for tmdb_id in args.tmdb_ids:
        if args.season is not None and args.episode is not None:
            episodes = [(args.season, args.episode)]
        elif args.season is not None:
            episodes = [
                (season, episode)
                for season, episode in fetch_meta_episodes(api_base, tmdb_id)
                if season == args.season
            ]
        else:
            episodes = fetch_meta_episodes(api_base, tmdb_id)

        if not episodes:
            print(f"→ tmdb:{tmdb_id} 无分集，跳过")
            continue

        print(f"→ tmdb:{tmdb_id} ({len(episodes)} 集)")
        for season, episode in episodes:
            r2_path = f"tmdb/tv/{tmdb_id}/season/{season}/episode/{episode}"
            print(f"  S{season:02d}E{episode:02d}")
            write_episode_nfo(
                tmdb_id,
                season,
                episode,
                r2_path,
                resolution="original",
                print_fn=print,
            )

    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
