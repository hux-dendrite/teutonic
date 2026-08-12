"""Tokenizer backend selection for raw-text evaluation datasets."""
from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

HF_BACKEND = "huggingface"
GIGATOKEN_BACKEND = "gigatoken"
SUPPORTED_TOKENIZER_BACKENDS = (HF_BACKEND, GIGATOKEN_BACKEND)
_BACKEND_ALIASES = {"hf": HF_BACKEND, "transformers": HF_BACKEND, "gt": GIGATOKEN_BACKEND}
_configured_default = os.environ.get(
    "TEUTONIC_TOKENIZER_BACKEND",
    GIGATOKEN_BACKEND,
).strip().lower()
DEFAULT_TOKENIZER_BACKEND = _BACKEND_ALIASES.get(_configured_default, _configured_default)
if DEFAULT_TOKENIZER_BACKEND not in SUPPORTED_TOKENIZER_BACKENDS:
    raise ValueError(
        f"invalid TEUTONIC_TOKENIZER_BACKEND={DEFAULT_TOKENIZER_BACKEND!r}; "
        f"expected one of {SUPPORTED_TOKENIZER_BACKENDS}"
    )


def normalize_tokenizer_backend(backend: str | None = None) -> str:
    value = (backend or DEFAULT_TOKENIZER_BACKEND).strip().lower()
    value = _BACKEND_ALIASES.get(value, value)
    if value not in SUPPORTED_TOKENIZER_BACKENDS:
        raise ValueError(
            f"unsupported tokenizer backend {value!r}; "
            f"expected one of {SUPPORTED_TOKENIZER_BACKENDS}"
        )
    return value


def prepare_tokenizer(hf_tokenizer: Any, backend: str | None = None) -> tuple[Any, str]:
    """Return an HF-compatible tokenizer backed by the selected engine."""
    selected = normalize_tokenizer_backend(backend)
    if selected == HF_BACKEND:
        return hf_tokenizer, selected

    try:
        import gigatoken
    except ImportError as exc:
        raise RuntimeError(
            "tokenizer_backend='gigatoken' requires the gigatoken package; "
            "install project dependencies or set tokenizer_backend='huggingface'"
        ) from exc

    return gigatoken.Tokenizer(hf_tokenizer).as_hf(), selected


def encode_batch(tokenizer: Any, texts: Iterable[str]) -> list[list[int]]:
    """Encode a text batch without model-added special tokens."""
    materialized = list(texts)
    if not materialized:
        return []
    encoded = tokenizer(
        materialized,
        add_special_tokens=False,
        return_attention_mask=False,
    )
    rows = encoded["input_ids"]
    return [row.tolist() if hasattr(row, "tolist") else list(row) for row in rows]


__all__ = [
    "DEFAULT_TOKENIZER_BACKEND",
    "GIGATOKEN_BACKEND",
    "HF_BACKEND",
    "SUPPORTED_TOKENIZER_BACKENDS",
    "encode_batch",
    "normalize_tokenizer_backend",
    "prepare_tokenizer",
]
