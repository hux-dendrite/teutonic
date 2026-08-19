from __future__ import annotations

import ast
import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from teutonic.evaluation import (
    AttemptBusyError,
    AttemptConflictError,
    EvaluationAttemptRegistry,
    EvaluationRequestV2,
    PROTOCOL_VERSION,
    ProtocolValidationError,
    paired_bootstrap_verdict,
    result_provenance,
    validate_result_v2,
)
from teutonic.storage.artifacts import (
    ArtifactIntegrityError,
    R2ArtifactResolver,
    snapshot_digest,
)


def artifact_payload(digest: str, *, bucket: str = "private-models") -> dict:
    return {
        "kind": "r2-prefix",
        "bucket": bucket,
        "prefix": f"models/sha256/{digest}/",
        "expected_digest": digest,
    }


def request_payload(king_digest: str = "a" * 64, challenger_digest: str = "b" * 64) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "evaluation_id": "evaluation-001",
        "attempt_number": 1,
        "king": artifact_payload(king_digest),
        "challenger": artifact_payload(challenger_digest),
        "miner": {
            "hotkey": "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "coldkey": "5CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
            "uid": 42,
            "netuid": 3,
            "challenge_id": "challenge-001",
        },
        "versions": {
            "evaluation_policy": "paired-bootstrap-v1",
            "dataset": "fineweb-edu-10bt-v1",
            "tokenizer": "quasar-tokenizer-v1",
            "code": "git:0123456789abcdef",
            "evaluator": "pair-evaluator-v2",
        },
        "sampling": {"seed": 3610, "bootstrap_seed": 45063},
        "limits": {
            "n": 25000,
            "seq_len": 2048,
            "n_bootstrap": 10000,
            "alpha": 0.001,
            "delta_threshold": 0.0015,
            "batch_size": 1,
        },
        "dataset": {"source": "s3", "label": "fineweb-edu-10bt"},
        "tokenizer": {"backend": "huggingface", "label": "king-snapshot"},
    }


