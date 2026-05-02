from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]


def _load_streamlit_secrets() -> dict[str, Any]:
    try:
        import streamlit as st

        return dict(st.secrets)
    except Exception:
        return {}


def _get_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_cik(cik: str | int) -> str:
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    if not digits:
        raise ValueError("CIK must contain digits")
    if len(digits) > 10:
        raise ValueError("CIK cannot be longer than 10 digits")
    return digits.zfill(10)


@dataclass(frozen=True)
class AppConfig:
    edgar_identity: str | None
    app_password: str | None
    alpaca_api_key: str | None
    alpaca_secret_key: str | None
    alpaca_paper: bool
    target_portfolio_size: float
    default_cik: str
    auto_execute: bool
    allow_live_trading: bool
    allow_live_auto_execute: bool
    min_trade_notional: float
    preview_ttl_seconds: int
    rebalance_interval_hours: int
    scan_form_types: list[str]
    smtp_host: str | None
    smtp_port: int | None
    smtp_username: str | None
    smtp_password: str | None
    email_from: str | None
    email_to: str | None
    slack_webhook_url: str | None

    @property
    def alpaca_configured(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def has_alpaca_keys(self) -> bool:
        return self.alpaca_configured

    @property
    def live_trading_enabled(self) -> bool:
        return not self.alpaca_paper and self.allow_live_trading

    @property
    def requires_app_auth(self) -> bool:
        return self.alpaca_configured or bool(self.app_password)


def load_config() -> AppConfig:
    load_dotenv(BASE_DIR / ".env")
    secrets = _load_streamlit_secrets()

    def get(name: str, default: Any = None) -> Any:
        return os.getenv(name, secrets.get(name, default))

    return AppConfig(
        edgar_identity=get("EDGAR_IDENTITY") or None,
        app_password=get("APP_PASSWORD") or None,
        alpaca_api_key=get("ALPACA_API_KEY") or None,
        alpaca_secret_key=get("ALPACA_SECRET_KEY") or None,
        alpaca_paper=_get_bool(get("ALPACA_PAPER"), True),
        target_portfolio_size=_get_float(get("TARGET_PORTFOLIO_SIZE"), 10_000.0),
        default_cik=normalize_cik(get("DEFAULT_CIK", "0002045724")),
        auto_execute=_get_bool(get("AUTO_EXECUTE"), False),
        allow_live_trading=_get_bool(get("ALLOW_LIVE_TRADING"), False),
        allow_live_auto_execute=_get_bool(get("ALLOW_LIVE_AUTO_EXECUTE"), False),
        min_trade_notional=_get_float(get("MIN_TRADE_NOTIONAL"), 10.0),
        preview_ttl_seconds=_get_int(get("PREVIEW_TTL_SECONDS"), 300),
        rebalance_interval_hours=_get_int(get("REBALANCE_INTERVAL_HOURS"), 1),
        scan_form_types=_parse_form_types(get("SCAN_FORM_TYPES"), ["13F-HR", "13F-HR/A"]),
        smtp_host=get("SMTP_HOST") or None,
        smtp_port=_get_int(get("SMTP_PORT"), 0) or None,
        smtp_username=get("SMTP_USERNAME") or None,
        smtp_password=get("SMTP_PASSWORD") or None,
        email_from=get("EMAIL_FROM") or None,
        email_to=get("EMAIL_TO") or None,
        slack_webhook_url=get("SLACK_WEBHOOK_URL") or None,
    )


Settings = AppConfig


def get_settings() -> AppConfig:
    return load_config()


def _parse_form_types(value: Any, default: list[str]) -> list[str]:
    if value in (None, ""):
        return default
    forms = [str(item).strip().upper() for item in str(value).split(",") if str(item).strip()]
    return forms or default

