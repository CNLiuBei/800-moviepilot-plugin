import unittest
from unittest.mock import MagicMock, patch

from uploader import notify as notify_mod
from uploader.notify_policy import (
    EVENT_REGISTER_SUCCESS,
    EVENT_SUCCESS,
    escape_html,
    format_telegram_html,
    normalize_notify_policy,
    resolve_tg_targets_for_event,
)


class NotifyPolicyHtmlTests(unittest.TestCase):
    def test_escape_html(self):
        self.assertEqual("a&amp;b&lt;c&gt;", escape_html("a&b<c>"))

    def test_format_telegram_html(self):
        text = format_telegram_html("标题_x", "file_name.mkv")
        self.assertIn("<b>标题_x</b>", text)
        self.assertIn("file_name.mkv", text)
        self.assertNotIn("*标题", text)


class NotifyRoutingTests(unittest.TestCase):
    def test_channel_only_gets_register_success(self):
        policy = normalize_notify_policy(
            {
                "tg_bot_token": "tok",
                "tg_bot_enabled": True,
                "tg_chat_id": "111",
                "tg_channel_enabled": True,
                "tg_channel_id": "-1003229748357",
                "tg_event_success": True,
                "tg_event_register_success": False,
            }
        )
        self.assertEqual(["111"], resolve_tg_targets_for_event(policy, EVENT_SUCCESS))
        self.assertEqual(
            ["-1003229748357"],
            resolve_tg_targets_for_event(policy, EVENT_REGISTER_SUCCESS),
        )


class NotifyDispatchTests(unittest.TestCase):
    def setUp(self):
        notify_mod.set_mp_notifier(None)
        notify_mod.configure_notify_policy(
            {
                "tg_bot_token": "tok",
                "tg_bot_enabled": True,
                "tg_bot_chat_id": "111",
                "tg_channel_enabled": True,
                "tg_channel_id": "@ch",
                "tg_event_success": True,
                "tg_event_enqueue": False,
                "tg_event_failed": True,
                "tg_event_register_success": False,
            }
        )

    def tearDown(self):
        notify_mod.set_mp_notifier(None)
        notify_mod.configure_notify_policy({})

    def test_success_sends_only_to_bot(self):
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"ok": True}
        with patch("uploader.notify.httpx.post", return_value=ok_response) as post:
            notify_mod.notify_upload_success(
                filename="a_b.mkv",
                tmdb_id=1,
                quality="1080p",
                upload_mode="direct",
            )
        self.assertEqual(1, post.call_count)
        payload = post.call_args.kwargs["json"]
        self.assertEqual("HTML", payload["parse_mode"])
        self.assertEqual("111", payload["chat_id"])
        self.assertIn("a_b.mkv", payload["text"])

    def test_register_success_sends_photo_to_channel(self):
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"ok": True}
        meta = {
            "title": "千与千寻",
            "year": "2001",
            "media_type": "movie",
            "rating": 8.5,
            "genres": ["动画", "家庭"],
            "runtime_minutes": 125,
            "image_url": "https://image.tmdb.org/t/p/w1280/x.jpg",
            "season": None,
            "episode": None,
        }
        with patch(
            "uploader.notify_register_card.fetch_register_card_meta",
            return_value=meta,
        ), patch(
            "uploader.notify.settings.API_BASE", "https://guangying.org"
        ), patch("uploader.notify.httpx.post", return_value=ok_response) as post:
            notify_mod.notify_register_success(
                filename="movie.mkv",
                tmdb_id=129,
                quality="BluRay 1080p",
                media_type="movie",
                duration_secs=7500,
            )
        self.assertEqual(1, post.call_count)
        self.assertIn("/sendPhoto", post.call_args.args[0])
        payload = post.call_args.kwargs["json"]
        self.assertEqual("@ch", payload["chat_id"])
        self.assertEqual(meta["image_url"], payload["photo"])
        self.assertIn("千与千寻 (2001) 已入库", payload["caption"])
        self.assertIn("时长：2小时5分", payload["caption"])
        self.assertNotIn("大小", payload["caption"])
        self.assertEqual(
            "▶️ 立即播放",
            payload["reply_markup"]["inline_keyboard"][0][0]["text"],
        )
        self.assertEqual(
            "https://guangying.org/movie/129",
            payload["reply_markup"]["inline_keyboard"][0][0]["url"],
        )

    def test_enqueue_respects_event_switch(self):
        with patch("uploader.notify.httpx.post") as post:
            notify_mod.notify_enqueue("【云端上传】已入队", "foo")
        post.assert_not_called()

        notify_mod.configure_notify_policy(
            {
                "tg_bot_token": "tok",
                "tg_bot_chat_id": "111",
                "tg_event_enqueue": True,
            }
        )
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"ok": True}
        with patch("uploader.notify.httpx.post", return_value=ok_response) as post:
            notify_mod.notify_enqueue("【云端上传】已入队", "foo")
        post.assert_called_once()
        self.assertEqual("111", post.call_args.kwargs["json"]["chat_id"])

    def test_send_test_notification_reports_targets(self):
        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"ok": True}
        with patch("uploader.notify.httpx.post", return_value=ok_response):
            result = notify_mod.send_test_notification()
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["sent"])
        self.assertEqual({"111", "@ch"}, set(result["targets"]))

    def test_send_telegram_reports_api_error(self):
        bad = MagicMock()
        bad.status_code = 400
        bad.json.return_value = {"ok": False, "description": "chat not found"}
        with patch("uploader.notify.httpx.post", return_value=bad):
            result = notify_mod.send_telegram_message("t", "m", force_all_targets=True)
        self.assertEqual(0, result["sent"])
        self.assertTrue(result["errors"])
        self.assertIn("chat not found", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
