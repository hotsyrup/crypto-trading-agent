import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


MARKER_PATH = Path(os.getenv("TELEGRAM_REPORT_MARKER", "/app/data/telegram-report-date"))


def reporting_enabled() -> bool:
    return os.getenv("TELEGRAM_REPORTING_ENABLED", "false").strip().lower() == "true"


def report_is_due(now: datetime) -> bool:
    if not reporting_enabled():
        return False
    timezone = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/Los_Angeles"))
    local_now = now.astimezone(timezone)
    report_hour = int(os.getenv("REPORT_HOUR_LOCAL", "9"))
    if report_hour < 0 or report_hour > 23:
        raise ValueError("REPORT_HOUR_LOCAL must be between 0 and 23.")
    if local_now.hour < report_hour:
        return False
    try:
        return MARKER_PATH.read_text(encoding="utf-8").strip() != local_now.date().isoformat()
    except FileNotFoundError:
        return True


def format_daily_report(state: dict[str, object], now: datetime) -> str:
    timezone = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/Los_Angeles"))
    local_now = now.astimezone(timezone)
    service = os.getenv("RAILWAY_SERVICE_NAME", "Lumen trading monitor")
    environment = os.getenv("RAILWAY_ENVIRONMENT_NAME", "production")
    return "\n".join(
        [
            f"Lumen daily cloud report — {local_now:%Y-%m-%d}",
            f"Service: {service} ({environment})",
            f"Agent status: {state.get('status', 'unknown')}",
            f"Mode: {state.get('mode', 'monitoring_only')}",
            f"Last cycle: {state.get('last_cycle_at') or 'not completed'}",
            f"Observed signal: {state.get('signal', 'none')}",
            f"Reference price: {state.get('reference_price', 'unavailable')}",
            f"Last error: {state.get('last_error') or 'none'}",
            "Execution: disabled; no orders submitted or signed.",
        ]
    )


def send_daily_report(state: dict[str, object], now: datetime) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise ValueError("Telegram reporting requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")

    payload = urlencode({"chat_id": chat_id, "text": format_daily_report(state, now)}).encode()
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    with urlopen(request, timeout=15) as response:  # nosec B310 - fixed HTTPS host
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the daily report.")

    timezone = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/Los_Angeles"))
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(now.astimezone(timezone).date().isoformat(), encoding="utf-8")
