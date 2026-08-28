from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.base_asset_universe import GovernedAsset, GovernedAssetUniverse
from app.controlled_live_execution import NATIVE_ETH_ADDRESS
from app.live_trading_config import BASE_USDC_ADDRESS


SCHEMA_VERSION = 1
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"


class AssetLifecycleError(RuntimeError):
    pass


class AssetLifecycleState(StrEnum):
    CANDIDATE = "candidate"
    RETAINED = "retained"


class TokenBalance(Protocol):
    token_address: str
    amount: Decimal
    decimals: int


@dataclass(frozen=True)
class LifecycleAsset:
    state: AssetLifecycleState
    symbol: str
    token_address: str | None
    decimals: int
    first_governed_at: datetime
    last_candidate_at: datetime | None
    asset: GovernedAsset | None


@dataclass(frozen=True)
class HistoricalGovernedContract:
    token_address: str
    decimals: int
    governed_at: datetime


@dataclass(frozen=True)
class _RegistryRecord:
    symbol: str | None
    token_address: str | None
    decimals: int
    first_governed_at: datetime
    last_candidate_at: datetime | None
    asset: GovernedAsset | None


@dataclass(frozen=True)
class QuarantinedHolding:
    token_address: str
    amount: Decimal
    decimals: int
    reason_code: str


@dataclass(frozen=True)
class AssetLifecycleAssessment:
    held_governed: tuple[LifecycleAsset, ...]
    quarantined: tuple[QuarantinedHolding, ...]
    candidate_contracts: tuple[str, ...]
    required_research_contracts: tuple[str, ...]


def _asset_key(token_address: str | None) -> str:
    return NATIVE_ETH_ADDRESS if token_address is None else token_address.lower()


