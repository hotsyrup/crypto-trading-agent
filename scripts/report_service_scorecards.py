from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.service_receipts import RECEIPT_JOURNAL_PATH, load_receipts, score_provider


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print observation-only paid-service provider scorecards."
    )
    parser.add_argument(
        "--journal",
        type=Path,
        default=RECEIPT_JOURNAL_PATH,
        help="Path to the append-only service-receipt JSONL journal.",
    )
    parser.add_argument(
        "--provider",
        help="Optional case-insensitive provider name filter.",
    )
    args = parser.parse_args()

    receipts = load_receipts(args.journal)
    providers = sorted({receipt.provider for receipt in receipts}, key=str.casefold)
    if args.provider:
        providers = [
            provider
            for provider in providers
            if provider.casefold() == args.provider.strip().casefold()
        ]

    report = {
        "mode": "observation_only",
        "journal": str(args.journal),
        "providers": [
            _json_safe(asdict(score_provider(receipts, provider)))
            for provider in providers
        ],
        "execution_permitted": False,
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
