import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.subtitles import (
    _external_subtitle_metadata,
    _has_chinese_subtitle,
    find_external_subtitle_files,
    resolve_subtitles_for_upload,
)


class ExternalSubtitleTests(unittest.TestCase):
    def test_find_same_stem_ass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "铁拳教育 - S01E01 - 第 1 集.mkv"
            ass = root / "铁拳教育 - S01E01 - 第 1 集.ass"
            video.write_bytes(b"")
            ass.write_bytes(b"[Script Info]")
            found = find_external_subtitle_files(str(video))
            self.assertEqual([ass], found)

    def test_metadata_defaults_same_stem_to_zh_hans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "show - S01E01.mkv"
            ass = root / "show - S01E01.ass"
            video.write_bytes(b"")
            meta = _external_subtitle_metadata(ass, video)
            self.assertEqual("zh-Hans", meta["category"])

    def test_has_chinese_subtitle(self):
        self.assertTrue(_has_chinese_subtitle([{"category": "zh-Hans"}]))
        self.assertFalse(_has_chinese_subtitle([{"category": "en"}]))

    def test_resolve_prefers_embedded_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "show - S01E01.mkv"
            ass = root / "show - S01E01.ass"
            video.write_bytes(b"")
            ass.write_bytes(b"[Script Info]")

            embedded = [{"lang": "chi", "label": "简体", "file": "sub-0-chi.vtt", "category": "zh-Hans"}]
            with patch("uploader.subtitles.extract_subtitles", return_value=embedded), patch(
                "uploader.subtitles._load_external_subtitles",
                side_effect=AssertionError("should not load external when embedded has Chinese"),
            ):
                result = resolve_subtitles_for_upload(str(video), root)
            self.assertEqual(1, len(result))
            self.assertEqual("zh-Hans", result[0]["category"])


if __name__ == "__main__":
    unittest.main()
