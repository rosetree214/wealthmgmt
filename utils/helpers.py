from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import smtplib
import socket
from datetime import UTC, date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _host_is_public(hostname: str) -> bool:
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def _valid_slack_webhook(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"hooks.slack.com", "hooks.slack-gov.com"}
        and _host_is_public(parsed.hostname)
    )


def _valid_smtp_host(hostname: str) -> bool:
    return bool(hostname and _host_is_public(hostname))


def send_notifications(subject: str, body: str, config: Any) -> list[str]:
    outcomes: list[str] = []
    if getattr(config, "slack_webhook_url", ""):
        if not _valid_slack_webhook(config.slack_webhook_url):
            outcomes.append("slack_skipped_invalid_destination")
        else:
            try:
                response = requests.post(
                    config.slack_webhook_url,
                    json={"text": f"*{subject}*\n{body}"},
                    timeout=15,
                )
                response.raise_for_status()
                outcomes.append("slack_sent")
            except Exception:  # pragma: no cover - external service
                outcomes.append("slack_failed")

    smtp_ready = all(
        [
            getattr(config, "smtp_host", ""),
            getattr(config, "smtp_port", None),
            getattr(config, "email_from", ""),
            getattr(config, "email_to", ""),
        ]
    )
    if smtp_ready:
        if not _valid_smtp_host(config.smtp_host):
            outcomes.append("email_skipped_invalid_destination")
        else:
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
            except Exception:  # pragma: no cover - external service
                outcomes.append("email_failed")

    if not outcomes:
        outcomes.append("notifications_skipped")
    return outcomes


def read_recent_history(limit: int = 10) -> list[dict[str, Any]]:
    ensure_runtime_dirs()
    files = sorted(HISTORY_DIR.glob("rebalance_*.json"), key=os.path.getmtime, reverse=True)
    return [read_json(path, {}) for path in files[:limit]]
