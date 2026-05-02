"""Background filing detection and dry-run-first rebalance automation."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler

from core.config import load_config
from core.portfolio_scaler import calculate_scaled_targets
from core.sec13f_fetcher import FilingMetadata, fetch_latest_13f_holdings, get_latest_filing_metadata
from core.trade_executor import AlpacaService
from utils.helpers import dataframe_hash, log_event, normalize_cik, read_json, send_notifications, write_json

STATE_PATH = Path("state/last_filings.json")


@dataclass
class RebalanceResult:
    cik: str
    accession_number: str | None
    dry_run: bool
    preview_path: str | None
    executed_orders: list[dict[str, Any]]
    warnings: list[str]
    skipped_reason: str | None = None


def check_latest_13f(cik: str) -> FilingMetadata:
    return get_latest_filing_metadata(cik)


def load_last_seen_filing(cik: str) -> dict:
    state = read_json(STATE_PATH, default={})
    return state.get(normalize_cik(cik), {})


def save_last_seen_filing(cik: str, metadata: FilingMetadata) -> None:
    normalized = normalize_cik(cik)
    state = read_json(STATE_PATH, default={})
    state[normalized] = {
        "accession_number": metadata.accession_number,
        "filing_date": metadata.filing_date.isoformat() if metadata.filing_date else None,
        "report_date": metadata.report_date.isoformat() if metadata.report_date else None,
        "form_type": metadata.form_type,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(STATE_PATH, state)


def run_rebalance_once(
    cik: str,
    target_portfolio_size: float,
    dry_run: bool = True,
) -> RebalanceResult:
    config = load_config()
    normalized = normalize_cik(cik)
    latest = check_latest_13f(normalized)
    last_seen = load_last_seen_filing(normalized)

    log_event(
        "filing_checked",
        cik=normalized,
        accession_number=latest.accession_number,
        paper_mode=config.alpaca_paper,
        dry_run=dry_run,
        details={"last_seen": last_seen.get("accession_number")},
    )

    if last_seen.get("accession_number") == latest.accession_number:
        save_last_seen_filing(normalized, latest)
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
    alpaca = AlpacaService(config)
    prices = alpaca.get_prices(scaled["ticker"].dropna().astype(str).tolist())
    if prices:
        scaled["current_price"] = scaled["ticker"].map(lambda symbol: prices.get(str(symbol), {}).get("price"))
        scaled["price_source"] = scaled["ticker"].map(lambda symbol: prices.get(str(symbol), {}).get("source"))
        scaled["price_timestamp"] = scaled["ticker"].map(
            lambda symbol: prices.get(str(symbol), {}).get("timestamp")
        )

    assets = alpaca.get_asset_metadata(scaled["ticker"].dropna().astype(str).tolist())
    for column in ["tradable", "asset_status", "fractionable"]:
        scaled[column] = scaled["ticker"].map(
            lambda symbol, col=column: assets.get(str(symbol), {}).get(col)
        )
    scaled["tradable"] = scaled["tradable"].fillna(False)
    scaled["asset_status"] = scaled["asset_status"].fillna("unknown")
    scaled["fractionable"] = scaled["fractionable"].fillna(False)

    positions = alpaca.get_positions()
    preview = alpaca.build_trade_preview(scaled, positions)
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
    save_last_seen_filing(normalized, latest)

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
    if not dry_run and config.auto_execute:
        executed = alpaca.submit_orders(preview)
    else:
        warnings.append("Dry-run mode is enabled; no trades were submitted.")

    return RebalanceResult(
        cik=normalized,
        accession_number=latest.accession_number,
        dry_run=dry_run,
        preview_path=str(preview_path),
        executed_orders=executed,
        warnings=warnings,
    )


def start_scheduler(interval_hours: int = 1) -> None:
    config = load_config()
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_rebalance_once,
        "interval",
        hours=interval_hours,
        args=[config.default_cik, config.target_portfolio_size, True],
        id=f"rebalance-{config.default_cik}",
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
    parser.add_argument("--dry-run", action="store_true", help="Generate previews without trading")
    parser.add_argument("--schedule", action="store_true", help="Start local APScheduler loop")
    parser.add_argument("--interval-hours", type=int, default=1, help="Scheduler interval")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.once:
        result = run_rebalance_once(args.cik, args.portfolio_size, dry_run=args.dry_run or True)
        print(result)
        return
    if args.schedule:
        start_scheduler(args.interval_hours)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
