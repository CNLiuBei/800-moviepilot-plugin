"""OpenSubtitles v3（Stremio）字幕兜底：无中文内嵌/外挂时拉取并转 VTT。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from uploader.opensubtitles import (
    build_stremio_media_id,
    convert_srt_to_webvtt,
    fetch_opensubtitles_subtitles,
    normalize_opensubtitle_entries,
    pick_opensubtitle_candidates,
    resolve_opensubtitles_for_upload,
)
from uploader.subtitles import resolve_subtitles_for_upload


class OpenSubtitlesHelpersTests(unittest.TestCase):
    def test_build_stremio_media_id(self):
        self.assertEqual(build_stremio_media_id("movie", "tt0245429"), ("movie", "tt0245429"))
        self.assertEqual(
            build_stremio_media_id("tv", "tt0944947", season=1, episode=2),
            ("series", "tt0944947:1:2"),
        )
        self.assertIsNone(build_stremio_media_id("movie", "not-imdb")[1])
        self.assertIsNone(build_stremio_media_id("tv", "tt1", season=1, episode=None)[1])

    def test_convert_srt_to_webvtt(self):
        vtt = convert_srt_to_webvtt("1\n00:00:01,000 --> 00:00:02,000\n你好\n\n")
        self.assertIn("WEBVTT", vtt)
        self.assertIn("00:00:01.000 --> 00:00:02.000", vtt)
        self.assertIn("你好", vtt)

    def test_normalize_and_pick_prefers_chinese(self):
        payload = {
            "subtitles": [
                {"id": "1", "url": "https://subs5.strem.io/en.srt", "lang": "eng"},
                {"id": "2", "url": "https://subs5.strem.io/zh.srt", "lang": "chi"},
                {"id": "3", "url": "javascript:alert(1)", "lang": "chi"},
                {"id": "4", "url": "https://subs5.strem.io/tw.srt", "lang": "cht"},
            ]
        }
        entries = normalize_opensubtitle_entries(payload)
        self.assertEqual(3, len(entries))
        picked = pick_opensubtitle_candidates(entries, max_per_category=1)
        langs = [item["lang"] for item in picked]
        self.assertIn("chi", langs)
        self.assertIn("cht", langs)
        self.assertIn("eng", langs)

    def test_fetch_builds_correct_url_and_parses(self):
        captured = {}

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "subtitles": [
                        {"id": "9", "url": "https://subs5.strem.io/a.srt", "lang": "chi"},
                    ]
                }

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, timeout=20, headers=None):
                captured["url"] = url
                return FakeResp()

        with patch("uploader.opensubtitles.httpx.Client", FakeClient):
            entries = fetch_opensubtitles_subtitles("movie", "tt0245429")
        self.assertEqual(
            "https://opensubtitles-v3.strem.io/subtitles/movie/tt0245429.json",
            captured["url"],
        )
        self.assertEqual(1, len(entries))
        self.assertEqual("chi", entries[0]["lang"])

    def test_resolve_opensubtitles_downloads_srt_to_vtt(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)

            def fake_fetch(media_type, media_id, base_url=None, timeout=20):
                return [
                    {
                        "id": "2",
                        "url": "https://subs5.strem.io/zh.srt",
                        "lang": "chi",
                        "category": "zh-Hans",
                        "label": "简体中文",
                    }
                ]

            class FakeResp:
                status_code = 200
                text = "1\n00:00:01,000 --> 00:00:02,000\n你好\n\n"
                content = text.encode("utf-8")

            class FakeClient:
                def __init__(self, *args, **kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def get(self, url, timeout=30, headers=None):
                    return FakeResp()

            with patch("uploader.opensubtitles.fetch_opensubtitles_subtitles", side_effect=fake_fetch), patch(
                "uploader.opensubtitles.httpx.Client", FakeClient
            ):
                result = resolve_opensubtitles_for_upload(
                    out,
                    imdb_id="tt0245429",
                    media_type="movie",
                    print_fn=lambda *_: None,
                )
            self.assertEqual(1, len(result))
            self.assertEqual("zh-Hans", result[0]["category"])
            self.assertEqual("opensubtitles", result[0]["source"])
            vtt_path = out / result[0]["file"]
            self.assertTrue(vtt_path.is_file())
            self.assertIn("WEBVTT", vtt_path.read_text(encoding="utf-8"))

    def test_resolve_subtitles_falls_back_to_opensubtitles_without_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "movie.mkv"
            video.write_bytes(b"")

            remote = [
                {
                    "lang": "zh-Hans",
                    "label": "简体中文 (OpenSubtitles)",
                    "file": "os-0-zh-Hans.vtt",
                    "category": "zh-Hans",
                    "source": "opensubtitles",
                }
            ]
            with patch("uploader.subtitles.extract_subtitles", return_value=[]), patch(
                "uploader.subtitles._load_external_subtitles", return_value=[]
            ), patch(
                "uploader.opensubtitles.resolve_opensubtitles_for_upload",
                return_value=remote,
            ) as mock_os:
                result = resolve_subtitles_for_upload(
                    str(video),
                    root,
                    imdb_id="tt0245429",
                    media_type="movie",
                )
            mock_os.assert_called_once()
            self.assertEqual(1, len(result))
            self.assertEqual("opensubtitles", result[0]["source"])

    def test_resolve_skips_opensubtitles_when_embedded_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "movie.mkv"
            video.write_bytes(b"")
            embedded = [
                {"lang": "chi", "label": "简体", "file": "sub-0-chi.vtt", "category": "zh-Hans"}
            ]
            with patch("uploader.subtitles.extract_subtitles", return_value=embedded), patch(
                "uploader.opensubtitles.resolve_opensubtitles_for_upload",
                side_effect=AssertionError("should not call opensubtitles"),
            ):
                result = resolve_subtitles_for_upload(
                    str(video),
                    root,
                    imdb_id="tt0245429",
                    media_type="movie",
                )
            self.assertEqual(1, len(result))
            self.assertEqual("zh-Hans", result[0]["category"])


if __name__ == "__main__":
    unittest.main()
