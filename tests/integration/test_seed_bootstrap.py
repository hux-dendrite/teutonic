from __future__ import annotations

import os
import unittest

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None

from teutonic.bootstrap import (
    GenesisIdentity,
    SeedArtifact,
    SeedBootstrapError,
    bootstrap_genesis,
)


DATABASE_URL = os.environ.get("TEUTONIC_TEST_DATABASE_URL")
HOTKEY = "5E6yHkmZmSpBT5aa2rNZcmeYa1y3N9jw1h7g53oNPzMUpnqG"


@unittest.skipUnless(DATABASE_URL and psycopg, "TEUTONIC_TEST_DATABASE_URL and psycopg required")
class SeedBootstrapIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def setUp(self):
        self.connection.execute(
            "TRUNCATE control_plane.weight_publications, control_plane.king_reigns, "
            "control_plane.competitions CASCADE"
        )
        self.connection.commit()
        self.artifact = SeedArtifact(
            repo_id="owner/model",
            revision="1" * 40,
            model_digest="a" * 64,
            files=(),
            manifest=b"{}",
        )

    def identity(self, *, uid=7, block=100):
        return GenesisIdentity(HOTKEY, uid, block)

    def bootstrap(self, identity=None, artifact=None):
        return bootstrap_genesis(
            self.connection,
            netuid=3,
            chain_generation="generation-1",
            competition="mimo",
            public_bucket="public-models",
            artifact=artifact or self.artifact,
            identity=identity or self.identity(),
        )

    def test_creates_genesis_only_after_caller_supplies_public_artifact_identity(self):
        created = self.bootstrap()
        replay = self.bootstrap(self.identity(uid=99, block=500))
        self.assertTrue(created.created)
        self.assertFalse(replay.created)
        self.assertEqual(created.reign_id, replay.reign_id)
        row = self.connection.execute(
            """
            SELECT c.current_reign_id, c.next_reign_number, r.reign_number,
                   r.model_digest, r.public_bucket, r.public_prefix, r.hotkey,
                   r.uid, r.crowned_finalized_block, r.operator_provenance
              FROM control_plane.competitions c
              JOIN control_plane.king_reigns r ON r.reign_id = c.current_reign_id
            """
        ).fetchone()
        self.assertEqual(row["reign_number"], 0)
        self.assertEqual(row["model_digest"], "a" * 64)
        self.assertEqual(row["public_bucket"], "public-models")
        self.assertEqual(row["public_prefix"], f"models/sha256/{'a' * 64}/")
        self.assertEqual((row["hotkey"], row["uid"]), (HOTKEY, 7))
        self.assertEqual(row["crowned_finalized_block"], 100)
        self.assertIn('"backend":"hf"', row["operator_provenance"])

    def test_rejects_conflicting_genesis(self):
        self.bootstrap()
        conflicting = SeedArtifact(
            repo_id="owner/model",
            revision="1" * 40,
            model_digest="b" * 64,
            files=(),
            manifest=b"{}",
        )
        with self.assertRaisesRegex(SeedBootstrapError, "different genesis"):
            self.bootstrap(artifact=conflicting)


if __name__ == "__main__":
    unittest.main()
