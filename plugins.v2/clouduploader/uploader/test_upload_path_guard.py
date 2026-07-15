import os
import tempfile
import unittest
from pathlib import Path

from uploader.upload_path_guard import assert_filepath_allowed


class UploadPathGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "library"
        self.root.mkdir()
        self.allowed = self.root / "movie.mkv"
        self.allowed.write_bytes(b"ok")
        self.outside = Path(self.temp.name) / "secret.txt"
        self.outside.write_bytes(b"secret")

    def test_allows_file_under_root(self):
        resolved, err = assert_filepath_allowed(str(self.allowed), [str(self.root)])
        self.assertIsNone(err)
        self.assertEqual(os.path.realpath(self.allowed), resolved)

    def test_rejects_file_outside_roots(self):
        resolved, err = assert_filepath_allowed(str(self.outside), [str(self.root)])
        self.assertIsNone(resolved)
        self.assertIn("不在允许", err)

    def test_rejects_when_no_roots_configured(self):
        resolved, err = assert_filepath_allowed(str(self.allowed), [])
        self.assertIsNone(resolved)
        self.assertIn("未配置", err)

    def test_rejects_symlink_escape(self):
        link = self.root / "escape.mkv"
        link.symlink_to(self.outside)
        resolved, err = assert_filepath_allowed(str(link), [str(self.root)])
        self.assertIsNone(resolved)
        self.assertIn("不在允许", err)


if __name__ == "__main__":
    unittest.main()
