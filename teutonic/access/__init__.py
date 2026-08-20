"""Finalized-chain registration and immutable-upload control plane."""

from importlib import import_module
from typing import Any

from .contracts import (
    Manifest,
    ManifestFile,
    MetagraphSnapshot,
    ReadySignal,
    UidAssignment,
    ready_signal_payload,
)
from .crypto import (
    MailboxCipher,
    decode_ss58_public_key,
    encode_ss58_public_key,
    verify_hotkey_signature,
)

_LAZY_EXPORTS = {
    "FinalizedChainScanner": (".chain", "FinalizedChainScanner"),
    "activation_signals_from_block": (".chain", "activation_signals_from_block"),
    "commitment_payload": (".chain", "commitment_payload"),
    "ready_signals_from_block": (".chain", "ready_signals_from_block"),
    "AccessControllerRepository": (".repository", "AccessControllerRepository"),
    "ControllerInvariantError": (".repository", "ControllerInvariantError"),
    "ControllerLockUnavailable": (".repository", "ControllerLockUnavailable"),
    "AccessControllerJobRunner": (".service", "AccessControllerJobRunner"),
    "MailboxStore": (".service", "MailboxStore"),
    "ImmutableSnapshotResult": (".storage", "ImmutableSnapshotResult"),
    "R2UploadController": (".storage", "R2UploadController"),
}


def __getattr__(name: str) -> Any:
    """Load control-plane-only exports without burdening miner installations."""
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value

__all__ = [
    "AccessControllerRepository",
    "AccessControllerJobRunner",
    "ControllerLockUnavailable",
    "ControllerInvariantError",
    "FinalizedChainScanner",
    "activation_signals_from_block",
    "ImmutableSnapshotResult",
    "MailboxCipher",
    "MailboxStore",
    "Manifest",
    "ManifestFile",
    "MetagraphSnapshot",
    "R2UploadController",
    "ReadySignal",
    "UidAssignment",
    "decode_ss58_public_key",
    "encode_ss58_public_key",
    "ready_signal_payload",
    "commitment_payload",
    "ready_signals_from_block",
    "verify_hotkey_signature",
]
