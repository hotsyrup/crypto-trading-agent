import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


INITIAL_ASSET_ALLOWLIST = frozenset({"USDC", "ETH"})
BASE_USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
INITIAL_ERC20_CONTRACT_ALLOWLIST = frozenset({BASE_USDC_ADDRESS})
ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]{40}")


@dataclass(frozen=True)
class LiveTradingConfig:
    enabled: bool
    approved_assets: frozenset[str]
    approved_erc20_contracts: frozenset[str]
    max_position_percent: Decimal
    max_new_strategy_percent: Decimal
    max_daily_loss_percent: Decimal
    max_drawdown_percent: Decimal


def _percent(name: str, default: str) -> Decimal:
    value = os.getenv(name, default).strip()
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal percentage.") from error

    if not parsed.is_finite():
        raise ValueError(f"{name} must be a finite decimal percentage.")
    if parsed <= 0 or parsed > 100:
        raise ValueError(f"{name} must be greater than 0 and at most 100.")
    return parsed


def load_live_trading_config() -> LiveTradingConfig:
    enabled_value = os.getenv("LIVE_TRADING_ENABLED", "false").strip().lower()
    if enabled_value not in {"true", "false"}:
        raise ValueError("LIVE_TRADING_ENABLED must be true or false.")

    assets = frozenset(
        item.strip().upper()
        for item in os.getenv("LIVE_APPROVED_ASSETS", "USDC,ETH").split(",")
        if item.strip()
    )
    if not assets:
        raise ValueError("LIVE_APPROVED_ASSETS must not be empty.")
    if not assets.issubset(INITIAL_ASSET_ALLOWLIST):
        raise ValueError(
            "Assets outside the initial USDC/ETH mandate require amendment."
        )

    contracts = frozenset(
        item.strip().lower()
        for item in os.getenv(
            "LIVE_APPROVED_ERC20_CONTRACTS",
            BASE_USDC_ADDRESS,
        ).split(",")
        if item.strip()
    )
    if "USDC" in assets and BASE_USDC_ADDRESS not in contracts:
        raise ValueError(
            "Base USDC must use its exact approved contract address."
        )
    if any(not ADDRESS_PATTERN.fullmatch(item) for item in contracts):
        raise ValueError(
            "LIVE_APPROVED_ERC20_CONTRACTS contains an invalid address."
        )
    if not contracts.issubset(INITIAL_ERC20_CONTRACT_ALLOWLIST):
        raise ValueError(
            "ERC-20 contracts outside the initial Base USDC mandate "
            "require amendment."
        )

    max_position = _percent("LIVE_MAX_POSITION_PERCENT", "20")
    max_new_strategy = _percent("LIVE_MAX_NEW_STRATEGY_PERCENT", "5")
    max_daily_loss = _percent("LIVE_MAX_DAILY_LOSS_PERCENT", "5")
    max_drawdown = _percent("LIVE_MAX_DRAWDOWN_PERCENT", "20")

    if max_new_strategy > max_position:
        raise ValueError(
            "LIVE_MAX_NEW_STRATEGY_PERCENT cannot exceed the position limit."
        )

    return LiveTradingConfig(
        enabled=enabled_value == "true",
        approved_assets=assets,
        approved_erc20_contracts=contracts,
        max_position_percent=max_position,
        max_new_strategy_percent=max_new_strategy,
        max_daily_loss_percent=max_daily_loss,
        max_drawdown_percent=max_drawdown,
    )
