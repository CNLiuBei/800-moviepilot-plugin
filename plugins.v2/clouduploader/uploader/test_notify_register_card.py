import unittest
from unittest.mock import patch

from uploader.notify_register_card import (
    build_image_url,
    build_play_url,
    format_duration,
    format_register_caption,
    format_runtime_minutes,
    play_button_markup,
)


class RegisterCardFormatTests(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual("2小时5分", format_duration(7500))
        self.assertEqual("45分钟", format_duration(2700))
        self.assertEqual("", format_duration(0))

    def test_format_runtime_minutes(self):
        self.assertEqual("2小时5分", format_runtime_minutes(125))

    def test_build_image_url(self):
        self.assertEqual(
            "https://image.tmdb.org/t/p/w1280/abc.jpg",
            build_image_url("/abc.jpg", "w1280"),
        )
        self.assertIsNone(build_image_url(""))

    def test_build_play_url_and_button(self):
        url = build_play_url("https://guangying.org", "movie", 129)
        self.assertEqual("https://guangying.org/movie/129", url)
        tv = build_play_url("guangying.org", "tv", 1396, season=1, episode=2)
        self.assertEqual("https://guangying.org/tv/1396?season=1&episode=2", tv)
        markup = play_button_markup(url)
        self.assertEqual("▶️ 立即播放", markup["inline_keyboard"][0][0]["text"])
        self.assertEqual(url, markup["inline_keyboard"][0][0]["url"])
        self.assertIsNone(play_button_markup(""))

    def test_caption_movie_matches_requested_fields(self):
        caption = format_register_caption(
            {
                "title": "千与千寻",
                "year": "2001",
                "media_type": "movie",
                "rating": 8.5,
                "genres": ["动画", "家庭", "奇幻"],
                "runtime_minutes": 125,
            },
            quality="BluRay 1080p",
        )
        self.assertEqual(
            "千与千寻 (2001) 已入库\n"
            "评分：8.5，类型：电影，类别：动画、家庭，质量：BluRay 1080p，时长：2小时5分",
            caption,
        )
        self.assertNotIn("大小", caption)
        self.assertNotIn("文件", caption)

    def test_caption_tv_includes_season_episode(self):
        caption = format_register_caption(
            {
                "title": "某剧",
                "year": "2024",
                "media_type": "tv",
                "rating": 9.0,
                "genres": ["剧情"],
                "season": 1,
                "episode": 2,
                "runtime_minutes": 45,
            },
            quality="1080p",
            duration_secs=2700,
        )
        self.assertIn("某剧 S01E02 (2024) 已入库", caption)
        self.assertIn("类型：剧集", caption)
        self.assertIn("时长：45分钟", caption)

    def test_duration_secs_overrides_tmdb_runtime(self):
        caption = format_register_caption(
            {
                "title": "A",
                "year": "2020",
                "media_type": "movie",
                "rating": 7,
                "genres": ["动作"],
                "runtime_minutes": 100,
            },
            quality="1080p",
            duration_secs=3660,
        )
        self.assertIn("时长：1小时1分", caption)


if __name__ == "__main__":
    unittest.main()
