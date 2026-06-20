#!/usr/bin/env python3
"""
Backfill Apple-compatible HLS WebVTT subtitle tracks for uploaded R2 videos.

Discovery uses the configured Guangying TMDB mirror API, then object writes use Wrangler.
Set CLOUDFLARE_API_TOKEN and GY_SITE_URL (or pass --site-url) before running.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


DEFAULT_SITE_URL = os.environ.get("GY_SITE_URL") or os.environ.get("SITE_URL") or ""
BUCKET = "flix-800-assets"

LANG_BCP47 = {
    "chi": "zh-Hans",
    "zho": "zh-Hans",
    "cmn": "zh-Hans",
    "zhs": "zh-Hans",
    "zh-hans": "zh-Hans",
    "chs": "zh-Hans",
    "zht": "zh-Hant",
    "zh-hant": "zh-Hant",
    "cht": "zh-Hant",
    "yue": "zh-Hant",
    "can": "zh-Hant",
    "eng": "en",
    "en": "en",
    "jpn": "ja",
    "ja": "ja",
    "kor": "ko",
    "ko": "ko",
}


def normalize_site_url(value: str) -> str:
    site_url = (value or "").strip().rstrip("/")
    if not site_url:
        raise ValueError("--site-url or GY_SITE_URL is required")
    if not re.match(r"^https?://", site_url, re.IGNORECASE):
        raise ValueError("site URL must start with http:// or https://")
    return site_url


def fetch_json(url: str, site_url: str):
    return json.loads(fetch_text(url, site_url))


def fetch_text(url: str, site_url: str) -> str:
    request = Request(url, headers={
        "Accept": "application/json,text/plain,*/*",
        "Origin": site_url,
        "User-Agent": "GuangyingSubtitleBackfill/1.0",
    })
    with urlopen(request, timeout=12) as response:
        return response.read().decode("utf-8", errors="ignore")


def normalize_lang(lang: str) -> str:
    normalized = (lang or "und").strip().replace("_", "-")
    return LANG_BCP47.get(normalized.lower(), normalized or "und")


def safe_track_dir(lang: str, used: dict[str, int]) -> str:
    base = re.sub(r"[^A-Za-z0-9-]+", "-", normalize_lang(lang)).strip("-") or "und"
    used[base] = used.get(base, 0) + 1
    return base if used[base] == 1 else f"{base}-{used[base]}"


def normalize_vtt(text: str) -> str:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("WEBVTT"):
        text = f"WEBVTT\n\n{text}"
    if "X-TIMESTAMP-MAP=" not in text:
        text = re.sub(
            r"^WEBVTT[^\n]*\n?",
            lambda m: f"{m.group(0).strip()}\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n",
            text,
            count=1,
        )
    return text.rstrip() + "\n"


def parse_vtt_timestamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (TypeError, ValueError):
        return 0.0


def vtt_duration(text: str) -> float:
    duration = 1.0
    for match in re.finditer(r"-->\s*([0-9:.,]+)", text):
        duration = max(duration, parse_vtt_timestamp(match.group(1)))
    return duration


def hls_attr(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', r'\"')


def object_key_from_source(url: str) -> str | None:
    url = (url or "").strip()
    url = re.sub(r"^https?://[^/]+", "", url)
    url = re.sub(r"^/api/r2/", "", url)
    url = re.sub(r"^/r2/", "", url)
    if not url.startswith("videos/") or not url.endswith("/master.m3u8"):
        return None
    return url


def tmdb_items(site_url: str, media_type: str, page_size: int, max_pages: int):
    page = 1
    while True:
        if max_pages and page > max_pages:
            break
        data = fetch_json(f"{site_url}/3/discover/{media_type}?page={page}", site_url)
        items = data.get("results") or []
        print(f"discover {media_type} page {page}: {len(items)} items", flush=True)
        if not items:
            break
        for item in items[:page_size]:
            tmdb_id = item.get("id")
            if isinstance(tmdb_id, int) and tmdb_id > 0:
                yield media_type, tmdb_id
        page += 1


def discover_r2_dirs(site_url: str, page_size: int, max_pages: int) -> list[str]:
    dirs: set[str] = set()
    for media_type in ("movie", "tv"):
        for item_media_type, tmdb_id in tmdb_items(site_url, media_type, page_size, max_pages):
            try:
                detail = fetch_json(f"{site_url}/3/{item_media_type}/{tmdb_id}", site_url)
            except (HTTPError, URLError, TimeoutError) as exc:
                print(f"skip tmdb:{item_media_type}:{tmdb_id}: detail failed: {exc}")
                continue
            guangying = detail.get("guangying") or {}
            for source in guangying.get("play_sources") or []:
                key = object_key_from_source(source.get("url", ""))
                if key:
                    dirs.add(key.rsplit("/", 1)[0])
    return sorted(dirs)


def run_wrangler(args: list[str], env: dict[str, str]) -> None:
    cmd = ["npx", "wrangler", *args]
    subprocess.run(cmd, check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=120)


def r2_get(bucket: str, key: str, target: Path, env: dict[str, str]) -> bool:
    try:
        run_wrangler(["r2", "object", "get", f"{bucket}/{key}", "--remote", "--file", str(target)], env)
        return True
    except subprocess.CalledProcessError:
        return False


def r2_put(bucket: str, key: str, source: Path, content_type: str, env: dict[str, str]) -> None:
    run_wrangler([
        "r2", "object", "put", f"{bucket}/{key}", "--remote",
        "--file", str(source), "--content-type", content_type,
    ], env)


def build_subtitle_tracks(site_url: str, r2_dir: str, subtitles: list[dict], workdir: Path) -> list[dict]:
    used: dict[str, int] = {}
    tracks: list[dict] = []
    for index, sub in enumerate(subtitles):
        url = str(sub.get("url") or "")
        if not url:
            continue
        text = normalize_vtt(fetch_text(urljoin(site_url + "/", url), site_url))
        lang = normalize_lang(str(sub.get("lang") or "und"))
        track_dir_name = safe_track_dir(lang, used)
        track_dir = workdir / "subs" / track_dir_name
        track_dir.mkdir(parents=True, exist_ok=True)

        (track_dir / "full.vtt").write_text(text, encoding="utf-8")
        duration = max(1.0, vtt_duration(text))
        playlist = "\n".join([
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{max(1, math.ceil(duration))}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f"#EXTINF:{duration:.3f},",
            "full.vtt",
            "#EXT-X-ENDLIST",
            "",
        ])
        (track_dir / "stream.m3u8").write_text(playlist, encoding="utf-8")

        name = str(sub.get("label") or sub.get("name") or lang)
        duplicate_count = used.get(re.sub(r"[^A-Za-z0-9-]+", "-", lang).strip("-") or "und", 1)
        if duplicate_count > 1:
            name = f"{name} {duplicate_count}"
        tracks.append({
            "lang": lang,
            "name": name,
            "uri": f"subs/{track_dir_name}/stream.m3u8",
            "default": index == 0,
        })
    return tracks


def inject_master_subtitles(master: str, tracks: list[dict]) -> str:
    lines = [
        line for line in master.replace("\r\n", "\n").split("\n")
        if not line.startswith("#EXT-X-MEDIA:TYPE=SUBTITLES")
    ]
    lines = [re.sub(r',SUBTITLES="[^"]+"', "", line) for line in lines]
    stream_idx = next((i for i, line in enumerate(lines) if line.startswith("#EXT-X-STREAM-INF:")), -1)
    if stream_idx < 0:
        raise ValueError("master.m3u8 missing EXT-X-STREAM-INF")

    subtitle_lines = []
    for track in tracks:
        subtitle_lines.append(
            '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",'
            f'NAME="{hls_attr(track["name"])}",'
            f'DEFAULT={"YES" if track["default"] else "NO"},'
            'AUTOSELECT=YES,FORCED=NO,'
            f'LANGUAGE="{hls_attr(track["lang"])}",'
            f'URI="{hls_attr(track["uri"])}"'
        )
    lines[stream_idx:stream_idx] = subtitle_lines + [""]
    stream_idx = next(i for i, line in enumerate(lines) if line.startswith("#EXT-X-STREAM-INF:"))
    lines[stream_idx] = f'{lines[stream_idx]},SUBTITLES="subs"'
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).rstrip() + "\n")


def backfill_dir(site_url: str, r2_dir: str, bucket: str, env: dict[str, str], dry_run: bool, force: bool) -> str:
    subtitles_url = f"{site_url}/api/r2/subtitles/{r2_dir}"
    subtitles = fetch_json(subtitles_url, site_url)
    if not subtitles:
        return "no_subtitles"

    with tempfile.TemporaryDirectory(prefix="gy-subtitles-") as tmp:
        workdir = Path(tmp)
        master_path = workdir / "master.m3u8"
        if not r2_get(bucket, f"{r2_dir}/master.m3u8", master_path, env):
            return "no_master"

        master = master_path.read_text(encoding="utf-8", errors="ignore")
        if not force and 'TYPE=SUBTITLES' in master and 'SUBTITLES="subs"' in master:
            return "already_ok"

        tracks = build_subtitle_tracks(site_url, r2_dir, subtitles, workdir)
        if not tracks:
            return "no_valid_tracks"

        master_path.write_text(inject_master_subtitles(master, tracks), encoding="utf-8")
        if dry_run:
            return f"would_update:{len(tracks)}"

        for file_path in sorted((workdir / "subs").glob("**/*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(workdir).as_posix()
            content_type = "application/vnd.apple.mpegurl" if file_path.suffix == ".m3u8" else "text/vtt"
            r2_put(bucket, f"{r2_dir}/{rel}", file_path, content_type, env)
        r2_put(bucket, f"{r2_dir}/master.m3u8", master_path, "application/vnd.apple.mpegurl", env)
        return f"updated:{len(tracks)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-url", default=DEFAULT_SITE_URL, help="streaming site origin; defaults to GY_SITE_URL or SITE_URL")
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dir", action="append", dest="dirs", default=[], help="specific R2 directory to process; can be repeated")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="rewrite masters that already contain subtitle tracks")
    args = parser.parse_args()

    try:
        site_url = normalize_site_url(args.site_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not os.environ.get("CLOUDFLARE_API_TOKEN"):
        print("CLOUDFLARE_API_TOKEN is required", file=sys.stderr)
        return 2
    if not shutil.which("npx"):
        print("npx is required to run wrangler", file=sys.stderr)
        return 2

    env = dict(os.environ)
    dirs = sorted(set(args.dirs)) if args.dirs else discover_r2_dirs(site_url, args.page_size, args.max_pages)
    if args.limit > 0 and not args.dirs:
        dirs = dirs[:args.limit]
    print(f"discovered r2 dirs: {len(dirs)}", flush=True)

    summary: dict[str, int] = {}
    for i, r2_dir in enumerate(dirs, 1):
        print(f"[{i}/{len(dirs)}] {r2_dir}: checking", flush=True)
        try:
            status = backfill_dir(site_url, r2_dir, args.bucket, env, args.dry_run, args.force)
        except Exception as exc:
            status = "error"
            print(f"[{i}/{len(dirs)}] {r2_dir}: error: {exc}")
        else:
            print(f"[{i}/{len(dirs)}] {r2_dir}: {status}")
        key = status.split(":", 1)[0]
        summary[key] = summary.get(key, 0) + 1

    print("summary:", json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
