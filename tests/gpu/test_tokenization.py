from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teutonic.evaluator.tokenization import (
    encode_batch,
    normalize_tokenizer_backend,
    prepare_tokenizer,
)


class FakeHFTokenizer:
    def __call__(self, texts, *, add_special_tokens, return_attention_mask):
        assert add_special_tokens is False
        assert return_attention_mask is False
        return {"input_ids": [[ord(char) for char in text] for text in texts]}


def test_backend_aliases_and_validation():
    assert normalize_tokenizer_backend("hf") == "huggingface"
    assert normalize_tokenizer_backend("gt") == "gigatoken"
    with pytest.raises(ValueError, match="unsupported tokenizer backend"):
        normalize_tokenizer_backend("unknown")


def test_huggingface_backend_and_batch_encoding():
    tokenizer = FakeHFTokenizer()
    prepared, backend = prepare_tokenizer(tokenizer, "huggingface")
    assert prepared is tokenizer
    assert backend == "huggingface"
    assert encode_batch(prepared, ["ab", "c"]) == [[97, 98], [99]]


def test_gigatoken_backend_uses_hf_compat_wrapper():
    source = FakeHFTokenizer()
    compat = FakeHFTokenizer()

    class FakeGigatokenTokenizer:
        def __init__(self, tokenizer):
            assert tokenizer is source

        def as_hf(self):
            return compat

    fake_module = SimpleNamespace(Tokenizer=FakeGigatokenTokenizer)
    with patch.dict(sys.modules, {"gigatoken": fake_module}):
        prepared, backend = prepare_tokenizer(source, "gigatoken")

    assert prepared is compat
    assert backend == "gigatoken"
