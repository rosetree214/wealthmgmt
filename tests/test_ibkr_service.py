from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from core.config import AppConfig
from core.trade_executor import IBKRService, get_broker_service


def _config(**overrides) -> AppConfig:
    values = {
        "edgar_identity": "Tester tester@example.com",
        "app_password": None,
        "alpaca_api_key": None,
        "alpaca_secret_key": None,
        "alpaca_paper": True,
        "target_portfolio_size": 10_000,
        "default_cik": "0002045724",
        "auto_execute": False,
        "allow_live_trading": False,
        "allow_live_auto_execute": False,
        "min_trade_notional": 10,
        "preview_ttl_seconds": 300,
        "rebalance_interval_hours": 1,
        "scan_form_types": ["13F-HR", "13F-HR/A"],
        "smtp_host": None,
        "smtp_port": None,
        "smtp_username": None,
        "smtp_password": None,
        "email_from": None,
        "email_to": None,
        "slack_webhook_url": None,
        "broker": "ibkr",
        "ibkr_host": "127.0.0.1",
        "ibkr_port": 7497,
        "ibkr_client_id": 13,
        "ibkr_account": None,
        "ibkr_read_only": True,
    }
    values.update(overrides)
    return AppConfig(**values)


class FakeIB:
    def __init__(self):
        self.connected = False
        self.orders = []

    def isConnected(self):
        return self.connected

    def connect(self, host, port, clientId, timeout, readonly):
        self.connected = True
        self.connect_args = {
            "host": host,
            "port": port,
            "clientId": clientId,
            "timeout": timeout,
            "readonly": readonly,
        }
        return self

    def accountSummary(self):
        return [
            SimpleNamespace(tag="BuyingPower", value="1000", currency="USD", account="DU123"),
            SimpleNamespace(tag="NetLiquidation", value="2500", currency="USD", account="DU123"),
        ]

    def portfolio(self):
        return [
            SimpleNamespace(contract=SimpleNamespace(symbol="AAA", secType="STK"), position=3, account="DU123"),
            SimpleNamespace(contract=SimpleNamespace(symbol="OPT", secType="OPT"), position=1, account="DU123"),
        ]

    def qualifyContracts(self, contract):
        contract.conId = 123
        return [contract]

    def placeOrder(self, contract, order):
        self.orders.append((contract, order))
        return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="Submitted"))


class FailingSellIB(FakeIB):
    def placeOrder(self, contract, order):
        self.orders.append((contract, order))
        if order.action == "SELL":
            raise RuntimeError("sell failed")
        return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="Submitted"))


def test_broker_factory_returns_ibkr_service():
    service = get_broker_service(_config())

    assert isinstance(service, IBKRService)


def test_ibkr_fetches_account_and_stock_positions(monkeypatch):
    fake = FakeIB()
    monkeypatch.setattr("core.trade_executor.IB", lambda: fake)

    service = IBKRService(_config())
    status = service.get_account_status()

    assert status.configured is True
    assert status.paper is True
    assert status.account["buying_power"] == 1000.0
    assert service.get_positions() == {"AAA": 3.0}
    assert fake.connect_args["readonly"] is True


def test_ibkr_filters_configured_account_and_routes_orders(monkeypatch):
    fake = FakeIB()
    monkeypatch.setattr("core.trade_executor.IB", lambda: fake)
    monkeypatch.setattr("core.trade_executor.Stock", lambda symbol, exchange, currency: SimpleNamespace(symbol=symbol))
    monkeypatch.setattr(
        "core.trade_executor.MarketOrder",
        lambda side, qty: SimpleNamespace(action=side, totalQuantity=qty, account=""),
    )
    service = IBKRService(_config(ibkr_account="DU123", ibkr_read_only=False))
    service.get_prices = lambda symbols: {"AAA": {"price": 10.0}}
    service.get_asset_metadata = lambda symbols: {
        "AAA": {"tradable": True, "asset_status": "active", "fractionable": False}
    }
    preview = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "side": "buy",
                "delta_shares": 1,
                "estimated_price": 10,
                "estimated_notional": 10,
                "warnings": "",
            }
        ]
    )

    status = service.get_account_status()
    responses = service.submit_preview_orders(
        preview,
        "same",
        "same",
        confirm=True,
        preview_created_at=datetime.now(UTC).isoformat(),
    )

    assert status.account["buying_power"] == 1000.0
    assert service.get_positions() == {"AAA": 3.0}
    assert responses[0]["status"] == "submitted"
    assert fake.orders[0][1].account == "DU123"


