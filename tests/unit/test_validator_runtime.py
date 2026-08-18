from __future__ import annotations

import unittest

from teutonic.validator.runtime import (
    CrownCoordinator,
    FinalizedMetagraph,
    equal_weight_plan,
    evaluation_policy_from_env,
)


class FakeRepository:
    def __init__(self):
        self.crown = None

    def promotion_weight_hotkeys(self, promotion_id, *, limit):
        self.lookup = (promotion_id, limit)
        return ("new", "old", "gone")

    def crown_promoted_winner(self, promotion_id, **values):
        self.crown = (promotion_id, values)
        return "reign-2"

    def current_weight_policy(self):
        return {
            "publication_id": "publication-1",
            "payload_revision": 1,
            "mapping_finalized_block": 4000,
            "policy_hotkeys": ("new", "old", "gone"),
        }

    def refresh_current_weight_plan(self, **values):
        self.refresh = values
        return True


class FakeChain:
    def snapshot(self):
        return FinalizedMetagraph(4321, {"new": 9, "old": 4})


class ValidatorRuntimeTests(unittest.TestCase):
    def test_equal_weight_plan_preserves_order_and_deduplicates_uids(self):
        hotkeys, uids, weights = equal_weight_plan(
            ("new", "old", "same-uid", "gone"),
            {"new": 9, "old": 4, "same-uid": 4},
            burn_uid=0,
        )
        self.assertEqual(hotkeys, ("new", "old"))
        self.assertEqual(uids, (9, 4))
        self.assertEqual(weights, (0.5, 0.5))

    def test_equal_weight_plan_falls_back_to_burn_uid(self):
        self.assertEqual(
            equal_weight_plan(("gone",), {}, burn_uid=7),
            (("burn:uid:7",), (7,), (1.0,)),
        )

    def test_crown_uses_one_finalized_snapshot_for_block_and_uids(self):
        repository = FakeRepository()
        result = CrownCoordinator(repository, FakeChain(), king_chain_size=5)("promotion-1")
        self.assertEqual(result, "reign-2")
        self.assertEqual(repository.lookup, ("promotion-1", 5))
        promotion_id, values = repository.crown
        self.assertEqual(promotion_id, "promotion-1")
        self.assertEqual(values["crowned_finalized_block"], 4321)
        self.assertEqual(values["policy_hotkeys"], ("new", "old", "gone"))
        self.assertEqual(values["target_hotkeys"], ("new", "old"))
        self.assertEqual(values["target_uids"], (9, 4))
        self.assertEqual(values["normalized_weights"], (0.5, 0.5))

    def test_reregistered_winner_revises_plan_to_its_new_uid(self):
        repository = FakeRepository()
        coordinator = CrownCoordinator(repository, FakeChain(), king_chain_size=5)
        self.assertTrue(coordinator.reconcile_current_weight_plan())
        self.assertEqual(repository.refresh["publication_id"], "publication-1")
        self.assertEqual(repository.refresh["expected_revision"], 1)
        self.assertEqual(repository.refresh["mapping_finalized_block"], 4321)
        self.assertEqual(repository.refresh["target_hotkeys"], ("new", "old"))
        self.assertEqual(repository.refresh["target_uids"], (9, 4))
        self.assertEqual(repository.refresh["normalized_weights"], (0.5, 0.5))

    def test_policy_requires_durable_version_identities(self):
        with self.assertRaisesRegex(RuntimeError, "TEUTONIC_EVALUATION_POLICY_VERSION"):
            evaluation_policy_from_env({})

    def test_policy_matches_evaluator_defaults(self):
        policy = evaluation_policy_from_env(
            {
                "TEUTONIC_EVALUATION_POLICY_VERSION": "paired-bootstrap-v1",
                "TEUTONIC_EVALUATOR_CODE_VERSION": "release-1",
                "TEUTONIC_DATASET_VERSION": "data-1",
                "TEUTONIC_TOKENIZER_VERSION": "tokenizer-1",
                "TEUTONIC_EVALUATOR_VERSION": "pair-evaluator-v2",
                "TEUTONIC_DATASET_SOURCE": "s3",
                "TEUTONIC_DATASET_LABEL": "bundle-1",
                "TEUTONIC_TOKENIZER_LABEL": "gigatoken-1",
            }
        )
        self.assertEqual(policy.n, 25000)
        self.assertEqual(policy.seq_len, 8192)
        self.assertEqual(policy.n_bootstrap, 10000)
        self.assertEqual(policy.tokenizer_backend, "gigatoken")
        self.assertFalse(policy.publish_non_winning_models)


if __name__ == "__main__":
    unittest.main()
