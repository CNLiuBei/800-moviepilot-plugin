"""Cloudflare-recommended low-level multipart upload (ThreadPoolExecutor)."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from uploader.r2 import (
    _MULTIPART_CHUNKSIZE,
    upload_concurrency,
    upload_file_resilient,
)
from uploader.runtime_config import settings


class UploadConcurrencyTests(unittest.TestCase):
    def tearDown(self):
        settings.configure(upload_concurrency=8)

    def test_follows_setting_and_clamps(self):
        settings.configure(upload_concurrency=8)
        self.assertEqual(8, upload_concurrency())
        settings.configure(upload_concurrency=99)
        self.assertEqual(16, upload_concurrency())
        settings.configure(upload_concurrency=0)
        self.assertEqual(1, upload_concurrency())


class UploadFileResilientFastPathTests(unittest.TestCase):
    def tearDown(self):
        settings.configure(upload_concurrency=8)

    def test_small_file_uses_put_object_not_upload_file(self):
        s3 = MagicMock()
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"hello-r2")
            path = tmp.name
        try:
            progress = []
            upload_file_resilient(
                s3,
                path,
                "bucket",
                "k/small.bin",
                extra_args={"ContentType": "application/octet-stream"},
                callback=progress.append,
            )
        finally:
            os.unlink(path)

        s3.upload_file.assert_not_called()
        s3.create_multipart_upload.assert_not_called()
        s3.put_object.assert_called_once()
        kwargs = s3.put_object.call_args.kwargs
        self.assertEqual("bucket", kwargs["Bucket"])
        self.assertEqual("k/small.bin", kwargs["Key"])
        self.assertEqual("application/octet-stream", kwargs["ContentType"])
        self.assertEqual(b"hello-r2", kwargs["Body"])
        self.assertEqual([8], progress)

    def test_large_file_uses_manual_multipart_with_thread_pool(self):
        settings.configure(upload_concurrency=4)
        s3 = MagicMock()
        s3.create_multipart_upload.return_value = {"UploadId": "uid-1"}
        s3.upload_part.side_effect = lambda **kw: {"ETag": f"etag-{kw['PartNumber']}"}

        # 2.5 parts at 16 MiB → 3 parts
        size = _MULTIPART_CHUNKSIZE * 2 + 1024
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"x" * size)
            path = tmp.name
        try:
            with patch("uploader.r2.ThreadPoolExecutor") as executor_cls:
                executor = MagicMock()
                executor_cls.return_value.__enter__.return_value = executor

                def submit(fn, *args, **kwargs):
                    fut = MagicMock()
                    fut.result.return_value = fn(*args, **kwargs)
                    return fut

                executor.submit.side_effect = submit
                with patch(
                    "uploader.r2.as_completed",
                    side_effect=lambda futures: list(futures),
                ):
                    upload_file_resilient(
                        s3,
                        path,
                        "bucket",
                        "k/large.bin",
                        extra_args={"ContentType": "video/mp4"},
                    )

                executor_cls.assert_called_once_with(max_workers=4)
                self.assertEqual(3, executor.submit.call_count)
        finally:
            os.unlink(path)

        s3.upload_file.assert_not_called()
        s3.create_multipart_upload.assert_called_once_with(
            Bucket="bucket",
            Key="k/large.bin",
            ContentType="video/mp4",
        )
        self.assertEqual(3, s3.upload_part.call_count)
        part_numbers = sorted(
            c.kwargs["PartNumber"] for c in s3.upload_part.call_args_list
        )
        self.assertEqual([1, 2, 3], part_numbers)
        complete = s3.complete_multipart_upload.call_args.kwargs
        self.assertEqual("uid-1", complete["UploadId"])
        parts = complete["MultipartUpload"]["Parts"]
        self.assertEqual(
            [
                {"PartNumber": 1, "ETag": "etag-1"},
                {"PartNumber": 2, "ETag": "etag-2"},
                {"PartNumber": 3, "ETag": "etag-3"},
            ],
            parts,
        )

    def test_multipart_failure_aborts_upload(self):
        settings.configure(upload_concurrency=2)
        s3 = MagicMock()
        s3.create_multipart_upload.return_value = {"UploadId": "uid-fail"}
        s3.upload_part.side_effect = RuntimeError("part boom")

        size = _MULTIPART_CHUNKSIZE + 1
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"y" * size)
            path = tmp.name
        try:
            with self.assertRaises(RuntimeError):
                upload_file_resilient(s3, path, "bucket", "k/fail.bin")
        finally:
            os.unlink(path)

        s3.abort_multipart_upload.assert_called_once_with(
            Bucket="bucket",
            Key="k/fail.bin",
            UploadId="uid-fail",
        )
        s3.complete_multipart_upload.assert_not_called()

    def test_part_concurrency_override_limits_workers(self):
        settings.configure(upload_concurrency=8)
        s3 = MagicMock()
        s3.create_multipart_upload.return_value = {"UploadId": "uid-1"}
        s3.upload_part.side_effect = lambda **kw: {"ETag": f"etag-{kw['PartNumber']}"}
        size = _MULTIPART_CHUNKSIZE + 1
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"z" * size)
            path = tmp.name
        try:
            with patch("uploader.r2.ThreadPoolExecutor") as executor_cls:
                executor = MagicMock()
                executor_cls.return_value.__enter__.return_value = executor

                def submit(fn, *args, **kwargs):
                    fut = MagicMock()
                    fut.result.return_value = fn(*args, **kwargs)
                    return fut

                executor.submit.side_effect = submit
                with patch(
                    "uploader.r2.as_completed",
                    side_effect=lambda futures: list(futures),
                ):
                    upload_file_resilient(
                        s3,
                        path,
                        "bucket",
                        "k/nested.bin",
                        part_concurrency=1,
                    )
                executor_cls.assert_called_once_with(max_workers=1)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
