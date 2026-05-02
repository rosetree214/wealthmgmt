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
    )


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
        service.submit_preview_orders(preview, "same", "same", confirm=True)


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
        monkeypatch.setattr(
            service,
            "get_asset_metadata",
            lambda symbols: {"AAA": {"tradable": True, "asset_status": "active", "fractionable": True}},
        )
        service.submit_preview_orders(preview, "same", "same", confirm=True)