def test_ibkr_configured_account_skips_missing_account_rows(monkeypatch):
    fake = FakeIB()
    fake.accountSummary = lambda: [
        SimpleNamespace(tag="BuyingPower", value="9999", currency="USD", account=""),
        SimpleNamespace(tag="NetLiquidation", value="9999", currency="USD"),
    ]
    fake.portfolio = lambda: [
        SimpleNamespace(contract=SimpleNamespace(symbol="AAA", secType="STK"), position=3, account=""),
        SimpleNamespace(contract=SimpleNamespace(symbol="BBB", secType="STK"), position=4),
    ]
    monkeypatch.setattr("core.trade_executor.IB", lambda: fake)

    service = IBKRService(_config(ibkr_account="DU123"))

    assert service.get_account_status().account == {"status": "connected", "currency": "USD"}
    assert service.get_positions() == {}


def test_ibkr_aborts_buys_after_sell_failure(monkeypatch):
    fake = FailingSellIB()
    monkeypatch.setattr("core.trade_executor.IB", lambda: fake)
    monkeypatch.setattr("core.trade_executor.Stock", lambda symbol, exchange, currency: SimpleNamespace(symbol=symbol))
    monkeypatch.setattr(
        "core.trade_executor.MarketOrder",
        lambda action, qty: SimpleNamespace(action=action, totalQuantity=qty),
    )
    service = IBKRService(_config(ibkr_read_only=False))
    service.get_prices = lambda symbols: {symbol: {"price": 10.0} for symbol in symbols}
    service.get_asset_metadata = lambda symbols: {
        symbol: {"tradable": True, "asset_status": "active", "fractionable": False}
        for symbol in symbols
    }
    service.get_positions = lambda: {"SELL": 2.0}
    service.get_account_status = lambda: SimpleNamespace(account={"buying_power": 0.0})
    preview = pd.DataFrame(
        [
            {
                "symbol": "SELL",
                "side": "sell",
                "delta_shares": -1,
                "estimated_price": 10,
                "estimated_notional": 10,
                "warnings": "",
            },
            {
                "symbol": "BUY",
                "side": "buy",
                "delta_shares": 1,
                "estimated_price": 10,
                "estimated_notional": 10,
                "warnings": "",
            },
        ]
    )

    responses = service.submit_preview_orders(
        preview,
        "same",
        "same",
        confirm=True,
        preview_created_at=datetime.now(UTC).isoformat(),
    )

    assert responses == [{"symbol": "SELL", "side": "sell", "status": "failed", "error": "sell failed"}]
    assert [order.action for _, order in fake.orders] == ["SELL"]


def test_ibkr_rejects_execution_when_trading_not_enabled(monkeypatch):
    fake = FakeIB()
    monkeypatch.setattr("core.trade_executor.IB", lambda: fake)
    service = IBKRService(_config())
    preview = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "side": "buy",
                "delta_shares": 1,
                "estimated_price": 10,
                "estimated_notional": 10,
                "warnings": "",
            }
        ]
    )

    with pytest.raises(PermissionError, match="IBKR_ALLOW_TRADING"):
        service.submit_preview_orders(
            preview,
            "same",
            "same",
            confirm=True,
            preview_created_at=datetime.now(UTC).isoformat(),
        )
