"""Credential lifecycle contracts."""

from .contracts import (
    ActivationSignal,
    activation_signal_payload,
    activation_message,
    mailbox_object_key,
    registration_id,
)

__all__ = [
    "ActivationSignal",
    "activation_signal_payload",
    "activation_message",
    "mailbox_object_key",
    "registration_id",
]
