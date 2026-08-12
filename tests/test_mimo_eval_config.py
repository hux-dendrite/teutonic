from __future__ import annotations

from types import SimpleNamespace

import pytest

from eval_server_quasar_pair import EvalRequest, resolved_attention_types


def mimo_config(**overrides):
    pattern = [0, 1, 1, 0]
    values = {
        "num_hidden_layers": len(pattern),
        "hybrid_layer_pattern": pattern,
        "layer_types": [
            "full_attention",
            "sliding_attention",
            "sliding_attention",
            "full_attention",
        ],
        "sliding_window": 128,
        "sliding_window_size": 128,
        "add_swa_attention_sink_bias": True,
        "_attn_implementation": "eager",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_eager_mimo_schedule_is_resolved_in_layer_order():
    assert resolved_attention_types(mimo_config()) == [
        "full_attention",
        "sliding_window_attention",
        "sliding_window_attention",
        "full_attention",
    ]


def test_non_eager_or_malformed_hybrid_config_is_rejected():
    with pytest.raises(RuntimeError, match="requires eager"):
        resolved_attention_types(mimo_config(_attn_implementation="sdpa"))
    with pytest.raises(RuntimeError, match="entries for 4 surviving layers"):
        resolved_attention_types(mimo_config(hybrid_layer_pattern=[0, 1]))


def test_eval_request_fixes_reference_runtime_settings():
    request = EvalRequest(king_repo="king", challenger_repo="challenger")
    assert request.attn_implementation == "eager"
    assert request.batch_size == 1
    assert request.parallel_batch_size == 1
    assert request.parallel_models is True
    assert request.seq_len == 4096

