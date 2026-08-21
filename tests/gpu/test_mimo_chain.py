from __future__ import annotations

import chain_config
from teutonic.archs import mimo


def test_mimo_chain_is_pinned_to_immutable_hf_seed():
    assert chain_config.CONFIG_PATH.name == "chain.toml"
    assert chain_config.ARCH_MODULE == "teutonic.archs.mimo"
    assert chain_config.SEED_REPO == "dendriteholdings/teutonic-II-110B-genesis"
    assert chain_config.SEED_DIGEST == "hf:dbacb5e544b03345ca7b8cc322e74ac08bb0cc36"
    assert chain_config.SEED_REPO_BACKEND == "hf"
    assert chain_config.SEED_HOTKEY == "5E6yHkmZmSpBT5aa2rNZcmeYa1y3N9jw1h7g53oNPzMUpnqG"
    assert mimo.MODEL_TYPE == "mimo_v2"
    assert set(mimo.ALLOWED_CODE_FILES) == {
        "configuration_mimo_v2.py",
        "modeling_mimo_v2.py",
    }
    assert set(chain_config.GENESIS_CONTRACT_FILES) == {
        "chat_template.jinja.txt",
        "tokenizer.json",
        "tokenizer_config.json",
        "modeling_mimo_v2.py",
        "config.json",
        "configuration_mimo_v2.py",
    }
