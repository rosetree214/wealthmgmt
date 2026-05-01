"""Alpaca integration: account, asset metadata, positions, prices, order submission."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from core.config import AppConfig
from core.portfolio_scaler import (
    ACTION_BUY,
    ACTION_SELL,
    TradeAction,
)
from utils.helpers import hash_payload, log_event


# ---------------------------------------------------------------------------
# Lightweight DTOs (avoid leaking SDK objects across module boundaries)
# ---------------------------------------------------------------------------
@dataclass
class AccountSnapshot:
    paper: bool
    account_number: Optional[str]
    status: Optional[str]
    buying_power: float
    portfolio_value: float
    cash: float
    equity: float
    currency: str = "USD"


@dataclass
class AssetMeta:
    symbol: str
    tradable: bool
    fractionable: bool
    status: str
    asset_class: str
    name: Optional[str] = None


@dataclass
class PriceQuote:
    symbol: str
    price: float
    source: str  # "alpaca" or "yfinance"
    timestamp: str


@dataclass
class OrderResult:
    symbol: str
    side: str
    qty: float
    notional: Optional[float]
    submitted: bool
    order_id: Optional[str]
    status: Optional[str]
    error: Optional[str]


@dataclass
class TradePreview:
    actions: list[TradeAction]
    account: Optional[AccountSnapshot]
    cik: str
    accession_number: Optional[str]
    target_portfolio_size: float
    full_account_rebalance: bool
    paper: bool
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def hash(self) -> str:
        return hash_payload(self.to_serializable())

    def to_serializable(self) -> dict:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "account": self.account.__dict__ if self.account else None,
            "cik": self.cik,
            "accession_number": self.accession_number,
            "target_portfolio_size": self.target_portfolio_size,
            "full_account_rebalance": self.full_account_rebalance,
            "paper": self.paper,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Trading client wrapper
# ---------------------------------------------------------------------------
class AlpacaClient:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._trading = None
        self._data = None

    @property
    def configured(self) -> bool:
        return self.cfg.has_alpaca

    def _trading_client(self):
        if self._trading is None:
            from alpaca.trading.client import TradingClient  # type: ignore

            self._trading = TradingClient(
                self.cfg.alpaca_api_key,
                self.cfg.alpaca_secret_key,
                paper=self.cfg.alpaca_paper,
            )
        return self._trading

    def _data_client(self):
        if self._data is None:
            from alpaca.data.historical import StockHistoricalDataClient  # type: ignore

            self._data = StockHistoricalDataClient(
                self.cfg.alpaca_api_key, self.cfg.alpaca_secret_key
            )
        return self._data

    # -------- Account / positions --------
    def get_account(self) -> Optional[AccountSnapshot]:
        if not self.configured:
            return None
        client = self._trading_client()
        acct = client.get_account()
        return AccountSnapshot(
            paper=self.cfg.alpaca_paper,
            account_number=getattr(acct, "account_number", None),
            status=str(getattr(acct, "status", "")) or None,
            buying_power=float(getattr(acct, "buying_power", 0) or 0),
            portfolio_value=float(getattr(acct, "portfolio_value", 0) or 0),
            cash=float(getattr(acct, "cash", 0) or 0),
            equity=float(getattr(acct, "equity", 0) or 0),
            currency=str(getattr(acct, "currency", "USD") or "USD"),
        )

    def get_positions(self) -> dict[str, float]:
        if not self.configured:
            return {}
        client = self._trading_client()
        try:
            positions = client.get_all_positions()
        except Exception as exc:
            log_event("alpaca.positions_error", error=str(exc))
            return {}
        out: dict[str, float] = {}
        for p in positions:
            symbol = str(getattr(p, "symbol", "")).upper()
            qty = float(getattr(p, "qty", 0) or 0)
            if symbol:
                out[symbol] = qty
        return out

    # -------- Asset metadata --------
    def get_asset_metadata(self, symbols: Iterable[str]) -> dict[str, AssetMeta]:
        if not self.configured:
            return {}
        client = self._trading_client()
        out: dict[str, AssetMeta] = {}
        for sym in {s.strip().upper() for s in symbols if s}:
            try:
                a = client.get_asset(sym)
            except Exception as exc:
                log_event("alpaca.asset_error", symbol=sym, error=str(exc))
                continue
            out[sym] = AssetMeta(
                symbol=sym,
                tradable=bool(getattr(a, "tradable", False)),
                fractionable=bool(getattr(a, "fractionable", False)),
                status=str(getattr(a, "status", "")),
                asset_class=str(getattr(a, "asset_class", "")),
                name=getattr(a, "name", None),
            )
        return out

    # -------- Prices --------
    def get_prices(self, symbols: Iterable[str]) -> dict[str, PriceQuote]:
        symbols = [s.strip().upper() for s in symbols if s]
        if not symbols:
            return {}
        out: dict[str, PriceQuote] = {}

        if self.configured:
            try:
                from alpaca.data.requests import StockLatestTradeRequest  # type: ignore

                client = self._data_client()
                req = StockLatestTradeRequest(symbol_or_symbols=symbols)
                trades = client.get_stock_latest_trade(req)
                ts = datetime.now(timezone.utc).isoformat()
                for sym, trade in trades.items():
                    price = float(getattr(trade, "price", 0) or 0)
                    if price > 0:
                        out[sym.upper()] = PriceQuote(sym.upper(), price, "alpaca", ts)
            except Exception as exc:
                log_event("alpaca.prices_error", error=str(exc))

        # Fallback: yfinance for anything still missing
        missing = [s for s in symbols if s not in out]
        if missing:
            try:
                import yfinance as yf  # type: ignore

                tickers = yf.Tickers(" ".join(missing))
                ts = datetime.now(timezone.utc).isoformat()
                for sym in missing:
                    try:
                        t = tickers.tickers.get(sym) or tickers.tickers.get(sym.upper())
                        if t is None:
                            continue
                        # Fast path: regular market price.
                        info = getattr(t, "fast_info", None) or {}
                        price = (
                            info.get("last_price")
                            if isinstance(info, dict)
                            else getattr(info, "last_price", None)
                        )
                        if not price:
                            hist = t.history(period="1d")
                            if hist is not None and len(hist) > 0:
                                price = float(hist["Close"].iloc[-1])
                        if price and float(price) > 0:
                            out[sym.upper()] = PriceQuote(
                                sym.upper(), float(price), "yfinance", ts
                            )
                    except Exception as exc:
                        log_event("yfinance.price_error", symbol=sym, error=str(exc))
            except Exception as exc:
                log_event("yfinance.import_error", error=str(exc))

        return out

    # -------- Order submission --------
    def submit_order(self, action: TradeAction) -> OrderResult:
        if not self.configured:
            return OrderResult(
                symbol=action.ticker,
                side=action.side,
                qty=abs(action.delta_shares),
                notional=action.estimated_notional,
                submitted=False,
                order_id=None,
                status=None,
                error="Alpaca not configured",
            )
        from alpaca.trading.enums import OrderSide, TimeInForce  # type: ignore
        from alpaca.trading.requests import MarketOrderRequest  # type: ignore

        side = OrderSide.BUY if action.side == ACTION_BUY else OrderSide.SELL
        qty = abs(float(action.delta_shares))
        if action.fractionable is False:
            qty = float(int(qty))  # whole shares only
        if qty <= 0:
            return OrderResult(
                symbol=action.ticker,
                side=action.side,
                qty=0,
                notional=0,
                submitted=False,
                order_id=None,
                status=None,
                error="Quantity rounded to zero; order not submitted.",
            )

        req = MarketOrderRequest(
            symbol=action.ticker,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        try:
            client = self._trading_client()
            order = client.submit_order(req)
            res = OrderResult(
                symbol=action.ticker,
                side=action.side,
                qty=qty,
                notional=action.estimated_notional,
                submitted=True,
                order_id=str(getattr(order, "id", "")) or None,
                status=str(getattr(order, "status", "")) or None,
                error=None,
            )
            log_event(
                "alpaca.order_submitted",
                symbol=action.ticker,
                side=action.side,
                qty=qty,
                paper=self.cfg.alpaca_paper,
                order_id=res.order_id,
                status=res.status,
            )
            return res
        except Exception as exc:
            log_event(
                "alpaca.order_error",
                symbol=action.ticker,
                side=action.side,
                qty=qty,
                error=str(exc),
            )
            return OrderResult(
                symbol=action.ticker,
                side=action.side,
                qty=qty,
                notional=action.estimated_notional,
                submitted=False,
                order_id=None,
                status=None,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Execute a preview (sells first)
# ---------------------------------------------------------------------------
def execute_preview(
    preview: TradePreview,
    cfg: AppConfig,
    *,
    dry_run: bool = True,
) -> list[OrderResult]:
    """Submit orders described by ``preview``. Sells before buys; logs each result.

    Will not submit any order unless safety gates pass:
      - Alpaca configured.
      - paper-trading mode OR (live + ALLOW_LIVE_TRADING).
      - dry_run False.
    """
    results: list[OrderResult] = []
    if dry_run:
        log_event("execute.dry_run", n_actions=len(preview.actions), paper=cfg.alpaca_paper)
        for a in preview.actions:
            if a.side in (ACTION_BUY, ACTION_SELL):
                results.append(
                    OrderResult(
                        symbol=a.ticker,
                        side=a.side,
                        qty=abs(a.delta_shares),
                        notional=a.estimated_notional,
                        submitted=False,
                        order_id=None,
                        status="DRY_RUN",
                        error=None,
                    )
                )
        return results

    if not cfg.has_alpaca:
        raise RuntimeError("Alpaca credentials not configured.")
    if not cfg.alpaca_paper and not cfg.allow_live_trading:
        raise RuntimeError(
            "LIVE trading requested but ALLOW_LIVE_TRADING is False. Refusing."
        )

    client = AlpacaClient(cfg)
    sells = [a for a in preview.actions if a.side == ACTION_SELL]
    buys = [a for a in preview.actions if a.side == ACTION_BUY]

    for a in sells + buys:
        results.append(client.submit_order(a))
    return results
