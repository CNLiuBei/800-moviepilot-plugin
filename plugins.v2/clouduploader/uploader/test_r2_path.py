import unittest

from uploader.r2_path import (
    build_media_r2_prefix,
    build_quality_r2_path,
    marker_lookup_paths,
)


class R2PathTests(unittest.TestCase):
    def test_movie_base_prefix(self):
        self.assertEqual(
            "tmdb/movie/550",
            build_media_r2_prefix("movie", 550),
        )

    def test_episode_base_prefix(self):
        self.assertEqual(
            "tmdb/tv/1396/season/1/episode/2",
            build_media_r2_prefix("tv", 1396, season=1, episode=2),
        )

    def test_quality_path_appends_resolution(self):
        path, key = build_quality_r2_path(
            "movie", 550, "1080p"
        )
        self.assertEqual("1080p", key)
        self.assertEqual("tmdb/movie/550/1080p", path)

    def test_quality_path_for_episode(self):
        path, key = build_quality_r2_path(
            "tv", 1396, "4K", season=1, episode=2
        )
        self.assertEqual("2160p", key)
        self.assertEqual(
            "tmdb/tv/1396/season/1/episode/2/2160p",
            path,
        )

    def test_marker_lookup_prefers_quality_then_legacy_base(self):
        paths = marker_lookup_paths(
            "tmdb/movie/550",
            "1080p",
        )
        self.assertEqual(
            ["tmdb/movie/550/1080p", "tmdb/movie/550"],
            paths,
        )

    def test_marker_lookup_unknown_includes_unknown_segment(self):
        paths = marker_lookup_paths("tmdb/movie/550", "")
        self.assertEqual("tmdb/movie/550/unknown", paths[0])
        self.assertIn("tmdb/movie/550", paths)


if __name__ == "__main__":
    unittest.main()
