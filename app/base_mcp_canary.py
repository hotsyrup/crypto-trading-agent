from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.live_trading_config import BASE_USDC_ADDRESS, LiveTradingConfig
from app.trading_executor import (
    AUTHORIZED_TREASURY_ADDRESS,
    BASE_MAINNET_CHAIN_ID,
    STATUS_SHADOW_APPROVED,
    ExecutionDecision,
    TradeIntent,
    intent_fingerprint,
)


CANARY_MODE_PREPARE_ONLY = "prepare_only"
CANARY_KILL_SWITCH_ARMED = "armed"
CANARY_KILL_SWITCH_HALTED = "halted"
STATUS_BLOCKED = "BLOCKED"
STATUS_READY = "READY_FOR_HUMAN_APPROVAL"
CANARY_NOTIONAL_USDC = Decimal("1.00")
DEFAULT_APPROVAL_TTL_SECONDS = 300
DEFAULT_MAX_INTENT_AGE_SECONDS = 120
HEX_64_PATTERN = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class BaseMcpCanaryConfig:
    mode: str
    kill_switch_state: str
    maximum_notional_usdc: Decimal
    approval_ttl_seconds: int
    maximum_intent_age_seconds: int


@dataclass(frozen=True)
class BaseMcpSwapRequest:
    amount: str
    chain: str
    from_asset: str
    from_decimals: int
    to_asset: str

    def tool_arguments(self) -> dict[str, object]:
        return {
            "amount": self.amount,
            "chain": self.chain,
            "fromAsset": self.from_asset,
            "fromDecimals": self.from_decimals,
            "toAsset": self.to_asset,
        }


@dataclass(frozen=True)
class BaseMcpCanaryPreparation:
    status: str
    reasons: tuple[str, ...]
    canary_id: str
    intent_id: str
    intent_fingerprint: str
    request_digest: str
    treasury_address: str
    journal_sequence: int
    journal_entry_hash: str
    prepared_at: datetime
    expires_at: datetime
    request: BaseMcpSwapRequest | None
    ready_to_request_human_approval: bool = False
    approval_requested: bool = False
    executable: bool = False
    signing_authority: str = "base_account_human_only"


def _decimal_setting(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default).strip())
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal amount.") from error
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero.")
    return value


