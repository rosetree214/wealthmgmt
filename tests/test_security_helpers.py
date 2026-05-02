from types import SimpleNamespace

from core.config import AppConfig
from utils.helpers import send_notifications


def _config(**overrides) -> AppConfig:
    values = {
        "edgar_identity": "Tester tester@example.com",
        "alpaca_api_key": None,
        "alpaca_secret_key": None,
        "alpaca_paper": True,
        "target_portfolio_size": 10_000,
        "default_cik": "0002045724",
        "auto_execute": False,
        "allow_live_trading": False,
        "allow_live_auto_execute": False,
        "min_trade_notional": 10,
        "rebalance_interval_hours": 1,
        "smtp_host": None,
        "smtp_port": None,
        "smtp_username": None,
        "smtp_password": None,
        "email_from": None,
        "email_to": None,
        "slack_webhook_url": None,
        "app_password": None,
        "preview_ttl_seconds": 300,
    }
    values.update(overrides)
    return AppConfig(**values)


def test_rejects_private_slack_webhook_without_calling_requests(monkeypatch):
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr("utils.helpers.requests.post", fake_post)

    outcomes = send_notifications(
        "subject",
        "body",
        _config(slack_webhook_url="http://127.0.0.1/hook"),
    )

    assert outcomes == ["slack_skipped_invalid_destination"]
    assert called is False
