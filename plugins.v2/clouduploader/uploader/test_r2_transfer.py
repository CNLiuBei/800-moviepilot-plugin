import unittest

from uploader.r2 import _BOTO_CONFIG, get_transfer_config


class R2TransferConfigTests(unittest.TestCase):
    def test_read_timeout_is_extended_for_slow_uplinks(self):
        self.assertGreaterEqual(_BOTO_CONFIG.read_timeout, 300)

    def test_multipart_uses_small_parts_and_low_concurrency(self):
        cfg = get_transfer_config()
        self.assertEqual(8 * 1024 * 1024, cfg.multipart_chunksize)
        self.assertEqual(8 * 1024 * 1024, cfg.multipart_threshold)
        self.assertLessEqual(cfg.max_concurrency, 4)


if __name__ == "__main__":
    unittest.main()
