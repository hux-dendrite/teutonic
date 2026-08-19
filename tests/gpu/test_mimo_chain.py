from __future__ import annotations

import chain_config
from teutonic.archs import mimo


def test_mimo_chain_is_pinned_to_immutable_hf_seed():
    assert chain_config.ARCH_MODULE == "teutonic.archs.mimo"
    assert chain_config.SEED_REPO == (
        "dendriteholdings/mimo-v2.5-pro-104b-64e-w1024-top4"
    )
    assert chain_config.SEED_DIGEST == "hf:56fc5b83784cb00a32c3f59e5ef92b9b58a7d7a9"
    assert chain_config.SEED_REPO_BACKEND == "hf"
    assert mimo.MODEL_TYPE == "mimo_v2"
    assert set(mimo.ALLOWED_CODE_FILES) == {
        "configuration_mimo_v2.py",
        "modeling_mimo_v2.py",
    }