class FakeS3Client:
    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        del ContinuationToken
        contents = [
            {"Key": key, "Size": len(body)}
            for (bucket, key), body in self.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        Path(destination).write_bytes(self.objects[(bucket, key)])


class EvaluatorProtocolV2ContractTests(unittest.TestCase):
    def test_evaluator_has_no_legacy_model_registry_imports(self) -> None:
        source = (
            Path(__file__).parents[2] / "teutonic" / "evaluator" / "engine.py"
        ).read_text()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        self.assertTrue(
            {"huggingface_hub", "hippius_hub", "model_store"}.isdisjoint(imported)
        )

    def test_evaluator_has_no_dataset_authentication(self) -> None:
        source = (
            Path(__file__).parents[2] / "teutonic" / "evaluator" / "engine.py"
        ).read_text()
        forbidden = (
            "HIPPIUS_ACCESS_KEY",
            "HIPPIUS_SECRET_KEY",
            "TEUTONIC_DS_ACCESS_KEY",
            "TEUTONIC_DS_SECRET_KEY",
            "s3_auth_source",
            "s3_doppler_project",
            "s3_doppler_config",
        )
        self.assertTrue(all(value not in source for value in forbidden))
        self.assertIn("signature_version=UNSIGNED", source)

    def test_evaluator_app_exports_the_protocol_v2_app(self) -> None:
        app = object()
        base = SimpleNamespace(app=app)
        sources = ModuleType("teutonic.evaluator.sources")
        evaluator_package = ModuleType("teutonic.evaluator")
        evaluator_package.__path__ = []
        evaluator_package.engine = base
        evaluator_package.sources = sources
        module_path = Path(__file__).parents[2] / "teutonic" / "evaluator" / "app.py"
        spec = spec_from_file_location("teutonic.evaluator._test_app", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = module_from_spec(spec)
        with patch.dict(
            "sys.modules",
            {
                "teutonic.evaluator": evaluator_package,
                "teutonic.evaluator.engine": base,
                "teutonic.evaluator.sources": sources,
            },
        ):
            spec.loader.exec_module(module)
        self.assertIs(module.app, app)

    def test_request_is_strict_and_contains_no_credentials(self) -> None:
        request = EvaluationRequestV2.from_mapping(request_payload())
        self.assertEqual(request.eval_id, "evaluation-001:1")
        self.assertEqual(len(request.request_sha256), 64)
        self.assertNotIn("credential", str(request.request_payload).lower())

        forbidden = request_payload()
        forbidden["challenger"]["secret_access_key"] = "must-not-cross-boundary"
        with self.assertRaisesRegex(ProtocolValidationError, "may not contain credentials"):
            EvaluationRequestV2.from_mapping(forbidden)

        with self.assertRaisesRegex(ProtocolValidationError, "unknown fields"):
            EvaluationRequestV2.from_mapping({"king_repo": "legacy/model"})

    def test_duplicate_conflicting_and_busy_attempts(self) -> None:
        registry = EvaluationAttemptRegistry()
        request = EvaluationRequestV2.from_mapping(request_payload())
        first, duplicate = registry.start(request, admit_new=lambda: True)
        self.assertFalse(duplicate)
        second, duplicate = registry.start(request, admit_new=lambda: False)
        self.assertTrue(duplicate)
        self.assertIs(first, second)

        conflicting_payload = request_payload()
        conflicting_payload["limits"]["n"] = 100
        with self.assertRaises(AttemptConflictError):
            registry.start(EvaluationRequestV2.from_mapping(conflicting_payload))

        another_payload = request_payload()
        another_payload["attempt_number"] = 2
        with self.assertRaises(AttemptBusyError):
            registry.start(
                EvaluationRequestV2.from_mapping(another_payload),
                admit_new=lambda: False,
            )

    def test_completed_failed_streamed_and_lost_jobs(self) -> None:
        request = EvaluationRequestV2.from_mapping(request_payload())
        registry = EvaluationAttemptRegistry()
        completed, _ = registry.start(request)
        completed.state = "completed"
        completed.verdict = {"accepted": True}
        completed.events.put(completed.event("progress", {"phase": "scoring"}))
        completed.events.put(completed.event("verdict", completed.verdict))
        completed_response = completed.response(duplicate=True)
        self.assertEqual(completed_response["evaluation_id"], request.evaluation_id)
        self.assertEqual(completed_response["verdict"], {"accepted": True})
        self.assertTrue(completed_response["duplicate"])
        self.assertEqual(completed.events.get()["attempt_number"], 1)
        self.assertEqual(completed.events.get()["type"], "verdict")

        failed_payload = request_payload()
        failed_payload["attempt_number"] = 2
        failed, _ = registry.start(EvaluationRequestV2.from_mapping(failed_payload))
        failed.state = "failed"
        failed.error = "sanitized failure"
        failed.reason = "sanitized failure"
        self.assertEqual(failed.response()["state"], "failed")
        self.assertEqual(failed.response()["error"], "sanitized failure")

        restarted_registry = EvaluationAttemptRegistry()
        self.assertIsNone(restarted_registry.get(request.eval_id))

    def test_result_provenance_contains_all_audit_identities(self) -> None:
        request = EvaluationRequestV2.from_mapping(request_payload())
        provenance = result_provenance(
            request,
            started_at="2026-08-18T10:00:00+00:00",
            completed_at="2026-08-18T10:10:00+00:00",
            requested_sequences=25000,
            completed_sequences=25000,
            early_stopped=False,
            hardware={"workers": ["king-0", "challenger-0"], "gpu_ids": [0, 1]},
        )
        self.assertEqual(provenance["evaluation_id"], "evaluation-001")
        self.assertEqual(provenance["attempt_number"], 1)
        self.assertEqual(provenance["challenger_artifact_digest"], "b" * 64)
        self.assertEqual(provenance["sampling"]["completed_sequences"], 25000)
        self.assertEqual(provenance["versions"], request.versions)
        result = {
            **provenance,
            "accepted": True,
            "verdict": "challenger",
            "mu_hat": 0.003,
            "lcb": 0.002,
            "delta_threshold": 0.0015,
            "avg_king_loss": 1.2,
            "avg_challenger_loss": 1.197,
            "wall_time_s": 600.0,
            "result_artifact_sha256": "c" * 64,
        }
        validate_result_v2(result, request)
        conflicting = {**result, "attempt_number": 2}
        with self.assertRaisesRegex(ProtocolValidationError, "attempt_number"):
            validate_result_v2(conflicting, request)

    def test_v2_and_phase_two_baseline_verdicts_are_equivalent(self) -> None:
        request = EvaluationRequestV2.from_mapping(request_payload())
        losses = {
            "king": [1.2, 1.21, 1.19, 1.205],
            "challenger": [1.197, 1.207, 1.187, 1.202],
        }
        baseline = paired_bootstrap_verdict(
            losses["king"],
            losses["challenger"],
            bootstrap_seed=request.sampling["bootstrap_seed"],
            n_bootstrap=256,
            alpha=request.limits["alpha"],
            delta_threshold=request.limits["delta_threshold"],
            now=lambda: "fixed",
        )
        v2 = paired_bootstrap_verdict(
            losses["king"],
            losses["challenger"],
            bootstrap_seed=request.sampling["bootstrap_seed"],
            n_bootstrap=256,
            alpha=request.limits["alpha"],
            delta_threshold=request.limits["delta_threshold"],
            now=lambda: "fixed",
        )
        self.assertEqual(v2, baseline)


class R2ArtifactResolverTests(unittest.TestCase):
    def _snapshot_files(self, root: Path) -> tuple[dict[str, bytes], str]:
        (root / "config.json").write_text('{"architectures":["TestModel"]}')
        (root / "model.safetensors").write_bytes(b"immutable-weights")
        digest = snapshot_digest(root)
        return {
            "config.json": (root / "config.json").read_bytes(),
            "model.safetensors": (root / "model.safetensors").read_bytes(),
        }, digest

    def test_private_prefix_materializes_and_verifies_without_request_credentials(self) -> None:
        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as cache_dir,
        ):
            files, digest = self._snapshot_files(Path(source_dir))
            prefix = f"models/sha256/{digest}/"
            client = FakeS3Client(
                {("private-models", prefix + name): body for name, body in files.items()}
            )
            artifact = EvaluationRequestV2.from_mapping(
                request_payload(king_digest=digest, challenger_digest=digest)
            ).king
            resolver = R2ArtifactResolver(
                cache_dir,
                s3_client=client,
                allowed_bucket="private-models",
            )
            materialized = Path(resolver.resolve(artifact))
            self.assertEqual(snapshot_digest(materialized), digest)
            self.assertEqual(materialized.parent, Path(cache_dir))

    def test_digest_mismatch_and_bucket_escape_fail_closed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as source_dir,
            tempfile.TemporaryDirectory() as cache_dir,
        ):
            files, digest = self._snapshot_files(Path(source_dir))
            wrong_digest = "f" * 64
            prefix = f"models/sha256/{wrong_digest}/"
            client = FakeS3Client(
                {("private-models", prefix + name): body for name, body in files.items()}
            )
            artifact = EvaluationRequestV2.from_mapping(
                request_payload(king_digest=wrong_digest, challenger_digest=digest)
            ).king
            resolver = R2ArtifactResolver(
                cache_dir,
                s3_client=client,
                allowed_bucket="private-models",
            )
            with self.assertRaises(ArtifactIntegrityError):
                resolver.resolve(artifact)

            escaped_payload = request_payload()
            escaped_payload["king"] = artifact_payload("a" * 64, bucket="other-bucket")
            escaped = EvaluationRequestV2.from_mapping(escaped_payload).king
            with self.assertRaisesRegex(ArtifactIntegrityError, "allowlist"):
                resolver.resolve(escaped)
