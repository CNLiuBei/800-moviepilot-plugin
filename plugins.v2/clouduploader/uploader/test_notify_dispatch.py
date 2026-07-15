import unittest
from unittest.mock import MagicMock, patch

from uploader import notify as notify_mod
from uploader.notify_policy import (
    EVENT_ENQUEUE,
    escape_html,
    format_telegram_html,
)


class NotifyPolicyHtmlTests(unittest.TestCase):
    def test_escape_html(self):
        self.assertEqual("a&amp;b&lt;c&gt;", escape_html("a&b<c>"))

    def test_format_telegram_html(self):
        text = format_telegram_html("标题_x", "file_name.mkv")
        self.assertIn("<b>标题_x</b>", text)
        self.assertIn("file_name.mkv", text)
        self.assertNotIn("*标题", text)


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
            }
        )

    def tearDown(self):
        notify_mod.set_mp_notifier(None)
        notify_mod.configure_notify_policy({})

    def test_success_sends_html_to_bot_and_channel(self):
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
        self.assertEqual(2, post.call_count)
        payload = post.call_args_list[0].kwargs["json"]
        self.assertEqual("HTML", payload["parse_mode"])
        self.assertIn("<b>", payload["text"])
        self.assertIn("a_b.mkv", payload["text"])
        chats = {c.kwargs["json"]["chat_id"] for c in post.call_args_list}
        self.assertEqual({"111", "@ch"}, chats)

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
            result = notify_mod.send_telegram_message("t", "m")
        self.assertEqual(0, result["sent"])
        self.assertTrue(result["errors"])
        self.assertIn("chat not found", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
