from __future__ import annotations

import hashlib
import io
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from nacl.signing import SigningKey

from teutonic.access import (
    MailboxCipher,
    Manifest,
    ManifestFile,
    MetagraphSnapshot,
    R2UploadController,
    ReadySignal,
    UidAssignment,
    encode_ss58_public_key,
    ready_signal_payload,
)
from teutonic.access.crypto import SecretCipher, encode_signature
from teutonic.storage.artifacts import ArtifactIntegrityError, model_digest_from_inventory


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.metadata: dict[tuple[str, str], dict[str, str]] = {}
        self.multipart: list[dict[str, str]] = []

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.read()
        self.metadata[(Bucket, Key)] = dict(kwargs.get("Metadata") or {})
        return {"ETag": self._etag(self.objects[(Bucket, Key)])}

    def get_object(self, *, Bucket, Key):
        value = self.objects[(Bucket, Key)]
        return {
            "Body": io.BytesIO(value),
            "ETag": self._etag(value),
            "Metadata": self.metadata.get((Bucket, Key), {}),
        }

    def head_object(self, *, Bucket, Key):
        value = self.objects[(Bucket, Key)]
        return {
            "ContentLength": len(value),
            "ETag": self._etag(value),
            "Metadata": self.metadata.get((Bucket, Key), {}),
        }

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):
        contents = [
            {"Key": key, "Size": len(value), "ETag": self._etag(value)}
            for (bucket, key), value in sorted(self.objects.items())
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def list_multipart_uploads(self, *, Bucket, Prefix, **kwargs):
        uploads = [item for item in self.multipart if item["Key"].startswith(Prefix)]
        return {"Uploads": uploads, "IsTruncated": False}

    def copy_object(self, *, Bucket, Key, CopySource, **kwargs):
        self.objects[(Bucket, Key)] = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        self.metadata[(Bucket, Key)] = dict(kwargs.get("Metadata") or {})
        return {"CopyObjectResult": {"ETag": self._etag(self.objects[(Bucket, Key)])}}

    @staticmethod
    def _etag(value: bytes) -> str:
        return f'"{hashlib.md5(value, usedforsecurity=False).hexdigest()}"'


def signed_manifest(key: SigningKey, registration: str, files: dict[str, bytes]) -> Manifest:
    inventory = tuple(
        ManifestFile(path=path, size=len(value), sha256=hashlib.sha256(value).hexdigest())
        for path, value in sorted(files.items())
    )
    manifest = Manifest(
        registration_id=registration,
        hotkey=encode_ss58_public_key(bytes(key.verify_key)),
        model_name="phase4/test-model",
        files=inventory,
        model_digest=model_digest_from_inventory(
            [(item.path, item.size, item.sha256) for item in inventory]
        ),
        signature="unsigned",
    )
    return replace(manifest, signature=encode_signature(key.sign(manifest.signing_payload()).signature))


class AccessControllerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.miner = SigningKey.generate()
        self.registration = "a" * 64
        self.hotkey = encode_ss58_public_key(bytes(self.miner.verify_key))

    def test_finalized_snapshot_and_ready_signal_are_deterministic(self) -> None:
        snapshot = MetagraphSnapshot(
            netuid=3,
            chain_generation="genesis-1",
            finalized_block=100,
            finalized_block_hash="0x100",
            assignments=(UidAssignment(42, self.hotkey, self.hotkey),),
            observed_at=datetime(2026, 8, 18, tzinfo=timezone.utc),
        )
        replay = replace(snapshot)
        self.assertEqual(snapshot.checksum, replay.checksum)
        signal = ReadySignal.parse(
            f"r2ready:v1|{self.registration}|{'b' * 64}",
            signalling_hotkey=self.hotkey,
            block_number=100,
            extrinsic_index=2,
            event_index=3,
        )
        self.assertEqual(signal.manifest_sha256, "b" * 64)
        compact = ready_signal_payload(self.registration, "b" * 64)
        self.assertLessEqual(len(compact.encode()), 128)
        compact_signal = ReadySignal.parse(
            compact,
            signalling_hotkey=self.hotkey,
            block_number=100,
            extrinsic_index=2,
            event_index=3,
        )
        self.assertEqual(compact_signal.registration_id, self.registration)
        self.assertEqual(compact_signal.manifest_sha256, "b" * 64)
        self.assertEqual(compact_signal.raw_payload, compact)
        with self.assertRaises(ValueError):
            ReadySignal.parse(
                "r2ready:v2|bad",
                signalling_hotkey=self.hotkey,
                block_number=100,
                extrinsic_index=2,
                event_index=3,
            )

    def test_mailbox_is_signed_and_encrypted_to_the_miner(self) -> None:
        validator = SigningKey.generate()
        cipher = MailboxCipher(validator)
        ciphertext, envelope = cipher.create_ciphertext(
            hotkey=self.hotkey,
            netuid=3,
            uid=42,
            registration_id=self.registration,
            generation=1,
            endpoint="https://account.r2.cloudflarestorage.com",
            private_model_bucket="private",
            allowed_prefix=f"models/registrations/{self.registration}/",
            access_key_id="access",
            secret_access_key="secret",
            session_token="session",
            expires_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
            chain_generation="genesis-1",
            registration_block=100,
        )
        decrypted = cipher.decrypt_for_test(ciphertext, self.miner)
        self.assertEqual(decrypted, envelope)
        secret_cipher = SecretCipher(b"x" * 32)
        self.assertEqual(secret_cipher.decrypt(secret_cipher.encrypt("parent-secret")), "parent-secret")

    def test_r2_upload_is_revoked_verified_in_place_and_reverified(self) -> None:
        files = {"config.json": b"{}", "weights/model.bin": b"phase-four"}
        manifest = signed_manifest(self.miner, self.registration, files)
        prefix = f"models/registrations/{self.registration}/"
        s3 = FakeS3()
        for path, value in files.items():
            s3.put_object(
                Bucket="private",
                Key=f"{prefix}{path}",
                Body=value,
                Metadata={"sha256": hashlib.sha256(value).hexdigest()},
            )
        s3.put_object(
            Bucket="private",
            Key=f"{prefix}manifest.json",
            Body=manifest.as_bytes(),
            Metadata={"sha256": manifest.manifest_sha256},
        )
        controller = R2UploadController(s3, private_model_bucket="private")
        verified = controller.verify_manifest(
            model_prefix=prefix,
            registration_id=self.registration,
            hotkey=self.hotkey,
            expected_manifest_sha256=manifest.manifest_sha256,
        )
        immutable = controller.create_immutable_snapshot(
            model_prefix=prefix, verified=verified
        )
        self.assertEqual(immutable.prefix, prefix)
        self.assertEqual(immutable.bucket, "private")
        self.assertEqual(set(immutable.etags), set(files))
        self.assertEqual(immutable.manifest_size, len(manifest.as_bytes()))
        for path, value in files.items():
            self.assertEqual(s3.objects[("private", f"{prefix}{path}")], value)

    def test_verifier_rejects_undeclared_objects_and_multipart_state(self) -> None:
        files = {"model.bin": b"phase-four"}
        manifest = signed_manifest(self.miner, self.registration, files)
        prefix = f"models/registrations/{self.registration}/"
        s3 = FakeS3()
        s3.put_object(
            Bucket="private",
            Key=f"{prefix}model.bin",
            Body=files["model.bin"],
            Metadata={"sha256": hashlib.sha256(files["model.bin"]).hexdigest()},
        )
        s3.put_object(
            Bucket="private",
            Key=f"{prefix}manifest.json",
            Body=manifest.as_bytes(),
            Metadata={"sha256": manifest.manifest_sha256},
        )
        s3.put_object(Bucket="private", Key=f"{prefix}undeclared.bin", Body=b"bad")
        controller = R2UploadController(s3, private_model_bucket="private")
        with self.assertRaises(ArtifactIntegrityError):
            controller.verify_manifest(
                model_prefix=prefix,
                registration_id=self.registration,
                hotkey=self.hotkey,
                expected_manifest_sha256=manifest.manifest_sha256,
            )
        del s3.objects[("private", f"{prefix}undeclared.bin")]
        s3.multipart.append({"Key": f"{prefix}unfinished.bin", "UploadId": "upload-1"})
        with self.assertRaises(ArtifactIntegrityError):
            controller.verify_manifest(
                model_prefix=prefix,
                registration_id=self.registration,
                hotkey=self.hotkey,
                expected_manifest_sha256=manifest.manifest_sha256,
            )

    def test_verifier_rejects_missing_sha256_metadata_before_evaluation(self) -> None:
        files = {"model.bin": b"phase-four"}
        manifest = signed_manifest(self.miner, self.registration, files)
        prefix = f"models/registrations/{self.registration}/"
        s3 = FakeS3()
        s3.put_object(Bucket="private", Key=f"{prefix}model.bin", Body=files["model.bin"])
        s3.put_object(
            Bucket="private",
            Key=f"{prefix}manifest.json",
            Body=manifest.as_bytes(),
            Metadata={"sha256": manifest.manifest_sha256},
        )
        with self.assertRaisesRegex(ArtifactIntegrityError, "metadata"):
            R2UploadController(s3, private_model_bucket="private").verify_manifest(
                model_prefix=prefix,
                registration_id=self.registration,
                hotkey=self.hotkey,
                expected_manifest_sha256=manifest.manifest_sha256,
            )


if __name__ == "__main__":
    unittest.main()
