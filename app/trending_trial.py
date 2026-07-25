import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.trending_tokens import (
    TrendingPool,
    fetch_token_price,
    fetch_trending_pools,
    get_non_core_token,
    select_trial_candidates,
)


STATE_PATH = Path("data/trending_trial_state.json")
JOURNAL_PATH = Path("data/trending_trial_journal.jsonl")
STARTING_BALANCE = Decimal("40.00")
POSITION_SIZE = Decimal("4.00")
MAXIMUM_OPEN_POSITIONS = 3
TRIAL_DAYS = 7
STOP_LOSS_PERCENT = Decimal("8")
TAKE_PROFIT_PERCENT = Decimal("15")
TRADING_COST_PERCENT = Decimal("1")


@dataclass
class TrialPosition:
    address: str
    symbol: str
    name: str
    quantity: Decimal
    entry_price: Decimal
    latest_price: Decimal
    opened_at: str


@dataclass
class TrialState:
    started_at: str
    ends_at: str
    cash_usdc: Decimal
    positions: dict[str, TrialPosition]
    realized_pnl: Decimal
    status: str = "RUNNING"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_state(now: datetime | None = None) -> TrialState:
    started = now or _now()
    return TrialState(
        started_at=started.isoformat(),
        ends_at=(started + timedelta(days=TRIAL_DAYS)).isoformat(),
        cash_usdc=STARTING_BALANCE,
        positions={},
        realized_pnl=Decimal("0"),
    )


def load_state() -> TrialState:
    if not STATE_PATH.exists():
        return new_state()
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    positions = {
        address: TrialPosition(
            **{
                **position,
                "quantity": Decimal(position["quantity"]),
                "entry_price": Decimal(position["entry_price"]),
                "latest_price": Decimal(position["latest_price"]),
            }
        )
        for address, position in data["positions"].items()
    }
    return TrialState(
        started_at=data["started_at"],
        ends_at=data["ends_at"],
        cash_usdc=Decimal(data["cash_usdc"]),
        positions=positions,
        realized_pnl=Decimal(data["realized_pnl"]),
        status=data["status"],
    )


def save_state(state: TrialState) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(state)
    data["cash_usdc"] = str(state.cash_usdc)
    data["realized_pnl"] = str(state.realized_pnl)
    for position in data["positions"].values():
        for field in ("quantity", "entry_price", "latest_price"):
            position[field] = str(position[field])
    STATE_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def record_event(event: str, **details: object) -> None:
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": _now().isoformat(),
        "event": event,
        **{key: str(value) for key, value in details.items()},
    }
    with JOURNAL_PATH.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(payload) + "\n")


def total_value(state: TrialState) -> Decimal:
    return state.cash_usdc + sum(
        position.quantity * position.latest_price
        for position in state.positions.values()
    )


def close_position(
    state: TrialState,
    address: str,
    price: Decimal,
    reason: str,
) -> None:
    position = state.positions.pop(address)
    gross_proceeds = position.quantity * price
    proceeds = gross_proceeds * (
        Decimal("1") - TRADING_COST_PERCENT / Decimal("100")
    )
    cost_basis = position.quantity * position.entry_price
    pnl = proceeds - cost_basis
    state.cash_usdc += proceeds
    state.realized_pnl += pnl
    record_event(
        "SELL",
        symbol=position.symbol,
        address=address,
        price=price,
        proceeds=proceeds,
        pnl=pnl,
        reason=reason,
    )


def refresh_and_exit_positions(
    state: TrialState,
    now: datetime,
) -> None:
    trial_finished = now >= datetime.fromisoformat(state.ends_at)
    for address, position in list(state.positions.items()):
        price = fetch_token_price(address)
        if price <= 0:
            record_event("PRICE_UNAVAILABLE", symbol=position.symbol, address=address)
            continue
        position.latest_price = price
        change = (price / position.entry_price - Decimal("1")) * Decimal("100")
        if trial_finished:
            close_position(state, address, price, "Seven-day trial ended.")
        elif change <= -STOP_LOSS_PERCENT:
            close_position(state, address, price, "Stop loss reached.")
        elif change >= TAKE_PROFIT_PERCENT:
            close_position(state, address, price, "Take profit reached.")


def open_candidate(state: TrialState, pool: TrendingPool, now: datetime) -> None:
    token = get_non_core_token(pool)
    if token is None or token.price_usd <= 0:
        return
    address = token.address.lower()
    if address in state.positions or state.cash_usdc < POSITION_SIZE:
        return
    net_investment = POSITION_SIZE * (
        Decimal("1") - TRADING_COST_PERCENT / Decimal("100")
    )
    state.positions[address] = TrialPosition(
        address=address,
        symbol=token.symbol,
        name=token.name,
        quantity=net_investment / token.price_usd,
        entry_price=token.price_usd,
        latest_price=token.price_usd,
        opened_at=now.isoformat(),
    )
    state.cash_usdc -= POSITION_SIZE
    record_event(
        "BUY",
        symbol=token.symbol,
        address=address,
        price=token.price_usd,
        amount=POSITION_SIZE,
        pool=pool.pool_address,
    )


def run_trial_cycle(now: datetime | None = None) -> TrialState:
    current_time = now or _now()
    state = load_state()
    if state.status == "COMPLETE":
        return state

    refresh_and_exit_positions(state, current_time)
    if current_time >= datetime.fromisoformat(state.ends_at):
        if not state.positions:
            state.status = "COMPLETE"
            record_event("TRIAL_COMPLETE", final_value=total_value(state))
    else:
        candidates = select_trial_candidates(fetch_trending_pools())
        for pool in candidates:
            if len(state.positions) >= MAXIMUM_OPEN_POSITIONS:
                break
            open_candidate(state, pool, current_time)

    save_state(state)
    record_event(
        "CYCLE_COMPLETE",
        status=state.status,
        cash=state.cash_usdc,
        open_positions=len(state.positions),
        total_value=total_value(state),
    )
    return state


def print_report(state: TrialState) -> None:
    print("Seven-Day Base Trending Token Paper Trial")
    print(f"Status: {state.status}")
    print(f"Started: {state.started_at}")
    print(f"Ends: {state.ends_at}")
    print(f"Simulated cash: ${state.cash_usdc:,.2f}")
    print(f"Open positions: {len(state.positions)}")
    for position in state.positions.values():
        value = position.quantity * position.latest_price
        print(f"- {position.symbol}: ${value:,.2f} at ${position.latest_price}")
    value = total_value(state)
    print(f"Total simulated value: ${value:,.2f}")
    print(f"Simulated profit/loss: ${value - STARTING_BALANCE:,.2f}")
    print("Paper trading only. No wallet transaction was submitted.")


if __name__ == "__main__":
    print_report(run_trial_cycle())
