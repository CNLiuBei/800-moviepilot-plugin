import unittest

from uploader.upload_policy import (
    direct_mode_enabled,
    normalize_upload_mode,
    recovery_policy_from_marker,
    recovery_policy_from_source_type,
    validate_upload_identity,
)


class UploadPolicyTests(unittest.TestCase):
    def test_direct_is_default(self):
        self.assertEqual("direct", normalize_upload_mode(None))
        self.assertTrue(direct_mode_enabled({}))

    def test_hls_is_explicit(self):
        self.assertEqual("hls", normalize_upload_mode("hls"))
        self.assertFalse(direct_mode_enabled({"upload_mode": "hls"}))

    def test_legacy_direct_mp4_is_supported(self):
        self.assertTrue(direct_mode_enabled({"direct_mp4": True}))
        self.assertFalse(direct_mode_enabled({"direct_mp4": False}))

    def test_upload_mode_overrides_legacy_direct_mp4(self):
        self.assertFalse(direct_mode_enabled({"upload_mode": "hls", "direct_mp4": True}))
        self.assertTrue(direct_mode_enabled({"upload_mode": "direct", "direct_mp4": False}))

    def test_tv_requires_season_and_episode(self):
        result = validate_upload_identity("tv", None, None)
        self.assertEqual("电视剧直传必须提供 season 和 episode", result[3])

    def test_movie_rejects_partial_episode_identity(self):
        result = validate_upload_identity("movie", 1, None)
        self.assertEqual("season 和 episode 必须同时提供", result[3])

    def test_recovery_policy_from_marker(self):
        marker = {
            "uploadMode": "direct",
            "h264Compat": False,
            "sourceType": "mp4",
        }
        self.assertEqual(
            {"upload_mode": "direct", "direct_mp4": True, "h264_compat": False},
            recovery_policy_from_marker(marker),
        )

    def test_recovery_policy_from_source_type(self):
        self.assertEqual(
            {"upload_mode": "direct", "direct_mp4": True, "h264_compat": False},
            recovery_policy_from_source_type("mp4"),
        )
        self.assertEqual(
            {"upload_mode": "hls", "direct_mp4": False, "h264_compat": False},
            recovery_policy_from_source_type("cmaf"),
        )


if __name__ == "__main__":
    unittest.main()
