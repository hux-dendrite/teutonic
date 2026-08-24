from __future__ import annotations

import unittest
import hashlib
from datetime import timedelta

from teutonic.evaluation.configuration import DatasetManifestSnapshot, canonical_manifest_bytes
from teutonic.promotion import promotion_worker_lock_key
from teutonic.validator import EvaluationPolicyConfig, scheduler_lock_key


class ValidatorSchedulerPolicyTests(unittest.TestCase):
    def _policy(self, **overrides):
        manifest = {
            "shards": [{
                "key": "shards/part-000.npy",
                "sha256": "a" * 64,
                "size_bytes": 16384,
                "n_tokens": 16384,
            }]
        }
        snapshot = DatasetManifestSnapshot(
            name="fixture",
            manifest_url="https://datasets.example/fixture/manifest.json",
            manifest_sha256=hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest(),
            proportion=1.0,
            manifest=manifest,
        )
        values = {
            "policy_version": "policy-v1",
            "code_version": "code-v1",
            "dataset_version": "b" * 64,
            "evaluator_version": "evaluator-v2",
            "sampling_seed": 1,
            "bootstrap_seed": 2,
            "n": 32,
            "seq_len": 64,
            "n_bootstrap": 100,
            "alpha": 0.05,
            "delta_threshold": 0.0015,
            "dataset_source": "pretokenized_npy",
            "dataset_label": "fixture-v1",
            "dataset_manifests": (snapshot,),
            "retry_base_delay": timedelta(seconds=5),
        }
        values.update(overrides)
        return EvaluationPolicyConfig(**values)

    def test_retry_backoff_is_bounded_and_deterministic(self) -> None:
        policy = self._policy()
        self.assertEqual(policy.retry_delay(1), timedelta(seconds=5))
        self.assertEqual(policy.retry_delay(3), timedelta(seconds=20))
        self.assertEqual(policy.retry_delay(100), timedelta(seconds=1280))

    def test_scheduler_lock_key_scopes_network_generation_and_competition(self) -> None:
        first = scheduler_lock_key(306, "test", "quasar")
        self.assertEqual(first, scheduler_lock_key(306, "test", "quasar"))
        self.assertNotEqual(first, scheduler_lock_key(307, "test", "quasar"))
        self.assertNotEqual(first, scheduler_lock_key(306, "test-2", "quasar"))
        self.assertNotEqual(first, scheduler_lock_key(306, "test", "mimo"))

    def test_promotion_worker_uses_an_independent_lock(self) -> None:
        promotion = promotion_worker_lock_key(306, "test", "quasar")
        self.assertEqual(
            promotion,
            promotion_worker_lock_key(306, "test", "quasar"),
        )
        self.assertNotEqual(promotion, scheduler_lock_key(306, "test", "quasar"))

    def test_invalid_policy_configuration_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            self._policy(max_attempts=0)
        with self.assertRaises(ValueError):
            self._policy(dataset_source="unknown")