def _research_contract(token_address: str | None) -> str:
    if token_address is None or token_address.lower() == NATIVE_ETH_ADDRESS:
        return WETH_ADDRESS
    return token_address.lower()


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def _timestamp(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise AssetLifecycleError(f"{label} is invalid.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AssetLifecycleError(f"{label} must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as error:
        raise AssetLifecycleError(f"{label} is invalid.") from error
    if not parsed.is_finite() or parsed < 0:
        raise AssetLifecycleError(f"{label} must be finite and non-negative.")
    return parsed


def _serialize_asset(asset: GovernedAsset) -> dict[str, object]:
    payload = asdict(asset)
    for field in ("market_cap_usd", "liquidity_usd", "daily_volume_usd"):
        payload[field] = str(payload[field])
    payload["oldest_pool_created_at"] = asset.oldest_pool_created_at.isoformat()
    return payload


def _deserialize_asset(payload: object) -> GovernedAsset:
    if not isinstance(payload, dict):
        raise AssetLifecycleError("Lifecycle asset is invalid.")
    expected = {
        "rank",
        "symbol",
        "name",
        "token_address",
        "decimals",
        "market_cap_usd",
        "liquidity_usd",
        "daily_volume_usd",
        "oldest_pool_created_at",
    }
    if set(payload) != expected:
        raise AssetLifecycleError("Lifecycle asset fields are invalid.")
    address = payload["token_address"]
    if address is not None:
        address = str(address).lower()
        if len(address) != 42 or not address.startswith("0x"):
            raise AssetLifecycleError("Lifecycle contract address is invalid.")
    rank = payload["rank"]
    decimals = payload["decimals"]
    if type(rank) is not int or rank < 1:
        raise AssetLifecycleError("Lifecycle rank is invalid.")
    if type(decimals) is not int or not 0 <= decimals <= 36:
        raise AssetLifecycleError("Lifecycle decimals are invalid.")
    return GovernedAsset(
        rank=rank,
        symbol=str(payload["symbol"]).upper(),
        name=str(payload["name"]),
        token_address=address,
        decimals=decimals,
        market_cap_usd=_decimal(payload["market_cap_usd"], "Lifecycle market cap"),
        liquidity_usd=_decimal(payload["liquidity_usd"], "Lifecycle liquidity"),
        daily_volume_usd=_decimal(payload["daily_volume_usd"], "Lifecycle volume"),
        oldest_pool_created_at=_timestamp(
            payload["oldest_pool_created_at"], "Lifecycle pool timestamp"
        ),
    )


class AssetLifecycle:
    """Persist exact-contract governance separately from candidate discovery."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, _RegistryRecord]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AssetLifecycleError("Asset lifecycle registry is unreadable.") from error
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "records",
            "registry_sha256",
        }:
            raise AssetLifecycleError("Asset lifecycle registry fields are invalid.")
        unsigned = dict(payload)
        stored_digest = unsigned.pop("registry_sha256")
        if stored_digest != _digest(unsigned):
            raise AssetLifecycleError("Asset lifecycle registry digest is invalid.")
        if payload["schema_version"] != SCHEMA_VERSION or not isinstance(
            payload["records"], list
        ):
            raise AssetLifecycleError("Asset lifecycle registry schema is invalid.")
        records: dict[str, _RegistryRecord] = {}
        for item in payload["records"]:
            if not isinstance(item, dict) or set(item) != {
                "asset",
                "symbol",
                "token_address",
                "decimals",
                "first_governed_at",
                "last_candidate_at",
            }:
                raise AssetLifecycleError("Asset lifecycle record is invalid.")
            asset = (
                _deserialize_asset(item["asset"])
                if item["asset"] is not None
                else None
            )
            address = item["token_address"]
            if address is not None:
                address = str(address).lower()
                if len(address) != 42 or not address.startswith("0x"):
                    raise AssetLifecycleError("Lifecycle contract address is invalid.")
            decimals = item["decimals"]
            if type(decimals) is not int or not 0 <= decimals <= 36:
                raise AssetLifecycleError("Lifecycle decimals are invalid.")
            symbol = item["symbol"]
            if symbol is not None:
                symbol = str(symbol).upper()
            if asset is not None and (
                asset.token_address != address
                or asset.decimals != decimals
                or asset.symbol != symbol
            ):
                raise AssetLifecycleError("Lifecycle candidate metadata is contradictory.")
            key = _asset_key(address)
            if key in records:
                raise AssetLifecycleError("Asset lifecycle registry has duplicate contracts.")
            records[key] = _RegistryRecord(
                symbol=symbol,
                token_address=address,
                decimals=decimals,
                first_governed_at=_timestamp(
                    item["first_governed_at"], "First governed timestamp"
                ),
                last_candidate_at=(
                    _timestamp(item["last_candidate_at"], "Last candidate timestamp")
                    if item["last_candidate_at"] is not None
                    else None
                ),
                asset=asset,
            )
        return records

    def _store(
        self,
        records: dict[str, _RegistryRecord],
    ) -> None:
        unsigned: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "records": [
                {
                    "asset": _serialize_asset(record.asset) if record.asset else None,
                    "symbol": record.symbol,
                    "token_address": record.token_address,
                    "decimals": record.decimals,
                    "first_governed_at": record.first_governed_at.isoformat(),
                    "last_candidate_at": (
                        record.last_candidate_at.isoformat()
                        if record.last_candidate_at is not None
                        else None
                    ),
                }
                for _, record in sorted(records.items())
            ],
        }
        payload = {**unsigned, "registry_sha256": _digest(unsigned)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            temporary_path.replace(self.path)
        except OSError as error:
            raise AssetLifecycleError("Asset lifecycle registry update failed.") from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    def evaluate(
        self,
        candidates: GovernedAssetUniverse,
        balances: tuple[TokenBalance, ...],
        *,
        now: datetime,
        historical_governance: tuple[HistoricalGovernedContract, ...] = (),
    ) -> AssetLifecycleAssessment:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Asset lifecycle time must include a timezone.")
        current_time = now.astimezone(timezone.utc)
        records = self._load()
        current: dict[str, GovernedAsset] = {}
        for asset in candidates.assets:
            key = _asset_key(asset.token_address)
            if key in current:
                raise AssetLifecycleError("Candidate universe repeats an exact contract.")
            current[key] = asset
            previous = records.get(key)
            first = previous.first_governed_at if previous is not None else current_time
            records[key] = _RegistryRecord(
                symbol=asset.symbol,
                token_address=asset.token_address,
                decimals=asset.decimals,
                first_governed_at=first,
                last_candidate_at=current_time,
                asset=asset,
            )
        for historical in historical_governance:
            address = historical.token_address.strip().lower()
            if len(address) != 42 or not address.startswith("0x"):
                raise AssetLifecycleError("Historical governed contract is invalid.")
            if type(historical.decimals) is not int or not 0 <= historical.decimals <= 36:
                raise AssetLifecycleError("Historical governed decimals are invalid.")
            if historical.governed_at.tzinfo is None or historical.governed_at.utcoffset() is None:
                raise AssetLifecycleError("Historical governance time must include a timezone.")
            previous = records.get(address)
            if previous is None:
                records[address] = _RegistryRecord(
                    symbol=None,
                    token_address=address,
                    decimals=historical.decimals,
                    first_governed_at=historical.governed_at.astimezone(timezone.utc),
                    last_candidate_at=None,
                    asset=None,
                )
            elif previous.decimals != historical.decimals:
                raise AssetLifecycleError("Historical governance decimals conflict.")
        self._store(records)

        held: list[LifecycleAsset] = []
        quarantined: list[QuarantinedHolding] = []
        seen: set[str] = set()
        for balance in balances:
            key = balance.token_address.strip().lower()
            if key in seen:
                raise AssetLifecycleError("Wallet inventory repeats an exact contract.")
            seen.add(key)
            if not balance.amount.is_finite() or balance.amount < 0:
                raise AssetLifecycleError("Wallet inventory amount is invalid.")
            if type(balance.decimals) is not int or not 0 <= balance.decimals <= 36:
                raise AssetLifecycleError("Wallet inventory decimals are invalid.")
            if balance.amount == 0 or key == BASE_USDC_ADDRESS:
                continue
            record = records.get(key)
            if record is None:
                quarantined.append(
                    QuarantinedHolding(
                        token_address=key,
                        amount=balance.amount,
                        decimals=balance.decimals,
                        reason_code="UNKNOWN_EXACT_CONTRACT",
                    )
                )
                continue
            if balance.decimals != record.decimals:
                quarantined.append(
                    QuarantinedHolding(
                        token_address=key,
                        amount=balance.amount,
                        decimals=balance.decimals,
                        reason_code="GOVERNED_DECIMALS_CONFLICT",
                    )
                )
                continue
            held.append(
                LifecycleAsset(
                    state=(
                        AssetLifecycleState.CANDIDATE
                        if key in current
                        else AssetLifecycleState.RETAINED
                    ),
                    symbol=record.symbol or "",
                    token_address=record.token_address,
                    decimals=record.decimals,
                    first_governed_at=record.first_governed_at,
                    last_candidate_at=record.last_candidate_at,
                    asset=record.asset,
                )
            )

        candidate_contracts = tuple(
            sorted(_research_contract(asset.token_address) for asset in candidates.assets)
        )
        held_contracts = tuple(
            _research_contract(item.token_address) for item in held
        )
        required = tuple(
            dict.fromkeys((*candidate_contracts, *held_contracts, BASE_USDC_ADDRESS))
        )
        return AssetLifecycleAssessment(
            held_governed=tuple(held),
            quarantined=tuple(quarantined),
            candidate_contracts=candidate_contracts,
            required_research_contracts=required,
        )
