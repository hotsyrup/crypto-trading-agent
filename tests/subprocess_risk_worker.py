from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.paper_cycle_ledger as ledger
import app.risk_accounting as risk
from app.paper_portfolio import PaperPortfolio


def configure(directory: Path) -> None:
    ledger.LEDGER_PATH = directory / "ledger.jsonl"
    ledger.LOCK_PATH = directory / "ledger.lock"
    risk.RISK_STATE_PATH = directory / "risk.json"
    risk.RISK_LOCK_PATH = directory / "risk.lock"


def portfolio_and_price(target_value: Decimal) -> tuple[PaperPortfolio, Decimal]:
    usdc, eth = ledger.current_portfolio()
    portfolio = PaperPortfolio(usdc, eth)
    if eth == 0:
        if target_value != usdc:
            raise ValueError("Initial worker cycle must use the starting portfolio value.")
        return portfolio, Decimal("2000")
    return portfolio, (target_value - usdc) / eth


def payload(
    cycle_name: str,
    portfolio: PaperPortfolio,
    reference_price: Decimal,
    recorded_at: datetime,
    decision: risk.RiskAccountingDecision,
) -> dict[str, object]:
    cycle_id = hashlib.sha256(cycle_name.encode()).hexdigest()
    after = (
        {"usdc_balance": "0", "eth_balance": "5"}
        if portfolio.eth_balance == 0
        else {
            "usdc_balance": str(portfolio.usdc_balance),
            "eth_balance": str(portfolio.eth_balance),
        }
    )
    return {
        "recorded_at": recorded_at.isoformat(),
        "cycle_id": cycle_id,
        "signal_id": cycle_id,
        "reference_price": str(reference_price),
        "accounting_date": decision.daily_date.isoformat(),
        "portfolio_value": str(decision.current_value),
        "high_water_mark": str(decision.high_water_mark),
        "daily_start_value": str(decision.daily_start_value),
        "system_healthy": True,
        "paper_eligible": False,
        "simulated": False,
        "blocked_reason": "subprocess test",
        "order": {"status": "BLOCKED"},
        "portfolio_before": {
            "usdc_balance": str(portfolio.usdc_balance),
            "eth_balance": str(portfolio.eth_balance),
        },
        "portfolio_after": after,
    }


def commit(directory: Path, cycle_name: str, target_value: Decimal, recorded_at: datetime) -> None:
    portfolio, reference_price = portfolio_and_price(target_value)
    with risk.portfolio_risk_transaction(
        portfolio,
        reference_price,
        now=recorded_at,
    ) as transaction:
        if not transaction.decision.ready:
            raise RuntimeError(transaction.decision.reason)
        transaction.commit_cycle(
            payload(
                cycle_name,
                portfolio,
                reference_price,
                recorded_at,
                transaction.decision,
            )
        )


def main() -> None:
    mode = sys.argv[1]
    directory = Path(sys.argv[2])
    configure(directory)
    if mode in {"commit", "hold", "crash_after_append"}:
        cycle_name = sys.argv[3]
        target_value = Decimal(sys.argv[4])
        recorded_at = datetime.fromisoformat(sys.argv[5])
        if mode == "hold":
            portfolio, reference_price = portfolio_and_price(target_value)
            with risk.portfolio_risk_transaction(
                portfolio, reference_price, now=recorded_at
            ) as transaction:
                if not transaction.decision.ready:
                    raise RuntimeError(transaction.decision.reason)
                (directory / "holder-ready").touch()
                release = directory / "holder-release"
                deadline = time.monotonic() + 10
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Timed out waiting to release held risk locks.")
                    time.sleep(0.01)
                transaction.commit_cycle(
                    payload(
                        cycle_name,
                        portfolio,
                        reference_price,
                        recorded_at,
                        transaction.decision,
                    )
                )
            return
        if mode == "crash_after_append":
            risk._save_state = lambda _state: os._exit(73)
        if cycle_name == "contender":
            (directory / "contender-started").touch()
        commit(directory, cycle_name, target_value, recorded_at)
        return
    if mode == "evaluate":
        target_value = Decimal(sys.argv[3])
        recorded_at = datetime.fromisoformat(sys.argv[4])
        decision = risk.evaluate_portfolio_risk(
            PaperPortfolio(Decimal("0"), Decimal("5")),
            target_value / Decimal("5"),
            now=recorded_at,
        )
        print(
            json.dumps(
                {
                    "ready": decision.ready,
                    "high_water_mark": str(decision.high_water_mark),
                    "drawdown_halt": decision.drawdown_halt,
                    "daily_loss_halt": decision.daily_loss_halt,
                }
            )
        )
        return
    raise ValueError(f"Unsupported worker mode: {mode}")


if __name__ == "__main__":
    main()
