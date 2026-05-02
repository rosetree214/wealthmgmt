from __future__ import annotations

import math
from typing import Any

import pandas as pd


def calculate_scaled_targets(holdings: pd.DataFrame, target_portfolio_size: float) -> pd.DataFrame:
    if target_portfolio_size <= 0:
        raise ValueError("Target portfolio size must be positive")
    if holdings.empty:
        raise ValueError("No holdings available to scale")
    if "reported_value_usd" not in holdings.columns:
        raise ValueError("Holdings are missing reported_value_usd")

    df = holdings.copy()
    total_value = float(pd.to_numeric(df["reported_value_usd"], errors="coerce").fillna(0).sum())
    if total_value <= 0:
        raise ValueError("Cannot scale holdings with non-positive total value")

    df["fund_weight"] = pd.to_numeric(df["reported_value_usd"], errors="coerce").fillna(0) / total_value
    df["target_notional_usd"] = df["fund_weight"] * float(target_portfolio_size)
    return df.sort_values("fund_weight", ascending=False).reset_index(drop=True)


def add_target_shares(targets: pd.DataFrame) -> pd.DataFrame:
    df = targets.copy()
    target_shares = []
    warnings = []
    for _, row in df.iterrows():
        price = _as_float(row.get("current_price"))
        row_warnings = str(row.get("warnings") or "")
        if price is None or price <= 0:
            target_shares.append(0.0)
            warnings.append(_append_warning(row_warnings, "Missing price; skipped for trading"))
            continue
        raw_shares = float(row.get("target_notional_usd") or 0.0) / price
        if bool(row.get("fractionable", False)):
            target_shares.append(raw_shares)
        else:
            target_shares.append(math.floor(raw_shares))
        warnings.append(row_warnings)
    df["target_shares"] = target_shares
    df["warnings"] = warnings
    return df


def generate_trade_preview(
    targets: pd.DataFrame,
    current_positions: dict[str, float] | None = None,
    min_trade_notional: float = 10.0,
    full_account_rebalance: bool = False,
    extra_positions: dict[str, float] | None = None,
) -> pd.DataFrame:
    current_positions = {k.upper(): float(v) for k, v in (current_positions or {}).items()}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for _, row in targets.iterrows():
        symbol = str(row.get("ticker") or "").upper().strip()
        if not symbol:
            missing_row = row.copy()
            rows.append(_preview_row(missing_row, "", 0.0, min_trade_notional))
            continue
        price = _as_float(row.get("current_price"))
        if price is None or price <= 0:
            rows.append(_preview_row(row, symbol, current_positions.get(symbol, 0.0), min_trade_notional))
            continue
        seen.add(symbol)
        rows.append(_preview_row(row, symbol, current_positions.get(symbol, 0.0), min_trade_notional))

    if full_account_rebalance:
        for symbol, shares in (extra_positions or current_positions).items():
            symbol = symbol.upper()
            if symbol in seen or shares <= 0:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "company": "",
                    "side": "sell",
                    "current_shares": shares,
                    "target_shares": 0.0,
                    "delta_shares": -shares,
                    "estimated_price": None,
                    "estimated_notional": None,
                    "order_type": "market",
                    "time_in_force": "day",
                    "fractionable": False,
                    "reason": "Full-account rebalance: symbol not in 13F target portfolio",
                    "warnings": "Price required before execution",
                }
            )

    if not rows:
        return pd.DataFrame()

    preview = pd.DataFrame(rows)
    order = {"sell": 0, "buy": 1, "hold": 2, "skip": 3}
    preview["_sort"] = preview["side"].map(order).fillna(9)
    return preview.sort_values(["_sort", "symbol"]).drop(columns=["_sort"]).reset_index(drop=True)


def _preview_row(row: pd.Series, symbol: str, current_shares: float, min_trade_notional: float) -> dict[str, Any]:
    warnings: list[str] = []
    existing_warnings = str(row.get("warnings") or "").strip()
    if existing_warnings:
        warnings.append(existing_warnings)

    price = _as_float(row.get("current_price"))
    trade_eligible = bool(row.get("is_trade_eligible", True))
    tradable = bool(row.get("tradable", True))
    active = str(row.get("asset_status") or "active").lower() == "active"
    fractionable = bool(row.get("fractionable", False))

    if not trade_eligible:
        warnings.append("Holding is not trade eligible")
    if not tradable:
        warnings.append("Asset is not tradable")
    if not active:
        warnings.append("Asset is not active")
    if not symbol:
        target_shares = 0.0
        delta = 0.0
        side = "skip"
        notional = None
        reason = "Missing ticker"
        warnings.append("Missing ticker; skipped for trading")
    elif price is None or price <= 0:
        target_shares = 0.0
        delta = 0.0
        side = "skip"
        notional = None
        reason = "Missing price"
        warnings.append("Missing price; skipped for trading")
    else:
        raw_target_shares = float(row.get("target_notional_usd") or 0.0) / price
        target_shares = raw_target_shares if fractionable else math.floor(raw_target_shares)
        delta = target_shares - current_shares
        notional = abs(delta) * price
        if not trade_eligible or not tradable or not active:
            side = "skip"
            reason = "Safety checks failed"
        elif notional < min_trade_notional:
            side = "hold"
            reason = f"Below minimum trade threshold ${min_trade_notional:,.2f}"
        elif delta > 0:
            side = "buy"
            reason = "Target shares exceed current shares"
        elif delta < 0:
            side = "sell"
            reason = "Current shares exceed target shares"
            if abs(delta) > current_shares:
                delta = -current_shares
                warnings.append("Sell clipped to current position")
        else:
            side = "hold"
            reason = "Already at target"

    return {
        "symbol": symbol,
        "company": row.get("company_name") or row.get("issuer") or "",
        "side": side,
        "current_shares": current_shares,
        "target_shares": target_shares,
        "delta_shares": delta,
        "estimated_price": price,
        "estimated_notional": notional,
        "order_type": "market",
        "time_in_force": "day",
        "fractionable": fractionable,
        "reason": reason,
        "warnings": "; ".join(dict.fromkeys(warnings)),
    }


def _as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _append_warning(existing: str, warning: str) -> str:
    return f"{existing}; {warning}" if existing else warning
