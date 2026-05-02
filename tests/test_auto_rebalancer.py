from datetime import date
from types import SimpleNamespace

from core.auto_rebalancer import (
    build_parser,
    get_last_rebalanced_13f,
    load_last_seen_filing,
    parse_form_types,
    run_rebalance_once,
    run_scheduled_cycle,
    scan_latest_filings,
    save_last_seen_filing,
)
from core.sec13f_fetcher import FilingMetadata
from utils.helpers import write_json


def test_cli_defaults_to_dry_run_unless_execute_flag_is_present():
    parser = build_parser()

    default_args = parser.parse_args(["--once"])
    execute_args = parser.parse_args(["--once", "--execute"])

    assert default_args.execute is False
    assert default_args.dry_run is True
    assert execute_args.execute is True


def test_cli_accepts_scan_forms_for_comparable_filing_detection():
    parser = build_parser()

    args = parser.parse_args(["--once", "--scan-forms", "13F-HR,13F-HR/A,SC 13G"])

    assert args.scan_forms == "13F-HR,13F-HR/A,SC 13G"


def test_last_seen_state_is_keyed_by_cik_and_form_type(tmp_path, monkeypatch):
    monkeypatch.setattr("core.auto_rebalancer.STATE_PATH", tmp_path / "last_filings.json")
    metadata = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000002",
        filing_date=date(2026, 2, 11),
        report_date=date(2025, 12, 31),
        form_type="13F-HR",
    )

    save_last_seen_filing("2045724", metadata)
    state = load_last_seen_filing("0002045724")

    assert state["accession_number"] == "0002045724-26-000002"
    assert state["forms"]["13F-HR"]["accession_number"] == "0002045724-26-000002"


def test_non_13f_state_does_not_replace_rebalance_marker(tmp_path, monkeypatch):
    monkeypatch.setattr("core.auto_rebalancer.STATE_PATH", tmp_path / "last_filings.json")
    thirteen_f = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000002",
        filing_date=date(2026, 2, 11),
        report_date=date(2025, 12, 31),
        form_type="13F-HR",
    )
    comparable = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000003",
        filing_date=date(2026, 3, 1),
        report_date=date(2026, 3, 1),
        form_type="SC 13G",
    )

    save_last_seen_filing("2045724", thirteen_f, processed_for_rebalance=True)
    save_last_seen_filing("2045724", comparable)

    assert get_last_rebalanced_13f("0002045724")["accession_number"] == "0002045724-26-000002"


def test_legacy_13f_state_survives_comparable_filing_scan(tmp_path, monkeypatch):
    monkeypatch.setattr("core.auto_rebalancer.STATE_PATH", tmp_path / "last_filings.json")
    legacy_state = {
        "0002045724": {
            "accession_number": "0002045724-26-000002",
            "filing_date": "2026-02-11",
            "report_date": "2025-12-31",
            "form_type": "13F-HR",
            "checked_at": "2026-02-11T00:00:00+00:00",
        }
    }
    write_json(tmp_path / "last_filings.json", legacy_state)
    comparable = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000003",
        filing_date=date(2026, 3, 1),
        report_date=date(2026, 3, 1),
        form_type="SC 13G",
    )

    save_last_seen_filing("2045724", comparable)

    assert get_last_rebalanced_13f("0002045724")["accession_number"] == "0002045724-26-000002"


def test_latest_13f_amendment_is_rebalance_marker(tmp_path, monkeypatch):
    monkeypatch.setattr("core.auto_rebalancer.STATE_PATH", tmp_path / "last_filings.json")
    original = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000002",
        filing_date=date(2026, 2, 11),
        report_date=date(2025, 12, 31),
        form_type="13F-HR",
    )
    amendment = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000004",
        filing_date=date(2026, 2, 12),
        report_date=date(2025, 12, 31),
        form_type="13F-HR/A",
    )

    save_last_seen_filing("2045724", original, processed_for_rebalance=True)
    save_last_seen_filing("2045724", amendment, processed_for_rebalance=True)

    assert get_last_rebalanced_13f("0002045724")["accession_number"] == "0002045724-26-000004"


def test_cli_absent_scan_forms_preserves_config_fallback():
    parser = build_parser()
    args = parser.parse_args(["--scan-only"])

    assert args.scan_forms is None
    assert parse_form_types(args.scan_forms) == ["13F-HR", "13F-HR/A"]


def test_rebalance_skips_when_latest_metadata_matches_processed_amendment(monkeypatch, tmp_path):
    monkeypatch.setattr("core.auto_rebalancer.STATE_PATH", tmp_path / "last_filings.json")
    amendment = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000004",
        filing_date=date(2026, 2, 12),
        report_date=date(2025, 12, 31),
        form_type="13F-HR/A",
    )
    save_last_seen_filing("2045724", amendment, processed_for_rebalance=True)
    monkeypatch.setattr("core.auto_rebalancer.check_latest_13f", lambda cik: amendment)

    result = run_rebalance_once("0002045724", 100_000, dry_run=True)

    assert result.preview_path is None
    assert result.skipped_reason == "No new accession number detected."


def test_scan_only_13f_does_not_mark_rebalance_processed(tmp_path, monkeypatch):
    monkeypatch.setattr("core.auto_rebalancer.STATE_PATH", tmp_path / "last_filings.json")
    observed = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000005",
        filing_date=date(2026, 5, 14),
        report_date=date(2026, 3, 31),
        form_type="13F-HR",
    )

    save_last_seen_filing("2045724", observed)

    assert load_last_seen_filing("0002045724")["forms"]["13F-HR"]["accession_number"] == "0002045724-26-000005"
    assert get_last_rebalanced_13f("0002045724") == {}


def test_scan_latest_filings_saves_observed_state_without_rebalance_marker(tmp_path, monkeypatch):
    monkeypatch.setattr("core.auto_rebalancer.STATE_PATH", tmp_path / "last_filings.json")
    observed = FilingMetadata(
        manager_name="Example",
        cik="0002045724",
        accession_number="0002045724-26-000006",
        filing_date=date(2026, 5, 14),
        report_date=date(2026, 3, 31),
        form_type="13F-HR",
    )
    config = SimpleNamespace(edgar_identity="Tester tester@example.com", scan_form_types=["13F-HR"])
    monkeypatch.setattr("core.auto_rebalancer.load_config", lambda: config)
    monkeypatch.setattr("core.auto_rebalancer.get_latest_filing_metadata", lambda cik, settings: observed)
    monkeypatch.setattr("core.auto_rebalancer.send_notifications", lambda *args, **kwargs: ["skipped"])

    result = scan_latest_filings("0002045724", ["13F-HR"])

    assert result.new_filings == [observed]
    assert load_last_seen_filing("0002045724")["forms"]["13F-HR"]["accession_number"] == "0002045724-26-000006"
    assert get_last_rebalanced_13f("0002045724") == {}
