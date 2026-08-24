from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Protocol, cast

from app.base_asset_universe import GovernedAssetUniverse
from app.live_execution_journal import (
    DailyExecutionLimitError,
    LiveExecutionJournalError,
    append_live_execution_event,
    reserve_live_execution,
)
from app.live_trading_config import BASE_USDC_ADDRESS, LiveTradingConfig
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    BASE_MAINNET_CHAIN_ID,
    EXECUTOR_MODE_CONTROLLED_LIVE,
    MAX_TRADE_NOTIONAL_USDC,
    STATUS_CONTROLLED_LIVE_APPROVED,
    ExecutorConfig,
    RiskSnapshot,
    TradeIntent,
    evaluate_trade_intent,
    load_executor_config,
)


# CDP's account lookup requires the exact checksummed form returned at creation.
CDP_WALLET_LOOKUP_ADDRESS = "0x716B5D6Bf67A4C01103B52365C8fB5fdFEf0ff06"


ROUTE_ID = "cdp_agentkit_base_governed_asset_usdc_v2"
CDP_NETWORK_ID = "base-mainnet"
CDP_SWAP_NETWORK = "base"
NATIVE_ETH_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
PERMIT2_ADDRESS = "0x000000000022d473030f116ddee9f6b43ac78ba3"
MAX_SLIPPAGE_BPS = 100
RECEIPT_SLIPPAGE_BPS_TOLERANCE = Decimal("0.00001")
TRANSACTION_HASH_PATTERN = re.compile(r"0x[0-9a-fA-F]{64}")

STATUS_CONFIRMED = "CONFIRMED"
STATUS_POLICY_REJECTED = "POLICY_REJECTED"
STATUS_DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
STATUS_DAILY_LIMIT_BLOCKED = "DAILY_LIMIT_BLOCKED"
STATUS_BACKEND_FAILED = "BACKEND_FAILED"
STATUS_RECEIPT_REJECTED = "RECEIPT_REJECTED"
STATUS_AUDIT_FAILURE = "AUDIT_FAILURE"


@dataclass(frozen=True)
class ApprovedSwap:
    quote_id: str
    quote_observed_at: datetime
    route_id: str
    wallet_address: str
    chain_id: int
    from_token: str
    to_token: str
    from_amount: Decimal
    from_decimals: int
    to_decimals: int
    notional_usdc: Decimal
    slippage_bps: int


@dataclass(frozen=True)
class SwapReceipt:
    success: bool
    transaction_hash: str | None
    quote_id: str
    wallet_address: str
    network_id: str
    from_token: str
    to_token: str
    from_amount: Decimal
    to_amount: Decimal
    min_to_amount: Decimal
    slippage_bps: int
    approval_transaction_hash: str | None = None
    approval_token: str | None = None
    approval_spender: str | None = None
    approval_amount: Decimal | None = None
    error: str | None = None


@dataclass(frozen=True)
class ControlledLiveResult:
    status: str
    reasons: tuple[str, ...]
    transaction_hash: str | None = None
    reservation_sequence: int | None = None
    outcome_sequence: int | None = None


class SwapBackend(Protocol):
    def submit_swap(self, request: ApprovedSwap) -> SwapReceipt:
        ...


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _checksum_address(address: str) -> str:
    """Return the canonical EVM address required by AgentKit's web3 boundary."""

    try:
        from eth_utils import to_checksum_address
    except ImportError as error:
        raise RuntimeError(
            "eth-utils is required for controlled-live EVM address handling."
        ) from error
    try:
        return str(to_checksum_address(address))
    except ValueError as error:
        raise ValueError("CDP backend received an invalid EVM address.") from error


