from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from core.config import AppConfig
from core.trade_executor import AlpacaService


def _config() -> AppConfig:
    return AppConfig(
        edgar_identity="Tester tester@example.com",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        alpaca_paper=True,
        target_portfolio_size=10_000,
        default_cik="0002045724",
        auto_execute=False,
        allow_live_trading=False,
        allow_live_auto_execute=False,
        min_trade_notional=10,
        rebalance_interval_hours=1,
        smtp_host=None,
        smtp_port=None,
        smtp_username=None,
        smtp_password=None,
        email_from=None,
        email_to=None,
        slack_webhook_url=None,
        app_password=None,
        preview_ttl_seconds=300,
        scan_form_types=["13F-HR", "13F-HR/A"],
    )


def _preview_row(symbol: str = "AAA", side: str = "buy") -> pd.DataFrame:
    delta = 1 if side == "buy" else -1
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "side": side,
                "delta_shares": delta,
                "estimated_price": 10,
                "estimated_notional": 10,
                "warnings": "",
            }
        ]
    )


def _ready_service(config: AppConfig | None = None) -> AlpacaService:
    service = AlpacaService(config or _config())
    service._client = object()
    service.get_account_status = lambda: type(
        "Status",
        (),
        {"account": {"buying_power": 1000.0}},
    )()
    service.get_positions = lambda: {"AAA": 10.0}
    service.get_asset_metadata = lambda symbols: {
        symbol: {"tradable": True, "asset_status": "active", "fractionable": True}
        for symbol in symbols
    }
    service.get_prices = lambda symbols: {symbol: {"price": 10.0} for symbol in symbols}
    return service


def test_executor_rejects_rows_with_safety_warnings_before_submission():
    service = AlpacaService(_config())
    service._client = object()
    preview = pd.DataFrame(
        [
            {
                "symbol": "SH",
                "side": "buy",
                "delta_shares": 1,
                "estimated_price": 10,
                "estimated_notional": 10,
                "warnings": "Missing ticker; excluded from trading",
            }
        ]
    )

    with pytest.raises(PermissionError, match="warnings"):
        service.submit_preview_orders(
            preview,
            "same",
            "same",
            confirm=True,
            preview_created_at=datetime.now(UTC).isoformat(),
        )


def test_executor_rejects_sell_larger_than_current_position(monkeypatch):
    service = AlpacaService(_config())
    service._client = object()
    monkeypatch.setattr(service, "get_positions", lambda: {"AAA": 1.0})
    preview = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "side": "sell",
                "delta_shares": -2,
                "estimated_price": 10,
                "estimated_notional": 20,
                "warnings": "",
            }
        ]
    )

    with pytest.raises(PermissionError, match="exceeds current position"):
        service.get_asset_metadata = lambda symbols: {
            "AAA": {"tradable": True, "asset_status": "active", "fractionable": True}
        }
        service.get_prices = lambda symbols: {"AAA": {"price": 10.0}}
        service.submit_preview_orders(
            preview,
            "same",
            "same",
            confirm=True,
            preview_created_at=datetime.now(UTC).isoformat(),
        )


def test_executor_rejects_stale_preview_before_submission():
    service = _ready_service()
    stale = datetime.now(UTC) - timedelta(minutes=10)

    with pytest.raises(PermissionError, match="stale"):
        service.submit_preview_orders(
            _preview_row(),
            "same",
            "same",
            confirm=True,
            preview_created_at=stale.isoformat(),
        )


def test_executor_uses_configured_preview_ttl():
    config = _config().__class__(**{**_config().__dict__, "preview_ttl_seconds": 30})
    service = _ready_service(config)
    stale = datetime.now(UTC) - timedelta(seconds=31)

    with pytest.raises(PermissionError, match="stale"):
        service.submit_preview_orders(
            _preview_row(),
            "same",
            "same",
            confirm=True,
            preview_created_at=stale.isoformat(),
        )


def test_executor_rejects_live_without_live_confirmation():
    live_config = _config().__class__(**{**_config().__dict__, "alpaca_paper": False, "allow_live_trading": True})
    service = _ready_service(live_config)

    with pytest.raises(PermissionError, match="Live trading requires explicit live confirmation"):
        service.submit_preview_orders(
            _preview_row(),
            "same",
            "same",
            confirm=True,
            preview_created_at=datetime.now(UTC).isoformat(),
            live_confirm=False,
        )


def test_executor_rejects_execution_time_price_move():
    service = _ready_service()
    service.get_prices = lambda symbols: {"AAA": {"price": 20.0}}

    with pytest.raises(PermissionError, match="price moved"):
        service.submit_preview_orders(
            _preview_row(),
            "same",
            "same",
            confirm=True,
            preview_created_at=datetime.now(UTC).isoformat(),
        )
