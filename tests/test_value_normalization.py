from datetime import date

import pytest

from core.sec13f_fetcher import normalize_13f_value_to_usd


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