def _bounded_seconds(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def load_base_mcp_canary_config() -> BaseMcpCanaryConfig:
    mode = os.getenv("BASE_MCP_CANARY_MODE", CANARY_MODE_PREPARE_ONLY)
    mode = mode.strip().lower()
    if mode != CANARY_MODE_PREPARE_ONLY:
        raise ValueError("BASE_MCP_CANARY_MODE supports prepare_only only.")

    kill_switch = os.getenv(
        "BASE_MCP_CANARY_KILL_SWITCH",
        CANARY_KILL_SWITCH_HALTED,
    ).strip().lower()
    if kill_switch not in {
        CANARY_KILL_SWITCH_ARMED,
        CANARY_KILL_SWITCH_HALTED,
    }:
        raise ValueError(
            "BASE_MCP_CANARY_KILL_SWITCH must be armed or halted."
        )

    maximum_notional = _decimal_setting(
        "BASE_MCP_CANARY_MAX_NOTIONAL_USDC",
        "1.00",
    )
    if maximum_notional > CANARY_NOTIONAL_USDC:
        raise ValueError("The first Base canary cannot exceed 1.00 USDC.")

    return BaseMcpCanaryConfig(
        mode=mode,
        kill_switch_state=kill_switch,
        maximum_notional_usdc=maximum_notional,
        approval_ttl_seconds=_bounded_seconds(
            "BASE_MCP_CANARY_APPROVAL_TTL_SECONDS",
            DEFAULT_APPROVAL_TTL_SECONDS,
            60,
            900,
        ),
        maximum_intent_age_seconds=_bounded_seconds(
            "BASE_MCP_CANARY_MAX_INTENT_AGE_SECONDS",
            DEFAULT_MAX_INTENT_AGE_SECONDS,
            30,
            300,
        ),
    )


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _blocked(
    *,
    reasons: list[str],
    intent: TradeIntent,
    journal_sequence: int,
    journal_entry_hash: str,
    prepared_at: datetime,
    expires_at: datetime,
) -> BaseMcpCanaryPreparation:
    return BaseMcpCanaryPreparation(
        status=STATUS_BLOCKED,
        reasons=tuple(reasons),
        canary_id="",
        intent_id=intent.intent_id,
        intent_fingerprint=intent_fingerprint(intent),
        request_digest="",
        treasury_address=intent.treasury_address.lower(),
        journal_sequence=journal_sequence,
        journal_entry_hash=journal_entry_hash,
        prepared_at=prepared_at,
        expires_at=expires_at,
        request=None,
    )


def prepare_base_mcp_canary(
    intent: TradeIntent,
    decision: ExecutionDecision,
    *,
    journal_sequence: int,
    journal_entry_hash: str,
    live_config: LiveTradingConfig,
    canary_config: BaseMcpCanaryConfig | None = None,
    now: datetime | None = None,
) -> BaseMcpCanaryPreparation:
    """Prepare an exact Base MCP request without calling Base MCP.

    The returned object is a human-review package. It contains no approval
    URL, request ID, signature, transaction, calldata, or submission method.
    """

    prepared_at = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    try:
        config = canary_config or load_base_mcp_canary_config()
    except ValueError as error:
        config = BaseMcpCanaryConfig(
            mode=CANARY_MODE_PREPARE_ONLY,
            kill_switch_state=CANARY_KILL_SWITCH_HALTED,
            maximum_notional_usdc=CANARY_NOTIONAL_USDC,
            approval_ttl_seconds=DEFAULT_APPROVAL_TTL_SECONDS,
            maximum_intent_age_seconds=DEFAULT_MAX_INTENT_AGE_SECONDS,
        )
        reasons.append(f"Canary configuration invalid: {error}")

    if not _is_aware(prepared_at):
        reasons.append("Canary preparation time must include a timezone.")
        prepared_at = prepared_at.replace(tzinfo=timezone.utc)
    prepared_at = prepared_at.astimezone(timezone.utc)
    expires_at = prepared_at + timedelta(seconds=config.approval_ttl_seconds)

    fingerprint = intent_fingerprint(intent)
    if live_config.enabled:
        reasons.append("LIVE_TRADING_ENABLED must remain false during preparation.")
    if config.mode != CANARY_MODE_PREPARE_ONLY:
        reasons.append("The Base MCP canary must remain prepare_only.")
    if config.kill_switch_state != CANARY_KILL_SWITCH_ARMED:
        reasons.append("The independent Base MCP canary kill switch is halted.")
    if decision.status != STATUS_SHADOW_APPROVED:
        reasons.append("The deterministic executor did not shadow-approve the intent.")
    if decision.intent_id != intent.intent_id:
        reasons.append("The decision intent ID does not match the canary intent.")
    if decision.intent_fingerprint != fingerprint:
        reasons.append("The decision fingerprint does not match the canary intent.")
    if decision.executable or decision.signing_authority != "none":
        reasons.append("The source decision must not contain execution authority.")

    if intent.side.strip().upper() != "BUY":
        reasons.append("The first Base canary must buy ETH with USDC.")
    if intent.notional_usdc != CANARY_NOTIONAL_USDC:
        reasons.append("The first Base canary must be exactly 1.00 USDC.")
    if intent.notional_usdc > config.maximum_notional_usdc:
        reasons.append("The canary exceeds its configured notional ceiling.")
    if intent.chain_id != BASE_MAINNET_CHAIN_ID:
        reasons.append("The first Base canary must use Base mainnet chain ID 8453.")
    treasury = intent.treasury_address.strip().lower()
    if treasury != AUTHORIZED_TREASURY_ADDRESS:
        reasons.append("The canary treasury is outside the adopted mandate.")
    if intent.recipient_address.strip().lower() != treasury:
        reasons.append("Canary proceeds must return to the same treasury.")
    if intent.asset_symbol.strip().upper() != "ETH" or intent.asset_token_address:
        reasons.append("The canary output must be native ETH.")
    if intent.settlement_symbol.strip().upper() != "USDC":
        reasons.append("The canary input must be USDC.")
    if (
        intent.settlement_token_address is None
        or intent.settlement_token_address.strip().lower() != BASE_USDC_ADDRESS
    ):
        reasons.append("The canary input must use official Base USDC.")

    if not _is_aware(intent.created_at):
        reasons.append("The canary intent timestamp must include a timezone.")
    else:
        age = (prepared_at - intent.created_at.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > config.maximum_intent_age_seconds:
            reasons.append("The canary intent is stale or future-dated.")
    if journal_sequence < 1:
        reasons.append("A recorded execution-journal sequence is required.")
    if not HEX_64_PATTERN.fullmatch(journal_entry_hash):
        reasons.append("A valid execution-journal entry hash is required.")

    if reasons:
        return _blocked(
            reasons=reasons,
            intent=intent,
            journal_sequence=journal_sequence,
            journal_entry_hash=journal_entry_hash,
            prepared_at=prepared_at,
            expires_at=expires_at,
        )

    request = BaseMcpSwapRequest(
        amount="1.00",
        chain="base",
        from_asset=BASE_USDC_ADDRESS,
        from_decimals=6,
        to_asset="ETH",
    )
    bound_payload: dict[str, object] = {
        "schema_version": 1,
        "intent_id": intent.intent_id,
        "intent_fingerprint": fingerprint,
        "treasury_address": treasury,
        "journal_sequence": journal_sequence,
        "journal_entry_hash": journal_entry_hash,
        "prepared_at": prepared_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "base_mcp_tool": "swap",
        "base_mcp_arguments": request.tool_arguments(),
    }
    request_digest = hashlib.sha256(
        _canonical(bound_payload).encode("utf-8")
    ).hexdigest()
    canary_id = f"base-canary-{request_digest[:24]}"
    return BaseMcpCanaryPreparation(
        status=STATUS_READY,
        reasons=(
            "Exact request is ready to be shown to Ben before Base MCP is called.",
        ),
        canary_id=canary_id,
        intent_id=intent.intent_id,
        intent_fingerprint=fingerprint,
        request_digest=request_digest,
        treasury_address=treasury,
        journal_sequence=journal_sequence,
        journal_entry_hash=journal_entry_hash,
        prepared_at=prepared_at,
        expires_at=expires_at,
        request=request,
        ready_to_request_human_approval=True,
    )