def _prepare_cdp_swap_response_model() -> None:
    """Repair CDP SDK 1.48.0's impossible boolean liquidity validator.

    The generated model declares ``liquidityAvailable`` as ``StrictBool`` but
    also requires the string literal ``"true"``. Coinbase's API correctly
    returns a JSON boolean, so no response can pass both constraints. Remove
    only that generated enum validator while retaining Pydantic validation for
    the boolean field and every other quote field.
    """

    from importlib.metadata import version

    if version("cdp-sdk") != "1.48.0":
        return
    from cdp.openapi_client.models.create_swap_quote_response import (
        CreateSwapQuoteResponse,
    )

    decorators = getattr(CreateSwapQuoteResponse, "__pydantic_decorators__", None)
    validators = getattr(decorators, "field_validators", None)
    fields = getattr(CreateSwapQuoteResponse, "model_fields", {})
    validator_name = "liquidity_available_validate_enum"
    if not isinstance(validators, dict) or "liquidity_available" not in fields:
        raise RuntimeError("CDP swap response model is incompatible with the reviewed fix.")
    if validator_name not in validators:
        return
    validators.pop(validator_name)
    CreateSwapQuoteResponse.model_rebuild(force=True)


def _validate_swap(
    swap: ApprovedSwap,
    intent: TradeIntent,
    *,
    now: datetime,
    max_age_seconds: int,
    max_future_skew_seconds: int,
    asset_universe: GovernedAssetUniverse | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not swap.quote_id.strip():
        reasons.append("A quote ID is required.")
    if not _aware(swap.quote_observed_at):
        reasons.append("Swap quote timestamp must include a timezone.")
    else:
        age = (now - swap.quote_observed_at).total_seconds()
        if age > max_age_seconds:
            reasons.append("Swap quote is stale.")
        if age < -max_future_skew_seconds:
            reasons.append("Swap quote timestamp is too far in the future.")
    if swap.route_id != ROUTE_ID:
        reasons.append("Swap route is not approved.")
    if swap.wallet_address.strip().lower() != AUTHORIZED_TREASURY_ADDRESS:
        reasons.append("Swap wallet is not the authorized treasury.")
    if swap.wallet_address.strip().lower() != intent.treasury_address.strip().lower():
        reasons.append("Swap wallet does not match the trade intent.")
    if swap.chain_id != BASE_MAINNET_CHAIN_ID or swap.chain_id != intent.chain_id:
        reasons.append("Swap chain must be Base mainnet chain ID 8453.")
    asset_token = (
        intent.asset_token_address.strip().lower()
        if intent.asset_token_address is not None
        else NATIVE_ETH_ADDRESS
    )
    side = intent.side.strip().upper()
    expected_from = BASE_USDC_ADDRESS if side == "BUY" else asset_token
    expected_to = asset_token if side == "BUY" else BASE_USDC_ADDRESS
    if swap.from_token.strip().lower() != expected_from:
        reasons.append("Swap input token does not match the governed trade direction.")
    if swap.to_token.strip().lower() != expected_to:
        reasons.append("Swap output token does not match the governed trade direction.")
    expected_asset_decimals = 18
    if asset_universe is not None:
        try:
            expected_asset_decimals = asset_universe.require(
                intent.asset_symbol,
                intent.asset_token_address,
            ).decimals
        except ValueError:
            reasons.append("Swap asset is outside the governed universe.")
    expected_from_decimals = 6 if side == "BUY" else expected_asset_decimals
    expected_to_decimals = expected_asset_decimals if side == "BUY" else 6
    if swap.from_decimals != expected_from_decimals:
        reasons.append("Swap input decimals do not match the governed asset metadata.")
    if swap.to_decimals != expected_to_decimals:
        reasons.append("Swap output decimals do not match the governed asset metadata.")
    if (
        not swap.from_amount.is_finite()
        or swap.from_amount <= 0
        or swap.from_amount.as_tuple().exponent < -expected_from_decimals
    ):
        reasons.append("Swap input amount is invalid for the governed token decimals.")
    if side == "BUY" and swap.from_amount != intent.notional_usdc:
        reasons.append("A buy must spend exactly the approved USDC notional.")
    if swap.notional_usdc != intent.notional_usdc:
        reasons.append("Swap notional must exactly match the trade intent.")
    if type(swap.slippage_bps) is not int or not 0 <= swap.slippage_bps <= MAX_SLIPPAGE_BPS:
        reasons.append("Swap slippage exceeds the absolute 100 bps limit.")
    return tuple(reasons)


def _validate_receipt(
    receipt: SwapReceipt,
    request: ApprovedSwap,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not receipt.success:
        reasons.append("CDP backend reported failure.")
    if not receipt.transaction_hash or not TRANSACTION_HASH_PATTERN.fullmatch(
        receipt.transaction_hash
    ):
        reasons.append("CDP receipt transaction hash is missing or invalid.")
    if not receipt.quote_id.strip():
        reasons.append("CDP receipt quote ID is missing.")
    if receipt.wallet_address.strip().lower() != request.wallet_address.strip().lower():
        reasons.append("CDP receipt wallet does not match the approved wallet.")
    if receipt.network_id != CDP_NETWORK_ID:
        reasons.append("CDP receipt network is not Base mainnet.")
    if receipt.from_token.strip().lower() != request.from_token.strip().lower():
        reasons.append("CDP receipt input token does not match the approved route.")
    if receipt.to_token.strip().lower() != request.to_token.strip().lower():
        reasons.append("CDP receipt output token does not match the approved route.")
    if receipt.from_amount != request.from_amount:
        reasons.append("CDP receipt input amount does not match the approved amount.")
    if receipt.slippage_bps != request.slippage_bps:
        reasons.append("CDP receipt slippage does not match the approved maximum.")
    if (
        not receipt.to_amount.is_finite()
        or not receipt.min_to_amount.is_finite()
        or receipt.to_amount <= 0
        or receipt.min_to_amount <= 0
        or receipt.to_amount < receipt.min_to_amount
        or (
            request.to_token.strip().lower() == BASE_USDC_ADDRESS
            and receipt.to_amount > request.notional_usdc
        )
    ):
        reasons.append("CDP receipt output amounts are invalid.")
    elif (
        (receipt.to_amount - receipt.min_to_amount)
        * Decimal("10000")
        / receipt.to_amount
        > Decimal(request.slippage_bps) + RECEIPT_SLIPPAGE_BPS_TOLERANCE
    ):
        reasons.append("CDP receipt minimum output exceeds approved slippage.")
    approval_values = (
        receipt.approval_transaction_hash,
        receipt.approval_token,
        receipt.approval_spender,
        receipt.approval_amount,
    )
    native_input = request.from_token.strip().lower() == NATIVE_ETH_ADDRESS
    if native_input and any(value is not None for value in approval_values):
        reasons.append("Native ETH must not create an approval transaction.")
    elif not native_input and any(value is not None for value in approval_values):
        if not all(value is not None for value in approval_values):
            reasons.append("ERC-20 approval evidence is incomplete.")
        else:
            approval_transaction_hash = cast(str, receipt.approval_transaction_hash)
            approval_token = cast(str, receipt.approval_token)
            approval_spender = cast(str, receipt.approval_spender)
            approval_amount = cast(Decimal, receipt.approval_amount)
            if not TRANSACTION_HASH_PATTERN.fullmatch(
                approval_transaction_hash
            ):
                reasons.append("ERC-20 approval transaction hash is invalid.")
            if approval_token.strip().lower() != request.from_token.strip().lower():
                reasons.append("ERC-20 approval token does not match the swap input.")
            if approval_spender.strip().lower() != PERMIT2_ADDRESS:
                reasons.append("ERC-20 approval spender is not Permit2.")
            if approval_amount != request.from_amount:
                reasons.append("ERC-20 approval must equal the exact swap amount.")
    return tuple(reasons)


def execute_controlled_live_trade(
    intent: TradeIntent,
    risk: RiskSnapshot,
    swap: ApprovedSwap,
    backend: SwapBackend,
    *,
    decision_journal_path: Path,
    live_audit_path: Path,
    now: datetime | None = None,
    live_config: LiveTradingConfig | None = None,
    executor_config: ExecutorConfig | None = None,
    asset_universe: GovernedAssetUniverse | None = None,
) -> ControlledLiveResult:
    from app.execution_journal import (
        JournalIntegrityError,
        append_execution_decision,
    )

    current_time = now or datetime.now(timezone.utc)
    decision = evaluate_trade_intent(
        intent,
        risk,
        now=current_time,
        live_config=live_config,
        executor_config=executor_config,
        asset_universe=asset_universe,
    )
    try:
        recorded = append_execution_decision(
            decision,
            path=decision_journal_path,
            recorded_at=current_time,
        )
    except (JournalIntegrityError, OSError, ValueError) as error:
        return ControlledLiveResult(
            status=STATUS_AUDIT_FAILURE,
            reasons=(f"Execution decision journal unavailable: {error}",),
        )
    if recorded.duplicate:
        return ControlledLiveResult(
            status=STATUS_DUPLICATE_BLOCKED,
            reasons=("Intent ID was already recorded; replay blocked.",),
        )
    if (
        decision.status != STATUS_CONTROLLED_LIVE_APPROVED
        or not decision.executable
        or decision.signing_authority != "cdp_agentkit"
        or decision.mode != EXECUTOR_MODE_CONTROLLED_LIVE
    ):
        return ControlledLiveResult(
            status=STATUS_POLICY_REJECTED,
            reasons=decision.reasons,
        )

    freshness_config = executor_config or load_executor_config()
    swap_reasons = _validate_swap(
        swap,
        intent,
        now=current_time,
        max_age_seconds=freshness_config.max_data_age_seconds,
        max_future_skew_seconds=freshness_config.max_future_skew_seconds,
        asset_universe=asset_universe,
    )
    if swap_reasons:
        return ControlledLiveResult(
            status=STATUS_POLICY_REJECTED,
            reasons=swap_reasons,
        )

    try:
        reservation = reserve_live_execution(
            intent_id=decision.intent_id,
            intent_fingerprint=decision.intent_fingerprint,
            notional_usdc=intent.notional_usdc,
            route_id=swap.route_id,
            wallet_address=swap.wallet_address,
            chain_id=swap.chain_id,
            quote_id=swap.quote_id,
            quote_observed_at=swap.quote_observed_at,
            from_token=swap.from_token,
            to_token=swap.to_token,
            from_amount=swap.from_amount,
            from_decimals=swap.from_decimals,
            to_decimals=swap.to_decimals,
            slippage_bps=swap.slippage_bps,
            path=live_audit_path,
            recorded_at=current_time,
        )
    except DailyExecutionLimitError as error:
        return ControlledLiveResult(
            status=STATUS_DAILY_LIMIT_BLOCKED,
            reasons=(str(error),),
        )
    except (LiveExecutionJournalError, OSError, ValueError) as error:
        return ControlledLiveResult(
            status=STATUS_AUDIT_FAILURE,
            reasons=(f"Live execution reservation failed: {error}",),
        )
    if reservation.duplicate:
        return ControlledLiveResult(
            status=STATUS_DUPLICATE_BLOCKED,
            reasons=("Live execution was already reserved; replay blocked.",),
            reservation_sequence=reservation.sequence,
        )

    try:
        receipt = backend.submit_swap(swap)
    except Exception as error:
        details = {
            "error": "CDP backend call failed.",
            "error_type": type(error).__name__,
        }
        try:
            outcome = append_live_execution_event(
                event="BACKEND_FAILED",
                intent_id=decision.intent_id,
                intent_fingerprint=decision.intent_fingerprint,
                details=details,
                path=live_audit_path,
                recorded_at=current_time,
            )
        except (LiveExecutionJournalError, OSError, ValueError) as journal_error:
            return ControlledLiveResult(
                status=STATUS_AUDIT_FAILURE,
                reasons=(
                    f"Backend failed and outcome audit failed: {journal_error}",
                ),
                reservation_sequence=reservation.sequence,
            )
        return ControlledLiveResult(
            status=STATUS_BACKEND_FAILED,
            reasons=(details["error"],),
            reservation_sequence=reservation.sequence,
            outcome_sequence=outcome,
        )

    receipt_reasons = _validate_receipt(receipt, swap)
    event = "RECEIPT_REJECTED" if receipt_reasons else "CONFIRMED"
    details = asdict(receipt)
    details.update({
        "from_amount": str(receipt.from_amount),
        "to_amount": str(receipt.to_amount),
        "min_to_amount": str(receipt.min_to_amount),
    })
    if receipt.approval_amount is not None:
        details["approval_amount"] = str(receipt.approval_amount)
    details["backend_error_reported"] = receipt.error is not None
    details.pop("error", None)
    if receipt_reasons:
        details["validation_reasons"] = list(receipt_reasons)
    try:
        outcome = append_live_execution_event(
            event=event,
            intent_id=decision.intent_id,
            intent_fingerprint=decision.intent_fingerprint,
            details=details,
            path=live_audit_path,
            recorded_at=current_time,
        )
    except (LiveExecutionJournalError, OSError, ValueError) as error:
        return ControlledLiveResult(
            status=STATUS_AUDIT_FAILURE,
            reasons=(f"Live execution outcome audit failed: {error}",),
            transaction_hash=receipt.transaction_hash,
            reservation_sequence=reservation.sequence,
        )
    return ControlledLiveResult(
        status=STATUS_RECEIPT_REJECTED if receipt_reasons else STATUS_CONFIRMED,
        reasons=receipt_reasons or ("CDP swap confirmed and audit-recorded.",),
        transaction_hash=receipt.transaction_hash,
        reservation_sequence=reservation.sequence,
        outcome_sequence=outcome,
    )


class CdpAgentKitBackend:
    """Production adapter for governed Base asset <-> USDC spot routes.

    AgentKit supplies the CDP EVM wallet provider and credential isolation. The
    adapter uses the provider's CDP client directly so the requested slippage
    is cryptographically bound into the quote; the generic AgentKit action is
    intentionally not exposed to strategy or model output.
    """

    def __init__(self, *, wallet_address: str = CDP_WALLET_LOOKUP_ADDRESS):
        try:
            from coinbase_agentkit import (
                CdpEvmWalletProvider,
                CdpEvmWalletProviderConfig,
            )
        except ImportError as error:
            raise RuntimeError(
                "coinbase-agentkit is required for controlled-live execution."
            ) from error
        self._wallet = CdpEvmWalletProvider(
            CdpEvmWalletProviderConfig(
                address=wallet_address,
                network_id=CDP_NETWORK_ID,
            )
        )
        if self._wallet.get_address().strip().lower() != wallet_address.strip().lower():
            raise RuntimeError("CDP wallet provider returned the wrong wallet.")
        network = self._wallet.get_network()
        if (
            str(network.chain_id) != str(BASE_MAINNET_CHAIN_ID)
            or network.network_id != CDP_NETWORK_ID
        ):
            raise RuntimeError("CDP wallet provider returned the wrong network.")

    @property
    def wallet_address(self) -> str:
        return self._wallet.get_address()

    @property
    def network_id(self) -> str:
        return self._wallet.get_network().network_id

    @staticmethod
    def _run(coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise RuntimeError("CDP backend cannot run inside an active event loop.")

    def submit_swap(self, request: ApprovedSwap) -> SwapReceipt:
        if request.route_id != ROUTE_ID:
            raise ValueError("CDP backend received an unapproved route.")

        atomic_value = request.from_amount * Decimal(10**request.from_decimals)
        if atomic_value != atomic_value.to_integral_value() or atomic_value <= 0:
            raise ValueError("CDP backend received an invalid atomic input amount.")
        atomic_input = int(atomic_value)
        approval_transaction_hash: str | None = None
        checksum_from_token = _checksum_address(request.from_token)
        checksum_to_token = _checksum_address(request.to_token)
        checksum_wallet = _checksum_address(self._wallet.get_address())
        checksum_permit2 = _checksum_address(PERMIT2_ADDRESS)
        _prepare_cdp_swap_response_model()

        if request.from_token.lower() != NATIVE_ETH_ADDRESS:
            allowance_abi = [
                {
                    "constant": True,
                    "inputs": [
                        {"name": "owner", "type": "address"},
                        {"name": "spender", "type": "address"},
                    ],
                    "name": "allowance",
                    "outputs": [{"name": "", "type": "uint256"}],
                    "type": "function",
                }
            ]
            current_allowance = int(
                self._wallet.read_contract(
                    contract_address=checksum_from_token,
                    abi=allowance_abi,
                    function_name="allowance",
                    args=[checksum_wallet, checksum_permit2],
                )
            )
            if current_allowance != atomic_input:
                spender_word = PERMIT2_ADDRESS[2:].rjust(64, "0")
                amount_word = f"{atomic_input:064x}"
                approval_data = f"0x095ea7b3{spender_word}{amount_word}"
                approval_transaction_hash = str(
                    self._wallet.send_transaction(
                        {
                            "to": checksum_from_token,
                            "value": 0,
                            "data": approval_data,
                        }
                    )
                )
                approval_receipt = self._wallet.wait_for_transaction_receipt(
                    approval_transaction_hash
                )
                approval_status = (
                    approval_receipt.get("status")
                    if isinstance(approval_receipt, dict)
                    else getattr(approval_receipt, "status", None)
                )
                if str(approval_status).lower() not in {"1", "success"}:
                    raise RuntimeError("Exact Permit2 approval transaction failed.")

        async def execute_quote() -> tuple[str, str, Decimal, Decimal]:
            client = self._wallet.get_client()
            async with client as cdp:
                account = await cdp.evm.get_account(
                    address=self._wallet.get_address()
                )
                quote = await account.quote_swap(
                    from_token=checksum_from_token,
                    to_token=checksum_to_token,
                    from_amount=str(atomic_input),
                    network=CDP_SWAP_NETWORK,
                    slippage_bps=request.slippage_bps,
                    idempotency_key=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"{ROUTE_ID}:{request.quote_id}:quote",
                        )
                    ),
                )
                if not quote.liquidity_available:
                    raise RuntimeError("CDP reported no liquidity for the approved route.")
                output_scale = Decimal(10**request.to_decimals)
                quoted_output = Decimal(str(quote.to_amount)) / output_scale
                minimum_output = Decimal(str(quote.min_to_amount)) / output_scale
                if (
                    not quoted_output.is_finite()
                    or not minimum_output.is_finite()
                    or quoted_output <= 0
                    or minimum_output <= 0
                    or quoted_output < minimum_output
                    or (
                        request.to_token.lower() == BASE_USDC_ADDRESS
                        and (
                            quoted_output > request.notional_usdc
                            or quoted_output > MAX_TRADE_NOTIONAL_USDC
                        )
                    )
                ):
                    raise RuntimeError(
                        "CDP quote violates the approved output/notional boundary."
                    )
                result = await quote.execute(
                    idempotency_key=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"{ROUTE_ID}:{request.quote_id}:execute",
                        )
                    )
                )
                transaction_hash = str(result.transaction_hash)
                return (
                    transaction_hash,
                    str(quote.quote_id),
                    quoted_output,
                    minimum_output,
                )

        transaction_hash, quote_id, quoted_output, minimum_output = self._run(
            execute_quote()
        )
        chain_receipt = self._wallet.wait_for_transaction_receipt(transaction_hash)
        chain_status = (
            chain_receipt.get("status")
            if isinstance(chain_receipt, dict)
            else getattr(chain_receipt, "status", None)
        )
        success = str(chain_status).lower() in {"1", "success"}
        return SwapReceipt(
            success=success,
            transaction_hash=transaction_hash,
            quote_id=quote_id,
            wallet_address=self._wallet.get_address(),
            network_id=self._wallet.get_network().network_id,
            from_token=request.from_token,
            to_token=request.to_token,
            from_amount=request.from_amount,
            to_amount=quoted_output,
            min_to_amount=minimum_output,
            slippage_bps=request.slippage_bps,
            approval_transaction_hash=approval_transaction_hash,
            approval_token=(
                request.from_token if approval_transaction_hash is not None else None
            ),
            approval_spender=(
                PERMIT2_ADDRESS if approval_transaction_hash is not None else None
            ),
            approval_amount=(
                request.from_amount if approval_transaction_hash is not None else None
            ),
            error=None if success else "CDP swap transaction reverted.",
        )

    def list_token_balances(self) -> tuple[tuple[str, Decimal, int], ...]:
        """Return normalized Base balances from the same verified CDP account."""

        async def load_balances() -> tuple[tuple[str, Decimal, int], ...]:
            client = self._wallet.get_client()
            collected: list[tuple[str, Decimal, int]] = []
            page_token = None
            async with client as cdp:
                account = await cdp.evm.get_account(address=self.wallet_address)
                while True:
                    result = await account.list_token_balances(
                        network=CDP_SWAP_NETWORK,
                        page_size=100,
                        page_token=page_token,
                    )
                    for balance in result.balances:
                        decimals = int(balance.amount.decimals)
                        atomic = int(balance.amount.amount)
                        amount = Decimal(atomic) / Decimal(10**decimals)
                        collected.append(
                            (str(balance.token.contract_address).lower(), amount, decimals)
                        )
                    page_token = result.next_page_token
                    if not page_token:
                        break
            return tuple(collected)

        return self._run(load_balances())
