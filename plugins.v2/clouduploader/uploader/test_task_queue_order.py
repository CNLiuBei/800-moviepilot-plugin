import unittest

from uploader.task_queue_order import (
    episode_sort_key_from_params,
    episode_sort_key_from_progress,
)


class TaskQueueOrderTests(unittest.TestCase):
    def test_params_season_episode(self):
        self.assertEqual((1, 7), episode_sort_key_from_params({"season": 1, "episode": 7}))

    def test_params_filename_fallback(self):
        self.assertEqual(
            (1, 10),
            episode_sort_key_from_params({
                "filepath": "/media/地球超新鲜 - S01E10 - 第 10 集.mkv",
            }),
        )

    def test_progress_pending_sort_order(self):
        rows = [
            {"name": "地球超新鲜 - S01E17 - 第 17 集.mkv", "status": "pending"},
            {"name": "地球超新鲜 - S01E01 - 第 1 集.mkv", "status": "pending"},
            {"name": "地球超新鲜 - S01E07 - 第 7 集.mkv", "status": "pending"},
        ]
        ordered = sorted(rows, key=lambda p: episode_sort_key_from_progress(p))
        self.assertEqual(
            ["地球超新鲜 - S01E01 - 第 1 集.mkv", "地球超新鲜 - S01E07 - 第 7 集.mkv", "地球超新鲜 - S01E17 - 第 17 集.mkv"],
            [r["name"] for r in ordered],
        )


if __name__ == "__main__":
    unittest.main()
