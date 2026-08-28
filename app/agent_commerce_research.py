"""Bounded, veto-only x402 research purchasing for existing trade candidates."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import re
import secrets
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol


RESEARCH_ENDPOINT = (
    "https://lumen-agent-commerce-production.up.railway.app/v1/research"
)
RESEARCH_NETWORK = "eip155:8453"
RESEARCH_USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
RESEARCH_PAY_TO = "0xfdfadd01edcbabe025931e45cdc8532b00218500"
RESEARCH_AMOUNT_ATOMIC = 1_000_000
RESEARCH_AMOUNT_USDC = Decimal("1.00")
RESEARCH_DAILY_LIMIT_USDC = Decimal("5.00")
RESEARCH_WINDOW = timedelta(hours=24)
RESEARCH_JOURNAL_PATH = Path("data/agent_commerce_research_v1.jsonl")
BASE_RPC_URL = "https://mainnet.base.org"
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
MODES = {"disabled", "shadow", "enforced"}
VERDICTS = {"avoid", "watch", "neutral", "consider"}
THESIS_STATUSES = {
    "supported",
    "contradicted",
    "mixed",
    "insufficient_evidence",
}
CONFIDENCES = {"low", "medium", "high"}
HEX_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
HEX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


class ResearchPolicyError(RuntimeError):
    """A fail-closed research policy or journal failure."""


class ResearchUnavailable(ResearchPolicyError):
    """The service failed before a payment authorization was created."""


class AmbiguousResearchPayment(ResearchPolicyError):
    """A signed request may have settled and must never be retried automatically."""

    def __init__(self, message: str, *, transaction_hash: str | None = None):
        super().__init__(message)
        self.transaction_hash = transaction_hash


@dataclass(frozen=True)
class ResearchCandidate:
    normalized_asset: str
    symbol: str
    token_address: str
    side: str
    trading_decision_id: str
    requested_at: datetime


@dataclass(frozen=True)
class ResearchReport:
    report_id: str
    as_of: str
    verdict: str
    thesis_status: str
    confidence: str
    red_flags: tuple[str, ...]


@dataclass(frozen=True)
class PreparedResearchPayment:
    request_body: dict[str, object]
    payment_required: object


@dataclass(frozen=True)
class PurchasedResearch:
    response_body: object
    transaction_hash: str


@dataclass(frozen=True)
class ResearchGateDecision:
    allowed: bool
    reason: str
    report: ResearchReport | None = None
    cache_hit: bool = False


@dataclass(frozen=True)
class _TypedDataDomain:
    name: str
    version: str
    chain_id: int
    verifying_contract: str


@dataclass(frozen=True)
class _TypedDataField:
    name: str
    type: str


class ResearchPaymentProvider(Protocol):
    def prepare(self, candidate: ResearchCandidate) -> PreparedResearchPayment:
        """Fetch and validate an unpaid x402 challenge without signing."""

    def pay(
        self,
        prepared: PreparedResearchPayment,
        *,
        attempt_id: str,
        now: datetime,
    ) -> PurchasedResearch:
        """Make exactly one signed request and verify its settlement."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ResearchPolicyError("Research timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


def normalized_asset(token_address: str) -> str:
    address = token_address.strip().lower()
    if not HEX_ADDRESS.fullmatch(address):
        raise ResearchPolicyError("Research asset address is invalid.")
    return f"{RESEARCH_NETWORK}:erc20:{address}"


def candidate_for_trade(
    *,
    symbol: str,
    token_address: str,
    side: str,
    trading_decision_id: str,
    requested_at: datetime,
) -> ResearchCandidate:
    clean_symbol = symbol.strip().upper()
    clean_side = side.strip().upper()
    if not clean_symbol or len(clean_symbol) > 32:
        raise ResearchPolicyError("Research asset symbol is invalid.")
    if clean_side not in {"BUY", "SELL"}:
        raise ResearchPolicyError("Research candidate side is invalid.")
    if not re.fullmatch(r"[0-9a-f]{64}", trading_decision_id):
        raise ResearchPolicyError("Trading decision ID is invalid.")
    address = token_address.strip().lower()
    return ResearchCandidate(
        normalized_asset=normalized_asset(address),
        symbol=clean_symbol,
        token_address=address,
        side=clean_side,
        trading_decision_id=trading_decision_id,
        requested_at=_utc(requested_at),
    )


