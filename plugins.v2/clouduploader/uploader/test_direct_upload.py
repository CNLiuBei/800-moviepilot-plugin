import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from uploader.job_runner import (
    _remote_source_type,
    run_job,
    upload_directory_smart,
    upload_mp4_direct,
)


class DirectUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.video = root / "prepared.mp4"
        self.video.write_bytes(b"prepared-mp4")
        self.subtitle = root / "subtitle.vtt"
        self.subtitle.write_bytes(b"WEBVTT")
        self.progress = MagicMock()

        self.s3 = MagicMock()
        self.s3.delete_objects.return_value = {}
        self.s3.head_object.side_effect = self._head_uploaded_object
        self.s3.upload_file.side_effect = self._upload_file
        self.uploaded = {}

        self.client_patcher = patch(
            "uploader.job_runner.get_s3_client", return_value=self.s3
        )
        self.list_patcher = patch(
            "uploader.job_runner._list_r2_prefix", return_value={}
        )
        self.client_patcher.start()
        self.list_mock = self.list_patcher.start()
        self.addCleanup(patch.stopall)

    def _upload_file(self, local_path, _bucket, key, ExtraArgs=None, Callback=None, Config=None):
        del Config
        size = Path(local_path).stat().st_size
        self.uploaded[key] = {
            "ContentLength": size,
            "ContentType": (ExtraArgs or {})["ContentType"],
        }
        if Callback:
            Callback(size)

    def _head_uploaded_object(self, Bucket, Key):
        del Bucket
        return self.uploaded[Key]

    def test_force_overwrite_false_keeps_ready_object(self):
        self.list_mock.return_value = {"ready.json": 20, "video.mp4": 100}

        result = upload_mp4_direct(
            str(self.video),
            "tmdb/movie/1",
            [],
            self.progress,
            lambda: False,
            force_overwrite=False,
        )

        self.assertEqual((0, 0), result)
        self.s3.delete_objects.assert_not_called()
        self.s3.upload_file.assert_not_called()

    def test_force_overwrite_false_replaces_incomplete_prefix(self):
        self.list_mock.return_value = {"video.mp4": 100, "stale.vtt": 5}

        result = upload_mp4_direct(
            str(self.video),
            "tmdb/movie/1",
            [],
            self.progress,
            lambda: False,
            force_overwrite=False,
        )

        self.assertEqual((1, 2), result)
        deleted = self.s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
        self.assertEqual(
            {
                "tmdb/movie/1/video.mp4",
                "tmdb/movie/1/stale.vtt",
            },
            {item["Key"] for item in deleted},
        )

    def test_force_overwrite_true_replaces_ready_prefix(self):
        self.list_mock.return_value = {"ready.json": 20, "video.mp4": 100}

        result = upload_mp4_direct(
            str(self.video),
            "tmdb/movie/1",
            [],
            self.progress,
            lambda: False,
            force_overwrite=True,
        )

        self.assertEqual((1, 2), result)
        self.s3.upload_file.assert_called_once()

    def test_remote_video_size_must_match_local_size(self):
        self.s3.head_object.side_effect = lambda **_kwargs: {
            "ContentLength": 1,
            "ContentType": "video/mp4",
        }

        with self.assertRaisesRegex(RuntimeError, "远端文件大小不一致"):
            upload_mp4_direct(
                str(self.video),
                "tmdb/movie/1",
                [],
                self.progress,
                lambda: False,
                True,
            )

    def test_remote_video_content_type_must_be_mp4(self):
        self.s3.head_object.side_effect = lambda **_kwargs: {
            "ContentLength": self.video.stat().st_size,
            "ContentType": "application/octet-stream",
        }

        with self.assertRaisesRegex(RuntimeError, "Content-Type"):
            upload_mp4_direct(
                str(self.video),
                "tmdb/movie/1",
                [],
                self.progress,
                lambda: False,
                True,
            )

    def test_remote_sidecar_size_and_content_type_are_verified(self):
        def mismatched_sidecar(Bucket, Key):
            del Bucket
            if Key.endswith("subtitle.vtt"):
                return {"ContentLength": 1, "ContentType": "text/vtt"}
            return self.uploaded[Key]

        self.s3.head_object.side_effect = mismatched_sidecar

        with self.assertRaisesRegex(RuntimeError, "subtitle.vtt.*大小不一致"):
            upload_mp4_direct(
                str(self.video),
                "tmdb/movie/1",
                [self.subtitle],
                self.progress,
                lambda: False,
                True,
            )

        self.s3.head_object.side_effect = lambda Bucket, Key: {
            **self.uploaded[Key],
            "ContentType": (
                "application/octet-stream"
                if Key.endswith("subtitle.vtt")
                else self.uploaded[Key]["ContentType"]
            ),
        }
        with self.assertRaisesRegex(RuntimeError, "subtitle.vtt.*Content-Type"):
            upload_mp4_direct(
                str(self.video),
                "tmdb/movie/1",
                [self.subtitle],
                self.progress,
                lambda: False,
                True,
            )

    def test_remote_source_priority(self):
        self.s3.head_object.side_effect = None
        self.s3.head_object.return_value = {}
        self.assertEqual("cmaf", _remote_source_type("tmdb/movie/1"))
        self.assertEqual(
            "tmdb/movie/1/master.m3u8",
            self.s3.head_object.call_args.kwargs["Key"],
        )

    def test_marker_preserves_direct_policy(self):
        from uploader.job_runner import _upload_marker_payload
        from uploader.upload_policy import recovery_policy_from_marker

        marker = _upload_marker_payload(
            "movie.mkv",
            "mp4",
            "1080p",
            [],
            120,
            upload_mode="direct",
            h264_compat=False,
            video_codec="hevc",
            width=1920,
            height=1080,
            bitrate=8_000_000,
            frame_rate=23.976,
        )
        self.assertEqual("direct", marker["uploadMode"])
        self.assertFalse(marker["h264Compat"])
        self.assertEqual("hevc", marker["videoCodec"])
        self.assertEqual(1920, marker["width"])
        self.assertEqual(1080, marker["height"])
        self.assertEqual(8_000_000, marker["bitrate"])
        self.assertEqual(23.976, marker["frameRate"])
        self.assertEqual(
            {"upload_mode": "direct", "direct_mp4": True, "h264_compat": False},
            recovery_policy_from_marker(marker),
        )

    def test_marker_recovery_from_source_type_only(self):
        from uploader.upload_policy import recovery_policy_from_marker

        self.assertEqual(
            {"upload_mode": "direct", "direct_mp4": True, "h264_compat": False},
            recovery_policy_from_marker({"sourceType": "mp4"}),
        )
        self.assertEqual(
            {"upload_mode": "hls", "direct_mp4": False, "h264_compat": False},
            recovery_policy_from_marker({"sourceType": "cmaf"}),
        )


class DirectRunJobTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"raw-mkv")
        self.output_root = self.root / "output"

    def _params(self, **overrides):
        params = {
            "filepath": str(self.source),
            "tmdb_id": 1,
            "media_type": "movie",
            "upload_mode": "direct",
            "direct_mp4": True,
            "h264_compat": False,
            "clean_after": False,
            "force_overwrite": True,
            "skip_metadata_check": True,
            "skip_register": True,
            "no_subtitles": True,
            "original_language": "en",
            "retry_attempts": 1,
        }
        params.update(overrides)
        return params

    def _run_prepared_job(
        self,
        *,
        params=None,
        upload_side_effect=None,
        cancel_after_prepare=False,
        unlink_error=None,
        register_result=(True, ""),
    ):
        logs = []
        prepared_path = self.output_root / "tmdb/movie/1/video.mp4"

        def prepare(_source, output, **_kwargs):
            output.write_bytes(b"prepared-mp4")
            return {
                "path": str(output),
                "duration": 12.5,
                "videoCopied": True,
                "audioCopied": True,
            }

        cancel_check = (
            (lambda: prepared_path.exists())
            if cancel_after_prepare
            else (lambda: False)
        )
        with ExitStack() as stack:
            stack.enter_context(
                patch("uploader.job_runner.settings.validate", return_value=[])
            )
            stack.enter_context(
                patch("uploader.job_runner.settings.HLS_OUTPUT_DIR", self.output_root)
            )
            stack.enter_context(
                patch("uploader.job_runner.resolve_tool", return_value="/tool")
            )
            stack.enter_context(
                patch("uploader.job_runner.prepare_direct_mp4", side_effect=prepare)
            )
            upload_mock = stack.enter_context(
                patch("uploader.job_runner.upload_mp4_direct")
            )
            if upload_side_effect is None:
                upload_mock.return_value = (1, 0)
            else:
                upload_mock.side_effect = upload_side_effect
            stack.enter_context(patch("uploader.job_runner.write_show_nfo"))
            stack.enter_context(
                patch(
                    "uploader.job_runner.auto_register",
                    return_value=register_result,
                )
            )
            stack.enter_context(patch("uploader.job_runner.notify_upload_success"))
            stack.enter_context(patch("uploader.job_runner.notify_upload_failed"))
            stack.enter_context(patch("uploader.job_runner._put_upload_marker"))
            stack.enter_context(patch("uploader.job_runner._delete_upload_marker"))
            stack.enter_context(patch("uploader.job_runner.time.sleep"))
            if unlink_error is not None:
                stack.enter_context(
                    patch("uploader.job_runner.Path.unlink", side_effect=unlink_error)
                )
            result = run_job(
                params or self._params(),
                log_fn=logs.append,
                cancel_check=cancel_check,
            )
        return result, logs, prepared_path, upload_mock

    def test_direct_job_uploads_prepared_output_and_always_cleans_it(self):
        logs = []
        prepared_path = self.output_root / "tmdb/movie/1/video.mp4"

        def prepare(_source, output, **_kwargs):
            output.write_bytes(b"prepared-mp4")
            return {
                "path": str(output),
                "duration": 12.5,
                "videoCopied": True,
                "audioCopied": False,
            }

        with patch("uploader.job_runner.settings.validate", return_value=[]), patch(
            "uploader.job_runner.settings.HLS_OUTPUT_DIR", self.output_root
        ), patch(
            "uploader.job_runner.resolve_tool", return_value="/tool"
        ), patch(
            "uploader.job_runner.prepare_direct_mp4",
            side_effect=prepare,
            create=True,
        ) as prepare_mock, patch(
            "uploader.job_runner.upload_mp4_direct", return_value=(1, 0)
        ) as upload_mock, patch(
            "uploader.job_runner.write_show_nfo"
        ), patch(
            "uploader.job_runner.notify_upload_success"
        ), patch(
            "uploader.job_runner._put_upload_marker"
        ), patch(
            "uploader.job_runner._delete_upload_marker"
        ):
            result = run_job(self._params(), log_fn=logs.append)

        self.assertEqual("success", result["status"])
        prepare_mock.assert_called_once()
        self.assertEqual(str(self.source), prepare_mock.call_args.args[0])
        self.assertEqual(prepared_path, prepare_mock.call_args.args[1])
        self.assertEqual(str(prepared_path), upload_mock.call_args.args[0])
        self.assertTrue(self.source.exists())
        self.assertFalse(prepared_path.exists())
        self.assertTrue(any("音频转 AAC" in message for message in logs))

    def test_direct_mode_requires_both_ffmpeg_and_ffprobe(self):
        with patch("uploader.job_runner.settings.validate", return_value=[]), patch(
            "uploader.job_runner.resolve_tool", return_value=None
        ), patch("uploader.job_runner.notify_upload_failed") as notify_failed:
            result = run_job(self._params())

        self.assertEqual("precheck", result["stage"])
        self.assertEqual(
            "直传环境未就绪: 重封装需要 ffmpeg/ffprobe",
            result["error"],
        )
        notify_failed.assert_called_once()
        self.assertEqual("precheck", notify_failed.call_args.kwargs.get("stage"))

    def test_exhausted_upload_failure_cleans_prepared_output(self):
        result, _logs, prepared_path, upload_mock = self._run_prepared_job(
            upload_side_effect=RuntimeError("R2 unavailable")
        )

        self.assertEqual("upload", result["stage"])
        self.assertEqual(1, upload_mock.call_count)
        self.assertFalse(prepared_path.exists())
        self.assertTrue(self.source.exists())

    def test_cancellation_after_prepare_cleans_prepared_output(self):
        result, _logs, prepared_path, upload_mock = self._run_prepared_job(
            cancel_after_prepare=True
        )

        self.assertEqual("cancelled", result["status"])
        upload_mock.assert_not_called()
        self.assertFalse(prepared_path.exists())
        self.assertTrue(self.source.exists())

    def test_cleanup_unlink_failure_is_warning_and_nonfatal(self):
        result, logs, prepared_path, _upload_mock = self._run_prepared_job(
            params=self._params(skip_register=False),
            unlink_error=OSError("read-only filesystem")
        )

        self.assertEqual("success", result["status"])
        self.assertTrue(prepared_path.exists())
        self.assertTrue(
            any(
                "⚠️" in message and "read-only filesystem" in message
                for message in logs
            )
        )

    def test_upload_retry_reuses_prepared_output_then_cleans_it(self):
        result, _logs, prepared_path, upload_mock = self._run_prepared_job(
            params=self._params(retry_attempts=2),
            upload_side_effect=[RuntimeError("transient"), (1, 0)],
        )

        self.assertEqual("success", result["status"])
        self.assertEqual(2, upload_mock.call_count)
        self.assertEqual(
            [str(prepared_path), str(prepared_path)],
            [call.args[0] for call in upload_mock.call_args_list],
        )
        self.assertFalse(prepared_path.exists())
        self.assertTrue(self.source.exists())

    def test_registration_failure_still_cleans_prepared_output(self):
        result, _logs, prepared_path, _upload_mock = self._run_prepared_job(
            params=self._params(skip_register=False),
            register_result=(False, "database unavailable"),
        )

        self.assertEqual("register", result["stage"])
        self.assertFalse(prepared_path.exists())
        self.assertTrue(self.source.exists())

    def test_clean_after_controls_source_not_prepared_cleanup(self):
        result, _logs, prepared_path, _upload_mock = self._run_prepared_job(
            params=self._params(clean_after=True)
        )

        self.assertEqual("success", result["status"])
        self.assertFalse(prepared_path.exists())
        self.assertFalse(self.source.exists())

    def test_direct_precheck_rejects_either_missing_tool(self):
        tool_results = (
            (None, "/tools/ffprobe"),
            ("/tools/ffmpeg", None),
        )
        for resolved_tools in tool_results:
            with self.subTest(resolved_tools=resolved_tools), patch(
                "uploader.job_runner.settings.validate", return_value=[]
            ), patch(
                "uploader.job_runner.resolve_tool", side_effect=resolved_tools
            ) as resolve_mock:
                result = run_job(self._params())

            self.assertEqual("precheck", result["stage"])
            self.assertEqual(
                "直传环境未就绪: 重封装需要 ffmpeg/ffprobe",
                result["error"],
            )
            self.assertEqual(2, resolve_mock.call_count)

    def test_skip_register_does_not_write_ready_marker(self):
        marker_calls = []

        def capture_marker(r2_path, status, payload=None, log=print):
            del r2_path, payload, log
            marker_calls.append(status)

        logs = []
        prepared_path = self.output_root / "tmdb/movie/1/video.mp4"

        def prepare(_source, output, **_kwargs):
            output.write_bytes(b"prepared-mp4")
            return {
                "path": str(output),
                "duration": 12.5,
                "videoCopied": True,
                "audioCopied": True,
            }

        with ExitStack() as stack:
            stack.enter_context(
                patch("uploader.job_runner.settings.validate", return_value=[])
            )
            stack.enter_context(
                patch("uploader.job_runner.settings.HLS_OUTPUT_DIR", self.output_root)
            )
            stack.enter_context(
                patch("uploader.job_runner.resolve_tool", return_value="/tool")
            )
            stack.enter_context(
                patch("uploader.job_runner.prepare_direct_mp4", side_effect=prepare)
            )
            stack.enter_context(
                patch("uploader.job_runner.upload_mp4_direct", return_value=(1, 0))
            )
            stack.enter_context(patch("uploader.job_runner.write_show_nfo"))
            stack.enter_context(patch("uploader.job_runner.notify_upload_success"))
            stack.enter_context(
                patch(
                    "uploader.job_runner._put_upload_marker",
                    side_effect=capture_marker,
                )
            )
            stack.enter_context(patch("uploader.job_runner._delete_upload_marker"))
            result = run_job(self._params(skip_register=True), log_fn=logs.append)

        self.assertEqual("success", result["status"])
        self.assertIn("uploaded", marker_calls)
        self.assertNotIn("ready", marker_calls)
        self.assertFalse(prepared_path.exists())


class HlsDirectoryUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.local = Path(self.temp_dir.name) / "hls"
        self.local.mkdir()
        (self.local / "master.m3u8").write_text("#EXTM3U\n")
        (self.local / "seg.m4s").write_bytes(b"seg")
        self.progress = MagicMock()
        self.s3 = MagicMock()
        self.s3.delete_objects.return_value = {}
        self.client_patcher = patch(
            "uploader.job_runner.get_s3_client", return_value=self.s3
        )
        self.list_patcher = patch(
            "uploader.job_runner._list_r2_prefix", return_value={}
        )
        self.client_patcher.start()
        self.list_mock = self.list_patcher.start()
        self.addCleanup(patch.stopall)

    def test_force_overwrite_false_keeps_ready_hls_prefix(self):
        self.list_mock.return_value = {
            "ready.json": 20,
            "master.m3u8": 40,
            "seg.m4s": 10,
        }

        result = upload_directory_smart(
            self.local,
            "tmdb/movie/1",
            self.progress,
            lambda: False,
            force_overwrite=False,
        )

        self.assertEqual((0, 0), result)
        self.s3.delete_objects.assert_not_called()
        self.s3.upload_file.assert_not_called()

    def test_force_overwrite_true_replaces_ready_hls_prefix(self):
        self.list_mock.return_value = {
            "ready.json": 20,
            "master.m3u8": 40,
            "seg.m4s": 10,
        }

        result = upload_directory_smart(
            self.local,
            "tmdb/movie/1",
            self.progress,
            lambda: False,
            force_overwrite=True,
        )

        self.assertEqual((2, 3), result)
        self.s3.delete_objects.assert_called()
        self.assertEqual(2, self.s3.upload_file.call_count)


if __name__ == "__main__":
    unittest.main()
