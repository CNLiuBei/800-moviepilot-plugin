import unittest

from uploader.resolution_key import (
    append_resolution_to_r2_path,
    format_source_label,
    normalize_quality_key,
    quality_r2_segment,
)


class ResolutionKeyTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_quality_key("4K"), "2160p")
        self.assertEqual(normalize_quality_key("BluRay 1080p"), "1080p")
        self.assertEqual(normalize_quality_key(""), "未知")

    def test_r2_segment(self):
        self.assertEqual(quality_r2_segment("未知"), "unknown")
        self.assertEqual(quality_r2_segment("1080p"), "1080p")

    def test_label(self):
        self.assertEqual(format_source_label("1080p"), "1080p")
        self.assertEqual(format_source_label("未知"), "未知")

    def test_append_path(self):
        path, key = append_resolution_to_r2_path(
            "tmdb/tv/1/season/1/episode/2", "1080p"
        )
        self.assertEqual(key, "1080p")
        self.assertEqual(path, "tmdb/tv/1/season/1/episode/2/1080p")


if __name__ == "__main__":
    unittest.main()
