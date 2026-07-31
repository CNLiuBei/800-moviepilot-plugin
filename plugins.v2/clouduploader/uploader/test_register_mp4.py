import unittest
from unittest.mock import MagicMock, Mock

from uploader.register import _api_error, _do_register


class Mp4RegisterTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        import_resp = Mock(status_code=200)
        import_resp.json.return_value = {"ok": True}
        source_resp = Mock(status_code=200)
        source_resp.json.return_value = {"ok": True}
        self.client.post.side_effect = [import_resp, source_resp]

    def test_mp4_source_uses_video_mp4(self):
        ok, error = _do_register(
            self.client,
            1726601,
            "movie",
            None,
            None,
            "tmdb/movie/1726601",
            "1080p",
            [],
            120,
            "mp4",
            print,
        )
        self.assertTrue(ok)
        self.assertEqual("", error)
        source_call = self.client.post.call_args_list[1]
        self.assertEqual("/api/admin/sources", source_call.args[0])
        self.assertEqual(
            "/api/r2/tmdb/movie/1726601/video.mp4",
            source_call.kwargs["json"]["url"],
        )
        self.assertEqual("mp4", source_call.kwargs["json"]["sourceType"])
        self.assertEqual("1080p", source_call.kwargs["json"]["quality"])
        self.assertEqual("1080p", source_call.kwargs["json"]["label"])
        self.assertTrue(source_call.kwargs["json"]["replace"])
        self.assertNotIn("forceReplaceAll", source_call.kwargs["json"])

    def test_import_error_keeps_server_message(self):
        response = Mock(status_code=500)
        response.text = '{"message":"TMDB_API_KEY 未配置"}'
        response.json.return_value = {"message": "TMDB_API_KEY 未配置"}
        self.assertIn("TMDB_API_KEY 未配置", _api_error(response, "TMDB导入失败"))

    def test_import_error_prefers_detail_when_message_missing(self):
        response = Mock(status_code=500)
        response.text = '{"detail":"apikey 校验不通过"}'
        response.json.return_value = {"detail": "apikey 校验不通过"}
        self.assertIn("apikey 校验不通过", _api_error(response, "TMDB导入失败"))

    def test_import_error_falls_back_to_text(self):
        response = Mock(status_code=502)
        response.text = "bad gateway"
        response.json.side_effect = ValueError("not json")
        self.assertEqual("TMDB导入失败 [502] — bad gateway", _api_error(response, "TMDB导入失败"))


if __name__ == "__main__":
    unittest.main()
