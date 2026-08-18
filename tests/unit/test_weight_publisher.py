from __future__ import annotations

import math
import unittest

from teutonic.weights import WeightPlan, WeightPlanError, weight_payload_digest


def plan(**changes) -> WeightPlan:
    values = {
        "publication_id": "publication",
        "competition_id": "competition",
        "source_reign_id": "reign",
        "current_reign_id": "reign",
        "reign_number": 1,
        "policy_version": "policy-v1",
        "payload_revision": 1,
        "target_hotkeys": ("hotkey-1", "hotkey-3"),
        "target_uids": (1, 3),
        "normalized_weights": (0.75, 0.25),
        "payload_sha256": weight_payload_digest([1, 3], [0.75, 0.25]),
        "idempotency_key": "publish-weights:reign",
        "previous_state": "claimed",
        "attempt_count": 1,
    }
    values.update(changes)
    return WeightPlan(**values)


class WeightPlanTests(unittest.TestCase):
    def test_valid_frozen_plan(self) -> None:
        plan().validate(uid_count=4)

    def test_rejects_duplicate_out_of_range_and_nonfinite_values(self) -> None:
        cases = (
            {"target_uids": (1, 1)},
            {"target_uids": (1, 4)},
            {"normalized_weights": (math.nan, 0.25)},
            {"normalized_weights": (0.5, 0.25)},
            {"payload_sha256": "0" * 64},
            {"payload_revision": 0},
            {"target_hotkeys": ("hotkey-1",)},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(WeightPlanError):
                plan(**changes).validate(uid_count=4)

    def test_payload_digest_matches_validator_canonical_json(self) -> None:
        self.assertEqual(
            weight_payload_digest([1, 2], [0.75, 0.25]),
            "c5c4dafb26af0c8c3a70466dc62964c38134e953030e2bed06a30accfce883cd",
        )


if __name__ == "__main__":
    unittest.main()
