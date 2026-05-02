from __future__ import annotations

import hashlib
import json
import os
import smtplib
from datetime import UTC, date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
HISTORY_DIR = ROOT / "history"
STATE_DIR = ROOT / "state"
APP_LOG = LOG_DIR / "app.jsonl"


def ensure_runtime_dirs() -> None:
    for directory in (LOG_DIR, HISTORY_DIR, STATE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def write_json(path: Path, data: Any) -> None:
    ensure_runtime_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=json_default), encoding="utf-8")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    ensure_runtime_dirs()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, default=json_default) + "\n")


def log_event(
    event_type: str,
    *,
    cik: str | None = None,
    accession_number: str | None = None,
    paper_mode: bool | None = None,
    dry_run: bool | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "timestamp": utc_now_iso(),
        "event_type": event_type,
        "cik": cik,
        "accession_number": accession_number,
        "paper_mode": paper_mode,
        "dry_run": dry_run,
        "details": details or {},
    }
    append_jsonl(APP_LOG, event)
    return event


def normalize_cik(cik_or_text: str) -> str:
    digits = "".join(character for character in str(cik_or_text) if character.isdigit())
    if not digits:
        raise ValueError("CIK must contain digits")
    if len(digits) > 10:
        raise ValueError("CIK cannot exceed 10 digits")
    return digits.zfill(10)


def safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for candidate in (text[:10], text):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    return None


def money(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"${float(value):,.2f}"


def pct(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


def dataframe_hash(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(records, sort_keys=True, default=json_default)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def send_notifications(subject: str, body: str, config: Any) -> list[str]:
    outcomes: list[str] = []
    if getattr(config, "slack_webhook_url", ""):
        try:
            response = requests.post(
                config.slack_webhook_url,
                json={"text": f"*{subject}*\n{body}"},
                timeout=15,
            )
            response.raise_for_status()
            outcomes.append("slack_sent")
        except Exception as exc:  # pragma: no cover - external service
            outcomes.append(f"slack_failed: {exc}")

    smtp_ready = all(
        [
            getattr(config, "smtp_host", ""),
            getattr(config, "smtp_port", None),
            getattr(config, "email_from", ""),
            getattr(config, "email_to", ""),
        ]
    )
    if smtp_ready:
        try:
            message = EmailMessage()
            message["Subject"] = subject
            message["From"] = config.email_from
            message["To"] = config.email_to
            message.set_content(body)
            with smtplib.SMTP(config.smtp_host, int(config.smtp_port), timeout=20) as smtp:
                if getattr(config, "smtp_username", ""):
                    smtp.starttls()
                    smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(message)
            outcomes.append("email_sent")
        except Exception as exc:  # pragma: no cover - external service
            outcomes.append(f"email_failed: {exc}")

    if not outcomes:
        outcomes.append("notifications_skipped")
    return outcomes


def read_recent_history(limit: int = 10) -> list[dict[str, Any]]:
    ensure_runtime_dirs()
    files = sorted(HISTORY_DIR.glob("rebalance_*.json"), key=os.path.getmtime, reverse=True)
    return [read_json(path, {}) for path in files[:limit]]
