import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.direct_media import prepare_direct_mp4, probe_direct_media


def pairwise(items):
    return [items[i:i + 2] for i in range(len(items) - 1)]


class DirectMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output = Path(self.temp_dir.name) / "direct.mp4"

        self.run_patcher = patch("uploader.direct_media.subprocess.run")
        self.resolve_patcher = patch(
            "uploader.direct_media.resolve_tool",
            side_effect=lambda configured: f"/tools/{Path(configured).name}",
        )
        self.video_patcher = patch(
            "uploader.direct_media.probe_video_info",
            return_value={
                "codec": "hevc",
                "width": 1920,
                "height": 1080,
                "bitrate": 4_000_000,
                "frame_rate": 23.976,
                "duration": 60.0,
            },
        )
        self.audio_patcher = patch(
            "uploader.direct_media.probe_audio_streams",
            return_value=[
                {
                    "audio_index": 0,
                    "lang": "eng",
                    "title": "English DTS",
                    "codec": "dts",
                    "channels": 6,
                    "disposition": {"default": 1},
                }
            ],
        )
        self.select_patcher = patch(
            "uploader.direct_media.select_audio_streams",
            return_value=[
                {
                    "audio_index": 0,
                    "lang": "eng",
                    "title": "English DTS",
                    "codec": "dts",
                    "channels": 6,
                    "is_default": True,
                }
            ],
        )
        self.output_probe_patcher = patch(
            "uploader.direct_media.probe_direct_media",
            return_value={
                "formatName": "mov,mp4,m4a,3gp,3g2,mj2",
                "videoCodec": "hevc",
                "videoCodecTag": "hvc1",
                "width": 1920,
                "height": 1080,
                "bitrate": 4_000_000,
                "frameRate": 23.976,
                "duration": 60.0,
                "audioCodec": "aac",
            },
        )

        self.run_mock = self.run_patcher.start()
        self.resolve_patcher.start()
        self.video_mock = self.video_patcher.start()
        self.audio_mock = self.audio_patcher.start()
        self.select_mock = self.select_patcher.start()
        self.output_probe_mock = self.output_probe_patcher.start()
        self.addCleanup(patch.stopall)

        def create_output(*_args, **_kwargs):
            self.output.write_bytes(b"mp4")
            return subprocess.CompletedProcess([], 0, "", "")

        self.run_mock.side_effect = create_output

    def test_hevc_mkv_copies_video_tags_hvc1_and_transcodes_dts(self):
        result = prepare_direct_mp4("movie.mkv", self.output, False, "en")
        cmd = self.run_mock.call_args.args[0]
        self.assertIn(["-c:v", "copy"], pairwise(cmd))
        self.assertIn(["-tag:v", "hvc1"], pairwise(cmd))
        self.assertIn(["-c:a", "aac"], pairwise(cmd))
        self.assertIn(["-profile:a", "aac_low"], pairwise(cmd))
        self.assertTrue(result["videoCopied"])
        self.assertFalse(result["audioCopied"])

    def test_h264_compat_transcodes_hevc_video(self):
        prepare_direct_mp4("movie.mkv", self.output, True, "en")
        cmd = self.run_mock.call_args.args[0]
        self.assertIn("libx264", cmd)
        self.assertIn("yuv420p", cmd)

    def test_h264_compat_copies_existing_h264_video(self):
        self.video_mock.return_value["codec"] = "h264"
        result = prepare_direct_mp4("movie.mp4", self.output, True, "en")
        cmd = self.run_mock.call_args.args[0]
        self.assertIn(["-c:v", "copy"], pairwise(cmd))
        self.assertNotIn("libx264", cmd)
        self.assertTrue(result["videoCopied"])

    def test_faststart_is_always_enabled(self):
        prepare_direct_mp4("movie.mp4", self.output, False, "en")
        cmd = self.run_mock.call_args.args[0]
        self.assertIn("+faststart", cmd)

    def test_selects_tmdb_original_default_audio(self):
        prepare_direct_mp4("movie.mkv", self.output, False, "ja")
        self.select_mock.assert_called_once()
        self.assertEqual("ja", self.select_mock.call_args.args[1])
        cmd = self.run_mock.call_args.args[0]
        self.assertIn(["-map", "0:a:0"], pairwise(cmd))

    def test_aac_audio_is_copied(self):
        selected = self.select_mock.return_value[0]
        selected["codec"] = "aac"
        result = prepare_direct_mp4("movie.mp4", self.output, False, "en")
        cmd = self.run_mock.call_args.args[0]
        self.assertIn(["-c:a", "copy"], pairwise(cmd))
        self.assertTrue(result["audioCopied"])

    def test_missing_audio_track_is_rejected(self):
        self.select_mock.return_value = []
        with self.assertRaisesRegex(RuntimeError, "音轨"):
            prepare_direct_mp4("movie.mkv", self.output, False, "en")

    def test_ffmpeg_failure_includes_stderr_tail(self):
        self.output.write_bytes(b"partial")
        self.run_mock.side_effect = None
        self.run_mock.return_value = subprocess.CompletedProcess(
            [], 1, "", "excluded marker" + "x" * 1300 + " useful failure"
        )
        with self.assertRaisesRegex(RuntimeError, "useful failure") as raised:
            prepare_direct_mp4("movie.mkv", self.output, False, "en")
        self.assertNotIn("excluded marker", str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_empty_output_is_rejected(self):
        self.run_mock.side_effect = lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "", ""
        )
        with self.assertRaisesRegex(RuntimeError, "非空"):
            prepare_direct_mp4("movie.mkv", self.output, False, "en")

    def test_non_mp4_output_is_rejected(self):
        with patch("uploader.direct_media.probe_direct_media") as probe_mock:
            probe_mock.return_value = {
                "formatName": "matroska,webm",
                "videoCodec": "hevc",
                "audioCodec": "aac",
            }
            with self.assertRaisesRegex(RuntimeError, "容器不是 MP4"):
                prepare_direct_mp4("movie.mkv", self.output, False, "en")

    def test_output_missing_video_is_rejected(self):
        with patch("uploader.direct_media.probe_direct_media") as probe_mock:
            probe_mock.return_value = {
                "formatName": "mov,mp4,m4a,3gp,3g2,mj2",
                "videoCodec": "",
                "audioCodec": "aac",
            }
            with self.assertRaisesRegex(RuntimeError, "缺少视频或音频轨"):
                prepare_direct_mp4("movie.mkv", self.output, False, "en")

    def test_output_missing_audio_is_rejected(self):
        with patch("uploader.direct_media.probe_direct_media") as probe_mock:
            probe_mock.return_value = {
                "formatName": "mov,mp4,m4a,3gp,3g2,mj2",
                "videoCodec": "hevc",
                "audioCodec": "",
            }
            with self.assertRaisesRegex(RuntimeError, "缺少视频或音频轨"):
                prepare_direct_mp4("movie.mkv", self.output, False, "en")

    def test_conversion_timeout_removes_partial_output(self):
        self.output.write_bytes(b"partial")
        self.run_mock.side_effect = subprocess.TimeoutExpired(["ffmpeg"], 14400)
        with self.assertRaisesRegex(RuntimeError, "FFmpeg.*超时"):
            prepare_direct_mp4("movie.mkv", self.output, False, "en")
        self.assertFalse(self.output.exists())

    def test_probe_timeout_is_converted_to_runtime_error(self):
        self.video_mock.side_effect = subprocess.TimeoutExpired(["ffprobe"], 60)
        with self.assertRaisesRegex(RuntimeError, "ffprobe.*超时"):
            prepare_direct_mp4("movie.mkv", self.output, False, "en")

    def test_output_probe_timeout_removes_generated_output(self):
        self.output_probe_mock.side_effect = RuntimeError("ffprobe 验证超时（60 秒）")
        with self.assertRaisesRegex(RuntimeError, "ffprobe.*超时"):
            prepare_direct_mp4("movie.mkv", self.output, False, "en")
        self.assertFalse(self.output.exists())

    def test_resolved_ffprobe_is_passed_to_source_probes(self):
        prepare_direct_mp4("movie.mkv", self.output, False, "en")
        self.video_mock.assert_called_once_with(
            "movie.mkv", ffprobe_bin="/tools/ffprobe"
        )
        self.audio_mock.assert_called_once_with(
            "movie.mkv", ffprobe_bin="/tools/ffprobe"
        )
        self.output_probe_mock.assert_called_once_with(
            str(self.output), ffprobe_bin="/tools/ffprobe"
        )


