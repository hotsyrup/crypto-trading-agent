import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


INITIAL_ASSET_ALLOWLIST = frozenset({"USDC", "ETH"})


@dataclass(frozen=True)
class LiveTradingConfig:
    enabled: bool
    approved_assets: frozenset[str]
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
        max_position_percent=max_position,
        max_new_strategy_percent=max_new_strategy,
        max_daily_loss_percent=max_daily_loss,
        max_drawdown_percent=max_drawdown,
    )
