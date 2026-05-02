import pandas as pd

from core.portfolio_scaler import calculate_scaled_targets, generate_trade_preview


def test_calculate_scaled_targets_preserves_weights_and_notional():
    holdings = pd.DataFrame(
        [
            {"ticker": "AAA", "company_name": "Alpha", "reported_value_usd": 75_000.0},
            {"ticker": "BBB", "company_name": "Beta", "reported_value_usd": 25_000.0},
        ]
    )

    scaled = calculate_scaled_targets(holdings, target_portfolio_size=10_000)

    assert scaled.loc[0, "fund_weight"] == 0.75
    assert scaled.loc[0, "target_notional_usd"] == 7_500
    assert scaled.loc[1, "fund_weight"] == 0.25
    assert scaled.loc[1, "target_notional_usd"] == 2_500


def test_generate_trade_preview_skips_missing_price_and_sells_before_buys():
    targets = pd.DataFrame(
        [
            {
                "ticker": "BUY",
                "company_name": "Buy Co",
                "target_notional_usd": 120.0,
                "current_price": 10.0,
                "tradable": True,
                "asset_status": "active",
                "fractionable": True,
                "is_trade_eligible": True,
            },
            {
                "ticker": "SELL",
                "company_name": "Sell Co",
                "target_notional_usd": 0.0,
                "current_price": 10.0,
                "tradable": True,
                "asset_status": "active",
                "fractionable": False,
                "is_trade_eligible": True,
            },
            {
                "ticker": "MISS",
                "company_name": "Missing Price",
                "target_notional_usd": 50.0,
                "current_price": None,
                "tradable": True,
                "asset_status": "active",
                "fractionable": True,
                "is_trade_eligible": True,
            },
        ]
    )

    preview = generate_trade_preview(
        targets,
        current_positions={"BUY": 1.0, "SELL": 5.0},
        min_trade_notional=10.0,
    )

    assert list(preview["symbol"]) == ["SELL", "BUY", "MISS"]
    assert preview.loc[0, "side"] == "sell"
    assert preview.loc[0, "delta_shares"] == -5.0
    assert preview.loc[1, "side"] == "buy"
    assert preview.loc[1, "delta_shares"] == 11.0
    assert preview.loc[2, "side"] == "skip"
    assert "Missing price" in preview.loc[2, "warnings"]