class DirectMediaProbeTests(unittest.TestCase):
    @patch("uploader.direct_media.resolve_tool", return_value="/tools/ffprobe")
    @patch("uploader.direct_media.subprocess.run")
    def test_probe_returns_normalized_media_fields(self, run_mock, _resolve_mock):
        run_mock.return_value = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "format": {
                        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                        "duration": "3.5",
                        "bit_rate": "123456",
                    },
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "hevc",
                            "width": 1280,
                            "height": 720,
                            "avg_frame_rate": "24000/1001",
                        },
                        {"codec_type": "audio", "codec_name": "aac"},
                    ],
                }
            ),
            "",
        )
        result = probe_direct_media("movie.mp4")
        self.assertEqual("mov,mp4,m4a,3gp,3g2,mj2", result["formatName"])
        self.assertEqual("hevc", result["videoCodec"])
        self.assertEqual("aac", result["audioCodec"])
        self.assertAlmostEqual(23.976, result["frameRate"], places=3)

    @patch("uploader.direct_media.resolve_tool", return_value="/tools/ffprobe")
    @patch("uploader.direct_media.subprocess.run")
    def test_probe_timeout_is_converted(self, run_mock, _resolve_mock):
        run_mock.side_effect = subprocess.TimeoutExpired(["ffprobe"], 60)
        with self.assertRaisesRegex(RuntimeError, "ffprobe.*超时"):
            probe_direct_media("movie.mp4")


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required"
)
class DirectMediaSmokeTests(unittest.TestCase):
    def test_generated_mkv_becomes_valid_mp4(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mkv"
            output = Path(temp_dir) / "output.mp4"
            subprocess.run(
                [
                    shutil.which("ffmpeg"),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=160x90:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:duration=1",
                    "-c:v",
                    "libx264",
                    "-c:a",
                    "pcm_s16le",
                    str(source),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = prepare_direct_mp4(str(source), output, False, "en")
            probe = probe_direct_media(str(output))
            self.assertIn("mp4", probe["formatName"])
            self.assertEqual("aac", probe["audioCodec"])
            self.assertTrue(result["videoCopied"])
            self.assertFalse(result["audioCopied"])


if __name__ == "__main__":
    unittest.main()
