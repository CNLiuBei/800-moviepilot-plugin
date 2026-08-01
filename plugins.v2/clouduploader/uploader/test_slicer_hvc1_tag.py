"""HEVC CMAF HLS must set -tag:v hvc1 for Apple / VidHub / Infuse."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.slicer import apple_hls_slice


def pairwise(items):
    return [items[i:i + 2] for i in range(len(items) - 1)]


class AppleHlsSliceHvc1TagTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = Path(self.temp_dir.name)

        self.run_patcher = patch("uploader.slicer.subprocess.run")
        self.video_patcher = patch(
            "uploader.slicer.probe_video_info",
            return_value={
                "codec": "hevc",
                "width": 1920,
                "height": 1080,
                "bitrate": 4_000_000,
                "average_bitrate": 2_000_000,
                "frame_rate": 25.0,
                "duration": 60.0,
                "profile": "Main",
                "level": 120,
            },
        )
        self.audio_patcher = patch(
            "uploader.slicer.probe_audio_streams",
            return_value=[
                {
                    "audio_index": 0,
                    "lang": "chi",
                    "title": "中文",
                    "codec": "aac",
                    "channels": 2,
                    "disposition": {"default": 1},
                }
            ],
        )
        self.select_patcher = patch(
            "uploader.slicer.select_audio_streams",
            return_value=[
                {
                    "audio_index": 0,
                    "lang": "chi",
                    "title": "中文",
                    "codec": "aac",
                    "channels": 2,
                    "is_default": True,
                }
            ],
        )

        self.run_mock = self.run_patcher.start()
        self.video_mock = self.video_patcher.start()
        self.audio_patcher.start()
        self.select_patcher.start()
        self.addCleanup(patch.stopall)

        def create_outputs(*_args, **_kwargs):
            (self.output_dir / "stream.m3u8").write_text(
                "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg-0.m4s\n",
                encoding="utf-8",
            )
            (self.output_dir / "init.mp4").write_bytes(b"init")
            (self.output_dir / "seg-0.m4s").write_bytes(b"seg")
            return subprocess.CompletedProcess([], 0, "", "")

        self.run_mock.side_effect = create_outputs

    def test_hevc_slice_adds_hvc1_tag(self):
        result = apple_hls_slice(
            "show.mkv",
            self.output_dir,
            ffmpeg_bin="/tools/ffmpeg",
            ffprobe_bin="/tools/ffprobe",
        )
        self.assertIsNotNone(result)
        cmd = self.run_mock.call_args.args[0]
        self.assertEqual("/tools/ffmpeg", cmd[0])
        self.assertIn(["-c:v", "copy"], pairwise(cmd))
        self.assertIn(["-tag:v", "hvc1"], pairwise(cmd))
        self.assertEqual(result["videoCodec"], "hvc1.1.6.L120")

    def test_h264_slice_omits_hvc1_tag(self):
        self.video_mock.return_value["codec"] = "h264"
        result = apple_hls_slice(
            "show.mkv",
            self.output_dir,
            ffmpeg_bin="/tools/ffmpeg",
            ffprobe_bin="/tools/ffprobe",
        )
        self.assertIsNotNone(result)
        cmd = self.run_mock.call_args.args[0]
        self.assertNotIn("hvc1", cmd)
        self.assertTrue(str(result["videoCodec"]).startswith("avc1."))


if __name__ == "__main__":
    unittest.main()