def load_research_mode() -> str:
    mode = os.getenv("LUMEN_AGENT_COMMERCE_RESEARCH_MODE", "disabled").strip().lower()
    if mode not in MODES:
        raise ResearchPolicyError(
            "LUMEN_AGENT_COMMERCE_RESEARCH_MODE must be disabled, shadow, or enforced."
        )
    return mode


def research_public_status(mode: str | None = None) -> dict[str, object]:
    current = mode or load_research_mode()
    if current not in MODES:
        raise ResearchPolicyError("Research mode is invalid.")
    return {
        "mode": current,
        "network": RESEARCH_NETWORK,
        "amount_per_report_usdc": "1.00",
        "rolling_24h_limit_usdc": "5.00",
        "per_asset_rolling_24h_reports": 1,
    }


def _canonical(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _entry_hash(payload: dict[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("entry_hash", None)
    return hashlib.sha256(_canonical(unsigned).encode()).hexdigest()


def _read_locked(handle) -> list[dict[str, object]]:
    handle.seek(0)
    entries: list[dict[str, object]] = []
    previous_hash = GENESIS_HASH
    for sequence, line in enumerate(handle, start=1):
        if not line.endswith("\n"):
            raise ResearchPolicyError("Research journal has an incomplete entry.")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise ResearchPolicyError("Research journal contains invalid JSON.") from error
        if not isinstance(entry, dict):
            raise ResearchPolicyError("Research journal entry is not an object.")
        if entry.get("schema_version") != SCHEMA_VERSION:
            raise ResearchPolicyError("Research journal schema is unsupported.")
        if entry.get("sequence") != sequence:
            raise ResearchPolicyError("Research journal sequence is invalid.")
        if entry.get("previous_hash") != previous_hash:
            raise ResearchPolicyError("Research journal hash chain is broken.")
        if entry.get("entry_hash") != _entry_hash(entry):
            raise ResearchPolicyError("Research journal entry hash is invalid.")
        try:
            _utc(datetime.fromisoformat(str(entry["recorded_at"])))
        except (KeyError, ValueError) as error:
            raise ResearchPolicyError("Research journal timestamp is invalid.") from error
        previous_hash = str(entry["entry_hash"])
        entries.append(entry)
    return entries


def _append_locked(
    handle,
    entries: list[dict[str, object]],
    *,
    event: str,
    recorded_at: datetime,
    details: dict[str, object],
) -> dict[str, object]:
    entry: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "sequence": len(entries) + 1,
        "event": event,
        "recorded_at": _utc(recorded_at).isoformat(),
        "details": details,
        "previous_hash": entries[-1]["entry_hash"] if entries else GENESIS_HASH,
    }
    entry["entry_hash"] = _entry_hash(entry)
    handle.seek(0, 2)
    handle.write(_canonical(entry) + "\n")
    handle.flush()
    os.fsync(handle.fileno())
    entries.append(entry)
    return entry


def read_research_journal(
    path: Path = RESEARCH_JOURNAL_PATH,
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            return _read_locked(handle)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _bounded_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ResearchPolicyError(f"Research report {label} is invalid.")
    text = value.strip()
    if not text or len(text) > maximum:
        raise ResearchPolicyError(f"Research report {label} is invalid.")
    return text


def validate_report(payload: object, *, now: datetime) -> ResearchReport:
    current = _utc(now)
    try:
        encoded = json.dumps(payload, ensure_ascii=True)
    except (TypeError, ValueError) as error:
        raise ResearchPolicyError("Research report is not valid JSON data.") from error
    if len(encoded.encode()) > 131_072 or not isinstance(payload, dict):
        raise ResearchPolicyError("Research report is missing or too large.")
    report_id = _bounded_text(payload.get("report_id"), "ID", 128)
    report = payload.get("report")
    if not isinstance(report, dict):
        raise ResearchPolicyError("Research report body is invalid.")
    as_of = _bounded_text(report.get("as_of"), "as_of date", 10)
    try:
        report_date = date.fromisoformat(as_of)
    except ValueError as error:
        raise ResearchPolicyError("Research report as_of date is invalid.") from error
    if report_date > current.date() or (current.date() - report_date).days > 1:
        raise ResearchPolicyError("Research report is stale or from the future.")
    verdict = _bounded_text(report.get("verdict"), "verdict", 32).lower()
    thesis_status = _bounded_text(
        report.get("thesis_status"), "thesis status", 64
    ).lower()
    confidence = _bounded_text(report.get("confidence"), "confidence", 32).lower()
    if verdict not in VERDICTS:
        raise ResearchPolicyError("Research report verdict is invalid.")
    if thesis_status not in THESIS_STATUSES:
        raise ResearchPolicyError("Research report thesis status is invalid.")
    if confidence not in CONFIDENCES:
        raise ResearchPolicyError("Research report confidence is invalid.")
    red_flags_value = report.get("red_flags")
    if not isinstance(red_flags_value, list) or len(red_flags_value) > 25:
        raise ResearchPolicyError("Research report red flags are invalid.")
    red_flags = tuple(
        _bounded_text(item, "red flag", 500) for item in red_flags_value
    )
    return ResearchReport(
        report_id=report_id,
        as_of=as_of,
        verdict=verdict,
        thesis_status=thesis_status,
        confidence=confidence,
        red_flags=red_flags,
    )


def _veto(report: ResearchReport) -> tuple[bool, str]:
    if report.verdict == "avoid":
        return True, "Paid research verdict is avoid."
    if report.thesis_status == "contradicted":
        return True, "Paid research thesis is contradicted."
    if report.confidence == "high" and report.red_flags:
        return True, "High-confidence paid research contains red flags."
    return False, "Paid research raised no configured veto."


def _evaluation_details(
    candidate: ResearchCandidate,
    *,
    amount: Decimal,
    payment_status: str,
    settlement_status: str,
    report: ResearchReport | None,
    cache_hit: bool,
    final_result: str,
    reason: str,
    transaction_hash: str | None = None,
) -> dict[str, object]:
    report_source = (
        "cache_hit"
        if cache_hit
        else "new_purchase"
        if amount == RESEARCH_AMOUNT_USDC
        else "none"
    )
    return {
        "normalized_asset": candidate.normalized_asset,
        "research_request_time": candidate.requested_at.isoformat(),
        "amount": {"value": format(amount, ".2f"), "currency": "USDC"},
        "amount_usdc": format(amount, ".2f"),
        "payment_status": payment_status,
        "settlement_status": settlement_status,
        "report_id": report.report_id if report else None,
        "report_as_of": report.as_of if report else None,
        "verdict": report.verdict if report else None,
        "thesis_status": report.thesis_status if report else None,
        "confidence": report.confidence if report else None,
        "red_flags": list(report.red_flags) if report else [],
        "cache_hit": cache_hit,
        "report_source": report_source,
        "final_result": final_result,
        "trading_decision_id": candidate.trading_decision_id,
        "reason": reason,
        "transaction_hash": transaction_hash,
    }


class AgentCommerceResearchGate:
    """Atomically cache, reserve, purchase, validate, audit, and veto."""

    def __init__(
        self,
        *,
        mode: str,
        provider: ResearchPaymentProvider,
        journal_path: Path = RESEARCH_JOURNAL_PATH,
    ) -> None:
        if mode not in MODES:
            raise ResearchPolicyError("Research mode is invalid.")
        self.mode = mode
        self.provider = provider
        self.journal_path = journal_path

    def evaluate(self, candidate: ResearchCandidate) -> ResearchGateDecision:
        now = _utc(candidate.requested_at)
        if self.mode == "disabled":
            return ResearchGateDecision(True, "Agent Commerce research is disabled.")

        if self.mode == "shadow":
            try:
                self.provider.prepare(candidate)
            except Exception as error:
                reason = (
                    "Paid research is unavailable before payment: "
                    f"{type(error).__name__}."
                )
                self._record_without_reservation(candidate, reason, now)
                return ResearchGateDecision(False, reason)
            reason = "Shadow mode validated the unpaid challenge; payment is disabled."
            self._record_without_reservation(
                candidate,
                reason,
                now,
                payment_status="shadow_not_paid",
                settlement_status="not_attempted",
            )
            return ResearchGateDecision(False, reason)

        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                entries = _read_locked(handle)
                cutoff = now - RESEARCH_WINDOW
                recent = [
                    entry
                    for entry in entries
                    if _utc(datetime.fromisoformat(str(entry["recorded_at"]))) >= cutoff
                ]
                cached = self._cached_report(recent, candidate, now)
                if cached is not None:
                    vetoed, reason = _veto(cached)
                    _append_locked(
                        handle,
                        entries,
                        event="EVALUATED",
                        recorded_at=now,
                        details=_evaluation_details(
                            candidate,
                            amount=Decimal("0"),
                            payment_status="cache_reused",
                            settlement_status="confirmed",
                            report=cached,
                            cache_hit=True,
                            final_result="veto" if vetoed else "pass",
                            reason=reason,
                        ),
                    )
                    return ResearchGateDecision(not vetoed, reason, cached, True)

                reservations = [
                    entry
                    for entry in recent
                    if entry.get("event") == "RESERVED"
                    and isinstance(entry.get("details"), dict)
                ]
                if any(
                    entry["details"].get("normalized_asset")
                    == candidate.normalized_asset
                    for entry in reservations
                ):
                    reason = (
                        "A paid research authorization for this asset is already "
                        "reserved within the rolling 24-hour window."
                    )
                    _append_locked(
                        handle,
                        entries,
                        event="EVALUATED",
                        recorded_at=now,
                        details=_evaluation_details(
                            candidate,
                            amount=Decimal("0"),
                            payment_status="not_retried",
                            settlement_status="unreconciled",
                            report=None,
                            cache_hit=False,
                            final_result="veto",
                            reason=reason,
                        ),
                    )
                    return ResearchGateDecision(False, reason)
                reserved = sum(
                    (Decimal(str(entry["details"]["amount_usdc"])) for entry in reservations),
                    Decimal("0"),
                )
                if RESEARCH_AMOUNT_USDC > Decimal("1.00"):
                    raise ResearchPolicyError("Per-report research ceiling is invalid.")
                if reserved + RESEARCH_AMOUNT_USDC > RESEARCH_DAILY_LIMIT_USDC:
                    reason = "Rolling 24-hour paid research budget is exhausted."
                    _append_locked(
                        handle,
                        entries,
                        event="EVALUATED",
                        recorded_at=now,
                        details=_evaluation_details(
                            candidate,
                            amount=Decimal("0"),
                            payment_status="budget_rejected",
                            settlement_status="not_attempted",
                            report=None,
                            cache_hit=False,
                            final_result="veto",
                            reason=reason,
                        ),
                    )
                    return ResearchGateDecision(False, reason)

                try:
                    prepared = self.provider.prepare(candidate)
                except Exception as error:
                    reason = (
                        "Paid research is unavailable before payment: "
                        f"{type(error).__name__}."
                    )
                    _append_locked(
                        handle,
                        entries,
                        event="EVALUATED",
                        recorded_at=now,
                        details=_evaluation_details(
                            candidate,
                            amount=Decimal("0"),
                            payment_status="not_started",
                            settlement_status="unavailable",
                            report=None,
                            cache_hit=False,
                            final_result="veto",
                            reason=reason,
                        ),
                    )
                    return ResearchGateDecision(False, reason)

                attempt_id = hashlib.sha256(
                    f"{candidate.trading_decision_id}:{now.isoformat()}".encode()
                ).hexdigest()
                _append_locked(
                    handle,
                    entries,
                    event="RESERVED",
                    recorded_at=now,
                    details={
                        "attempt_id": attempt_id,
                        "normalized_asset": candidate.normalized_asset,
                        "trading_decision_id": candidate.trading_decision_id,
                        "amount_usdc": "1.00",
                    },
                )
                try:
                    purchased = self.provider.pay(
                        prepared,
                        attempt_id=attempt_id,
                        now=now,
                    )
                except Exception as error:
                    transaction_hash = getattr(error, "transaction_hash", None)
                    reason = (
                        "Paid research settlement or delivery is ambiguous; the "
                        "reservation remains charged and will not be retried."
                    )
                    _append_locked(
                        handle,
                        entries,
                        event="EVALUATED",
                        recorded_at=now,
                        details=_evaluation_details(
                            candidate,
                            amount=RESEARCH_AMOUNT_USDC,
                            payment_status="authorization_created",
                            settlement_status="ambiguous",
                            report=None,
                            cache_hit=False,
                            final_result="veto",
                            reason=f"{reason} ({type(error).__name__})",
                            transaction_hash=transaction_hash,
                        ),
                    )
                    return ResearchGateDecision(False, reason)

                try:
                    report = validate_report(purchased.response_body, now=now)
                except ResearchPolicyError as error:
                    reason = f"Paid research report is invalid: {error}"
                    _append_locked(
                        handle,
                        entries,
                        event="EVALUATED",
                        recorded_at=now,
                        details=_evaluation_details(
                            candidate,
                            amount=RESEARCH_AMOUNT_USDC,
                            payment_status="paid",
                            settlement_status="confirmed",
                            report=None,
                            cache_hit=False,
                            final_result="veto",
                            reason=reason,
                            transaction_hash=purchased.transaction_hash,
                        ),
                    )
                    return ResearchGateDecision(False, reason)

                vetoed, reason = _veto(report)
                _append_locked(
                    handle,
                    entries,
                    event="EVALUATED",
                    recorded_at=now,
                    details=_evaluation_details(
                        candidate,
                        amount=RESEARCH_AMOUNT_USDC,
                        payment_status="paid",
                        settlement_status="confirmed",
                        report=report,
                        cache_hit=False,
                        final_result="veto" if vetoed else "pass",
                        reason=reason,
                        transaction_hash=purchased.transaction_hash,
                    ),
                )
                return ResearchGateDecision(not vetoed, reason, report, False)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _record_without_reservation(
        self,
        candidate: ResearchCandidate,
        reason: str,
        now: datetime,
        *,
        payment_status: str = "not_started",
        settlement_status: str = "unavailable",
    ) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                entries = _read_locked(handle)
                _append_locked(
                    handle,
                    entries,
                    event="EVALUATED",
                    recorded_at=now,
                    details=_evaluation_details(
                        candidate,
                        amount=Decimal("0"),
                        payment_status=payment_status,
                        settlement_status=settlement_status,
                        report=None,
                        cache_hit=False,
                        final_result="veto",
                        reason=reason,
                    ),
                )
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _cached_report(
        entries: list[dict[str, object]],
        candidate: ResearchCandidate,
        now: datetime,
    ) -> ResearchReport | None:
        for entry in reversed(entries):
            if entry.get("event") != "EVALUATED":
                continue
            details = entry.get("details")
            if not isinstance(details, dict):
                continue
            if (
                details.get("normalized_asset") != candidate.normalized_asset
                or details.get("settlement_status") != "confirmed"
                or not details.get("report_id")
            ):
                continue
            report = ResearchReport(
                report_id=str(details["report_id"]),
                as_of=str(details["report_as_of"]),
                verdict=str(details["verdict"]),
                thesis_status=str(details["thesis_status"]),
                confidence=str(details["confidence"]),
                red_flags=tuple(str(item) for item in details.get("red_flags", [])),
            )
            # Revalidate freshness independently of the hash-chained cache record.
            validate_report(
                {
                    "report_id": report.report_id,
                    "report": {
                        "as_of": report.as_of,
                        "verdict": report.verdict,
                        "thesis_status": report.thesis_status,
                        "confidence": report.confidence,
                        "red_flags": list(report.red_flags),
                    },
                },
                now=now,
            )
            return report
        return None


class _CdpX402Signer:
    def __init__(self, wallet_provider: object, *, attempt_id: str, now: datetime):
        self._wallet = wallet_provider
        self._attempt_id = attempt_id
        self._now = _utc(now)

    @property
    def address(self) -> str:
        return str(self._wallet.get_address())

    def sign_typed_data(
        self,
        domain: object,
        types: dict[str, list[object]],
        primary_type: str,
        message: dict[str, Any],
    ) -> bytes:
        self._validate(domain, types, primary_type, message)

        async def sign() -> str:
            from cdp.openapi_client.models.eip712_domain import EIP712Domain

            client = self._wallet.get_client()
            async with client as cdp:
                account = await cdp.evm.get_account(address=self.address)
                return await account.sign_typed_data(
                    domain=EIP712Domain(
                        name=domain.name,
                        version=domain.version,
                        chain_id=domain.chain_id,
                        verifying_contract=domain.verifying_contract,
                    ),
                    types={
                        name: [asdict(field) for field in fields]
                        for name, fields in types.items()
                    },
                    primary_type=primary_type,
                    message=message,
                    idempotency_key=str(
                        uuid.uuid5(uuid.NAMESPACE_URL, self._attempt_id)
                    ),
                )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            signature = asyncio.run(sign())
        else:
            raise ResearchPolicyError("x402 signing cannot run in an active event loop.")
        if not isinstance(signature, str) or not re.fullmatch(
            r"0x[0-9a-fA-F]{130}", signature
        ):
            raise ResearchPolicyError("CDP returned an invalid x402 signature.")
        return bytes.fromhex(signature[2:])

    def _validate(
        self,
        domain: object,
        types: dict[str, list[object]],
        primary_type: str,
        message: dict[str, Any],
    ) -> None:
        if (
            getattr(domain, "name", None) != "USD Coin"
            or getattr(domain, "version", None) != "2"
            or getattr(domain, "chain_id", None) != 8453
            or str(getattr(domain, "verifying_contract", "")).lower()
            != RESEARCH_USDC_ADDRESS.lower()
            or primary_type != "TransferWithAuthorization"
        ):
            raise ResearchPolicyError("x402 signing domain is outside policy.")
        expected_fields = [
            ("from", "address"),
            ("to", "address"),
            ("value", "uint256"),
            ("validAfter", "uint256"),
            ("validBefore", "uint256"),
            ("nonce", "bytes32"),
        ]
        actual_fields = [
            (getattr(field, "name", None), getattr(field, "type", None))
            for field in types.get(primary_type, [])
        ]
        if set(types) != {primary_type} or actual_fields != expected_fields:
            raise ResearchPolicyError("x402 signing types are outside policy.")
        try:
            valid_before = datetime.fromtimestamp(
                int(message["validBefore"]), tz=timezone.utc
            )
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise ResearchPolicyError("x402 authorization expiry is invalid.") from error
        if (
            set(message) != {item[0] for item in expected_fields}
            or str(message["from"]).lower() != self.address.lower()
            or str(message["to"]).lower() != RESEARCH_PAY_TO.lower()
            or int(message["value"]) != RESEARCH_AMOUNT_ATOMIC
            or int(message["validAfter"]) != 0
            or not self._now < valid_before <= self._now + timedelta(seconds=300)
            or not re.fullmatch(r"0x[0-9a-fA-F]{64}", str(message["nonce"]))
        ):
            raise ResearchPolicyError("x402 authorization is outside policy.")


class CdpX402ResearchProvider:
    """Single-attempt x402 provider backed by the bot's exact CDP Base account."""

    def __init__(self, *, wallet_address: str, timeout_seconds: int = 20):
        if not HEX_ADDRESS.fullmatch(wallet_address):
            raise ResearchPolicyError("Research payer wallet is invalid.")
        if not 1 <= timeout_seconds <= 30:
            raise ResearchPolicyError("Research HTTP timeout is outside policy.")
        self.wallet_address = wallet_address.lower()
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _body(candidate: ResearchCandidate) -> dict[str, object]:
        return {
            "subject": f"{candidate.symbol} on Base ({candidate.token_address})",
            "question": (
                f"Risk-control review for an independently generated {candidate.side} "
                "candidate. Identify evidence that should veto this candidate; do not "
                "recommend, size, authorize, or execute a trade."
            ),
            "context": (
                "Existing strategy candidate; external research is veto-only. "
                f"Trading decision ID: {candidate.trading_decision_id}."
            ),
            "horizon": "days",
            "risk_tolerance": "high",
        }

    def prepare(self, candidate: ResearchCandidate) -> PreparedResearchPayment:
        import requests
        from requests.adapters import HTTPAdapter

        body = self._body(candidate)
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=0))
        try:
            response = session.post(
                RESEARCH_ENDPOINT,
                json=body,
                timeout=self.timeout_seconds,
                allow_redirects=False,
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as error:
            raise ResearchUnavailable("Research service is unavailable.") from error
        if response.url != RESEARCH_ENDPOINT or response.status_code != 402:
            raise ResearchUnavailable("Research service did not return the pinned 402 challenge.")
        header = response.headers.get("PAYMENT-REQUIRED")
        if not header or len(header) > 65_536:
            raise ResearchUnavailable("Research service omitted the x402 challenge.")
        try:
            payment_required = json.loads(
                base64.b64decode(header.encode(), validate=True).decode()
            )
        except Exception as error:
            raise ResearchUnavailable("Research service returned an invalid x402 challenge.") from error
        self._validate_challenge(payment_required)
        return PreparedResearchPayment(body, payment_required)

    def _validate_challenge(self, payment_required: object) -> None:
        if not isinstance(payment_required, dict):
            raise ResearchUnavailable("x402 challenge is not an object.")
        accepts = payment_required.get("accepts")
        resource = payment_required.get("resource")
        extensions = payment_required.get("extensions")
        if (
            payment_required.get("x402Version") != 2
            or not isinstance(resource, dict)
            or resource.get("url") != RESEARCH_ENDPOINT
            or not isinstance(accepts, list)
            or len(accepts) != 1
            or (
                extensions is not None
                and (
                    not isinstance(extensions, dict)
                    or not set(extensions) <= {"bazaar"}
                )
            )
        ):
            raise ResearchUnavailable("x402 resource or version is outside policy.")
        requirement = accepts[0]
        if not isinstance(requirement, dict):
            raise ResearchUnavailable("x402 payment requirement is invalid.")
        extra = requirement.get("extra")
        if (
            requirement.get("scheme") != "exact"
            or requirement.get("network") != RESEARCH_NETWORK
            or str(requirement.get("asset", "")).lower()
            != RESEARCH_USDC_ADDRESS.lower()
            or str(requirement.get("amount", "")) != str(RESEARCH_AMOUNT_ATOMIC)
            or str(requirement.get("payTo", "")).lower()
            != RESEARCH_PAY_TO.lower()
            or not 1 <= int(requirement.get("maxTimeoutSeconds", 0)) <= 300
            or not isinstance(extra, dict)
            or extra.get("name") != "USD Coin"
            or extra.get("version") != "2"
            or extra.get("assetTransferMethod") is not None
        ):
            raise ResearchUnavailable("x402 payment requirement is outside policy.")

    def pay(
        self,
        prepared: PreparedResearchPayment,
        *,
        attempt_id: str,
        now: datetime,
    ) -> PurchasedResearch:
        import requests
        from coinbase_agentkit import CdpEvmWalletProvider, CdpEvmWalletProviderConfig
        from requests.adapters import HTTPAdapter

        wallet = CdpEvmWalletProvider(
            CdpEvmWalletProviderConfig(address=self.wallet_address, network_id="base-mainnet")
        )
        if (
            str(wallet.get_address()).lower() != self.wallet_address
            or str(wallet.get_network().chain_id) != "8453"
            or wallet.get_network().network_id != "base-mainnet"
        ):
            raise ResearchPolicyError("CDP x402 wallet or network is outside policy.")
        signer = _CdpX402Signer(wallet, attempt_id=attempt_id, now=now)
        try:
            challenge = prepared.payment_required
            self._validate_challenge(challenge)
            requirement = challenge["accepts"][0]
            valid_before = int(_utc(now).timestamp()) + int(
                requirement["maxTimeoutSeconds"]
            )
            message = {
                "from": signer.address,
                "to": RESEARCH_PAY_TO,
                "value": str(RESEARCH_AMOUNT_ATOMIC),
                "validAfter": "0",
                "validBefore": str(valid_before),
                "nonce": "0x" + secrets.token_hex(32),
            }
            types = {
                "TransferWithAuthorization": [
                    _TypedDataField("from", "address"),
                    _TypedDataField("to", "address"),
                    _TypedDataField("value", "uint256"),
                    _TypedDataField("validAfter", "uint256"),
                    _TypedDataField("validBefore", "uint256"),
                    _TypedDataField("nonce", "bytes32"),
                ]
            }
            signature = signer.sign_typed_data(
                _TypedDataDomain(
                    name="USD Coin",
                    version="2",
                    chain_id=8453,
                    verifying_contract=RESEARCH_USDC_ADDRESS,
                ),
                types,
                "TransferWithAuthorization",
                message,
            )
            payment_payload = {
                "x402Version": 2,
                "accepted": requirement,
                "payload": {
                    "authorization": message,
                    "signature": "0x" + signature.hex(),
                },
                "resource": challenge["resource"],
            }
            if challenge.get("extensions"):
                payment_payload["extensions"] = challenge["extensions"]
            authorization = base64.b64encode(
                json.dumps(
                    payment_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode()
            ).decode()
        except Exception as error:
            raise AmbiguousResearchPayment(
                "x402 authorization creation failed after reservation."
            ) from error

        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=0))
        try:
            response = session.post(
                RESEARCH_ENDPOINT,
                json=prepared.request_body,
                timeout=self.timeout_seconds,
                allow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "PAYMENT-SIGNATURE": authorization,
                },
            )
        except requests.RequestException as error:
            raise AmbiguousResearchPayment("Signed x402 delivery is ambiguous.") from error
        if response.url != RESEARCH_ENDPOINT or response.status_code != 200:
            raise AmbiguousResearchPayment("Signed x402 response is ambiguous.")
        settlement_header = response.headers.get("PAYMENT-RESPONSE")
        if not settlement_header:
            raise AmbiguousResearchPayment("x402 settlement header is missing.")
        try:
            settlement = json.loads(
                base64.b64decode(settlement_header.encode(), validate=True).decode()
            )
        except Exception as error:
            raise AmbiguousResearchPayment("x402 settlement header is invalid.") from error
        if not isinstance(settlement, dict):
            raise AmbiguousResearchPayment("x402 settlement header is invalid.")
        transaction = str(settlement.get("transaction", ""))
        if (
            settlement.get("success") is not True
            or str(settlement.get("payer")).lower() != self.wallet_address
            or str(settlement.get("network")) != RESEARCH_NETWORK
            or str(settlement.get("amount")) != str(RESEARCH_AMOUNT_ATOMIC)
            or not HEX_HASH.fullmatch(transaction)
        ):
            raise AmbiguousResearchPayment(
                "x402 settlement is outside policy.", transaction_hash=transaction
            )
        self._verify_receipt(transaction)
        if len(response.content) > 131_072:
            raise ResearchPolicyError("Paid research response is too large.")
        try:
            response_body = response.json()
        except ValueError as error:
            raise ResearchPolicyError("Paid research response is not JSON.") from error
        return PurchasedResearch(response_body, transaction)

    def _verify_receipt(self, transaction_hash: str) -> None:
        import requests

        try:
            response = requests.post(
                BASE_RPC_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getTransactionReceipt",
                    "params": [transaction_hash],
                },
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
            response.raise_for_status()
            receipt = response.json().get("result")
        except (requests.RequestException, ValueError, AttributeError) as error:
            raise AmbiguousResearchPayment(
                "Base receipt verification is unavailable.",
                transaction_hash=transaction_hash,
            ) from error
        if not isinstance(receipt, dict) or receipt.get("status") != "0x1":
            raise AmbiguousResearchPayment(
                "Base receipt is missing or unsuccessful.",
                transaction_hash=transaction_hash,
            )
        expected_from = "0x" + self.wallet_address[2:].lower().rjust(64, "0")
        expected_to = "0x" + RESEARCH_PAY_TO[2:].lower().rjust(64, "0")
        matches = [
            log
            for log in receipt.get("logs", [])
            if isinstance(log, dict)
            and str(log.get("address", "")).lower() == RESEARCH_USDC_ADDRESS.lower()
            and isinstance(log.get("topics"), list)
            and len(log["topics"]) >= 3
            and str(log["topics"][0]).lower() == TRANSFER_TOPIC
            and str(log["topics"][1]).lower() == expected_from
            and str(log["topics"][2]).lower() == expected_to
            and int(str(log.get("data", "0x0")), 16) == RESEARCH_AMOUNT_ATOMIC
        ]
        if len(matches) != 1:
            raise AmbiguousResearchPayment(
                "Base receipt does not contain exactly one pinned USDC transfer.",
                transaction_hash=transaction_hash,
            )


def build_research_gate(
    *,
    wallet_address: str,
    journal_path: Path = RESEARCH_JOURNAL_PATH,
) -> AgentCommerceResearchGate:
    mode = load_research_mode()
    return AgentCommerceResearchGate(
        mode=mode,
        provider=CdpX402ResearchProvider(wallet_address=wallet_address),
        journal_path=journal_path,
    )
