from datetime import date

import pytest
import pandas as pd

from core.config import AppConfig
from core.sec13f_fetcher import FilingMetadata, _identifier_to_cik, clean_holdings, normalize_13f_value_to_usd


def test_normalizes_pre_2023_values_reported_in_thousands():
    values, convention = normalize_13f_value_to_usd(
        [125, 875],
        filing_date=date(2022, 11, 14),
        report_date=date(2022, 9, 30),
        total_value_usd=None,
    )

    assert values == [125_000.0, 875_000.0]
    assert convention == "thousands_by_legacy_date"


def test_keeps_recent_values_reported_in_dollars():
    values, convention = normalize_13f_value_to_usd(
        [125_000.0, 875_000.0],
        filing_date=date(2024, 11, 14),
        report_date=date(2024, 9, 30),
        total_value_usd=None,
    )

    assert values == [125_000.0, 875_000.0]
    assert convention == "dollars_by_modern_date"


def test_uses_total_value_consistency_when_available():
    values, convention = normalize_13f_value_to_usd(
        [125, 875],
        filing_date=date(2024, 11, 14),
        report_date=date(2024, 9, 30),
        total_value_usd=1_000_000,
    )

    assert values == [125_000.0, 875_000.0]
    assert convention == "thousands_by_total_value"


def test_fails_closed_when_dates_and_total_are_missing():
    with pytest.raises(ValueError, match="Cannot confidently normalize"):
        normalize_13f_value_to_usd(
            [125, 875],
            filing_date=None,
            report_date=None,
            total_value_usd=None,
        )


def test_share_type_is_not_treated_as_a_tradeable_ticker():
    metadata = FilingMetadata(
        manager_name="Example Manager",
        cik="0000000001",
        accession_number="0000000001-24-000001",
        filing_date=date(2024, 11, 14),
        report_date=date(2024, 9, 30),
        form_type="13F-HR",
    )
    raw = pd.DataFrame(
        [
            {
                "nameOfIssuer": "No Symbol Inc",
                "cusip": "123456789",
                "value": 100_000,
                "sshPrnamt": 100,
                "sshPrnamtType": "SH",
            }
        ]
    )

    holdings = clean_holdings(raw, metadata)

    assert pd.isna(holdings.loc[0, "ticker"])
    assert holdings.loc[0, "is_trade_eligible"] is False
    assert "Missing ticker" in holdings.loc[0, "warnings"]


def test_clean_holdings_accepts_metadata_units_and_total_value():
    metadata = FilingMetadata(
        manager_name="Example Manager",
        cik="0000000001",
        accession_number="0000000001-24-000001",
        filing_date=date(2024, 11, 14),
        report_date=date(2024, 9, 30),
        form_type="13F-HR",
    )
    raw = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "nameOfIssuer": "Alpha Inc",
                "cusip": "123456789",
                "value": 125,
                "sshPrnamt": 100,
            },
            {
                "ticker": "BBB",
                "nameOfIssuer": "Beta Inc",
                "cusip": "987654321",
                "value": 875,
                "sshPrnamt": 200,
            },
        ]
    )

    holdings = clean_holdings(raw, metadata, total_value_usd=1_000_000, metadata_units=None)

    assert list(holdings["reported_value_usd"]) == [875_000.0, 125_000.0]
    assert list(holdings["ticker"]) == ["BBB", "AAA"]


def test_identifier_to_cik_resolves_manager_names(monkeypatch):
    config = AppConfig(
        edgar_identity="Tester tester@example.com",
        alpaca_api_key=None,
        alpaca_secret_key=None,
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

    monkeypatch.setattr(
        "core.sec13f_fetcher._resolve_cik_by_company_name",
        lambda name, settings: "0001067983",
    )

    assert _identifier_to_cik("Berkshire Hathaway", config) == "0001067983"
