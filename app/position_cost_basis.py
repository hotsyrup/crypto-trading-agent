from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.live_execution_journal import read_live_execution_events
from app.live_trading_config import BASE_USDC_ADDRESS


@dataclass(frozen=True)
class PositionCostBasis:
    token_address: str
    confirmed_quantity: Decimal
    remaining_cost_usdc: Decimal
    average_entry_price_usdc: Decimal
    last_entry_at: datetime | None
    last_exit_at: datetime | None
    confirmed_buy_count: int
    verified: bool


class PositionCostBasisError(RuntimeError):
    pass


def _decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PositionCostBasisError(f"{label} is invalid.") from error
    if not parsed.is_finite() or parsed <= 0:
        raise PositionCostBasisError(f"{label} must be finite and positive.")
    return parsed


def _timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise PositionCostBasisError("Execution timestamp is invalid.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PositionCostBasisError("Execution timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def _contract(value: object) -> str:
    address = str(value).strip().lower()
    if len(address) != 42 or not address.startswith("0x"):
        raise PositionCostBasisError("Execution token address is invalid.")
    return address


def reconstruct_position_cost_basis(
    *, path: Path
) -> dict[str, PositionCostBasis]:
    """Rebuild weighted-average inventory only from confirmed swap receipts."""

    events = read_live_execution_events(path=path)
    reservations: dict[str, dict[str, object]] = {}
    mutable: dict[str, dict[str, object]] = {}

    def state(address: str) -> dict[str, object]:
        return mutable.setdefault(
            address,
            {
                "quantity": Decimal("0"),
                "cost": Decimal("0"),
                "last_entry": None,
                "last_exit": None,
                "buy_count": 0,
                "verified": True,
            },
        )

    for event in events:
        intent_id = str(event.get("intent_id", ""))
        event_name = event.get("event")
        if event_name == "RESERVED":
            if intent_id in reservations:
                raise PositionCostBasisError(
                    "Execution audit contains duplicate reservations."
                )
            reservations[intent_id] = event
            continue
        reservation = reservations.get(intent_id)
        if reservation is None:
            raise PositionCostBasisError("Execution outcome has no reservation.")
        if event_name in {"BACKEND_FAILED", "RECEIPT_REJECTED"}:
            continue
        if event_name != "CONFIRMED":
            raise PositionCostBasisError("Execution audit contains an unsupported event.")
        details = event.get("details")
        if not isinstance(details, dict):
            raise PositionCostBasisError("Confirmed receipt details are unavailable.")
        from_token = _contract(reservation.get("from_token"))
        to_token = _contract(reservation.get("to_token"))
        recorded_at = _timestamp(event.get("recorded_at"))
        from_amount = _decimal(details.get("from_amount"), "Confirmed input")
        to_amount = _decimal(details.get("to_amount"), "Confirmed output")
        if from_token == BASE_USDC_ADDRESS and to_token != BASE_USDC_ADDRESS:
            item = state(to_token)
            item["quantity"] = Decimal(item["quantity"]) + to_amount
            item["cost"] = Decimal(item["cost"]) + from_amount
            item["last_entry"] = recorded_at
            item["buy_count"] = int(item["buy_count"]) + 1
        elif to_token == BASE_USDC_ADDRESS and from_token != BASE_USDC_ADDRESS:
            item = state(from_token)
            quantity = Decimal(item["quantity"])
            cost = Decimal(item["cost"])
            tolerance = max(Decimal("0.000000000001"), quantity * Decimal("0.001"))
            if quantity <= 0 or from_amount > quantity + tolerance:
                item["quantity"] = Decimal("0")
                item["cost"] = Decimal("0")
                item["verified"] = False
            else:
                sold_quantity = min(from_amount, quantity)
                sold_cost = cost * sold_quantity / quantity
                item["quantity"] = quantity - sold_quantity
                item["cost"] = cost - sold_cost
            item["last_exit"] = recorded_at
        else:
            raise PositionCostBasisError(
                "Confirmed route is not an asset-USDC spot trade."
            )

    result: dict[str, PositionCostBasis] = {}
    for address, item in mutable.items():
        quantity = Decimal(item["quantity"])
        cost = Decimal(item["cost"])
        result[address] = PositionCostBasis(
            token_address=address,
            confirmed_quantity=quantity,
            remaining_cost_usdc=cost,
            average_entry_price_usdc=(
                cost / quantity if quantity > 0 else Decimal("0")
            ),
            last_entry_at=item["last_entry"],
            last_exit_at=item["last_exit"],
            confirmed_buy_count=int(item["buy_count"]),
            verified=bool(item["verified"]),
        )
    return result
