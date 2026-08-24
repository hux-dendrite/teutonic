from __future__ import annotations

from dataclasses import dataclass


INITIALIZATION_EVENT_EXTRINSIC_INDEX = 0
FINALIZATION_EVENT_EXTRINSIC_INDEX = 2_147_483_647


@dataclass(frozen=True, order=True, slots=True)
class FinalizedPosition:
    """Durable total ordering for signals observed in finalized block events.

    The scanner must read finalized block events/calls, not only the
    RevealedCommitments storage map: the map retains a block number but omits
    the within-block extrinsic/event positions needed by king-of-the-hill.
    """

    block_number: int
    extrinsic_index: int
    event_index: int

    def __post_init__(self) -> None:
        if min(self.block_number, self.extrinsic_index, self.event_index) < 0:
            raise ValueError("finalized chain positions must be non-negative")

    @property
    def idempotency_key(self) -> str:
        return f"{self.block_number}:{self.extrinsic_index}:{self.event_index}"

    @classmethod
    def from_event(
        cls,
        *,
        block_number: int,
        extrinsic_index: int | None,
        event_index: int,
        phase: str,
    ) -> FinalizedPosition:
        if extrinsic_index is None:
            if phase == "Initialization":
                extrinsic_index = INITIALIZATION_EVENT_EXTRINSIC_INDEX
            elif phase == "Finalization":
                extrinsic_index = FINALIZATION_EVENT_EXTRINSIC_INDEX
            else:
                raise ValueError(
                    "only initialization/finalization events may omit an extrinsic index"
                )
        return cls(block_number, extrinsic_index, event_index)
