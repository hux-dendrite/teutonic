from __future__ import annotations

import math
from typing import Protocol

from .contracts import ChainObservation, SubmissionReceipt, WeightPlan


class WeightChainGateway(Protocol):
    network: str
    signer_hotkey: str
    mortality_period: int

    def current_block(self) -> int: ...

    def observe(self, plan: WeightPlan) -> ChainObservation: ...

    def submit(self, plan: WeightPlan) -> SubmissionReceipt: ...

    def find_finalized(
        self, extrinsic_id: str, *, start_block: int
    ) -> SubmissionReceipt | None: ...


def _as_int(value) -> int:
    if hasattr(value, "item"):
        value = value.item()
    return int(value)


def _as_float(value) -> float:
    if hasattr(value, "item"):
        value = value.item()
    return float(value)


def _receipt_value(receipt, name: str):
    value = getattr(receipt, name, None)
    if callable(value):
        value = value()
    return value


class BittensorWeightGateway:
    """The only Phase 7 adapter that imports Bittensor or loads a signing wallet."""

    def __init__(
        self,
        *,
        network: str,
        netuid: int,
        wallet_path: str,
        wallet_name: str,
        wallet_hotkey: str,
        mortality_period: int = 128,
        version_key: int = 10_005_000,
    ) -> None:
        if mortality_period < 8:
            raise ValueError("weight extrinsic mortality period is too short")
        import bittensor as bt

        self.network = network
        self.netuid = netuid
        self.mortality_period = mortality_period
        self.version_key = version_key
        self.wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey, path=wallet_path)
        self.signer_hotkey = self.wallet.hotkey.ss58_address
        self.subtensor = bt.Subtensor(network=network)

    def current_block(self) -> int:
        return _as_int(self.subtensor.get_current_block())

    def observe(self, plan: WeightPlan) -> ChainObservation:
        finalized_hash = self.subtensor.substrate.get_chain_finalised_head()
        finalized_block = _as_int(self.subtensor.substrate.get_block_number(finalized_hash))
        current_block = self.current_block()
        # The SDK's default lite snapshot deliberately returns an empty W array.
        # Full mode is required for restart reconciliation against finalized weights.
        metagraph = self.subtensor.metagraph(self.netuid, block=finalized_block, lite=False)
        hotkeys = list(metagraph.hotkeys)
        if self.signer_hotkey not in hotkeys:
            raise RuntimeError("weight signer is not registered on the configured subnet")
        validator_uid = hotkeys.index(self.signer_hotkey)
        last_update = _as_int(metagraph.last_update[validator_uid])
        observed_row = metagraph.W[validator_uid]
        expected = {uid: weight for uid, weight in zip(plan.target_uids, plan.normalized_weights)}
        weights_match = True
        for uid in range(len(hotkeys)):
            actual = _as_float(observed_row[uid])
            wanted = expected.get(uid, 0.0)
            if not math.isclose(actual, wanted, rel_tol=1e-5, abs_tol=1e-5):
                weights_match = False
                break
        return ChainObservation(
            current_block=current_block,
            finalized_block=finalized_block,
            uid_count=len(hotkeys),
            validator_uid=validator_uid,
            last_update=last_update,
            weights_match=weights_match,
        )

    def submit(self, plan: WeightPlan) -> SubmissionReceipt:
        started_block = self.current_block()
        response = self.subtensor.set_weights(
            wallet=self.wallet,
            netuid=self.netuid,
            uids=list(plan.target_uids),
            weights=list(plan.normalized_weights),
            max_attempts=1,
            version_key=self.version_key,
            period=self.mortality_period,
            wait_for_inclusion=True,
            wait_for_finalization=True,
        )
        if not response.success:
            code = type(response.error).__name__ if response.error is not None else "chain_rejected"
            return SubmissionReceipt(False, error_code=code)
        receipt = response.extrinsic_receipt
        if receipt is None:
            return SubmissionReceipt(True)
        extrinsic_hash = _receipt_value(receipt, "extrinsic_hash")
        block_hash = _receipt_value(receipt, "block_hash")
        block_number = _receipt_value(receipt, "block_number")
        extrinsic_id = str(extrinsic_hash) if extrinsic_hash is not None else None
        if extrinsic_id is not None and block_number is None:
            finalized = self.find_finalized(
                extrinsic_id,
                start_block=started_block,
            )
            if finalized is not None:
                return finalized
        return SubmissionReceipt(
            True,
            extrinsic_id=extrinsic_id,
            included_block=_as_int(block_number) if block_number is not None else None,
            included_block_hash=str(block_hash) if block_hash is not None else None,
            finalized_block=_as_int(block_number) if block_number is not None else None,
            finalized_block_hash=str(block_hash) if block_hash is not None else None,
        )

    def find_finalized(
        self, extrinsic_id: str, *, start_block: int
    ) -> SubmissionReceipt | None:
        finalized_hash = self.subtensor.substrate.get_chain_finalised_head()
        finalized_block = _as_int(self.subtensor.substrate.get_block_number(finalized_hash))
        for block_number in range(max(0, start_block), finalized_block + 1):
            block = self.subtensor.substrate.get_block(block_number=block_number)
            for extrinsic in block.get("extrinsics", ()):  # GenericExtrinsic values
                value = getattr(extrinsic, "extrinsic_hash", None)
                if value is not None and f"0x{value.hex()}" == extrinsic_id:
                    block_hash = str(self.subtensor.substrate.get_block_hash(block_number))
                    return SubmissionReceipt(
                        True,
                        extrinsic_id=extrinsic_id,
                        included_block=block_number,
                        included_block_hash=block_hash,
                        finalized_block=block_number,
                        finalized_block_hash=block_hash,
                    )
        return None


class DryRunWeightGateway:
    def __init__(self, *, netuid: int, signer_hotkey: str = "dry-run-signer") -> None:
        self.network = "dry-run"
        self.netuid = netuid
        self.signer_hotkey = signer_hotkey
        self.mortality_period = 8
        self._block = 1_000
        self._last_update = 0
        self._last_payload: str | None = None
        self._receipts: dict[str, SubmissionReceipt] = {}

    def current_block(self) -> int:
        return self._block

    def advance_blocks(self, count: int) -> None:
        if count < 0:
            raise ValueError("cannot advance the dry-run chain backwards")
        self._block += count

    def observe(self, plan: WeightPlan) -> ChainObservation:
        return ChainObservation(
            current_block=self._block,
            finalized_block=self._block,
            uid_count=max(plan.target_uids) + 1,
            validator_uid=0,
            last_update=self._last_update,
            weights_match=self._last_payload == plan.payload_sha256,
        )

    def submit(self, plan: WeightPlan) -> SubmissionReceipt:
        self._block += 1
        self._last_update = self._block
        self._last_payload = plan.payload_sha256
        identity = f"dry-run:{plan.payload_sha256}:{self._block}"
        receipt = SubmissionReceipt(
            True,
            extrinsic_id=identity,
            included_block=self._block,
            included_block_hash=identity,
            finalized_block=self._block,
            finalized_block_hash=identity,
        )
        self._receipts[identity] = receipt
        return receipt

    def find_finalized(
        self, extrinsic_id: str, *, start_block: int
    ) -> SubmissionReceipt | None:
        receipt = self._receipts.get(extrinsic_id)
        if receipt is not None and (receipt.finalized_block or 0) >= start_block:
            return receipt
        return None
