import unittest
from unittest.mock import MagicMock, patch

from uploader.web_playback_check import (
    assert_web_playable,
    verify_remote_mp4_web_playable,
)


def _probe(**overrides):
    base = {
        "formatName": "mov,mp4,m4a,3gp,3g2,mj2",
        "videoCodec": "h264",
        "videoCodecTag": "avc1",
        "audioCodec": "aac",
        "width": 1920,
        "height": 1080,
        "duration": 120.5,
        "bitrate": 4_000_000,
        "frameRate": 23.976,
    }
    base.update(overrides)
    return base


class WebPlaybackCheckTests(unittest.TestCase):
    def test_accepts_h264_aac_mp4(self):
        assert_web_playable(_probe())

    def test_accepts_hevc_hvc1_aac(self):
        assert_web_playable(
            _probe(videoCodec="hevc", videoCodecTag="hvc1")
        )

    def test_rejects_non_mp4_container(self):
        with self.assertRaisesRegex(RuntimeError, "Web 可播"):
            assert_web_playable(_probe(formatName="matroska,webm"))

    def test_rejects_unsupported_video_codec(self):
        with self.assertRaisesRegex(RuntimeError, "视频编码"):
            assert_web_playable(_probe(videoCodec="av1"))

    def test_rejects_hevc_without_hvc1_tag(self):
        with self.assertRaisesRegex(RuntimeError, "hvc1"):
            assert_web_playable(
                _probe(videoCodec="hevc", videoCodecTag="hev1")
            )

    def test_rejects_non_aac_audio(self):
        with self.assertRaisesRegex(RuntimeError, "音频编码"):
            assert_web_playable(_probe(audioCodec="dts"))

    def test_rejects_zero_duration(self):
        with self.assertRaisesRegex(RuntimeError, "时长"):
            assert_web_playable(_probe(duration=0))

    def test_rejects_missing_dimensions(self):
        with self.assertRaisesRegex(RuntimeError, "分辨率"):
            assert_web_playable(_probe(width=0, height=1080))

    def test_verify_remote_uses_presigned_url_and_probe(self):
        s3 = MagicMock()
        s3.generate_presigned_url.return_value = "https://r2.example/video.mp4?sig=1"
        probe = _probe(videoCodec="hevc", videoCodecTag="hvc1")

        with patch(
            "uploader.direct_media.probe_direct_media",
            return_value=probe,
        ) as probe_mock:
            result = verify_remote_mp4_web_playable(
                s3, "bucket", "tmdb/movie/1/video.mp4"
            )

        self.assertEqual(probe, result)
        s3.generate_presigned_url.assert_called_once_with(
            "get_object",
            Params={"Bucket": "bucket", "Key": "tmdb/movie/1/video.mp4"},
            ExpiresIn=300,
        )
        probe_mock.assert_called_once_with(
            "https://r2.example/video.mp4?sig=1",
            ffprobe_bin=None,
        )

    def test_verify_remote_fails_when_probe_not_web_playable(self):
        s3 = MagicMock()
        s3.generate_presigned_url.return_value = "https://r2.example/video.mp4"
        with patch(
            "uploader.direct_media.probe_direct_media",
            return_value=_probe(audioCodec="ac3"),
        ):
            with self.assertRaisesRegex(RuntimeError, "音频编码"):
                verify_remote_mp4_web_playable(
                    s3, "bucket", "tmdb/movie/1/video.mp4"
                )


if __name__ == "__main__":
    unittest.main()
