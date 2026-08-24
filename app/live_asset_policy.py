from __future__ import annotations

from dataclasses import dataclass

from app.base_asset_universe import GovernedAssetUniverse
from app.live_trading_config import BASE_USDC_ADDRESS, LiveTradingConfig


@dataclass(frozen=True)
class AssetPolicyDecision:
    allowed: bool
    reason: str


def evaluate_asset_identity(
    *,
    symbol: str,
    token_address: str | None,
    unsolicited: bool,
    config: LiveTradingConfig,
    universe: GovernedAssetUniverse | None = None,
) -> AssetPolicyDecision:
    """Fail closed unless an asset matches the mandate's exact identity.

    Symbols are display metadata and can be copied by hostile contracts. ERC-20
    assets therefore require an exact allowlisted contract address. Native ETH
    is represented by a missing token address.
    """

    normalized_symbol = symbol.strip().upper()
    normalized_address = token_address.strip().lower() if token_address else None

    if unsolicited:
        return AssetPolicyDecision(
            allowed=False,
            reason="Unsolicited assets are never eligible for trading.",
        )

    if universe is not None:
        if normalized_symbol == "USDC":
            if normalized_address != BASE_USDC_ADDRESS:
                return AssetPolicyDecision(
                    allowed=False,
                    reason="USDC contract does not match the official Base contract.",
                )
            return AssetPolicyDecision(
                allowed=True,
                reason="USDC matches the official Base settlement contract.",
            )
        if universe.contains(normalized_symbol, normalized_address):
            return AssetPolicyDecision(
                allowed=True,
                reason="Asset matches the governed top-25 Base universe.",
            )
        return AssetPolicyDecision(
            allowed=False,
            reason=(
                "Asset symbol and exact contract are outside the governed "
                "top-25 Base universe."
            ),
        )

    if normalized_symbol not in config.approved_assets:
        return AssetPolicyDecision(
            allowed=False,
            reason="Asset symbol is outside the adopted mandate.",
        )

    if normalized_symbol == "ETH":
        if normalized_address is not None:
            return AssetPolicyDecision(
                allowed=False,
                reason="Native ETH must not be represented by a token contract.",
            )
        return AssetPolicyDecision(
            allowed=True,
            reason="Native Base ETH matches the adopted mandate.",
        )

    if normalized_symbol == "USDC":
        if normalized_address != BASE_USDC_ADDRESS:
            return AssetPolicyDecision(
                allowed=False,
                reason="USDC contract does not match the approved Base contract.",
            )
        if normalized_address not in config.approved_erc20_contracts:
            return AssetPolicyDecision(
                allowed=False,
                reason="USDC contract is not enabled in runtime configuration.",
            )
        return AssetPolicyDecision(
            allowed=True,
            reason="USDC matches the approved Base contract.",
        )

    return AssetPolicyDecision(
        allowed=False,
        reason="Asset identity is not implemented by the live policy.",
    )
