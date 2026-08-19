from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from teutonic.credentials import ActivationSignal

from .contracts import MetagraphSnapshot, ReadySignal, UidAssignment
from .repository import AccessControllerRepository, ControllerInvariantError


log = logging.getLogger("teutonic.access.chain")


def _values(value: Any) -> list[Any]:
    return value.tolist() if hasattr(value, "tolist") else list(value)


def _call_arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(argument.get("name")): argument.get("value")
        for argument in call.get("call_args", ())
        if isinstance(argument, Mapping)
    }


def commitment_payload(extrinsic: Mapping[str, Any], *, netuid: int) -> str | None:
    """Decode a successful Commitments.set_commitment call's Raw payload."""
    call = extrinsic.get("call")
    if not isinstance(call, Mapping):
        return None
    if call.get("call_module") != "Commitments" or call.get("call_function") != "set_commitment":
        return None
    arguments = _call_arguments(call)
    try:
        observed_netuid = int(arguments.get("netuid", -1))
    except (TypeError, ValueError):
        return None
    if observed_netuid != netuid:
        return None
    info = arguments.get("info")
    if not isinstance(info, Mapping):
        return None
    fields = info.get("fields")
    if not isinstance(fields, Iterable) or isinstance(fields, (str, bytes, Mapping)):
        return None
    raw_values: list[str] = []
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        for kind, value in field.items():
            if str(kind).startswith("Raw") and isinstance(value, str):
                raw_values.append(value)
    if len(raw_values) != 1:
        return None
    encoded = raw_values[0].removeprefix("0x")
    try:
        return bytes.fromhex(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _commitments_from_block(
    block: Mapping[str, Any],
    events: Iterable[Any],
    *,
    netuid: int,
    block_number: int,
) -> tuple[tuple[str, str, int, int], ...]:
    """Return authenticated (payload, hotkey, extrinsic, event) commitments."""
    commitment_events: dict[int, tuple[int, str]] = {}
    for position, event in enumerate(events):
        value = getattr(event, "value", event)
        if not isinstance(value, Mapping):
            continue
        attributes = value.get("attributes")
        try:
            observed_netuid = (
                int(attributes.get("netuid", -1))
                if isinstance(attributes, Mapping)
                else -1
            )
            extrinsic_index = int(value["extrinsic_idx"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            value.get("module_id") != "Commitments"
            or value.get("event_id") != "Commitment"
            or not isinstance(attributes, Mapping)
            or observed_netuid != netuid
            or value.get("extrinsic_idx") is None
        ):
            continue
        commitment_events[extrinsic_index] = (
            position,
            str(attributes.get("who", "")),
        )

    commitments: list[tuple[str, str, int, int]] = []
    for extrinsic_index, wrapped in enumerate(block.get("extrinsics", ())):
        extrinsic = getattr(wrapped, "value", wrapped)
        if not isinstance(extrinsic, Mapping):
            continue
        payload = commitment_payload(extrinsic, netuid=netuid)
        event = commitment_events.get(extrinsic_index)
        if payload is None or event is None:
            continue
        event_index, signalling_hotkey = event
        if not signalling_hotkey or extrinsic.get("address") != signalling_hotkey:
            log.warning(
                "ignoring commitment whose signer differs from finalized event "
                "block=%d extrinsic=%d",
                block_number,
                extrinsic_index,
            )
            continue
        commitments.append((payload, signalling_hotkey, extrinsic_index, event_index))
    return tuple(commitments)


def activation_signals_from_block(
    block: Mapping[str, Any],
    events: Iterable[Any],
    *,
    netuid: int,
    chain_generation: str,
    block_number: int,
) -> tuple[ActivationSignal, ...]:
    signals: list[ActivationSignal] = []
    for payload, hotkey, extrinsic_index, event_index in _commitments_from_block(
        block, events, netuid=netuid, block_number=block_number
    ):
        if not payload.startswith("r2activate:v1"):
            continue
        try:
            signals.append(
                ActivationSignal.parse(
                    payload,
                    netuid=netuid,
                    chain_generation=chain_generation,
                    signalling_hotkey=hotkey,
                    block_number=block_number,
                    extrinsic_index=extrinsic_index,
                    event_index=event_index,
                )
            )
        except ValueError as exc:
            log.warning(
                "ignoring malformed activation signal block=%d extrinsic=%d: %s",
                block_number,
                extrinsic_index,
                exc,
            )
    return tuple(signals)


def ready_signals_from_block(
    block: Mapping[str, Any],
    events: Iterable[Any],
    *,
    netuid: int,
    block_number: int,
) -> tuple[ReadySignal, ...]:
    """Extract authenticated ready signals using finalized event positions."""
    signals: list[ReadySignal] = []
    for payload, hotkey, extrinsic_index, event_index in _commitments_from_block(
        block, events, netuid=netuid, block_number=block_number
    ):
        if not payload.startswith("r2ready:v1"):
            continue
        try:
            signals.append(
                ReadySignal.parse(
                    payload,
                    signalling_hotkey=hotkey,
                    block_number=block_number,
                    extrinsic_index=extrinsic_index,
                    event_index=event_index,
                )
            )
        except ValueError as exc:
            log.warning(
                "ignoring malformed ready signal block=%d extrinsic=%d: %s",
                block_number,
                extrinsic_index,
                exc,
            )
    return tuple(signals)


class FinalizedChainScanner:
    """Advance registration state and ready signals from finalized chain data."""

    def __init__(
        self,
        subtensor: Any,
        *,
        netuid: int,
        chain_generation: str,
    ) -> None:
        self.subtensor = subtensor
        self.netuid = netuid
        self.chain_generation = chain_generation

    def snapshot(self, block_number: int) -> MetagraphSnapshot:
        block_hash = self.subtensor.substrate.get_block_hash(block_number)
        metagraph = self.subtensor.metagraph(self.netuid, block=block_number, lite=True)
        assignments = tuple(
            UidAssignment(
                uid=int(uid),
                hotkey=str(hotkey),
                coldkey=str(coldkey),
                registration_block=int(registration_block),
            )
            for uid, hotkey, coldkey, registration_block in zip(
                _values(metagraph.uids),
                metagraph.hotkeys,
                metagraph.coldkeys,
                _values(metagraph.block_at_registration),
            )
        )
        return MetagraphSnapshot(
            netuid=self.netuid,
            chain_generation=self.chain_generation,
            finalized_block=block_number,
            finalized_block_hash=str(block_hash),
            assignments=assignments,
            observed_at=datetime.now(timezone.utc),
            complete=True,
        )

    def _signals(
        self, block_number: int
    ) -> tuple[ActivationSignal | ReadySignal, ...]:
        block_hash = self.subtensor.substrate.get_block_hash(block_number)
        block = self.subtensor.substrate.get_block(block_hash=block_hash)
        events = self.subtensor.substrate.get_events(block_hash)
        signals = (
            *activation_signals_from_block(
                block,
                events,
                netuid=self.netuid,
                chain_generation=self.chain_generation,
                block_number=block_number,
            ),
            *ready_signals_from_block(
                block, events, netuid=self.netuid, block_number=block_number
            ),
        )
        return tuple(
            sorted(signals, key=lambda item: (item.extrinsic_index, item.event_index))
        )

    def scan(self, repository: AccessControllerRepository) -> tuple[int, int]:
        """Scan through the finalized head; return (blocks, accepted signals)."""
        finalized_hash = self.subtensor.substrate.get_chain_finalised_head()
        finalized_block = int(
            self.subtensor.substrate.get_block_number(finalized_hash)
        )
        cursor = repository.last_finalized_block(
            netuid=self.netuid, chain_generation=self.chain_generation
        )
        if cursor is None:
            repository.apply_finalized_snapshot(self.snapshot(finalized_block))
            return 1, 0
        if finalized_block <= cursor:
            return 0, 0

        signals_by_block: dict[int, tuple[ActivationSignal | ReadySignal, ...]] = {}
        for block_number in range(cursor + 1, finalized_block + 1):
            signals = self._signals(block_number)
            if signals:
                signals_by_block[block_number] = signals

        checkpoints = sorted({*signals_by_block, finalized_block})
        accepted = 0
        for block_number in checkpoints:
            repository.apply_finalized_snapshot(self.snapshot(block_number))
            for signal in signals_by_block.get(block_number, ()):
                try:
                    if isinstance(signal, ActivationSignal):
                        repository.accept_activation_signal(
                            signal, now=datetime.now(timezone.utc)
                        )
                    else:
                        repository.accept_ready_signal(
                            signal, now=datetime.now(timezone.utc)
                        )
                    accepted += 1
                except (ControllerInvariantError, ValueError) as exc:
                    kind = "activation" if isinstance(signal, ActivationSignal) else "ready"
                    log.warning(
                        "rejected finalized %s signal block=%d extrinsic=%d hotkey=%s: %s",
                        kind,
                        signal.block_number,
                        signal.extrinsic_index,
                        signal.signalling_hotkey,
                        exc,
                    )
        return finalized_block - cursor, accepted
