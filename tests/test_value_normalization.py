"""Tests for ``normalize_13f_value_to_usd``.

The 13F-HR form historically reported values in thousands of dollars; SEC
amendments effective 2023-01-03 require values in actual dollars. The
normalizer combines the date prior with sanity-checked plausibility ranges
and fails closed when neither interpretation is plausible.
"""
from __future__ import annotations

import pytest

from core.sec13f_fetcher import (
    DOLLAR_REPORTING_CUTOFF,
    normalize_13f_value_to_usd,
)


def test_modern_filing_treated_as_dollars():
    # $250M total reported in 2024 -> already in dollars
    total, convention, _ = normalize_13f_value_to_usd(
        250_000_000.0,
        filing_date="2024-02-14",
        report_date="2023-12-31",
    )
    assert convention == "dollars"
    assert total == 250_000_000.0


def test_modern_small_filing_in_thousands_with_explicit_hint():
    # An older shop migrated, but their software still reports thousands
    total, convention, _ = normalize_13f_value_to_usd(
        250_000.0,
        filing_date="2024-02-14",
        report_date="2023-12-31",
        metadata_unit_hint="thousands",
    )
    assert convention == "thousands"
    assert total == 250_000_000.0


def test_pre_cutoff_filing_in_thousands():
    # Pre-cutoff: 250,000 raw == $250M actual
    total, convention, reason = normalize_13f_value_to_usd(
        250_000.0,
        filing_date="2022-02-14",
        report_date="2021-12-31",
    )
    assert convention == "thousands"
    assert total == 250_000_000.0
    assert "thousands" in reason


def test_pre_cutoff_already_in_dollars_due_to_size():
    # Pre-cutoff, but raw value already huge (filer reported in dollars).
    total, convention, _ = normalize_13f_value_to_usd(
        10_000_000_000.0,  # $10B
        filing_date="2022-08-14",
        report_date="2022-06-30",
    )
    # $10B in dollars is plausible. $10T (as thousands) exceeds plausible AUM
    # ceiling, so only dollars survives the sanity check.
    assert convention == "dollars"
    assert total == 10_000_000_000.0


def test_failure_when_value_implausible_in_either_unit():
    # 1.0 raw is too small to be a real 13F (too small as both dollars and thousands).
    with pytest.raises(ValueError):
        normalize_13f_value_to_usd(
            1.0,
            filing_date="2024-02-14",
            report_date="2023-12-31",
        )


def test_explicit_dollars_hint_wins():
    total, convention, _ = normalize_13f_value_to_usd(
        500_000.0,
        filing_date="2018-05-15",
        report_date="2018-03-31",
        metadata_unit_hint="dollars",
    )
    assert convention == "dollars"
    assert total == 500_000.0


def test_negative_value_rejected():
    with pytest.raises(ValueError):
        normalize_13f_value_to_usd(-1.0, filing_date="2024-01-01")


def test_zero_value_rejected():
    with pytest.raises(ValueError):
        normalize_13f_value_to_usd(0.0, filing_date="2024-01-01")


def test_cutoff_day_treated_as_dollars():
    cutoff = DOLLAR_REPORTING_CUTOFF.isoformat()
    total, convention, _ = normalize_13f_value_to_usd(
        500_000_000.0,
        filing_date="2023-02-14",
        report_date=cutoff,
    )
    assert convention == "dollars"
    assert total == 500_000_000.0


def test_pre_cutoff_thousands_default():
    # 200,000 raw pre-2023: $200M when read as thousands; $200K is implausible.
    total, convention, _ = normalize_13f_value_to_usd(
        200_000.0,
        filing_date="2017-08-14",
        report_date="2017-06-30",
    )
    assert convention == "thousands"
    assert total == 200_000_000.0


def test_recent_filing_with_old_reporting_unit_via_hint():
    # Even after the cutoff, if metadata explicitly says thousands, honor it.
    total, convention, _ = normalize_13f_value_to_usd(
        100_000.0,
        filing_date="2023-08-14",
        report_date="2023-06-30",
        metadata_unit_hint="K",
    )
    assert convention == "thousands"
    assert total == 100_000_000.0
