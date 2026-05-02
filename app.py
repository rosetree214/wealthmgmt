from __future__ import annotations

import hmac

import pandas as pd
import plotly.express as px
import streamlit as st

from core.auto_rebalancer import run_rebalance_once, start_scheduler
from core.config import load_config
from core.portfolio_scaler import calculate_scaled_targets
from core.sec13f_fetcher import fetch_latest_13f_holdings
from core.trade_executor import AlpacaService
from utils.helpers import dataframe_hash, money, pct, read_recent_history


DISCLAIMER = """
**Important:** Form 13F filings are delayed, can omit short positions and many non-U.S.
securities, and do not show current intent. This app is not investment advice and is
not a recommendation to trade. Trading defaults to Alpaca paper trading and dry-run
automation.
"""


def inject_css() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: radial-gradient(circle at top left, #18324a 0, #0b1020 38%, #070a12 100%); }
        .hero {
            padding: 1.5rem 1.7rem;
            border: 1px solid rgba(125, 211, 252, .22);
            border-radius: 24px;
            background: linear-gradient(135deg, rgba(14, 165, 233, .14), rgba(15, 23, 42, .78));
            box-shadow: 0 24px 80px rgba(0, 0, 0, .32);
        }
        .risk {
            padding: 1rem 1.2rem;
            border-left: 5px solid #f59e0b;
            border-radius: 14px;
            background: rgba(245, 158, 11, .12);
        }
        .metric-card {
            padding: 1rem;
            border-radius: 18px;
            background: rgba(15, 23, 42, .74);
            border: 1px solid rgba(148, 163, 184, .2);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def initialize_state() -> None:
    st.session_state.setdefault("metadata", None)
    st.session_state.setdefault("holdings", None)
    st.session_state.setdefault("targets", None)
    st.session_state.setdefault("preview", None)
    st.session_state.setdefault("preview_hash", None)
    st.session_state.setdefault("authenticated", False)


def clear_alpaca_session_state() -> None:
    st.session_state.preview = None
    st.session_state.preview_hash = None
    st.session_state.preview_created_at = None


def require_app_auth(config) -> bool:
    if config.app_password:
        if st.session_state.authenticated:
            return True

        st.sidebar.warning("Authentication is required before Alpaca account data or trading controls are available.")
        password = st.sidebar.text_input("App password", type="password")
        if st.sidebar.button("Unlock app"):
            if hmac.compare_digest(password, config.app_password):
                st.session_state.authenticated = True
                st.rerun()
            st.sidebar.error("Invalid app password.")
        return False

    if config.alpaca_configured:
        st.sidebar.error("Set APP_PASSWORD before using configured Alpaca credentials.")
        st.error("Alpaca credentials are configured, but APP_PASSWORD is missing. The app is locked fail-closed.")
        return False

    st.sidebar.warning("Set APP_PASSWORD to unlock Alpaca features. Holdings-only use remains available without Alpaca keys.")
    return False


def main() -> None:
    st.set_page_config(page_title="13F Mirror Trader", page_icon="📊", layout="wide")
    inject_css()
    initialize_state()
    config = load_config()
    authenticated = require_app_auth(config)
    if not authenticated:
        clear_alpaca_session_state()
    alpaca = AlpacaService(config) if authenticated else None

    st.markdown('<div class="hero"><h1>13F Mirror Trader</h1><p>Scale public 13F holdings into a target portfolio and preview guarded Alpaca trades.</p></div>', unsafe_allow_html=True)
    st.markdown(f'<div class="risk">{DISCLAIMER}</div>', unsafe_allow_html=True)

    with st.sidebar:
        st.header("Settings")
        fund_input = st.text_input("Fund name or CIK", value=f"Situational Awareness LP ({config.default_cik})")
        cik = st.text_input("Preferred CIK", value=config.default_cik)
        target_size = st.number_input(
            "Target portfolio size",
            min_value=100.0,
            value=float(config.target_portfolio_size),
            step=1000.0,
        )
        min_trade = st.number_input(
            "Minimum trade notional",
            min_value=0.0,
            value=float(config.min_trade_notional),
            step=1.0,
        )
        show_options = st.toggle("Show options positions", value=False)
        full_account = st.checkbox("Full-account rebalance mode")
        full_account_confirm = st.checkbox("I understand this can sell holdings outside the 13F portfolio")
        st.divider()
        st.write("**Mode**")
        st.success("Paper trading ON" if config.alpaca_paper else "Live mode requested")
        if not config.alpaca_paper:
            st.error("Live trading requires environment safety flags and an additional confirmation.")
        st.write("Dry-run automation is the default.")

    status = alpaca.get_account_status() if alpaca else None
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Alpaca", "Locked" if not authenticated else ("Configured" if status and status.configured else "Not configured"))
    col2.metric("Paper mode", "Yes" if config.alpaca_paper else "No")
    col3.metric("Buying power", money(status.account.get("buying_power")) if status and status.account else "-")
    col4.metric("Portfolio value", money(status.account.get("portfolio_value")) if status and status.account else "-")
    if not authenticated:
        st.info("Enter APP_PASSWORD in the sidebar to unlock Alpaca account status, trade preview, and execution controls.")
    elif not status.configured:
        st.info("Alpaca keys are missing or unavailable. Holdings and scaled targets still work; comparison and execution are disabled.")
    else:
        st.caption(status.message)

    fetch_col, preview_col, force_col = st.columns([1, 1, 1])
    with fetch_col:
        fetch_clicked = st.button("Fetch Latest Filing", type="primary")
    with preview_col:
        preview_clicked = st.button("Preview Trades")
    with force_col:
        force_clicked = st.button("Force Rebalance Now")

    if fetch_clicked:
        try:
            identifier = cik.strip() or fund_input.strip()
            metadata, holdings = fetch_latest_13f_holdings(identifier, settings=config)
            st.session_state.metadata = metadata
            st.session_state.holdings = holdings
            st.session_state.targets = calculate_scaled_targets(holdings, target_size)
            st.success("Latest 13F filing loaded.")
        except Exception as exc:
            st.error(f"Could not fetch 13F filing: {exc}")

    if force_clicked and authenticated:
        try:
            result = run_rebalance_once(cik, target_size, dry_run=True)
            st.success(f"Dry-run rebalance complete: {result}")
        except Exception as exc:
            st.error(f"Force rebalance failed safely: {exc}")
    elif force_clicked:
        st.error("Unlock the app before running an Alpaca-backed rebalance job.")

    metadata = st.session_state.metadata
    holdings = st.session_state.holdings
    targets = st.session_state.targets

    if metadata:
        st.subheader("Filing metadata")
        st.json(metadata.to_dict())

    if holdings is not None:
        visible_holdings = holdings if show_options else holdings[~holdings["is_option"]]
        st.subheader("13F holdings")
        st.dataframe(
            visible_holdings,
            use_container_width=True,
            hide_index=True,
            column_config={
                "reported_value_usd": st.column_config.NumberColumn("Reported value", format="$%.2f"),
                "original_fund_weight": st.column_config.NumberColumn("Fund weight", format="%.4f"),
            },
        )
        if holdings["ticker"].isna().any():
            st.warning("Some holdings have missing tickers and are excluded from generated orders.")
        if holdings["is_option"].any():
            st.warning("Options positions are shown only when enabled and are excluded from generated orders.")

    if targets is not None:
        st.subheader("Scaled target portfolio")
        chart_data = targets.head(20).copy()
        chart_data["label"] = chart_data["ticker"].fillna(chart_data["company_name"])
        fig = px.pie(chart_data, names="label", values="target_notional_usd", hole=0.45)
        st.plotly_chart(fig, use_container_width=True)

        symbols = targets["ticker"].dropna().astype(str).tolist()
        prices = alpaca.get_prices(symbols) if alpaca else {}
        enriched = targets.copy()
        if prices:
            enriched["current_price"] = enriched["ticker"].map(lambda s: prices.get(str(s), {}).get("price"))
            enriched["price_source"] = enriched["ticker"].map(lambda s: prices.get(str(s), {}).get("source"))
            enriched["price_timestamp"] = enriched["ticker"].map(lambda s: prices.get(str(s), {}).get("timestamp"))
        else:
            enriched["current_price"] = None
            enriched["price_source"] = None
            enriched["price_timestamp"] = None
            st.warning("No current prices are available yet. Trading rows without prices will be skipped.")
        assets = alpaca.get_asset_metadata(symbols) if alpaca else {}
        for column, fallback in [("tradable", False), ("asset_status", "unknown"), ("fractionable", False)]:
            enriched[column] = enriched["ticker"].map(lambda s, c=column: assets.get(str(s), {}).get(c, fallback))
            enriched[column] = enriched[column].fillna(fallback)
        st.session_state.targets = enriched

        st.dataframe(
            enriched,
            use_container_width=True,
            hide_index=True,
            column_config={
                "fund_weight": st.column_config.NumberColumn("Fund weight", format="%.4f"),
                "target_notional_usd": st.column_config.NumberColumn("Target notional", format="$%.2f"),
                "current_price": st.column_config.NumberColumn("Price", format="$%.2f"),
            },
        )

    if preview_clicked:
        if st.session_state.targets is None:
            st.error("Fetch a filing before previewing trades.")
        elif not authenticated:
            st.error("Unlock the app before previewing Alpaca trades.")
        elif not status or not status.configured:
            st.error("Execution preview requires Alpaca keys for positions and asset metadata. Scaled targets remain available above.")
        else:
            preview = alpaca.build_preview(
                st.session_state.targets,
                min_trade_notional=min_trade,
                full_account_rebalance=full_account and full_account_confirm,
            )
            records = preview.to_dict(orient="records")
            st.session_state.preview = preview
            st.session_state.preview_hash = dataframe_hash(records)
            st.session_state.preview_created_at = pd.Timestamp.utcnow().isoformat()

    preview = st.session_state.preview if authenticated else None
    if authenticated and preview is not None:
        st.subheader("Trade preview")
        total_buys = float(preview.loc[preview["side"] == "buy", "estimated_notional"].fillna(0).sum())
        total_sells = float(preview.loc[preview["side"] == "sell", "estimated_notional"].fillna(0).sum())
        st.caption(f"Preview hash: `{st.session_state.preview_hash}`")
        st.write(f"Estimated buys: {money(total_buys)} | estimated sells: {money(total_sells)}")
        st.dataframe(preview, use_container_width=True, hide_index=True)

        buying_power = status.account.get("buying_power", 0) if status and status.account else 0
        insufficient = total_buys > float(buying_power) + total_sells
        if insufficient:
            st.warning("Estimated buys exceed available buying power plus previewed sells.")

        st.subheader("Execute trades")
        live_confirm = True
        if not config.alpaca_paper:
            st.error("LIVE TRADING MODE REQUESTED. Verify all safeguards before proceeding.")
            live_confirm = st.checkbox("I explicitly confirm live trading")
        confirm = st.checkbox("I confirm, execute these trades")
        current_hash = dataframe_hash(preview.to_dict(orient="records"))
        can_execute = (
            authenticated
            and alpaca is not None
            and status is not None
            and status.configured
            and confirm
            and current_hash == st.session_state.preview_hash
            and not insufficient
            and (config.alpaca_paper or (config.allow_live_trading and live_confirm))
        )
        if st.button("Execute Trades", disabled=not can_execute):
            try:
                responses = alpaca.submit_preview_orders(
                    preview,
                    st.session_state.preview_hash,
                    current_hash,
                    confirm,
                    preview_created_at=st.session_state.get("preview_created_at"),
                    live_confirm=live_confirm,
                )
                st.write(responses)
            except Exception as exc:
                st.error(f"Execution failed safely: {exc}")

    if authenticated:
        st.subheader("Recent rebalance history")
        history = read_recent_history()
        if history:
            st.dataframe(pd.DataFrame(history), use_container_width=True)
        else:
            st.caption("No rebalance history yet.")
    else:
        st.subheader("Recent rebalance history")
        st.caption("Unlock the app to view rebalance history because it may contain Alpaca-derived position data.")

    with st.expander("Start local development scheduler"):
        st.warning("This blocks the Streamlit process and is for local development only. Use the CLI for production scheduling.")
        interval = st.number_input("Interval hours", min_value=1, value=int(config.rebalance_interval_hours))
        if st.button("Start APScheduler loop", disabled=not authenticated):
            start_scheduler(interval)
        if not authenticated:
            st.caption("Unlock the app before starting the Alpaca-backed scheduler.")


if __name__ == "__main__":
    main()
