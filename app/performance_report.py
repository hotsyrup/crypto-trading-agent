import json
from collections import Counter

from app.trade_journal import JOURNAL_PATH


def load_records() -> list[dict]:
    if not JOURNAL_PATH.exists():
        return []

    with JOURNAL_PATH.open("r", encoding="utf-8") as journal:
        return [json.loads(line) for line in journal if line.strip()]


def generate_report() -> dict:
    records = load_records()
    signal_counts = Counter(record["signal"] for record in records)

    return {
        "total_decisions": len(records),
        "buy_signals": signal_counts["BUY"],
        "sell_signals": signal_counts["SELL"],
        "hold_signals": signal_counts["HOLD"],
        "latest_signal": records[-1]["signal"] if records else "NONE",
    }


if __name__ == "__main__":
    report = generate_report()

    print("Paper-Trading Activity Report")
    print(f"Total decisions: {report['total_decisions']}")
    print(f"BUY signals: {report['buy_signals']}")
    print(f"SELL signals: {report['sell_signals']}")
    print(f"HOLD signals: {report['hold_signals']}")
    print(f"Latest signal: {report['latest_signal']}")
    print("Profit and loss tracking will be added after portfolio accounting.")
