"""Background filing detection and dry-run-first rebalance automation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from apscheduler.schedulers.blocking import BlockingScheduler

from core.config import load_config
from core.portfolio_scaler import calculate_scaled_targets
from core.sec13f_fetcher import FilingMetadata, fetch_latest_13f_holdings, get_latest_filing_metadata
from core.trade_executor import get_broker_service
from utils.helpers import dataframe_hash, log_event, normalize_cik, read_json, send_notifications, write_json

STATE_PATH = Path("state/last_filings.json")
DEFAULT_SCAN_FORMS = ("13F-HR", "13F-HR/A")


@dataclass
class RebalanceResult:
    cik: str
    accession_number: str | None
    dry_run: bool
    preview_path: str | None
    executed_orders: list[dict[str, Any]]
    warnings: list[str]
    skipped_reason: str | None = None


@dataclass
class FilingScanResult:
    cik: str
    form_types: list[str]
    new_filings: list[FilingMetadata]
    warnings: list[str]


def check_latest_13f(cik: str) -> FilingMetadata:
    return get_latest_filing_metadata(cik)


def parse_form_types(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return list(DEFAULT_SCAN_FORMS)
    if isinstance(value, str):
        raw_forms = value.split(",")
    else:
        raw_forms = list(value)
    forms = []
    for form in raw_forms:
        normalized = str(form).strip().upper()
        if normalized and normalized not in forms:
            forms.append(normalized)
    return forms or list(DEFAULT_SCAN_FORMS)


def load_last_seen_filing(cik: str) -> dict:
    state = read_json(STATE_PATH, default={})
    return state.get(normalize_cik(cik), {})


def save_last_seen_filing(cik: str, metadata: FilingMetadata, *, processed_for_rebalance: bool = False) -> None:
    normalized = normalize_cik(cik)
    state = read_json(STATE_PATH, default={})
    existing = state.get(normalized, {})
    form_entry = {
        "accession_number": metadata.accession_number,
        "filing_date": metadata.filing_date.isoformat() if metadata.filing_date else None,
        "report_date": metadata.report_date.isoformat() if metadata.report_date else None,
        "form_type": metadata.form_type,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    forms = existing.get("forms", {})
    rebalanced_forms = existing.get("rebalanced_forms", {})
    if not forms and str(existing.get("form_type", "")).startswith("13F-HR"):
        legacy_entry = {
            "accession_number": existing.get("accession_number"),
            "filing_date": existing.get("filing_date"),
            "report_date": existing.get("report_date"),
            "form_type": existing.get("form_type"),
            "checked_at": existing.get("checked_at"),
        }
        forms[existing["form_type"]] = legacy_entry
        rebalanced_forms[existing["form_type"]] = legacy_entry
    forms[metadata.form_type] = form_entry
    if processed_for_rebalance and metadata.form_type.startswith("13F-HR"):
        rebalanced_forms[metadata.form_type] = form_entry
    state[normalized] = {**form_entry, "forms": forms, "rebalanced_forms": rebalanced_forms}
    write_json(STATE_PATH, state)


def get_last_rebalanced_13f(cik: str) -> dict:
    state = load_last_seen_filing(cik)
    forms = state.get("rebalanced_forms") or {}
    candidates = [
        entry
        for form_type, entry in forms.items()
        if str(form_type).startswith("13F-HR") and entry.get("accession_number")
    ]
    if candidates:
        return sorted(candidates, key=lambda entry: entry.get("filing_date") or entry.get("checked_at") or "")[-1]
    if not state.get("forms") and str(state.get("form_type", "")).startswith("13F-HR"):
        return state
    return {}


def save_last_rebalanced_13f(cik: str, metadata: FilingMetadata) -> None:
    save_last_seen_filing(cik, metadata, processed_for_rebalance=True)


def _latest_from_submissions(cik: str, form_type: str, edgar_identity: str) -> FilingMetadata | None:
    normalized = normalize_cik(cik)
    url = f"https://data.sec.gov/submissions/CIK{normalized}.json"
    response = requests.get(url, headers={"User-Agent": edgar_identity}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    recent = payload.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    target = form_type.upper()
    for index, form in enumerate(forms):
        current = str(form).upper()
        if current == target or (target == "13F-HR" and current.startswith("13F-HR")):
            return FilingMetadata(
                manager_name=payload.get("name") or normalized,
                cik=normalized,
                accession_number=recent.get("accessionNumber", [""])[index],
                filing_date=_date_at(recent.get("filingDate", []), index),
                report_date=_date_at(recent.get("reportDate", []), index),
                form_type=str(form),
                is_amendment="/A" in str(form),
                source="sec_submissions_json",
            )
    return None


def _date_at(values: list[Any], index: int):
    from utils.helpers import parse_date

    try:
        return parse_date(values[index])
    except (IndexError, TypeError):
        return None


def scan_latest_filings(cik: str, form_types: list[str] | None = None) -> FilingScanResult:
    config = load_config()
    normalized = normalize_cik(cik)
    forms = parse_form_types(form_types or config.scan_form_types)
    if not config.edgar_identity:
        raise ValueError("EDGAR_IDENTITY is required to scan SEC filings")

    last_seen = load_last_seen_filing(normalized)
    last_seen_forms = last_seen.get("forms", {})
    new_filings: list[FilingMetadata] = []
    warnings: list[str] = []
    seen_accessions: set[str] = set()

    for form_type in forms:
        try:
            metadata = get_latest_filing_metadata(normalized, settings=config) if form_type.startswith("13F-HR") else None
            if metadata is None:
                metadata = _latest_from_submissions(normalized, form_type, config.edgar_identity)
            if metadata is None:
                warnings.append(f"No latest filing found for {form_type}")
                continue
            if metadata.accession_number in seen_accessions:
                continue
            seen_accessions.add(metadata.accession_number)
            previous = last_seen_forms.get(metadata.form_type) or last_seen_forms.get(form_type) or {}
            if previous.get("accession_number") != metadata.accession_number:
                new_filings.append(metadata)
                log_event(
                    "new_filing_detected",
                    cik=normalized,
                    accession_number=metadata.accession_number,
                    details={"form_type": metadata.form_type},
                )
            save_last_seen_filing(normalized, metadata)
            last_seen_forms = load_last_seen_filing(normalized).get("forms", {})
        except Exception as exc:
            warnings.append(f"{form_type}: {exc}")

    if new_filings:
        send_notifications(
            subject=f"13F Mirror Trader: {len(new_filings)} new filing(s) for {normalized}",
            body="\n".join(
                f"{filing.form_type} {filing.accession_number} filed {filing.filing_date}"
                for filing in new_filings
            ),
            config=config,
        )

    return FilingScanResult(cik=normalized, form_types=forms, new_filings=new_filings, warnings=warnings)


def run_rebalance_once(
    cik: str,
    target_portfolio_size: float,
    dry_run: bool = True,
) -> RebalanceResult:
    config = load_config()
    normalized = normalize_cik(cik)
    latest = check_latest_13f(normalized)
    last_seen = get_last_rebalanced_13f(normalized)

    log_event(
        "filing_checked",
        cik=normalized,
        accession_number=latest.accession_number,
        paper_mode=config.alpaca_paper,
        dry_run=dry_run,
        details={"last_seen": last_seen.get("accession_number")},
    )

    if last_seen.get("accession_number") == latest.accession_number:
        save_last_rebalanced_13f(normalized, latest)
        return RebalanceResult(
            cik=normalized,
            accession_number=latest.accession_number,
            dry_run=dry_run,
            preview_path=None,
            executed_orders=[],
            warnings=[],
            skipped_reason="No new accession number detected.",
        )

    latest, holdings = fetch_latest_13f_holdings(normalized, settings=config)
    scaled = calculate_scaled_targets(holdings, target_portfolio_size)
    broker = get_broker_service(config)
    prices = broker.get_prices(scaled["ticker"].dropna().astype(str).tolist())
    if prices:
        scaled["current_price"] = scaled["ticker"].map(lambda symbol: prices.get(str(symbol), {}).get("price"))
        scaled["price_source"] = scaled["ticker"].map(lambda symbol: prices.get(str(symbol), {}).get("source"))
        scaled["price_timestamp"] = scaled["ticker"].map(
            lambda symbol: prices.get(str(symbol), {}).get("timestamp")
        )

    assets = broker.get_asset_metadata(scaled["ticker"].dropna().astype(str).tolist())
    for column in ["tradable", "asset_status", "fractionable"]:
        scaled[column] = scaled["ticker"].map(
            lambda symbol, col=column: assets.get(str(symbol), {}).get(col)
        )
    scaled["tradable"] = scaled["tradable"].fillna(False)
    scaled["asset_status"] = scaled["asset_status"].fillna("unknown")
    scaled["fractionable"] = scaled["fractionable"].fillna(False)

    positions = broker.get_positions()
    preview = broker.build_trade_preview(scaled, positions)
    accession_safe = latest.accession_number.replace("-", "")
    preview_path = Path("history") / f"rebalance_{normalized}_{accession_safe}.json"
    payload = {
        "metadata": latest.to_dict(),
        "target_portfolio_size": target_portfolio_size,
        "dry_run": dry_run,
        "preview_hash": dataframe_hash(preview.to_dict(orient="records")),
        "preview": preview.to_dict(orient="records"),
    }
    write_json(preview_path, payload)
    save_last_rebalanced_13f(normalized, latest)

    log_event(
        "rebalance_preview_created",
        cik=normalized,
        accession_number=latest.accession_number,
        paper_mode=config.alpaca_paper,
        dry_run=dry_run,
        details={"preview_path": str(preview_path), "orders": len(preview)},
    )
    send_notifications(
        subject=f"13F Mirror Trader: new filing {latest.accession_number}",
        body=(
            f"New 13F filing detected for CIK {normalized}: {latest.accession_number}.\n"
            f"Dry-run preview saved to {preview_path}."
        ),
        config=config,
    )

    executed: list[dict[str, Any]] = []
    warnings: list[str] = []
    result_dry_run = dry_run or not config.auto_execute
    if not dry_run and config.auto_execute:
        executed = broker.submit_orders(preview)
    elif not dry_run and not config.auto_execute:
        warnings.append("Execution was requested, but AUTO_EXECUTE is false; preview only.")
    else:
        warnings.append("Dry-run mode is enabled; no trades were submitted.")

    return RebalanceResult(
        cik=normalized,
        accession_number=latest.accession_number,
        dry_run=result_dry_run,
        preview_path=str(preview_path),
        executed_orders=executed,
        warnings=warnings,
    )


def run_scheduled_cycle(cik: str, target_portfolio_size: float, scan_form_types: list[str] | None = None) -> dict[str, Any]:
    rebalance = run_rebalance_once(cik, target_portfolio_size, dry_run=True)
    scan = scan_latest_filings(cik, scan_form_types)
    return {"rebalance": rebalance, "scan": scan}


def start_scheduler(interval_hours: int = 1) -> None:
    config = load_config()
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_scheduled_cycle,
        "interval",
        hours=interval_hours,
        args=[config.default_cik, config.target_portfolio_size, config.scan_form_types],
        id=f"filing-cycle-{config.default_cik}",
        replace_existing=True,
    )
    log_event(
        "scheduler_started",
        cik=config.default_cik,
        paper_mode=config.alpaca_paper,
        dry_run=True,
        details={"interval_hours": interval_hours},
    )
    scheduler.start()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="13F Mirror Trader automation job")
    parser.add_argument("--cik", default="0002045724", help="Manager CIK")
    parser.add_argument("--portfolio-size", type=float, default=10_000, help="Target portfolio size")
    parser.add_argument("--once", action="store_true", help="Run one filing check and exit")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only scan configured filing forms and notify/state-update; do not generate rebalance previews.",
    )
    parser.add_argument(
        "--scan-forms",
        default=None,
        help="Comma-separated SEC form types to scan, e.g. 13F-HR,13F-HR/A,SC 13G.",
    )
    parser.add_argument("--dry-run", action="store_true", default=True, help="Generate previews without trading")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Opt into guarded execution. Requires AUTO_EXECUTE=true and trading safety gates.",
    )
    parser.add_argument("--schedule", action="store_true", help="Start local APScheduler loop")
    parser.add_argument("--interval-hours", type=int, default=1, help="Scheduler interval")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    scan_forms = parse_form_types(args.scan_forms) if args.scan_forms is not None else None
    if args.scan_only:
        result = scan_latest_filings(args.cik, scan_forms)
        print(result)
        return
    if args.once:
        result = run_rebalance_once(args.cik, args.portfolio_size, dry_run=not args.execute)
        scan_latest_filings(args.cik, scan_forms)
        print(result)
        return
    if args.schedule:
        start_scheduler(args.interval_hours)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
