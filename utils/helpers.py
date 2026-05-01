"""Shared helpers: structured logging, JSON state I/O, formatters, notifications."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import smtplib
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import requests

LOG_DIR = Path("logs")
HISTORY_DIR = Path("history")
STATE_DIR = Path("state")
LOG_FILE = LOG_DIR / "app.jsonl"

for _d in (LOG_DIR, HISTORY_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# --- Structured JSONL logging ---------------------------------------------
_logger = logging.getLogger("mirror_trader")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(handler)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj):
        return asdict(obj)
    try:
        import pandas as pd  # type: ignore

        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
    except Exception:
        pass
    return str(obj)


def log_event(event_type: str, **fields: Any) -> dict:
    """Write a structured JSONL line and return the record."""
    record: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
    }
    record.update(fields)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=_json_default) + "\n")
    except Exception as exc:  # pragma: no cover
        _logger.warning("failed to write log file: %s", exc)
    _logger.info("%s %s", event_type, {k: v for k, v in fields.items() if k != "details"})
    return record


# --- JSON state -----------------------------------------------------------
def load_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default if default is not None else {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log_event("state.load_error", path=str(p), error=str(exc))
        return default if default is not None else {}


def save_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=_json_default)
    tmp.replace(p)


# --- Formatters -----------------------------------------------------------
def fmt_currency(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.{digits}f}"


def fmt_percent(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v * 100:.{digits}f}%"


def fmt_shares(value: Optional[float], digits: int = 4) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    return f"{v:,.{digits}f}"


# --- Hashing --------------------------------------------------------------
def hash_payload(payload: Any) -> str:
    """Stable SHA-256 over a JSON-serializable payload."""
    blob = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# --- Safe parsing ---------------------------------------------------------
def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    f = safe_float(value)
    if f is None:
        return default
    try:
        return int(f)
    except (OverflowError, ValueError):
        return default


# --- Notifications --------------------------------------------------------
def send_slack(webhook_url: Optional[str], text: str) -> bool:
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=10)
        ok = 200 <= resp.status_code < 300
        log_event("notify.slack", ok=ok, status=resp.status_code)
        return ok
    except requests.RequestException as exc:
        log_event("notify.slack_error", error=str(exc))
        return False


def send_email(
    *,
    smtp_host: Optional[str],
    smtp_port: Optional[int],
    smtp_username: Optional[str],
    smtp_password: Optional[str],
    email_from: Optional[str],
    email_to: Optional[str],
    subject: str,
    body: str,
) -> bool:
    if not (smtp_host and smtp_port and email_from and email_to):
        return False
    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            try:
                server.starttls()
            except smtplib.SMTPException:
                pass
            if smtp_username and smtp_password:
                server.login(smtp_username, smtp_password)
            server.send_message(msg)
        log_event("notify.email", ok=True, to=email_to, subject=subject)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log_event("notify.email_error", error=str(exc))
        return False


def notify_all(
    *,
    cfg: Any,  # AppConfig (avoid circular import)
    subject: str,
    body: str,
) -> dict:
    """Send notifications via every channel configured on `cfg`. Returns status dict."""
    slack_ok = False
    email_ok = False
    if getattr(cfg, "slack_webhook_url", None):
        slack_ok = send_slack(cfg.slack_webhook_url, f"*{subject}*\n{body}")
    if getattr(cfg, "smtp_host", None):
        email_ok = send_email(
            smtp_host=cfg.smtp_host,
            smtp_port=cfg.smtp_port,
            smtp_username=cfg.smtp_username,
            smtp_password=cfg.smtp_password,
            email_from=cfg.email_from,
            email_to=cfg.email_to,
            subject=subject,
            body=body,
        )
    return {"slack": slack_ok, "email": email_ok}


# --- Misc -----------------------------------------------------------------
def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def chunked(iterable: Iterable, size: int) -> Iterable[list]:
    buf: list = []
    for item in iterable:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
