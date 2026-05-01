"""13F Mirror Trader — Streamlit dashboard."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from core.config import AppConfig, load_config, normalize_cik
from core.portfolio_scaler import (
    ACTION_BUY,
    ACTION_HOLD,
    ACTION_SELL,
    ACTION_SKIP,
    compute_scaled_targets,
    determine_actions,
    estimate_buying_power_required,
    estimate_sell_proceeds,
)
from core.sec13f_fetcher import (
    HoldingsResult,
    fetch_holdings,
    resolve_fund_to_cik,
)
from core.auto_rebalancer import run_rebalance_once
from core.trade_executor import (
    AlpacaClient,
    TradePreview,
    execute_preview,
)
from utils.helpers import (
    HISTORY_DIR,
    fmt_currency,
    fmt_percent,
    fmt_shares,
    log_event,
)

st.set_page_config(
    page_title="13F Mirror Trader",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---- Lightweight CSS polish ---------------------------------------------
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.6rem; padding-bottom: 2.5rem; }
    .stMetric { background: #111826; padding: 0.6rem 0.8rem; border-radius: 10px;
                border: 1px solid #1f2937; }
    .disclaimer-box { background: #1f1300; border: 1px solid #b45309;
                      color: #fde68a; padding: 0.85rem 1rem; border-radius: 10px;
                      font-size: 0.92rem; line-height: 1.4; }
    .badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
             font-size: 0.78rem; font-weight: 600; margin-right: 6px; }
    .badge-paper { background: #064e3b; color: #6ee7b7; border: 1px solid #047857; }
    .badge-live { background: #7f1d1d; color: #fecaca; border: 1px solid #b91c1c; }
    .badge-readonly { background: #1f2937; color: #9ca3af; border: 1px solid #374151; }
    .small-muted { color: #94a3b8; font-size: 0.86rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---- Helpers -------------------------------------------------------------
def _mode_badge(cfg: AppConfig) -> str:
    if not cfg.has_alpaca:
        return '<span class="badge badge-readonly">READ-ONLY (no Alpaca keys)</span>'
    if cfg.alpaca_paper:
        return '<span class="badge badge-paper">PAPER</span>'
    return '<span class="badge badge-live">LIVE</span>'


def _disclaimer():
    st.markdown(
        """
        <div class="disclaimer-box">
        <strong>⚠️ This is software, not investment advice.</strong> SEC Form 13F-HR filings
        are filed up to 45 days after quarter-end and may already be weeks or months stale.
        They are <em>incomplete</em>: they exclude shorts, options strategies' true intent,
        many international holdings, and most fixed income. Mirroring a manager's
        long-only equity book on a delay is not a recommended trading strategy. By default
        this app runs in <strong>paper trading</strong> and <strong>dry-run</strong> mode.
        </div>
        """,
        unsafe_allow_html=True,
    )


def _initial_state():
    st.session_state.setdefault("holdings_result", None)
    st.session_state.setdefault("preview", None)
    st.session_state.setdefault("preview_hash", None)
    st.session_state.setdefault("execute_results", None)
    st.session_state.setdefault("show_options", False)
    st.session_state.setdefault("full_account_rebalance", False)


# ---- Sidebar -------------------------------------------------------------
def render_sidebar(cfg: AppConfig):
    st.sidebar.header("⚙️ Settings")
    st.sidebar.markdown(_mode_badge(cfg), unsafe_allow_html=True)

    if cfg.issues:
        for issue in cfg.issues:
            st.sidebar.error(issue)

    st.sidebar.subheader("Fund")
    fund_query = st.sidebar.text_input(
        "Fund name or CIK",
        value=st.session_state.get("fund_query", cfg.default_cik),
        help="Default: Situational Awareness LP (CIK 0002045724)",
    )
    st.session_state["fund_query"] = fund_query

    st.sidebar.subheader("Target portfolio")
    target_size = st.sidebar.number_input(
        "Target portfolio size (USD)",
        min_value=100.0,
        value=float(cfg.target_portfolio_size),
        step=1000.0,
        format="%.2f",
    )
    st.session_state["target_size"] = target_size

    st.sidebar.subheader("Trading rules")
    st.sidebar.number_input(
        "Min trade notional (USD)",
        min_value=1.0,
        value=float(cfg.min_trade_notional),
        step=1.0,
        key="min_trade_notional",
        help="Trades smaller than this are downgraded to HOLD.",
    )
    st.sidebar.toggle(
        "Show options positions in holdings",
        key="show_options",
        help="Options are still excluded from auto-trading.",
    )
    st.sidebar.toggle(
        "Full-account rebalance (sell holdings not in 13F)",
        key="full_account_rebalance",
        help="Default OFF. Mirror-only mode never touches unrelated positions.",
    )

    with st.sidebar.expander("Safety status"):
        st.write(f"- Paper trading: **{cfg.alpaca_paper}**")
        st.write(f"- ALLOW_LIVE_TRADING: **{cfg.allow_live_trading}**")
        st.write(f"- ALLOW_LIVE_AUTO_EXECUTE: **{cfg.allow_live_auto_execute}**")
        st.write(f"- AUTO_EXECUTE: **{cfg.auto_execute}**")
        st.write(f"- Alpaca keys present: **{cfg.has_alpaca}**")


# ---- Header --------------------------------------------------------------
def render_header(cfg: AppConfig):
    col1, col2 = st.columns([3, 1])
    with col1:
        st.title("📊 13F Mirror Trader")
        st.caption(
            "Mirror the latest Form 13F-HR holdings of a selected manager into a "
            "target portfolio size. Compare against your Alpaca portfolio. Preview "
            "and (optionally) execute trades."
        )
    with col2:
        st.markdown(_mode_badge(cfg), unsafe_allow_html=True)
        if cfg.has_alpaca:
            try:
                acct = AlpacaClient(cfg).get_account()
                if acct:
                    st.metric("Buying power", fmt_currency(acct.buying_power))
                    st.caption(f"Portfolio: {fmt_currency(acct.portfolio_value)}")
            except Exception as exc:
                st.warning(f"Could not load Alpaca account: {exc}")
    _disclaimer()


# ---- Filing fetch --------------------------------------------------------
def render_filing_section(cfg: AppConfig):
    st.subheader("1. Latest 13F filing")
    fund_query = st.session_state.get("fund_query", cfg.default_cik)
    cols = st.columns([2, 1, 1])
    with cols[0]:
        st.write(f"Fund query: **{fund_query}**")
    with cols[1]:
        fetch_clicked = st.button("🔎 Fetch latest filing", use_container_width=True)
    with cols[2]:
        clear_clicked = st.button("Clear", use_container_width=True)
    if clear_clicked:
        st.session_state["holdings_result"] = None
        st.session_state["preview"] = None
        st.session_state["preview_hash"] = None
        st.session_state["execute_results"] = None

    if fetch_clicked:
        if not cfg.edgar_identity:
            st.error(
                "EDGAR_IDENTITY is not configured. Set it in `.env` or "
                "`.streamlit/secrets.toml` before fetching. Format: "
                "`Your Name your.email@example.com`."
            )
            return
        with st.spinner("Resolving CIK and fetching filing..."):
            try:
                cik = resolve_fund_to_cik(fund_query, cfg) or normalize_cik(fund_query)
            except ValueError as exc:
                st.error(f"Could not resolve fund: {exc}")
                return
            try:
                result = fetch_holdings(cik, cfg)
                st.session_state["holdings_result"] = result
                st.session_state["preview"] = None
                st.session_state["preview_hash"] = None
                st.session_state["execute_results"] = None
                log_event(
                    "ui.holdings_fetched",
                    cik=cik,
                    accession=result.metadata.accession_number,
                    n_holdings=len(result.holdings),
                )
            except Exception as exc:
                st.error(f"Failed to fetch holdings: {exc}")
                return

    result: HoldingsResult | None = st.session_state.get("holdings_result")
    if not result:
        st.info("Click **Fetch latest filing** to load holdings.")
        return

    md = result.metadata
    m1, m2, m3 = st.columns(3)
    m1.metric("Manager", md.manager_name or "—")
    m1.caption(f"CIK: {md.cik}")
    m2.metric("Form type", md.form_type or "—")
    m2.caption("Amendment: " + ("yes" if md.is_amendment else "no"))
    m3.metric("Filing date", md.filing_date or "—")
    m3.caption(f"Report date: {md.report_date or '—'}")
    st.caption(f"Accession: `{md.accession_number}`  ·  Source: {md.source}")

    n1, n2, n3 = st.columns(3)
    n1.metric("Total 13F value", fmt_currency(result.total_value_usd, digits=0))
    n2.metric("Normalization", result.normalization_convention)
    n3.metric("Holdings", f"{len(result.holdings):,}")
    st.caption(f"Normalization reason: {result.normalization_reason}")

    for w in result.warnings:
        st.warning(w)


# ---- Holdings + scaling --------------------------------------------------
def render_holdings_and_targets(cfg: AppConfig):
    result: HoldingsResult | None = st.session_state.get("holdings_result")
    if not result:
        return

    st.subheader("2. Holdings & scaled targets")
    df = result.holdings.copy()
    if not st.session_state.get("show_options"):
        df = df[~df["put_call"].fillna("").astype(str).str.upper().isin({"PUT", "CALL"})]

    display = df.copy()
    display["fund_weight_pct"] = display["fund_weight"].apply(lambda x: fmt_percent(x))
    display["value_usd_fmt"] = display["value_usd"].apply(lambda x: fmt_currency(x, 0))
    columns_to_show = [
        "ticker", "issuer", "cusip", "figi", "security_class", "put_call",
        "shares", "value_usd_fmt", "fund_weight_pct",
    ]
    columns_to_show = [c for c in columns_to_show if c in display.columns]
    st.dataframe(
        display[columns_to_show].rename(
            columns={
                "value_usd_fmt": "value_usd",
                "fund_weight_pct": "fund_weight",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    # Pie chart of top 10 by weight
    pie_df = result.holdings[result.holdings["ticker"].notna()].head(10)
    if len(pie_df) > 0:
        fig = px.pie(
            pie_df,
            values="value_usd",
            names="ticker",
            title="Top 10 holdings by 13F value",
            hole=0.45,
        )
        fig.update_traces(textinfo="percent+label")
        st.plotly_chart(fig, use_container_width=True)

    # Scale into target portfolio
    target_size = float(st.session_state.get("target_size") or cfg.target_portfolio_size)

    candidate_symbols = (
        result.holdings["ticker"].dropna().astype(str).str.upper().unique().tolist()
    )
    candidate_symbols = [s for s in candidate_symbols if s]

    alpaca = AlpacaClient(cfg)
    with st.spinner("Looking up prices and asset metadata..."):
        prices = alpaca.get_prices(candidate_symbols)
        asset_meta = alpaca.get_asset_metadata(candidate_symbols)

    targets = compute_scaled_targets(
        result.holdings,
        target_portfolio_size=target_size,
        total_13f_value_usd=result.total_value_usd,
        prices=prices,
        asset_meta=asset_meta,
        include_options=st.session_state.get("show_options", False),
    )

    st.session_state["targets"] = targets
    st.session_state["prices"] = prices
    st.session_state["asset_meta"] = asset_meta

    rows = []
    for t in targets:
        rows.append({
            "ticker": t.ticker,
            "issuer": t.issuer,
            "weight": fmt_percent(t.fund_weight),
            "target_notional": fmt_currency(t.target_notional_usd, 2),
            "price": fmt_currency(t.price) if t.price else "—",
            "price_source": t.price_source or "—",
            "target_shares": fmt_shares(t.target_shares),
            "fractionable": t.fractionable,
            "tradable": t.tradable,
            "warning": t.warning or "",
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No tradable targets in this filing.")

    missing_prices = [t.ticker for t in targets if not t.price]
    if missing_prices:
        st.warning(
            f"No price for: {', '.join(missing_prices)}. These symbols are skipped for trading."
        )


# ---- Preview & execute ---------------------------------------------------
def render_preview_and_execute(cfg: AppConfig):
    targets = st.session_state.get("targets")
    result: HoldingsResult | None = st.session_state.get("holdings_result")
    if not (targets and result):
        return

    st.subheader("3. Preview trades")
    if not cfg.has_alpaca:
        st.info(
            "Add Alpaca API keys to enable portfolio comparison and order preview. "
            "Without keys, the app remains read-only."
        )
        return

    alpaca = AlpacaClient(cfg)

    cols = st.columns([1, 1, 2])
    with cols[0]:
        preview_clicked = st.button("📝 Preview trades", use_container_width=True)
    with cols[1]:
        force_now = st.button("⚡ Force rebalance now (dry-run)", use_container_width=True)
    with cols[2]:
        st.caption(
            "Preview is hashed; execution requires the preview hash to match. "
            "Sells are submitted before buys."
        )

    if preview_clicked:
        try:
            positions = alpaca.get_positions()
            account = alpaca.get_account()
            actions = determine_actions(
                targets,
                current_positions=positions,
                min_trade_notional=float(st.session_state.get("min_trade_notional", cfg.min_trade_notional)),
                full_account_rebalance=st.session_state.get("full_account_rebalance", False),
            )
            preview = TradePreview(
                actions=actions,
                account=account,
                cik=result.metadata.cik,
                accession_number=result.metadata.accession_number,
                target_portfolio_size=float(st.session_state.get("target_size") or cfg.target_portfolio_size),
                full_account_rebalance=st.session_state.get("full_account_rebalance", False),
                paper=cfg.alpaca_paper,
            )
            st.session_state["preview"] = preview
            st.session_state["preview_hash"] = preview.hash()
            st.session_state["execute_results"] = None
            log_event(
                "ui.preview_generated",
                cik=preview.cik,
                accession=preview.accession_number,
                n_actions=len(actions),
                hash=st.session_state["preview_hash"],
            )
        except Exception as exc:
            st.error(f"Preview failed: {exc}")
            return

    if force_now:
        try:
            res = run_rebalance_once(
                result.metadata.cik,
                float(st.session_state.get("target_size") or cfg.target_portfolio_size),
                dry_run=True,
                full_account_rebalance=st.session_state.get("full_account_rebalance", False),
                cfg=cfg,
            )
            st.success(
                f"Force-rebalance dry-run complete. Actions={res.n_actions}, "
                f"buys={res.n_buys}, sells={res.n_sells}. "
                f"Preview saved: {res.preview_path}"
            )
        except Exception as exc:
            st.error(f"Force rebalance failed: {exc}")

    preview: TradePreview | None = st.session_state.get("preview")
    if not preview:
        return

    rows = []
    for a in preview.actions:
        rows.append({
            "side": a.side,
            "ticker": a.ticker,
            "issuer": a.issuer,
            "current": fmt_shares(a.current_shares),
            "target": fmt_shares(a.target_shares),
            "delta": fmt_shares(a.delta_shares),
            "price": fmt_currency(a.price) if a.price else "—",
            "notional": fmt_currency(a.estimated_notional, 2),
            "type": a.order_type,
            "tif": a.time_in_force,
            "fractionable": a.fractionable,
            "reason": a.reason,
            "warnings": "; ".join(a.warnings),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    est_buy = estimate_buying_power_required(preview.actions)
    est_sell = estimate_sell_proceeds(preview.actions)
    n_buys = sum(1 for a in preview.actions if a.side == ACTION_BUY)
    n_sells = sum(1 for a in preview.actions if a.side == ACTION_SELL)
    n_holds = sum(1 for a in preview.actions if a.side == ACTION_HOLD)
    n_skips = sum(1 for a in preview.actions if a.side == ACTION_SKIP)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Buys", n_buys, fmt_currency(est_buy, 2))
    c2.metric("Sells", n_sells, fmt_currency(est_sell, 2))
    c3.metric("Holds", n_holds)
    c4.metric("Skips", n_skips)

    buying_power = preview.account.buying_power if preview.account else 0.0
    insufficient = est_buy > buying_power and n_sells == 0
    if insufficient:
        st.warning(
            f"Estimated buy notional ({fmt_currency(est_buy)}) exceeds buying power "
            f"({fmt_currency(buying_power)}) and no sells are queued. Execution disabled."
        )

    st.subheader("4. Execute trades")
    confirm = st.checkbox("I confirm, execute these trades", value=False)
    live_acknowledge = True
    if not cfg.alpaca_paper:
        st.error(
            "🚨 LIVE TRADING MODE. Orders submitted will use real money. "
            f"ALLOW_LIVE_TRADING={cfg.allow_live_trading}."
        )
        live_acknowledge = st.checkbox(
            "I understand this is LIVE and accept full responsibility.", value=False
        )
        if not cfg.allow_live_trading:
            st.warning("Set `ALLOW_LIVE_TRADING=true` in your environment to enable live execution.")

    enabled = (
        confirm
        and live_acknowledge
        and not insufficient
        and st.session_state.get("preview_hash") == preview.hash()
        and (cfg.alpaca_paper or cfg.allow_live_trading)
    )
    execute_clicked = st.button(
        "🚀 Execute trades", disabled=not enabled, use_container_width=True
    )

    if execute_clicked:
        if st.session_state.get("preview_hash") != preview.hash():
            st.error("Preview has changed since generation; please re-preview.")
            return
        try:
            results = execute_preview(preview, cfg, dry_run=False)
            st.session_state["execute_results"] = [r.__dict__ for r in results]
            log_event(
                "ui.executed",
                n_orders=len(results),
                paper=cfg.alpaca_paper,
                cik=preview.cik,
                accession=preview.accession_number,
            )
            st.success(f"Submitted {len(results)} orders.")
        except Exception as exc:
            st.error(f"Execution error: {exc}")

    if st.session_state.get("execute_results"):
        st.dataframe(
            pd.DataFrame(st.session_state["execute_results"]),
            use_container_width=True,
            hide_index=True,
        )


# ---- History -------------------------------------------------------------
def render_history():
    st.subheader("5. Recent rebalance history")
    files = sorted(Path(HISTORY_DIR).glob("rebalance_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        st.caption("No previous rebalances on file.")
        return
    rows = []
    for f in files[:20]:
        try:
            data = json.loads(f.read_text())
            rows.append({
                "file": f.name,
                "cik": data.get("cik"),
                "accession": data.get("accession_number"),
                "created_at": data.get("created_at"),
                "n_actions": len(data.get("actions", [])),
                "paper": data.get("paper"),
            })
        except Exception:
            continue
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ---- Main ----------------------------------------------------------------
def main():
    _initial_state()
    cfg = load_config()
    render_sidebar(cfg)
    render_header(cfg)
    render_filing_section(cfg)
    render_holdings_and_targets(cfg)
    render_preview_and_execute(cfg)
    render_history()


if __name__ == "__main__":
    main()
