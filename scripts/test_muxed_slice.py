#!/usr/bin/env python3
"""本地测试 FFmpeg fMP4 HLS 切片（无需 MoviePilot 环境）。"""
from __future__ import annotations

import argparse
import subprocess
import sys
import types
from pathlib import Path

PLUGIN_V2 = Path(__file__).resolve().parents[1] / "plugins.v2"
CLOUD = PLUGIN_V2 / "clouduploader"
sys.path.insert(0, str(PLUGIN_V2))

_stub = types.ModuleType("clouduploader")
_stub.__path__ = [str(CLOUD)]
sys.modules.setdefault("clouduploader", _stub)

from clouduploader.uploader.runtime_config import settings  # noqa: E402
from clouduploader.uploader import slicer, manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="测试 FFmpeg fMP4 HLS 切片")
    parser.add_argument("input", type=Path, help="输入 MKV/MP4")
    parser.add_argument("-o", "--output", type=Path, default=Path("/tmp/gy-ffmpeg-hls-test"))
    parser.add_argument("--lang", default="ko", help="TMDB original_language")
    parser.add_argument("-t", "--duration", type=int, default=180, help="仅测试前 N 秒（0=全片）")
    args = parser.parse_args()

    if not args.input.is_file():
        print(f"❌ 文件不存在: {args.input}")
        return 1

    settings.configure(
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        hls_segment_seconds=6,
    )

    work_input = args.input
    if args.duration > 0:
        clip_path = args.output.parent / "test_clip.mkv"
        clip_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"✂️  截取前 {args.duration}s → {clip_path}")
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(args.input), "-t", str(args.duration), "-c", "copy", str(clip_path)],
            check=True,
            capture_output=True,
        )
        work_input = clip_path

    if args.output.exists():
        import shutil
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True, exist_ok=True)

    print(f"🎬 FFmpeg 切片: {work_input.name}")
    result = slicer.apple_hls_slice(
        str(work_input), args.output, print_fn=print, original_language=args.lang,
    )
    if not result:
        print("❌ 切片失败")
        return 1

    print(f"\n📋 hls={result.get('hlsVideo')} codecs={result.get('videoCodec')},{result.get('audioCodec')}")
    info = manifest.validate_hls_media_playlists(args.output)
    manifest.generate_hls_master(result, args.output)
    manifest.generate_dash_mpd(result, info, args.output)

    print("\n--- master.m3u8 ---")
    print((args.output / "master.m3u8").read_text(encoding="utf-8"))
    print(f"\n📁 文件: {', '.join(sorted(p.name for p in args.output.iterdir() if p.is_file())[:15])}")
    print(f"\n🌐 本地播放: cd {args.output} && python3 -m http.server 8765")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
