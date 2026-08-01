import unittest
from unittest.mock import patch

from uploader.tmdb import verify_tmdb_metadata


class VerifyTmdbMetadataTests(unittest.TestCase):
    @patch("uploader.tmdb.tmdb_get_json")
    def test_missing_episode_is_soft_warning(self, get_json):
        def side_effect(path, timeout=10):
            if path == "/tv/100":
                return {"id": 100, "name": "Show"}
            if path == "/tv/100/season/1/episode/21":
                return {}
            raise AssertionError(path)

        get_json.side_effect = side_effect
        ok, resolved, err, warn = verify_tmdb_metadata(100, "tv", 1, 21)
        self.assertTrue(ok)
        self.assertEqual("tv", resolved)
        self.assertIsNone(err)
        self.assertIn("未找到分集", warn)
        self.assertIn("S1E21", warn)
        self.assertIn("按文件名季集继续上传", warn)

    @patch("uploader.tmdb.tmdb_get_json")
    def test_missing_show_still_fails(self, get_json):
        get_json.return_value = {}
        ok, resolved, err, warn = verify_tmdb_metadata(999, "tv", 1, 1)
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertIsNone(warn)
        self.assertIn("未找到元数据", err)

    @patch("uploader.tmdb.tmdb_get_json")
    def test_existing_episode_ok(self, get_json):
        def side_effect(path, timeout=10):
            if path == "/tv/100":
                return {"id": 100, "name": "Show"}
            if path == "/tv/100/season/1/episode/1":
                return {"id": 200, "name": "Ep1"}
            raise AssertionError(path)

        get_json.side_effect = side_effect
        ok, resolved, err, warn = verify_tmdb_metadata(100, "tv", 1, 1)
        self.assertTrue(ok)
        self.assertEqual("tv", resolved)
        self.assertIsNone(err)
        self.assertIsNone(warn)


if __name__ == "__main__":
    unittest.main()
