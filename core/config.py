"""Typed configuration for 13F Mirror Trader.

Loads values from Streamlit secrets when available, otherwise from a `.env`
file or process environment. Never hard-codes credentials.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv(override=False)


def _get_streamlit_secrets() -> dict:
    try:
        import streamlit as st  # type: ignore
    except Exception:
        return {}
    try:
        # st.secrets is a Mapping; coerce to a plain dict if available.
        return dict(st.secrets)  # type: ignore[arg-type]
    except Exception:
        return {}


def _read(name: str, default: Any = None) -> Any:
    secrets = _get_streamlit_secrets()
    if name in secrets and secrets[name] not in (None, ""):
        return secrets[name]
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val


def _read_bool(name: str, default: bool = False) -> bool:
    raw = _read(name, default)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_float(name: str, default: float) -> float:
    raw = _read(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _read_int(name: str, default: int) -> int:
    raw = _read(name, default)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def normalize_cik(cik: str | int | None) -> str:
    """Return the 10-digit zero-padded CIK string."""
    if cik is None:
        return ""
    s = str(cik).strip().lower()
    if s.startswith("cik"):
        s = s[3:].lstrip("-").lstrip("_")
    s = s.lstrip("0") or "0"
    if not s.isdigit():
        raise ValueError(f"Invalid CIK: {cik!r}")
    return s.zfill(10)


@dataclass(frozen=True)
class AppConfig:
    edgar_identity: Optional[str]
    alpaca_api_key: Optional[str]
    alpaca_secret_key: Optional[str]
    alpaca_paper: bool
    target_portfolio_size: float
    default_cik: str
    auto_execute: bool
    allow_live_trading: bool
    allow_live_auto_execute: bool
    min_trade_notional: float
    rebalance_interval_hours: int
    smtp_host: Optional[str]
    smtp_port: Optional[int]
    smtp_username: Optional[str]
    smtp_password: Optional[str]
    email_from: Optional[str]
    email_to: Optional[str]
    slack_webhook_url: Optional[str]

    issues: tuple[str, ...] = field(default_factory=tuple)

    # ---- Derived helpers --------------------------------------------------
    @property
    def has_alpaca(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def can_live_trade(self) -> bool:
        return (not self.alpaca_paper) and self.allow_live_trading

    @property
    def can_live_auto_execute(self) -> bool:
        return self.can_live_trade and self.allow_live_auto_execute and self.auto_execute

    def describe_mode(self) -> str:
        if not self.has_alpaca:
            return "READ-ONLY (no Alpaca keys)"
        if self.alpaca_paper:
            return "PAPER"
        return "LIVE"


def load_config() -> AppConfig:
    issues: list[str] = []

    edgar_identity = _read("EDGAR_IDENTITY")
    if not edgar_identity:
        issues.append(
            "EDGAR_IDENTITY is not set. SEC fair-access policy requires identifying "
            "the requester. Set EDGAR_IDENTITY in .env or Streamlit secrets."
        )

    default_cik_raw = _read("DEFAULT_CIK", "0002045724")
    try:
        default_cik = normalize_cik(default_cik_raw)
    except ValueError as exc:
        issues.append(f"Invalid DEFAULT_CIK: {exc}")
        default_cik = "0002045724"

    smtp_port_raw = _read("SMTP_PORT")
    smtp_port: Optional[int]
    try:
        smtp_port = int(smtp_port_raw) if smtp_port_raw not in (None, "") else None
    except (TypeError, ValueError):
        smtp_port = None
        issues.append("SMTP_PORT is not a valid integer; SMTP notifications disabled.")

    cfg = AppConfig(
        edgar_identity=edgar_identity,
        alpaca_api_key=_read("ALPACA_API_KEY") or None,
        alpaca_secret_key=_read("ALPACA_SECRET_KEY") or None,
        alpaca_paper=_read_bool("ALPACA_PAPER", True),
        target_portfolio_size=_read_float("TARGET_PORTFOLIO_SIZE", 10_000.0),
        default_cik=default_cik,
        auto_execute=_read_bool("AUTO_EXECUTE", False),
        allow_live_trading=_read_bool("ALLOW_LIVE_TRADING", False),
        allow_live_auto_execute=_read_bool("ALLOW_LIVE_AUTO_EXECUTE", False),
        min_trade_notional=_read_float("MIN_TRADE_NOTIONAL", 10.0),
        rebalance_interval_hours=max(1, _read_int("REBALANCE_INTERVAL_HOURS", 1)),
        smtp_host=_read("SMTP_HOST") or None,
        smtp_port=smtp_port,
        smtp_username=_read("SMTP_USERNAME") or None,
        smtp_password=_read("SMTP_PASSWORD") or None,
        email_from=_read("EMAIL_FROM") or None,
        email_to=_read("EMAIL_TO") or None,
        slack_webhook_url=_read("SLACK_WEBHOOK_URL") or None,
        issues=tuple(issues),
    )
    return cfg
