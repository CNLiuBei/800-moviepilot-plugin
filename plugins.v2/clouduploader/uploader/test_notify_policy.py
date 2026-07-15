import unittest

from uploader.notify_policy import (
    EVENT_ENQUEUE,
    EVENT_FAILED,
    EVENT_REGISTER_FAILED,
    EVENT_SCAN,
    EVENT_SUCCESS,
    build_success_body,
    event_enabled,
    normalize_notify_policy,
    resolve_tg_chat_ids,
)


class NotifyPolicyTests(unittest.TestCase):
    def test_defaults_enable_success_fail_register_not_enqueue_scan(self):
        policy = normalize_notify_policy({})
        self.assertTrue(event_enabled(policy, EVENT_SUCCESS))
        self.assertTrue(event_enabled(policy, EVENT_FAILED))
        self.assertTrue(event_enabled(policy, EVENT_REGISTER_FAILED))
        self.assertFalse(event_enabled(policy, EVENT_ENQUEUE))
        self.assertFalse(event_enabled(policy, EVENT_SCAN))

    def test_legacy_tg_chat_id_maps_to_bot_chat(self):
        policy = normalize_notify_policy(
            {"tg_bot_token": "tok", "tg_chat_id": "111"}
        )
        self.assertEqual("111", policy["tg_bot_chat_id"])
        self.assertEqual(["111"], resolve_tg_chat_ids(policy))

    def test_bot_and_channel_targets_respect_switches(self):
        policy = normalize_notify_policy(
            {
                "tg_bot_token": "tok",
                "tg_bot_enabled": True,
                "tg_bot_chat_id": "111",
                "tg_channel_enabled": True,
                "tg_channel_id": "@mychannel",
            }
        )
        self.assertEqual(["111", "@mychannel"], resolve_tg_chat_ids(policy))

        policy_bot_off = normalize_notify_policy(
            {
                "tg_bot_token": "tok",
                "tg_bot_enabled": False,
                "tg_bot_chat_id": "111",
                "tg_channel_enabled": True,
                "tg_channel_id": "@mychannel",
            }
        )
        self.assertEqual(["@mychannel"], resolve_tg_chat_ids(policy_bot_off))

    def test_no_token_yields_no_targets(self):
        policy = normalize_notify_policy(
            {
                "tg_bot_chat_id": "111",
                "tg_channel_enabled": True,
                "tg_channel_id": "@ch",
            }
        )
        self.assertEqual([], resolve_tg_chat_ids(policy))

    def test_success_body_omits_r2_path_and_respects_fields(self):
        policy = normalize_notify_policy({})
        body = build_success_body(
            policy,
            filename="movie.mkv",
            tmdb_id=550,
            season=None,
            episode=None,
            quality="1080p",
            upload_mode="direct",
            r2_path="tmdb/movie/550",
        )
        self.assertIn("movie.mkv", body)
        self.assertIn("550", body)
        self.assertIn("电影", body)
        self.assertIn("1080p", body)
        self.assertIn("direct", body)
        self.assertNotIn("tmdb/movie/550", body)

    def test_success_body_can_disable_fields(self):
        policy = normalize_notify_policy(
            {
                "tg_field_filename": True,
                "tg_field_tmdb": False,
                "tg_field_episode": False,
                "tg_field_quality_mode": False,
            }
        )
        body = build_success_body(
            policy,
            filename="a.mkv",
            tmdb_id=1,
            season=1,
            episode=2,
            quality="4K",
            upload_mode="hls",
        )
        self.assertEqual("📤 a.mkv", body.strip())


if __name__ == "__main__":
    unittest.main()
