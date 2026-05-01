"""Portfolio scaling math: weights -> notional -> target shares -> action."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Optional

import pandas as pd


# Action codes -------------------------------------------------------------
ACTION_BUY = "BUY"
ACTION_SELL = "SELL"
ACTION_HOLD = "HOLD"
ACTION_SKIP = "SKIP"


@dataclass
class ScaledTarget:
    ticker: str
    issuer: Optional[str]
    cusip: Optional[str]
    fund_weight: float
    target_notional_usd: float
    target_shares: float
    price: Optional[float]
    price_source: Optional[str]
    fractionable: Optional[bool]
    tradable: Optional[bool]
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "issuer": self.issuer,
            "cusip": self.cusip,
            "fund_weight": self.fund_weight,
            "target_notional_usd": self.target_notional_usd,
            "target_shares": self.target_shares,
            "price": self.price,
            "price_source": self.price_source,
            "fractionable": self.fractionable,
            "tradable": self.tradable,
            "warning": self.warning,
        }


@dataclass
class TradeAction:
    ticker: str
    issuer: Optional[str]
    side: str  # BUY / SELL / HOLD / SKIP
    current_shares: float
    target_shares: float
    delta_shares: float
    price: Optional[float]
    estimated_notional: float
    fractionable: Optional[bool]
    tradable: Optional[bool]
    reason: str
    warnings: list[str] = field(default_factory=list)
    order_type: str = "market"
    time_in_force: str = "day"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "issuer": self.issuer,
            "side": self.side,
            "current_shares": self.current_shares,
            "target_shares": self.target_shares,
            "delta_shares": self.delta_shares,
            "price": self.price,
            "estimated_notional": self.estimated_notional,
            "fractionable": self.fractionable,
            "tradable": self.tradable,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
        }


# ---------------------------------------------------------------------------
# Weight + target notional
# ---------------------------------------------------------------------------
def compute_scaled_targets(
    holdings_df: pd.DataFrame,
    *,
    target_portfolio_size: float,
    total_13f_value_usd: float,
    prices: Mapping[str, "PriceQuote"],  # noqa: F821 - forward typed
    asset_meta: Mapping[str, "AssetMeta"],  # noqa: F821 - forward typed
    include_options: bool = False,
) -> list[ScaledTarget]:
    """Compute scaled target shares for each tradable holding.

    Skips holdings without a ticker, options (unless ``include_options`` is True),
    and any symbol without a usable current price.
    """
    if target_portfolio_size <= 0:
        raise ValueError("target_portfolio_size must be positive")
    if total_13f_value_usd <= 0:
        raise ValueError("total_13f_value_usd must be positive")

    out: list[ScaledTarget] = []
    seen: dict[str, ScaledTarget] = {}

    def _norm_str(value: object) -> str:
        if value is None:
            return ""
        try:
            if pd.isna(value):  # type: ignore[arg-type]
                return ""
        except (TypeError, ValueError):
            pass
        return str(value).strip().upper()

    for _, row in holdings_df.iterrows():
        ticker = _norm_str(row.get("ticker"))
        put_call = _norm_str(row.get("put_call"))
        value_usd = float(row.get("value_usd") or 0.0)
        if value_usd <= 0:
            continue
        if not ticker:
            continue
        if put_call in {"PUT", "CALL"} and not include_options:
            continue

        weight = value_usd / total_13f_value_usd
        target_notional = target_portfolio_size * weight

        quote = prices.get(ticker)
        meta = asset_meta.get(ticker)
        price = getattr(quote, "price", None) if quote is not None else None
        price_source = getattr(quote, "source", None) if quote is not None else None

        if price is None or price <= 0:
            target = ScaledTarget(
                ticker=ticker,
                issuer=row.get("issuer"),
                cusip=row.get("cusip"),
                fund_weight=weight,
                target_notional_usd=target_notional,
                target_shares=0.0,
                price=None,
                price_source=None,
                fractionable=getattr(meta, "fractionable", None) if meta else None,
                tradable=getattr(meta, "tradable", None) if meta else None,
                warning="No usable price; skipped for trading.",
            )
        else:
            shares = target_notional / price
            target = ScaledTarget(
                ticker=ticker,
                issuer=row.get("issuer"),
                cusip=row.get("cusip"),
                fund_weight=weight,
                target_notional_usd=target_notional,
                target_shares=shares,
                price=price,
                price_source=price_source,
                fractionable=getattr(meta, "fractionable", None) if meta else None,
                tradable=getattr(meta, "tradable", None) if meta else None,
                warning=None,
            )

        # Aggregate duplicate tickers (e.g., multi-class) by summing weights.
        if ticker in seen:
            existing = seen[ticker]
            combined_weight = existing.fund_weight + target.fund_weight
            combined_notional = existing.target_notional_usd + target.target_notional_usd
            combined_shares = (
                (combined_notional / target.price) if target.price else 0.0
            )
            seen[ticker] = ScaledTarget(
                ticker=ticker,
                issuer=existing.issuer or target.issuer,
                cusip=existing.cusip or target.cusip,
                fund_weight=combined_weight,
                target_notional_usd=combined_notional,
                target_shares=combined_shares,
                price=target.price or existing.price,
                price_source=target.price_source or existing.price_source,
                fractionable=target.fractionable
                if target.fractionable is not None
                else existing.fractionable,
                tradable=target.tradable if target.tradable is not None else existing.tradable,
                warning=target.warning or existing.warning,
            )
        else:
            seen[ticker] = target

    out = sorted(seen.values(), key=lambda t: t.fund_weight, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Action calculation
# ---------------------------------------------------------------------------
def determine_actions(
    targets: list[ScaledTarget],
    *,
    current_positions: Mapping[str, float],
    min_trade_notional: float,
    full_account_rebalance: bool = False,
) -> list[TradeAction]:
    """Compare targets to current positions and produce trade actions.

    - Mirror-only (default): only adjust symbols in ``targets``. Symbols held
      outside the target set are left alone.
    - Full-account rebalance: additionally generate SELL actions to liquidate
      symbols not present in ``targets``.
    """
    actions: list[TradeAction] = []
    target_symbols = {t.ticker for t in targets}

    for t in targets:
        current = float(current_positions.get(t.ticker, 0.0))
        warnings = []
        if t.warning:
            warnings.append(t.warning)
        if t.tradable is False:
            warnings.append("Asset not tradable on Alpaca.")
        if t.price is None or t.price <= 0:
            actions.append(
                TradeAction(
                    ticker=t.ticker,
                    issuer=t.issuer,
                    side=ACTION_SKIP,
                    current_shares=current,
                    target_shares=t.target_shares,
                    delta_shares=0.0,
                    price=None,
                    estimated_notional=0.0,
                    fractionable=t.fractionable,
                    tradable=t.tradable,
                    reason="No usable price",
                    warnings=warnings,
                )
            )
            continue

        target_shares = t.target_shares
        if t.fractionable is False:
            target_shares = math.floor(target_shares)

        delta = target_shares - current
        notional = abs(delta) * t.price

        if notional < float(min_trade_notional):
            actions.append(
                TradeAction(
                    ticker=t.ticker,
                    issuer=t.issuer,
                    side=ACTION_HOLD,
                    current_shares=current,
                    target_shares=target_shares,
                    delta_shares=delta,
                    price=t.price,
                    estimated_notional=notional,
                    fractionable=t.fractionable,
                    tradable=t.tradable,
                    reason=f"Below min trade notional (${min_trade_notional:.2f})",
                    warnings=warnings,
                )
            )
            continue

        side = ACTION_BUY if delta > 0 else ACTION_SELL

        # SELL safety: never sell more than current position.
        if side == ACTION_SELL and abs(delta) > current:
            delta = -current
            notional = abs(delta) * t.price
            warnings.append(
                "Sell quantity capped at current position size."
            )

        if t.tradable is False:
            actions.append(
                TradeAction(
                    ticker=t.ticker,
                    issuer=t.issuer,
                    side=ACTION_SKIP,
                    current_shares=current,
                    target_shares=target_shares,
                    delta_shares=delta,
                    price=t.price,
                    estimated_notional=notional,
                    fractionable=t.fractionable,
                    tradable=t.tradable,
                    reason="Asset is not tradable",
                    warnings=warnings,
                )
            )
            continue

        actions.append(
            TradeAction(
                ticker=t.ticker,
                issuer=t.issuer,
                side=side,
                current_shares=current,
                target_shares=target_shares,
                delta_shares=delta,
                price=t.price,
                estimated_notional=notional,
                fractionable=t.fractionable,
                tradable=t.tradable,
                reason=("Increase exposure" if side == ACTION_BUY else "Reduce exposure"),
                warnings=warnings,
            )
        )

    if full_account_rebalance:
        for symbol, qty in current_positions.items():
            if symbol in target_symbols or qty <= 0:
                continue
            actions.append(
                TradeAction(
                    ticker=symbol,
                    issuer=None,
                    side=ACTION_SELL,
                    current_shares=float(qty),
                    target_shares=0.0,
                    delta_shares=-float(qty),
                    price=None,
                    estimated_notional=0.0,
                    fractionable=None,
                    tradable=None,
                    reason="Full-account rebalance: position not in target portfolio",
                    warnings=["Price unknown until execution; estimated notional shown as 0."],
                )
            )

    # Sort: SELLs first, then BUYs, then HOLD/SKIP.
    side_order = {ACTION_SELL: 0, ACTION_BUY: 1, ACTION_HOLD: 2, ACTION_SKIP: 3}
    actions.sort(key=lambda a: (side_order.get(a.side, 9), -abs(a.estimated_notional)))
    return actions


def estimate_buying_power_required(actions: list[TradeAction]) -> float:
    """Total dollar amount required for BUY actions, not counting expected SELL proceeds."""
    return sum(a.estimated_notional for a in actions if a.side == ACTION_BUY)


def estimate_sell_proceeds(actions: list[TradeAction]) -> float:
    return sum(a.estimated_notional for a in actions if a.side == ACTION_SELL)
