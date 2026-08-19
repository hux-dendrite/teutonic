from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from teutonic.bootstrap import HuggingFaceSeed, PublicSeedStore, SeedBootstrapError

try:
    from boto3.s3.transfer import TransferConfig
except ImportError:
    TransferConfig = None


REVISION = "1" * 40


class FakeHubApi:
    def __init__(self, revision: str = REVISION) -> None:
        self.revision = revision

    def model_info(self, *, repo_id, revision):
        self.request = (repo_id, revision)
        return SimpleNamespace(sha=self.revision)


class MemoryS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, dict[str, str]]] = {}
        self.uploads = 0
        self.transfer_configs = []
        self.active_uploads = 0
        self.max_active_uploads = 0
        self.lock = threading.Lock()

    def list_objects_v2(self, *, Bucket, Prefix, **_kwargs):
        return {
            "Contents": [
                {"Key": key, "Size": len(body)}
                for (bucket, key), (body, _metadata) in sorted(self.objects.items())
                if bucket == Bucket and key.startswith(Prefix)
            ],
            "IsTruncated": False,
        }

    def head_object(self, *, Bucket, Key):
        body, metadata = self.objects[(Bucket, Key)]
        return {"ContentLength": len(body), "Metadata": metadata}

    def upload_file(self, filename, bucket, key, ExtraArgs, Config):
        with self.lock:
            self.uploads += 1
            self.active_uploads += 1
            self.max_active_uploads = max(self.max_active_uploads, self.active_uploads)
            self.transfer_configs.append(Config)
        try:
            time.sleep(0.01)
            self.objects[(bucket, key)] = (
                Path(filename).read_bytes(),
                dict(ExtraArgs["Metadata"]),
            )
        finally:
            with self.lock:
                self.active_uploads -= 1

    def put_object(self, *, Bucket, Key, Body, Metadata, **_kwargs):
        self.uploads += 1
        self.objects[(Bucket, Key)] = (bytes(Body), dict(Metadata))


class SeedBootstrapTests(unittest.TestCase):
    def materialize(self, root: Path):
        def download(*, local_dir, **kwargs):
            self.download_options = kwargs
            snapshot = Path(local_dir)
            (snapshot / "weights").mkdir(parents=True)
            (snapshot / "config.json").write_bytes(b'{"model_type":"mimo"}')
            (snapshot / "weights" / "model.safetensors").write_bytes(b"weights")
            cache = snapshot / ".cache" / "huggingface"
            cache.mkdir(parents=True)
            (cache / "metadata").write_bytes(b"not-an-artifact")
            return str(snapshot)

        return HuggingFaceSeed(api=FakeHubApi(), downloader=download).materialize(
            repo_id="owner/model",
            seed_digest=f"hf:{REVISION}",
            local_dir=root,
        )

    def test_materializes_exact_hf_commit_and_deterministic_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.materialize(Path(directory))
        self.assertEqual(artifact.revision, REVISION)
        self.assertEqual(self.download_options["max_workers"], 16)
        self.assertEqual(
            [item.path for item in artifact.files],
            ["config.json", "weights/model.safetensors"],
        )
        self.assertNotIn(b"not-an-artifact", artifact.manifest)
        self.assertEqual(len(artifact.model_digest), 64)
        self.assertIn(artifact.model_digest.encode(), artifact.manifest)

    def test_rejects_hf_revision_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            seed = HuggingFaceSeed(
                api=FakeHubApi("2" * 40),
                downloader=lambda **_kwargs: directory,
            )
            with self.assertRaisesRegex(SeedBootstrapError, "unexpected commit"):
                seed.materialize(
                    repo_id="owner/model",
                    seed_digest=f"hf:{REVISION}",
                    local_dir=Path(directory),
                )

    def test_publication_targets_content_addressed_public_prefix_and_is_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.materialize(Path(directory))
            s3 = MemoryS3()
            transfer_config = object()
            store = PublicSeedStore(
                s3,
                bucket="public-models",
                file_concurrency=16,
                part_concurrency=16,
                part_size=64 * 1024 * 1024,
                transfer_config=transfer_config,
            )
            store.publish(artifact)
            first_uploads = s3.uploads
            store.publish(artifact)

        self.assertEqual(first_uploads, len(artifact.files) + 1)
        self.assertEqual(s3.uploads, first_uploads)
        self.assertGreaterEqual(s3.max_active_uploads, 2)
        self.assertTrue(s3.transfer_configs)
        self.assertTrue(all(config is transfer_config for config in s3.transfer_configs))
        keys = {key for bucket, key in s3.objects if bucket == "public-models"}
        self.assertEqual(
            keys,
            {f"{artifact.prefix}{item.path}" for item in artifact.files}
            | {f"{artifact.prefix}manifest.json"},
        )
        for body, metadata in s3.objects.values():
            self.assertEqual(metadata["sha256"], hashlib.sha256(body).hexdigest())

    def test_publication_rejects_existing_object_without_matching_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.materialize(Path(directory))
            item = artifact.files[0]
            s3 = MemoryS3()
            s3.objects[("public-models", artifact.prefix + item.path)] = (
                item.local_path.read_bytes(),
                {"sha256": "0" * 64},
            )
            with self.assertRaisesRegex(SeedBootstrapError, "differs"):
                PublicSeedStore(
                    s3, bucket="public-models", transfer_config=object()
                ).publish(artifact)

    @unittest.skipUnless(TransferConfig, "boto3 required")
    def test_default_upload_profile_is_16_files_by_16_parts_at_64_mib(self):
        store = PublicSeedStore(MemoryS3(), bucket="public-models")
        self.assertEqual(store.file_concurrency, 16)
        self.assertEqual(store.transfer_config.max_concurrency, 16)
        self.assertEqual(store.transfer_config.multipart_chunksize, 64 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
