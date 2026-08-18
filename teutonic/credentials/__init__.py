"""Credential lifecycle contracts."""

from .contracts import (
    ActivationChallenge,
    ActivationResponse,
    activation_message,
    mailbox_object_key,
    registration_id,
)

__all__ = [
    "ActivationChallenge",
    "ActivationResponse",
    "activation_message",
    "mailbox_object_key",
    "registration_id",
]
