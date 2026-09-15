"""Six-hour OpenAI Responses synthesis for deterministic research facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.research_v3_runtime import MAX_SOURCE_BYTES, ResearchV3Store, seal


RESPONSES_URL = "https://api.openai.com/v1/responses"
PROMPT = (
    "Analyze only the supplied deterministic Base network facts. Return numeric "
    "macro findings with their periods and units, list material data limitations, "
    "and use null comparison fields when a trustworthy current fact has no valid "
    "comparison. Refuse only when no trustworthy numeric facts are supplied. Do "
    "not recommend or authorize orders, swaps, wallet actions, position sizes, or "
    "payments."
)
FINDING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "metric": {"type": "string", "minLength": 1},
        "period": {"type": "string", "minLength": 1},
        "value": {"type": "string", "pattern": r"^-?[0-9]+(?:\.[0-9]+)?$"},
        "unit": {"type": "string", "minLength": 1},
        "comparison_value": {
            "type": ["string", "null"],
            "pattern": r"^-?[0-9]+(?:\.[0-9]+)?$",
        },
        "comparison_period": {"type": ["string", "null"]},
    },
    "required": [
        "metric", "period", "value", "unit", "comparison_value", "comparison_period"
    ],
}
MACRO_DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "findings": {"type": "array", "items": FINDING_SCHEMA, "maxItems": 20},
        "uncertainties": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 20,
        },
        "refusal": {"type": ["string", "null"]},
    },
    "required": ["findings", "uncertainties", "refusal"],
}


@dataclass(frozen=True)
class MacroDraft:
    findings: tuple[dict[str, object], ...]
    uncertainties: tuple[str, ...]
    refusal: str | None


@dataclass(frozen=True)
class MacroResult:
    response_id: str
    model: str
    usage: dict[str, object]
    draft: MacroDraft


def _parse_draft(value: object) -> MacroDraft:
    if not isinstance(value, dict) or set(value) != {"findings", "uncertainties", "refusal"}:
        raise ValueError("OpenAI structured output is invalid.")
    findings = value["findings"]
    uncertainties = value["uncertainties"]
    refusal = value["refusal"]
    required_finding_fields = set(FINDING_SCHEMA["required"])
    if (
        not isinstance(findings, list)
        or len(findings) > 20
        or any(not isinstance(item, dict) or set(item) != required_finding_fields for item in findings)
        or not isinstance(uncertainties, list)
        or len(uncertainties) > 20
        or any(not isinstance(item, str) or not item for item in uncertainties)
        or (refusal is not None and (not isinstance(refusal, str) or not refusal))
    ):
        raise ValueError("OpenAI structured output is invalid.")
    for finding in findings:
        if any(
            not isinstance(finding[name], str) or not finding[name]
            for name in ("metric", "period", "value", "unit")
        ):
            raise ValueError("OpenAI structured output is invalid.")
        for name in ("comparison_value", "comparison_period"):
            if finding[name] is not None and not isinstance(finding[name], str):
                raise ValueError("OpenAI structured output is invalid.")
        try:
            numbers = [Decimal(finding["value"])]
            if finding["comparison_value"] is not None:
                numbers.append(Decimal(finding["comparison_value"]))
        except (InvalidOperation, ValueError):
            raise ValueError("OpenAI structured output is invalid.") from None
        if any(not number.is_finite() for number in numbers):
            raise ValueError("OpenAI structured output is invalid.")
    return MacroDraft(
        findings=() if refusal is not None else tuple(findings),
        uncertainties=tuple(dict.fromkeys(uncertainties)),
        refusal=refusal,
    )


@dataclass(frozen=True)
class OpenAIResponsesMacroClient:
    api_key: str = field(repr=False)
    model: str = "gpt-5.6-terra"
    timeout_seconds: float = 45
    max_output_tokens: int = 1800
    opener: Callable[..., object] = field(default=urlopen, repr=False, compare=False)

    def synthesize(self, *, source_facts: list[dict[str, object]]) -> MacroResult:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for macro synthesis.")
        body = {
            "model": self.model,
            "instructions": PROMPT,
            "input": json.dumps(source_facts, sort_keys=True, separators=(",", ":")),
            "max_output_tokens": self.max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "lumen_base_macro_draft",
                    "strict": True,
                    "schema": MACRO_DRAFT_SCHEMA,
                }
            },
        }
        request = Request(
            RESPONSES_URL,
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "lumen-research/3",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_SOURCE_BYTES + 1)
        except HTTPError as error:
            raise RuntimeError(f"OpenAI Responses API returned status {error.code}.") from error
        except (TimeoutError, URLError, OSError) as error:
            raise RuntimeError("OpenAI Responses API is unavailable.") from error
        if len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("OpenAI response exceeds the size limit.")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            raise ValueError("OpenAI response did not complete.")
        output_text = None
        refusal = None
        for item in payload.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if isinstance(part, dict) and part.get("type") == "output_text":
                    output_text = part.get("text")
                elif isinstance(part, dict) and part.get("type") == "refusal":
                    refusal = part.get("refusal")
        draft = (
            MacroDraft(findings=(), uncertainties=(), refusal=str(refusal))
            if refusal
            else _parse_draft(json.loads(str(output_text)))
        )
        response_id = payload.get("id")
        usage = payload.get("usage")
        if not isinstance(response_id, str) or not response_id or not isinstance(usage, dict):
            raise ValueError("OpenAI response metadata is invalid.")
        return MacroResult(
            response_id=response_id,
            model=str(payload.get("model") or self.model),
            usage=usage,
            draft=draft,
        )


def _report_id(
    source_facts: list[dict[str, object]],
    draft: MacroDraft,
    model: str,
    prompt_version: str,
) -> str:
    identity = {
        "source_facts": source_facts,
        "findings": draft.findings,
        "uncertainties": draft.uncertainties,
        "refusal": draft.refusal,
        "model": model,
        "prompt_version": prompt_version,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ResearchSynthesis:
    store: ResearchV3Store
    client: OpenAIResponsesMacroClient
    prompt_version: str
    interval: timedelta = timedelta(hours=6)

    def is_due(self, *, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Synthesis time must include a timezone.")
        slot_seconds = int(self.interval.total_seconds())
        if slot_seconds != 21600:
            raise ValueError("OpenAI research synthesis interval must remain six hours.")
        slot_id = int(now.astimezone(timezone.utc).timestamp()) // slot_seconds
        return not self.store.slot_claimed(slot_id)

    def run_if_due(
        self,
        *,
        source_facts: list[dict[str, object]],
        now: datetime,
    ) -> dict[str, object] | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Synthesis time must include a timezone.")
        current = now.astimezone(timezone.utc)
        slot_seconds = int(self.interval.total_seconds())
        if slot_seconds != 21600:
            raise ValueError("OpenAI research synthesis interval must remain six hours.")
        slot_id = int(current.timestamp()) // slot_seconds
        if not source_facts or any(
            not isinstance(item, dict) or not isinstance(item.get("source"), dict)
            for item in source_facts
        ):
            raise ValueError("Macro synthesis requires deterministic source facts.")
        if not self.store.claim_slot(slot_id, current.isoformat()):
            return None
        result = self.client.synthesize(source_facts=source_facts)
        source_refs = [item["source"] for item in source_facts]
        report_id = _report_id(
            source_facts, result.draft, result.model, self.prompt_version
        )
        resource = {
            "resource_type": "macro_report",
            "schema_version": 3,
            "network": "eip155:8453",
            "report_id": report_id,
            "generated_at": current.isoformat(),
            "expires_at": (current + self.interval).isoformat(),
            "model": result.model,
            "prompt_version": self.prompt_version,
            "source_refs": source_refs,
            "findings": list(result.draft.findings),
            "uncertainties": list(result.draft.uncertainties),
            "refusal": result.draft.refusal,
            "execution_authorized": False,
        }
        report = seal(resource)
        from app.research_contract_v3 import validate_research_bundle

        validate_research_bundle(
            {
                "service": "lumen-base-research-agent",
                "schema_version": 3,
                "mode": "observation_only",
                "execution": "disabled",
                "generated_at": current.isoformat(),
                "market_snapshots": [],
                "asset_failures": [],
                "macro_report": report,
            },
            now=current,
        )
        if not self.store.commit_slot(
            slot_id, report, result.response_id, result.usage
        ):
            return None
        return report
