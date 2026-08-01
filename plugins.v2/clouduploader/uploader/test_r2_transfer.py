import unittest

from uploader.r2 import _BOTO_CONFIG, get_transfer_config, upload_concurrency
from uploader.runtime_config import settings


class R2TransferConfigTests(unittest.TestCase):
    def tearDown(self):
        settings.configure(upload_concurrency=8)

    def test_read_timeout_is_extended_for_slow_uplinks(self):
        self.assertGreaterEqual(_BOTO_CONFIG.read_timeout, 300)

    def test_multipart_concurrency_follows_upload_concurrency_setting(self):
        settings.configure(upload_concurrency=8)
        self.assertEqual(8, upload_concurrency())
        cfg = get_transfer_config()
        self.assertEqual(8, cfg.max_concurrency)
        self.assertEqual(16 * 1024 * 1024, cfg.multipart_chunksize)
        self.assertEqual(16 * 1024 * 1024, cfg.multipart_threshold)

    def test_multipart_concurrency_clamped(self):
        settings.configure(upload_concurrency=99)
        self.assertEqual(16, upload_concurrency())
        self.assertEqual(16, get_transfer_config().max_concurrency)
        settings.configure(upload_concurrency=0)
        self.assertEqual(1, upload_concurrency())
        self.assertEqual(1, get_transfer_config().max_concurrency)


if __name__ == "__main__":
    unittest.main()
