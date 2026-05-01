"""Tests for portfolio scaling and action calculation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import pytest

from core.portfolio_scaler import (
    ACTION_BUY,
    ACTION_HOLD,
    ACTION_SELL,
    ACTION_SKIP,
    compute_scaled_targets,
    determine_actions,
    estimate_buying_power_required,
    estimate_sell_proceeds,
)


@dataclass
class FakeQuote:
    price: float
    source: str = "test"
    symbol: str = ""
    timestamp: str = ""


@dataclass
class FakeMeta:
    tradable: bool = True
    fractionable: bool = True
    symbol: str = ""
    status: str = "active"
    asset_class: str = "us_equity"
    name: Optional[str] = None


def _holdings_df():
    return pd.DataFrame(
        [
            {"ticker": "AAPL", "issuer": "Apple", "cusip": "037833100",
             "figi": None, "security_class": "COM", "put_call": None,
             "shares": 100.0, "shares_type": "SH", "value_raw": 60_000.0,
             "value_usd": 60_000.0, "fund_weight": 0.6},
            {"ticker": "MSFT", "issuer": "Microsoft", "cusip": "594918104",
             "figi": None, "security_class": "COM", "put_call": None,
             "shares": 50.0, "shares_type": "SH", "value_raw": 30_000.0,
             "value_usd": 30_000.0, "fund_weight": 0.3},
            {"ticker": "GOOG", "issuer": "Alphabet", "cusip": "02079K107",
             "figi": None, "security_class": "COM", "put_call": "CALL",
             "shares": 10.0, "shares_type": "SH", "value_raw": 10_000.0,
             "value_usd": 10_000.0, "fund_weight": 0.1},
        ]
    )


def test_weights_and_target_notional():
    holdings = _holdings_df()
    prices = {
        "AAPL": FakeQuote(price=200.0),
        "MSFT": FakeQuote(price=300.0),
        "GOOG": FakeQuote(price=150.0),
    }
    meta = {
        "AAPL": FakeMeta(),
        "MSFT": FakeMeta(),
        "GOOG": FakeMeta(),
    }
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=10_000.0,
        total_13f_value_usd=100_000.0,
        prices=prices,
        asset_meta=meta,
        include_options=False,
    )
    by_sym = {t.ticker: t for t in targets}
    # GOOG (CALL) excluded by default
    assert "GOOG" not in by_sym
    assert pytest.approx(by_sym["AAPL"].fund_weight, rel=1e-9) == 0.6
    assert pytest.approx(by_sym["AAPL"].target_notional_usd, rel=1e-9) == 6_000.0
    assert pytest.approx(by_sym["AAPL"].target_shares, rel=1e-9) == 30.0
    assert pytest.approx(by_sym["MSFT"].target_shares, rel=1e-9) == 10.0


def test_options_included_when_flag_true():
    holdings = _holdings_df()
    prices = {"AAPL": FakeQuote(200.0), "MSFT": FakeQuote(300.0), "GOOG": FakeQuote(150.0)}
    meta = {k: FakeMeta() for k in prices}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=10_000.0,
        total_13f_value_usd=100_000.0,
        prices=prices,
        asset_meta=meta,
        include_options=True,
    )
    assert any(t.ticker == "GOOG" for t in targets)


def test_missing_price_skipped():
    holdings = _holdings_df()
    prices = {"AAPL": FakeQuote(200.0)}  # no MSFT price
    meta = {"AAPL": FakeMeta(), "MSFT": FakeMeta()}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=10_000.0,
        total_13f_value_usd=100_000.0,
        prices=prices,
        asset_meta=meta,
    )
    msft = next(t for t in targets if t.ticker == "MSFT")
    assert msft.price is None
    assert msft.target_shares == 0.0
    assert "No usable price" in (msft.warning or "")


def test_actions_buy_sell_hold():
    holdings = _holdings_df()
    prices = {"AAPL": FakeQuote(200.0), "MSFT": FakeQuote(300.0)}
    meta = {"AAPL": FakeMeta(), "MSFT": FakeMeta()}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=10_000.0,
        total_13f_value_usd=100_000.0,
        prices=prices,
        asset_meta=meta,
    )
    # Current: 5 AAPL (need 30 -> BUY), 12 MSFT (need 10 -> SELL 2),
    actions = determine_actions(
        targets,
        current_positions={"AAPL": 5.0, "MSFT": 12.0},
        min_trade_notional=10.0,
        full_account_rebalance=False,
    )
    by_sym = {a.ticker: a for a in actions}
    assert by_sym["AAPL"].side == ACTION_BUY
    assert pytest.approx(by_sym["AAPL"].delta_shares) == 25.0
    assert by_sym["MSFT"].side == ACTION_SELL
    assert pytest.approx(by_sym["MSFT"].delta_shares) == -2.0


def test_min_trade_notional_threshold():
    holdings = _holdings_df()
    prices = {"AAPL": FakeQuote(200.0), "MSFT": FakeQuote(300.0)}
    meta = {"AAPL": FakeMeta(), "MSFT": FakeMeta()}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=10_000.0,
        total_13f_value_usd=100_000.0,
        prices=prices,
        asset_meta=meta,
    )
    # Already at target -> HOLD
    actions = determine_actions(
        targets,
        current_positions={"AAPL": 30.0, "MSFT": 10.0},
        min_trade_notional=10.0,
    )
    assert all(a.side == ACTION_HOLD for a in actions)


def test_sell_capped_to_current():
    holdings = pd.DataFrame(
        [{
            "ticker": "AAPL", "issuer": "Apple", "cusip": "037833100",
            "figi": None, "security_class": "COM", "put_call": None,
            "shares": 1.0, "shares_type": "SH",
            "value_raw": 1.0, "value_usd": 1.0, "fund_weight": 1.0,
        }]
    )
    prices = {"AAPL": FakeQuote(200.0)}
    meta = {"AAPL": FakeMeta()}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=10.0,
        total_13f_value_usd=1.0,
        prices=prices,
        asset_meta=meta,
    )
    # Current 100 shares, target ~0.05 shares; sells ~99.95 but capped at 100
    actions = determine_actions(
        targets,
        current_positions={"AAPL": 100.0},
        min_trade_notional=1.0,
    )
    a = actions[0]
    assert a.side == ACTION_SELL
    assert a.delta_shares >= -100.0
    assert abs(a.delta_shares) <= 100.0


def test_full_account_rebalance_liquidates_unknown():
    holdings = pd.DataFrame(
        [{
            "ticker": "AAPL", "issuer": "Apple", "cusip": "037833100",
            "figi": None, "security_class": "COM", "put_call": None,
            "shares": 30.0, "shares_type": "SH",
            "value_raw": 6000.0, "value_usd": 6000.0, "fund_weight": 1.0,
        }]
    )
    prices = {"AAPL": FakeQuote(200.0)}
    meta = {"AAPL": FakeMeta()}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=6_000.0,
        total_13f_value_usd=6_000.0,
        prices=prices,
        asset_meta=meta,
    )
    actions = determine_actions(
        targets,
        current_positions={"AAPL": 30.0, "TSLA": 5.0},
        min_trade_notional=10.0,
        full_account_rebalance=True,
    )
    tsla = next(a for a in actions if a.ticker == "TSLA")
    assert tsla.side == ACTION_SELL
    assert tsla.delta_shares == -5.0


def test_non_fractionable_floors_buys():
    holdings = pd.DataFrame(
        [{
            "ticker": "BRK.A", "issuer": "Berkshire", "cusip": "084670108",
            "figi": None, "security_class": "COM", "put_call": None,
            "shares": 1.0, "shares_type": "SH",
            "value_raw": 600_000.0, "value_usd": 600_000.0, "fund_weight": 1.0,
        }]
    )
    prices = {"BRK.A": FakeQuote(600_000.0)}
    meta = {"BRK.A": FakeMeta(fractionable=False)}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=1_500_000.0,
        total_13f_value_usd=600_000.0,
        prices=prices,
        asset_meta=meta,
    )
    actions = determine_actions(
        targets,
        current_positions={},
        min_trade_notional=10.0,
    )
    assert actions[0].side == ACTION_BUY
    # 2.5 -> floor to 2
    assert actions[0].target_shares == 2.0


def test_sells_appear_before_buys():
    holdings = _holdings_df()
    prices = {"AAPL": FakeQuote(200.0), "MSFT": FakeQuote(300.0)}
    meta = {"AAPL": FakeMeta(), "MSFT": FakeMeta()}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=10_000.0,
        total_13f_value_usd=100_000.0,
        prices=prices,
        asset_meta=meta,
    )
    actions = determine_actions(
        targets,
        current_positions={"AAPL": 5.0, "MSFT": 100.0},
        min_trade_notional=10.0,
    )
    sides_in_order = [a.side for a in actions]
    if ACTION_SELL in sides_in_order and ACTION_BUY in sides_in_order:
        assert sides_in_order.index(ACTION_SELL) < sides_in_order.index(ACTION_BUY)


def test_buying_power_helpers():
    holdings = _holdings_df()
    prices = {"AAPL": FakeQuote(200.0), "MSFT": FakeQuote(300.0)}
    meta = {"AAPL": FakeMeta(), "MSFT": FakeMeta()}
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=10_000.0,
        total_13f_value_usd=100_000.0,
        prices=prices,
        asset_meta=meta,
    )
    actions = determine_actions(
        targets,
        current_positions={"AAPL": 5.0, "MSFT": 100.0},
        min_trade_notional=10.0,
    )
    assert estimate_buying_power_required(actions) > 0
    assert estimate_sell_proceeds(actions) > 0


def test_zero_price_skipped_with_skip_action():
    holdings = pd.DataFrame(
        [{
            "ticker": "ZZZZ", "issuer": "Zombie", "cusip": "000000000",
            "figi": None, "security_class": "COM", "put_call": None,
            "shares": 1.0, "shares_type": "SH",
            "value_raw": 100.0, "value_usd": 100.0, "fund_weight": 1.0,
        }]
    )
    targets = compute_scaled_targets(
        holdings,
        target_portfolio_size=1_000.0,
        total_13f_value_usd=100.0,
        prices={},  # no price
        asset_meta={},
    )
    actions = determine_actions(targets, current_positions={}, min_trade_notional=10.0)
    assert actions[0].side == ACTION_SKIP
