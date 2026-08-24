from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from teutonic.promotion import (
    ObservedObject,
    PromotionCollisionError,
    PromotionObject,
    RcloneExecutionError,
    RclonePromotionExecutor,
    S3InventoryInspector,
    inventory_digest,
    verify_inventory,
)


class HeaderOnlyS3:
    def __init__(self):
        self.objects = {
            ("public-models", "models/sha256/" + "a" * 64 + "/model.bin"): {
                "size": 12,
                "sha256": "b" * 64,
            }
        }

    def list_objects_v2(self, *, Bucket, Prefix, **_kwargs):
        return {
            "Contents": [
                {"Key": key, "Size": value["size"], "ETag": '"etag"'}
                for (bucket, key), value in self.objects.items()
                if bucket == Bucket and key.startswith(Prefix)
            ],
            "IsTruncated": False,
        }

    def head_object(self, *, Bucket, Key):
        value = self.objects[(Bucket, Key)]
        return {
            "ContentLength": value["size"],
            "ETag": '"etag"',
            "Metadata": {"sha256": value["sha256"]},
        }


class PromotionStorageTests(unittest.TestCase):
    def test_inventory_uses_headers_without_reading_model_bodies(self) -> None:
        prefix = "models/sha256/" + "a" * 64 + "/"
        observed = S3InventoryInspector(HeaderOnlyS3()).inventory("public-models", prefix)
        self.assertEqual(observed["model.bin"].sha256, "b" * 64)
        self.assertEqual(observed["model.bin"].size, 12)

    def test_inventory_collision_fails_closed(self) -> None:
        expected = {"model.bin": PromotionObject("model.bin", 12, "b" * 64)}
        wrong = {"model.bin": ObservedObject("model.bin", 12, "c" * 64)}
        with self.assertRaises(PromotionCollisionError):
            verify_inventory(expected, wrong, complete=True)

        self.assertEqual(
            inventory_digest(expected), inventory_digest(dict(reversed(expected.items())))
        )

    def test_rclone_uses_one_remote_server_side_copy_and_never_move(self) -> None:
        commands = []

        def runner(command, **kwargs):
            commands.append((command, kwargs))
            output = "INFO : config.json: Copied (server-side copy)\n"
            return subprocess.CompletedProcess(command, 0, "", output)

        executor = RclonePromotionExecutor("r2", runner=runner)
        prefix = "models/sha256/" + "a" * 64 + "/"
        executor.copy(
            source_bucket="private-models",
            source_prefix=prefix,
            destination_bucket="public-models",
            destination_prefix=prefix,
            probe_path="config.json",
            expected_object_count=2,
        )
        executor.delete_source(bucket="private-models", prefix=prefix)

        probe = commands[0][0]
        self.assertEqual(probe[0:2], ["rclone", "copyto"])
        self.assertTrue(probe[2].startswith("r2:private-models/"))
        self.assertTrue(probe[3].startswith("r2:public-models/"))

        copy = commands[1][0]
        self.assertEqual(copy[0:2], ["rclone", "copy"])
        self.assertTrue(copy[2].startswith("r2:private-models/"))
        self.assertTrue(copy[3].startswith("r2:public-models/"))
        for option, value in (
            ("--transfers", "100"),
            ("--checkers", "100"),
            ("--retries", "5"),
        ):
            self.assertEqual(copy[copy.index(option) + 1], value)
        self.assertIn("--checksum", copy)
        self.assertIn("--fast-list", copy)
        self.assertIn("--immutable", copy)
        self.assertIn("--metadata", copy)
        self.assertNotIn("move", copy)
        self.assertEqual(commands[2][0][0:2], ["rclone", "delete"])

    def test_rclone_aborts_before_bulk_copy_when_probe_streams_through_host(self) -> None:
        commands = []

        def runner(command, **kwargs):
            commands.append((command, kwargs))
            return subprocess.CompletedProcess(
                command, 0, "", "INFO : config.json: Copied (new)\n"
            )

        executor = RclonePromotionExecutor("r2", runner=runner)
        with self.assertRaisesRegex(RcloneExecutionError, "host-streamed"):
            executor.copy(
                source_bucket="private-models",
                source_prefix="models/registrations/registration-id/",
                destination_bucket="public-models",
                destination_prefix="models/sha256/" + "a" * 64 + "/",
                probe_path="config.json",
                expected_object_count=52,
            )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0][0:2], ["rclone", "copyto"])

    def test_rclone_does_not_inherit_boto_custom_ca_bundle(self) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command, 0, "", "INFO : config.json: Copied (server-side copy)\n"
            )

        with patch.dict(os.environ, {"AWS_CA_BUNDLE": "/validator/private-ca.pem"}):
            RclonePromotionExecutor("r2", runner=runner).copy(
                source_bucket="private-models",
                source_prefix="models/registrations/registration-id/",
                destination_bucket="public-models",
                destination_prefix="models/sha256/" + "a" * 64 + "/",
                probe_path="config.json",
                expected_object_count=1,
            )

        self.assertNotIn("AWS_CA_BUNDLE", calls[0][1]["env"])

    def test_rclone_paths_reject_remote_or_prefix_escape(self) -> None:
        with self.assertRaises(ValueError):
            RclonePromotionExecutor("first:second")
        executor = RclonePromotionExecutor("r2")
        with self.assertRaises(ValueError):
            executor.delete_source(bucket="private-models", prefix="models/../escape")
