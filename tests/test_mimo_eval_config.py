from __future__ import annotations

from types import SimpleNamespace

import pytest

from eval_server_quasar_pair import (
    EvalRequest,
    MODEL_INSTANCES_PER_SIDE,
    MODEL_WORKER_PROCESSES,
    model_worker_specs,
    resolved_attention_types,
)


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
    assert request.seq_len == 8192
    assert request.lm_head_chunk == 1024


def test_worker_topology_uses_two_processes_per_side_and_two_gpus_each():
    specs = model_worker_specs(list(range(8)))
    assert MODEL_INSTANCES_PER_SIDE == 2
    assert MODEL_WORKER_PROCESSES == 4
    assert specs == [
        {"worker_id": "king-0", "role": "king", "gpu_ids": [0, 1]},
        {"worker_id": "king-1", "role": "king", "gpu_ids": [2, 3]},
        {"worker_id": "challenger-0", "role": "challenger", "gpu_ids": [4, 5]},
        {"worker_id": "challenger-1", "role": "challenger", "gpu_ids": [6, 7]},
    ]
    with pytest.raises(RuntimeError, match="exactly 8 GPUs"):
        model_worker_specs(list(range(4)))
