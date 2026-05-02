from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from core.config import AppConfig
from core.portfolio_scaler import generate_trade_preview
from utils.helpers import log_event

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
except Exception:  # pragma: no cover - optional until dependency is installed
    TradingClient = None
    OrderSide = None
    TimeInForce = None
    MarketOrderRequest = None


@dataclass(frozen=True)
class AlpacaStatus:
    configured: bool
    paper: bool
    account: dict[str, Any] | None
    message: str


class AlpacaService:
    def __init__(self, settings: AppConfig):
        self.settings = settings
        self.paper = settings.alpaca_paper
        self._client = None

        if settings.alpaca_configured and TradingClient is not None:
            self._client = TradingClient(
                settings.alpaca_api_key,
                settings.alpaca_secret_key,
                paper=settings.alpaca_paper,
            )

    @property
    def configured(self) -> bool:
        return self._client is not None

    def get_account_status(self) -> AlpacaStatus:
        if not self.settings.alpaca_configured:
            return AlpacaStatus(False, self.paper, None, "Alpaca keys are not configured.")
        if TradingClient is None:
            return AlpacaStatus(False, self.paper, None, "alpaca-py is not installed.")

        try:
            account = self._client.get_account()
            data = {
                "status": str(account.status),
                "buying_power": float(account.buying_power),
                "portfolio_value": float(account.portfolio_value),
                "currency": getattr(account, "currency", "USD"),
            }
            return AlpacaStatus(True, self.paper, data, "Connected to Alpaca.")
        except Exception as exc:
            log_event("alpaca_account_error", paper_mode=self.paper, details={"error": str(exc)})
            return AlpacaStatus(False, self.paper, None, f"Alpaca account fetch failed: {exc}")

    def get_positions(self) -> dict[str, float]:
        if not self.configured:
            return {}
        positions: dict[str, float] = {}
        try:
            for position in self._client.get_all_positions():
                positions[position.symbol.upper()] = float(position.qty)
        except Exception as exc:
            log_event("alpaca_positions_error", paper_mode=self.paper, details={"error": str(exc)})
        return positions

    def get_asset_metadata(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        metadata: dict[str, dict[str, Any]] = {}
        if not self.configured:
            return metadata
        for symbol in sorted({symbol.upper() for symbol in symbols if symbol}):
            try:
                asset = self._client.get_asset(symbol)
                metadata[symbol] = {
                    "tradable": bool(asset.tradable),
                    "asset_status": str(asset.status),
                    "fractionable": bool(asset.fractionable),
                }
            except Exception as exc:
                log_event(
                    "alpaca_asset_error",
                    paper_mode=self.paper,
                    details={"symbol": symbol, "error": str(exc)},
                )
        return metadata

    def get_prices(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        prices: dict[str, dict[str, Any]] = {}
        unique_symbols = sorted({symbol.upper() for symbol in symbols if symbol})
        if self.configured:
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockLatestTradeRequest

                data_client = StockHistoricalDataClient(
                    self.settings.alpaca_api_key,
                    self.settings.alpaca_secret_key,
                )
                request = StockLatestTradeRequest(symbol_or_symbols=unique_symbols)
                trades = data_client.get_stock_latest_trade(request)
                for symbol, trade in trades.items():
                    price = float(getattr(trade, "price", 0) or 0)
                    if price > 0:
                        timestamp = getattr(trade, "timestamp", None)
                        prices[symbol.upper()] = {
                            "price": price,
                            "source": "alpaca",
                            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
                        }
            except Exception as exc:
                log_event("alpaca_price_lookup_error", paper_mode=self.paper, details={"error": str(exc)})

        missing_symbols = [symbol for symbol in unique_symbols if symbol not in prices]
        try:
            import yfinance as yf

            for symbol in missing_symbols:
                ticker = yf.Ticker(symbol)
                history = ticker.history(period="1d", interval="1m")
                if history.empty:
                    history = ticker.history(period="5d", interval="1d")
                if history.empty:
                    continue
                last = history.tail(1)
                price = float(last["Close"].iloc[0])
                if price <= 0:
                    continue
                timestamp = last.index[-1].isoformat()
                prices[symbol] = {"price": price, "source": "yfinance", "timestamp": timestamp}
        except Exception as exc:
            log_event("price_lookup_error", paper_mode=self.paper, details={"error": str(exc)})
        return prices

    def annotate_assets(self, targets: pd.DataFrame) -> pd.DataFrame:
        annotated = targets.copy()
        for column, default in [("tradable", False), ("asset_status", "unknown"), ("fractionable", False)]:
            if column not in annotated.columns:
                annotated[column] = default

        if not self.configured:
            annotated["asset_warning"] = "Alpaca unavailable; asset metadata not checked"
            return annotated

        warnings: list[str] = []
        for idx, row in annotated.iterrows():
            symbol = str(row.get("ticker", "")).upper().strip()
            warning = ""
            if not symbol:
                warnings.append("Missing ticker")
                continue
            try:
                asset = self._client.get_asset(symbol)
                annotated.at[idx, "tradable"] = bool(asset.tradable)
                annotated.at[idx, "asset_status"] = str(asset.status)
                annotated.at[idx, "fractionable"] = bool(asset.fractionable)
            except Exception as exc:
                warning = f"Asset lookup failed: {exc}"
                log_event(
                    "alpaca_asset_error",
                    paper_mode=self.paper,
                    details={"symbol": symbol, "error": str(exc)},
                )
            warnings.append(warning)
        annotated["asset_warning"] = warnings
        return annotated

    def build_preview(
        self,
        targets: pd.DataFrame,
        min_trade_notional: float,
        full_account_rebalance: bool = False,
    ) -> pd.DataFrame:
        positions = self.get_positions()
        return generate_trade_preview(
            targets,
            positions,
            min_trade_notional=min_trade_notional,
            full_account_rebalance=full_account_rebalance,
        )

    def build_trade_preview(self, targets: pd.DataFrame, current_positions: dict[str, float]) -> pd.DataFrame:
        return generate_trade_preview(
            targets,
            current_positions,
            min_trade_notional=self.settings.min_trade_notional,
        )

    def submit_orders(self, preview: pd.DataFrame) -> list[dict[str, Any]]:
        if not self.settings.auto_execute:
            raise PermissionError("AUTO_EXECUTE must be true before automated order submission.")
        if not self.paper and not (
            self.settings.allow_live_trading and self.settings.allow_live_auto_execute
        ):
            raise PermissionError("Automated live trading is disabled by safety gates.")
        return self.submit_preview_orders(preview, "auto", "auto", confirm=True, automated=True)

    def submit_preview_orders(
        self,
        preview: pd.DataFrame,
        preview_hash: str,
        current_hash: str,
        confirm: bool,
        automated: bool = False,
    ) -> list[dict[str, Any]]:
        if not confirm:
            raise PermissionError("Execution requires explicit user confirmation.")
        if not automated and preview_hash != current_hash:
            raise PermissionError("Preview hash changed; regenerate preview before execution.")
        if not self.configured:
            raise PermissionError("Alpaca credentials are required for execution.")
        if not self.paper and not self.settings.allow_live_trading:
            raise PermissionError("Live trading requires ALLOW_LIVE_TRADING=true.")

        executable = preview[preview["side"].isin(["sell", "buy"])].copy()
        self._validate_executable_preview(executable)
        executable["_sort"] = executable["side"].map({"sell": 0, "buy": 1})
        executable = executable.sort_values(["_sort", "symbol"])

        responses: list[dict[str, Any]] = []
        for _, row in executable.iterrows():
            symbol = row["symbol"]
            qty = abs(float(row["delta_shares"]))
            side = row["side"]
            if qty <= 0:
                continue
            try:
                request = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                order = self._client.submit_order(order_data=request)
                response = {"symbol": symbol, "side": side, "status": "submitted", "order_id": str(order.id)}
                log_event(
                    "order_submitted",
                    paper_mode=self.paper,
                    dry_run=False,
                    details=response,
                )
            except Exception as exc:
                response = {"symbol": symbol, "side": side, "status": "failed", "error": str(exc)}
                log_event(
                    "order_failed",
                    paper_mode=self.paper,
                    dry_run=False,
                    details=response,
                )
            responses.append(response)
        return responses

    def _validate_executable_preview(self, executable: pd.DataFrame) -> None:
        if executable.empty:
            return

        account = self.get_account_status()
        positions = self.get_positions()
        total_buy_notional = 0.0
        total_sell_notional = 0.0

        for _, row in executable.iterrows():
            symbol = str(row.get("symbol") or "").upper().strip()
            side = str(row.get("side") or "").lower()
            qty = abs(float(row.get("delta_shares") or 0))
            warnings = str(row.get("warnings") or "").strip()
            price = row.get("estimated_price")
            notional = row.get("estimated_notional")

            if not symbol:
                raise PermissionError("Executable preview row is missing a symbol.")
            if warnings:
                raise PermissionError(f"{symbol} has unresolved warnings: {warnings}")
            if qty <= 0:
                raise PermissionError(f"{symbol} has non-positive order quantity.")
            if price is None or pd.isna(price) or float(price) <= 0:
                raise PermissionError(f"{symbol} is missing a valid execution price.")
            if notional is None or pd.isna(notional) or float(notional) <= 0:
                raise PermissionError(f"{symbol} is missing a valid execution notional.")

            asset = self.get_asset_metadata([symbol]).get(symbol)
            if not asset or not asset.get("tradable") or str(asset.get("asset_status")).lower() != "active":
                raise PermissionError(f"{symbol} is not active and tradable at execution time.")

            if side == "sell":
                current_qty = float(positions.get(symbol, 0.0))
                if qty > current_qty:
                    raise PermissionError(f"{symbol} sell quantity exceeds current position.")
                total_sell_notional += float(notional)
            elif side == "buy":
                total_buy_notional += float(notional)

        buying_power = 0.0
        if account.account:
            buying_power = float(account.account.get("buying_power") or 0)
        if total_buy_notional > buying_power + total_sell_notional:
            raise PermissionError("Preview buy notional exceeds buying power plus sell proceeds.")

        log_event(
            "order_preflight_passed",
            paper_mode=self.paper,
            dry_run=False,
            details={
                "orders": len(executable),
                "checked_at": datetime.now(UTC).isoformat(),
                "buy_notional": total_buy_notional,
                "sell_notional": total_sell_notional,
            },
        )
