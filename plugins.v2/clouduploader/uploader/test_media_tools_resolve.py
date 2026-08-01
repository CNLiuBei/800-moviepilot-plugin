import unittest
from unittest.mock import patch

from uploader.env import resolve_ffmpeg_tools, resolved_bin
from uploader.runtime_config import settings


class MediaToolsResolveTests(unittest.TestCase):
    def test_resolved_bin_prefers_explicit_resolved_path(self):
        with patch("uploader.env.resolve_tool", side_effect=lambda name: f"/bin/{name}"):
            self.assertEqual("/bin/custom-ff", resolved_bin("custom-ff", "ffmpeg"))

    def test_resolved_bin_keeps_explicit_when_unresolved(self):
        with patch("uploader.env.resolve_tool", return_value=None):
            self.assertEqual("fake-ffmpeg", resolved_bin("fake-ffmpeg", "ffmpeg"))

    def test_resolved_bin_falls_back_to_configured(self):
        with patch("uploader.env.resolve_tool", side_effect=lambda name: f"/opt/{name}"):
            self.assertEqual("/opt/ffmpeg", resolved_bin(None, "ffmpeg"))

    def test_resolve_ffmpeg_tools_uses_settings(self):
        settings.configure(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
        with patch(
            "uploader.env.resolve_tool",
            side_effect=lambda name: f"/tools/{name}",
        ):
            ffmpeg, ffprobe = resolve_ffmpeg_tools()
        self.assertEqual("/tools/ffmpeg", ffmpeg)
        self.assertEqual("/tools/ffprobe", ffprobe)


if __name__ == "__main__":
    unittest.main()
