"""Background automation: detect new 13F filings, run dry-run rebalances, optional live execution.

Usage:
    python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run
    python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --schedule
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.config import AppConfig, load_config, normalize_cik
from core.portfolio_scaler import (
    ACTION_BUY,
    ACTION_SELL,
    compute_scaled_targets,
    determine_actions,
    estimate_buying_power_required,
)
from core.sec13f_fetcher import (
    FilingMetadata,
    fetch_holdings,
    fetch_latest_filing_metadata,
)
from core.trade_executor import (
    AlpacaClient,
    TradePreview,
    execute_preview,
)
from utils.helpers import (
    HISTORY_DIR,
    STATE_DIR,
    load_json,
    log_event,
    notify_all,
    save_json,
    utcnow_iso,
)

LAST_FILINGS_PATH = STATE_DIR / "last_filings.json"


@dataclass
class RebalanceResult:
    cik: str
    accession_number: Optional[str]
    is_new_filing: bool
    dry_run: bool
    paper: bool
    n_actions: int
    n_buys: int
    n_sells: int
    estimated_buy_notional: float
    preview_path: Optional[str]
    executed: bool = False
    order_results: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "cik": self.cik,
            "accession_number": self.accession_number,
            "is_new_filing": self.is_new_filing,
            "dry_run": self.dry_run,
            "paper": self.paper,
            "n_actions": self.n_actions,
            "n_buys": self.n_buys,
            "n_sells": self.n_sells,
            "estimated_buy_notional": self.estimated_buy_notional,
            "preview_path": self.preview_path,
            "executed": self.executed,
            "order_results": self.order_results,
            "notes": self.notes,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_last_seen_filing(cik: str) -> dict:
    cik10 = normalize_cik(cik)
    state = load_json(LAST_FILINGS_PATH, default={}) or {}
    return state.get(cik10, {})


def save_last_seen_filing(cik: str, metadata: FilingMetadata) -> None:
    cik10 = normalize_cik(cik)
    state = load_json(LAST_FILINGS_PATH, default={}) or {}
    state[cik10] = {
        "accession_number": metadata.accession_number,
        "filing_date": metadata.filing_date,
        "report_date": metadata.report_date,
        "form_type": metadata.form_type,
        "manager_name": metadata.manager_name,
        "checked_at": utcnow_iso(),
    }
    save_json(LAST_FILINGS_PATH, state)
    log_event(
        "state.last_filing_saved",
        cik=cik10,
        accession=metadata.accession_number,
    )


def check_latest_13f(cik: str, cfg: Optional[AppConfig] = None) -> FilingMetadata:
    cfg = cfg or load_config()
    return fetch_latest_filing_metadata(cik, cfg)


# ---------------------------------------------------------------------------
# Single rebalance run
# ---------------------------------------------------------------------------
def run_rebalance_once(
    cik: str,
    target_portfolio_size: float,
    *,
    dry_run: bool = True,
    full_account_rebalance: bool = False,
    include_options: bool = False,
    cfg: Optional[AppConfig] = None,
) -> RebalanceResult:
    """Run a single rebalance pass for ``cik``.

    Defaults to dry-run. Live execution requires:
      - dry_run=False
      - cfg.has_alpaca
      - if cfg.alpaca_paper is False: cfg.allow_live_trading must be True
    Automatic live execution additionally requires
    ``cfg.allow_live_auto_execute`` and ``cfg.auto_execute``.
    """
    cfg = cfg or load_config()
    cik10 = normalize_cik(cik)
    last_seen = load_last_seen_filing(cik10)

    try:
        metadata = fetch_latest_filing_metadata(cik10, cfg)
    except Exception as exc:
        log_event("auto.fetch_metadata_error", cik=cik10, error=str(exc))
        return RebalanceResult(
            cik=cik10,
            accession_number=None,
            is_new_filing=False,
            dry_run=dry_run,
            paper=cfg.alpaca_paper,
            n_actions=0,
            n_buys=0,
            n_sells=0,
            estimated_buy_notional=0.0,
            preview_path=None,
            error=f"Failed to fetch latest filing metadata: {exc}",
        )

    is_new_filing = (
        metadata.accession_number is not None
        and metadata.accession_number != last_seen.get("accession_number")
    )

    log_event(
        "auto.filing_check",
        cik=cik10,
        accession=metadata.accession_number,
        previous_accession=last_seen.get("accession_number"),
        is_new=is_new_filing,
    )

    try:
        holdings = fetch_holdings(cik10, cfg)
    except Exception as exc:
        log_event("auto.fetch_holdings_error", cik=cik10, error=str(exc))
        return RebalanceResult(
            cik=cik10,
            accession_number=metadata.accession_number,
            is_new_filing=is_new_filing,
            dry_run=dry_run,
            paper=cfg.alpaca_paper,
            n_actions=0,
            n_buys=0,
            n_sells=0,
            estimated_buy_notional=0.0,
            preview_path=None,
            error=f"Failed to fetch holdings: {exc}",
        )

    candidate_symbols = [
        t for t in holdings.holdings["ticker"].dropna().astype(str).str.upper().tolist() if t
    ]

    alpaca = AlpacaClient(cfg)
    prices = alpaca.get_prices(candidate_symbols)
    asset_meta = alpaca.get_asset_metadata(candidate_symbols)
    positions = alpaca.get_positions()
    account = alpaca.get_account() if cfg.has_alpaca else None

    targets = compute_scaled_targets(
        holdings.holdings,
        target_portfolio_size=float(target_portfolio_size),
        total_13f_value_usd=holdings.total_value_usd,
        prices=prices,
        asset_meta=asset_meta,
        include_options=include_options,
    )
    actions = determine_actions(
        targets,
        current_positions=positions,
        min_trade_notional=cfg.min_trade_notional,
        full_account_rebalance=full_account_rebalance,
    )

    preview = TradePreview(
        actions=actions,
        account=account,
        cik=cik10,
        accession_number=metadata.accession_number,
        target_portfolio_size=float(target_portfolio_size),
        full_account_rebalance=full_account_rebalance,
        paper=cfg.alpaca_paper,
    )

    n_buys = sum(1 for a in actions if a.side == ACTION_BUY)
    n_sells = sum(1 for a in actions if a.side == ACTION_SELL)
    est_buy = estimate_buying_power_required(actions)

    history_path: Optional[Path] = None
    if metadata.accession_number:
        history_path = HISTORY_DIR / f"rebalance_{cik10}_{metadata.accession_number}.json"
        save_json(history_path, preview.to_serializable())

    notes: list[str] = []
    executed = False
    order_results: list[dict] = []
    error: Optional[str] = None

    can_auto_execute = (
        cfg.has_alpaca
        and (cfg.alpaca_paper or (cfg.allow_live_trading and cfg.allow_live_auto_execute))
        and cfg.auto_execute
        and not dry_run
    )

    if not dry_run and not can_auto_execute:
        notes.append(
            "Live execution requested but safety gates not satisfied; falling back to dry-run."
        )
        dry_run = True

    if can_auto_execute:
        if account is not None and est_buy > account.buying_power and n_sells == 0:
            notes.append(
                f"Insufficient buying power (${account.buying_power:,.2f}) for "
                f"estimated buys (${est_buy:,.2f}); skipping execution."
            )
        else:
            try:
                results = execute_preview(preview, cfg, dry_run=False)
                order_results = [r.__dict__ for r in results]
                executed = True
            except Exception as exc:
                error = str(exc)
                log_event("auto.execute_error", cik=cik10, error=error)

    if is_new_filing and metadata.accession_number:
        save_last_seen_filing(cik10, metadata)
        try:
            notify_all(
                cfg=cfg,
                subject=f"[13F Mirror] New filing: {metadata.manager_name or cik10}",
                body=(
                    f"Manager: {metadata.manager_name or cik10}\n"
                    f"CIK: {cik10}\n"
                    f"Accession: {metadata.accession_number}\n"
                    f"Filing date: {metadata.filing_date}\n"
                    f"Report date: {metadata.report_date}\n"
                    f"Actions: {len(actions)} (buys={n_buys}, sells={n_sells})\n"
                    f"Estimated buy notional: ${est_buy:,.2f}\n"
                    f"Mode: paper={cfg.alpaca_paper}, dry_run={dry_run}, executed={executed}\n"
                    f"Preview: {history_path}"
                ),
            )
        except Exception as exc:
            log_event("auto.notify_error", error=str(exc))

    log_event(
        "auto.rebalance_complete",
        cik=cik10,
        accession=metadata.accession_number,
        is_new_filing=is_new_filing,
        n_actions=len(actions),
        n_buys=n_buys,
        n_sells=n_sells,
        executed=executed,
        dry_run=dry_run,
        paper=cfg.alpaca_paper,
    )

    return RebalanceResult(
        cik=cik10,
        accession_number=metadata.accession_number,
        is_new_filing=is_new_filing,
        dry_run=dry_run,
        paper=cfg.alpaca_paper,
        n_actions=len(actions),
        n_buys=n_buys,
        n_sells=n_sells,
        estimated_buy_notional=est_buy,
        preview_path=str(history_path) if history_path else None,
        executed=executed,
        order_results=order_results,
        notes=notes,
        error=error,
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def start_scheduler(
    cik: str,
    target_portfolio_size: float,
    *,
    interval_hours: int = 1,
    dry_run: bool = True,
    full_account_rebalance: bool = False,
    cfg: Optional[AppConfig] = None,
) -> None:
    """Start an APScheduler BlockingScheduler that runs `run_rebalance_once` periodically."""
    from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore

    cfg = cfg or load_config()
    interval_hours = max(1, int(interval_hours))
    scheduler = BlockingScheduler()

    def _job():
        try:
            res = run_rebalance_once(
                cik,
                target_portfolio_size,
                dry_run=dry_run,
                full_account_rebalance=full_account_rebalance,
                cfg=cfg,
            )
            log_event("scheduler.job_done", **res.to_dict())
        except Exception as exc:  # pragma: no cover
            log_event("scheduler.job_error", error=str(exc))

    scheduler.add_job(_job, "interval", hours=interval_hours, next_run_time=datetime.now(timezone.utc))
    log_event("scheduler.starting", cik=cik, interval_hours=interval_hours, dry_run=dry_run)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        log_event("scheduler.stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m core.auto_rebalancer",
        description="13F Mirror Trader — background rebalancer.",
    )
    p.add_argument("--cik", required=True, help="CIK of the 13F filer (10-digit, may include leading zeros).")
    p.add_argument("--portfolio-size", type=float, required=True, help="Target portfolio size in USD.")
    p.add_argument("--once", action="store_true", help="Run a single pass and exit.")
    p.add_argument("--schedule", action="store_true", help="Run on a recurring schedule (APScheduler).")
    p.add_argument("--interval-hours", type=int, default=None, help="Scheduler interval in hours (default from .env).")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                   help="Generate previews only; do not submit orders (default).")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                   help="Allow execution if all safety gates are satisfied.")
    p.add_argument("--full-rebalance", action="store_true",
                   help="Liquidate positions not present in the target portfolio (use with caution).")
    p.add_argument("--include-options", action="store_true",
                   help="Include options positions (still excluded from auto-trading).")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cfg = load_config()

    try:
        cik = normalize_cik(args.cik)
    except ValueError as exc:
        print(f"Invalid CIK: {exc}", file=sys.stderr)
        return 2

    if args.schedule:
        interval = args.interval_hours or cfg.rebalance_interval_hours
        start_scheduler(
            cik,
            args.portfolio_size,
            interval_hours=interval,
            dry_run=args.dry_run,
            full_account_rebalance=args.full_rebalance,
            cfg=cfg,
        )
        return 0

    if not args.once:
        # Default behavior is "--once" if neither flag specified.
        pass

    result = run_rebalance_once(
        cik,
        args.portfolio_size,
        dry_run=args.dry_run,
        full_account_rebalance=args.full_rebalance,
        include_options=args.include_options,
        cfg=cfg,
    )
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.error is None else 1


if __name__ == "__main__":
    sys.exit(main())
