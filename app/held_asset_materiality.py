"""Conservative, read-only exact-contract evidence for dust classification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.live_trading_config import BASE_USDC_ADDRESS
from app.research_agent import fetch_pairs


DUST_THRESHOLD_USDC = Decimal("5")
DUST_PRICE_SAFETY_MULTIPLIER = Decimal("1.25")
DUST_PRICE_CACHE_TTL = timedelta(minutes=10)
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
USDBC_ADDRESS = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"
APPROVED_QUOTE_ADDRESSES = {BASE_USDC_ADDRESS, USDBC_ADDRESS, WETH_ADDRESS}
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-f]{40}$")


@dataclass(frozen=True)
class ExactContractPriceEvidence:
    contract_address: str
    observed_at: datetime
    price_usd: Decimal
    conservative_price_usd: Decimal


class ExactContractPriceReader(Protocol):
    def read_conservative_prices(
        self,
        contract_addresses: tuple[str, ...],
        *,
        now: datetime,
    ) -> dict[str, ExactContractPriceEvidence]:
        ...


@dataclass
class DexScreenerDustPriceReader:
    """Provide conservative prices only to prove a holding is immaterial."""

    _cache: dict[str, ExactContractPriceEvidence] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _miss_cache: dict[str, datetime] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def read_conservative_prices(
        self,
        contract_addresses: tuple[str, ...],
        *,
        now: datetime,
    ) -> dict[str, ExactContractPriceEvidence]:
        addresses = tuple(
            dict.fromkeys(item.strip().lower() for item in contract_addresses)
        )
        if any(not ADDRESS_PATTERN.fullmatch(address) for address in addresses):
            raise ValueError("Dust valuation requires exact Base token contracts.")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Dust valuation time must include a timezone.")
        current_time = now.astimezone(timezone.utc)
        accepted: dict[str, ExactContractPriceEvidence] = {}
        pending: list[str] = []
        for address in addresses:
            cached = self._cache.get(address)
            if (
                cached is not None
                and current_time - cached.observed_at <= DUST_PRICE_CACHE_TTL
            ):
                accepted[address] = cached
                continue
            missed_at = self._miss_cache.get(address)
            if (
                missed_at is not None
                and current_time - missed_at <= DUST_PRICE_CACHE_TTL
            ):
                continue
            pending.append(address)

        for start in range(0, len(pending), 30):
            chunk = pending[start : start + 30]
            pairs = fetch_pairs(chunk)
            prices: dict[str, list[Decimal]] = {address: [] for address in chunk}
            for pair in pairs:
                if pair.get("chainId") != "base":
                    continue
                base = pair.get("baseToken")
                quote = pair.get("quoteToken")
                if not isinstance(base, dict) or not isinstance(quote, dict):
                    continue
                address = str(base.get("address", "")).lower()
                if (
                    address not in prices
                    or str(quote.get("address", "")).lower()
                    not in APPROVED_QUOTE_ADDRESSES
                ):
                    continue
                try:
                    price = Decimal(str(pair.get("priceUsd")))
                except (InvalidOperation, ValueError):
                    continue
                if price.is_finite() and price > 0:
                    prices[address].append(price)
            for address in chunk:
                if not prices[address]:
                    self._miss_cache[address] = current_time
                    continue
                price = max(prices[address])
                evidence = ExactContractPriceEvidence(
                    contract_address=address,
                    observed_at=current_time,
                    price_usd=price,
                    conservative_price_usd=price * DUST_PRICE_SAFETY_MULTIPLIER,
                )
                self._cache[address] = evidence
                self._miss_cache.pop(address, None)
                accepted[address] = evidence
        return accepted
