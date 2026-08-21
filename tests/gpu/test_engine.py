from __future__ import annotations

import sys
import threading
import time
import types
from queue import Queue
from types import SimpleNamespace

import pytest
import torch
from transformers import GPT2Config, GPT2LMHeadModel

from teutonic.evaluator import engine as eval_server
from teutonic.evaluator.engine import (
    EvalRequest,
    MODEL_INSTANCES_PER_SIDE,
    MODEL_WORKER_PROCESSES,
    TwoGpuSequencePipeline,
    checkpoint_load_key,
    kernel_cache_identity,
    load_eval_model,
    model_worker_specs,
    patch_mimo_masking_compat,
    resolved_attention_types,
    snapshot_safetensor_keys,
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


def test_mimo_schedule_is_derived_before_model_sets_layer_types():
    config = mimo_config()
    del config.layer_types
    assert resolved_attention_types(config) == [
        "full_attention",
        "sliding_window_attention",
        "sliding_window_attention",
        "full_attention",
    ]


def test_mimo_masking_compat_removes_only_obsolete_cache_position():
    module = types.ModuleType("test_mimo_masking_compat_module")

    def current_mask(*, config, inputs_embeds, attention_mask, past_key_values, position_ids=None):
        return config, inputs_embeds, attention_mask, past_key_values, position_ids

    module.create_causal_mask = current_mask
    module.create_sliding_window_causal_mask = current_mask
    sys.modules[module.__name__] = module
    try:
        model_type = type("TestMiMo", (), {"__module__": module.__name__})
        model = model_type()
        model.config = SimpleNamespace(model_type="mimo_v2")
        assert set(patch_mimo_masking_compat(model)) == {
            "create_causal_mask",
            "create_sliding_window_causal_mask",
        }
        assert module.create_causal_mask(
            config="config",
            inputs_embeds="embeds",
            attention_mask="mask",
            cache_position="obsolete",
            past_key_values="cache",
            position_ids="positions",
        ) == ("config", "embeds", "mask", "cache", "positions")
        assert patch_mimo_masking_compat(model) == ()
    finally:
        sys.modules.pop(module.__name__, None)


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


def test_checkpoint_key_excludes_randomized_sequences(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    request = EvalRequest(king_repo="king", challenger_repo="challenger", seed=1)
    first = checkpoint_load_key(str(tmp_path), request, [0, 1])
    request.seed = 999
    request.n = 17
    assert checkpoint_load_key(str(tmp_path), request, [0, 1]) == first


def test_safetensor_keys_are_read_from_index_without_loading_payloads(tmp_path):
    (tmp_path / "model.safetensors.index.json").write_text(
        '{"weight_map":{"model.layers.1.weight":"b.safetensors",'
        '"model.layers.0.weight":"a.safetensors"}}'
    )
    assert snapshot_safetensor_keys(str(tmp_path)) == [
        "model.layers.0.weight",
        "model.layers.1.weight",
    ]


def test_direct_checkpoint_loader_resolves_tied_meta_weights(tmp_path):
    config = GPT2Config(
        n_layer=1,
        n_head=2,
        n_embd=8,
        n_positions=16,
        vocab_size=16,
        bos_token_id=0,
        eos_token_id=1,
    )
    GPT2LMHeadModel(config).save_pretrained(tmp_path, safe_serialization=True)
    request = EvalRequest(king_repo="king", challenger_repo="challenger")
    loaded = load_eval_model(str(tmp_path), config, "cpu", "tiny", request, gpu_ids=[])
    assert not any(parameter.is_meta for parameter in loaded.parameters())
    assert next(loaded.parameters()).dtype == torch.bfloat16


def test_kernel_cache_is_architecture_keyed(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    config_a = SimpleNamespace(
        to_dict=lambda: {"model_type": "mimo_v2", "hidden_size": 4096, "num_experts": 64}
    )
    config_b = SimpleNamespace(
        to_dict=lambda: {"model_type": "mimo_v2", "hidden_size": 8192, "num_experts": 64}
    )
    assert kernel_cache_identity(config_a, [0, 1]) == kernel_cache_identity(config_a, [2, 3])
    assert kernel_cache_identity(config_a, [0, 1]) != kernel_cache_identity(config_b, [0, 1])


def test_two_gpu_pipeline_overlaps_next_stage_one_with_current_stage_two(monkeypatch):
    class FakeModel(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.model = SimpleNamespace(layers=torch.nn.ModuleList([torch.nn.Linear(1, 1), torch.nn.Linear(1, 1)]))

    events = []
    events_lock = threading.Lock()
    output = Queue()
    pipeline_ref = {}

    monkeypatch.setattr(TwoGpuSequencePipeline, "_find_boundary_layer", lambda self: 1)

    def fake_loss(_model, token_batches, _chunk_size, **_kwargs):
        sequence_id = token_batches[0][0]
        with events_lock:
            events.append((sequence_id, "stage1"))
        time.sleep(0.03)
        pipeline_ref["pipeline"]._enter_stage2(None, None)
        with events_lock:
            events.append((sequence_id, "stage2"))
        time.sleep(0.03)
        with events_lock:
            events.append((sequence_id, "done"))
        return [float(sequence_id)]

    monkeypatch.setattr(eval_server, "compute_per_sequence_loss", fake_loss)
    request = EvalRequest(king_repo="king", challenger_repo="challenger")
    pipeline = TwoGpuSequencePipeline(
        FakeModel(),
        request,
        {"worker_id": "king-0", "role": "king", "gpu_ids": [0, 1]},
        output,
        "generation",
    )
    pipeline_ref["pipeline"] = pipeline
    pipeline.submit(0, [0])
    pipeline.submit(1, [1])
    pipeline.close()

    assert pipeline.depth == 2
    assert events.index((1, "stage1")) < events.index((0, "done"))
    assert {output.get()["sequence_index"], output.get()["sequence_index"]} == {0, 1}
