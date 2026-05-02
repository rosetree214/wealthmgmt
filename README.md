# 13F Mirror Trader

13F Mirror Trader is a personal-use Streamlit app for mirroring the latest Form 13F-HR holdings of an investment manager into a target portfolio size, comparing those targets against a broker portfolio, previewing trades, and optionally submitting orders through Alpaca or Interactive Brokers with strict safety gates.

Default manager:

- Situational Awareness LP
- CIK `0002045724`

## Critical disclaimers

This project is software tooling, not investment advice.

- Form 13F filings are delayed, incomplete, and backward-looking.
- 13F filings do not show intraperiod trades, short positions, cash, most derivatives exposure, or current intent.
- A manager's disclosed holdings are not a recommendation to trade.
- The app defaults to paper trading and dry-run automation.
- Live trading requires explicit environment flags and UI confirmation.

## What the app does

- Fetches the latest 13F-HR filing for a CIK or fund name using EdgarTools.
- Normalizes 13F reported values with confidence checks.
- Displays clean holdings, including missing ticker and option warnings.
- Scales disclosed holdings to a target portfolio size.
- Looks up prices using yfinance fallback.
- Connects to Alpaca with `alpaca-py` or Interactive Brokers through TWS/IB Gateway when configured.
- Compares target shares with current positions.
- Generates sell-before-buy trade previews.
- Stores preview hashes so execution cannot use stale previews.
- Logs JSONL events to `logs/app.jsonl`.
- Saves rebalance previews to `history/`.
- Tracks last seen filings in `state/last_filings.json`, keyed by CIK and form type.
- Scans configurable SEC form types for new filing notifications.
- Provides a standalone automation CLI suitable for cron-style schedulers.

## What it does not do

- It does not provide investment advice.
- It does not guarantee 13F data completeness or timeliness.
- It does not auto-convert options positions into underlying shares.
- It does not liquidate unrelated holdings unless full-account rebalance is explicitly enabled.
- It does not trade without broker connectivity and explicit execution confirmation.
- It does not make automatic live trading possible by default.
- It does not mirror non-13F forms. Comparable filings such as Schedule 13D/13G are detection/notification inputs only unless a safe parser is added later.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set at least:

```bash
EDGAR_IDENTITY="Your Name your.email@example.com"
```

Broker configuration is optional. Without it, the app still shows 13F holdings and scaled targets, but portfolio comparison and execution are disabled.

```bash
BROKER=alpaca
ALPACA_API_KEY="..."
ALPACA_SECRET_KEY="..."
ALPACA_PAPER=true
```

Interactive Brokers support uses TWS or IB Gateway plus `ib_insync`:

```bash
BROKER=ibkr
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=13
IBKR_READ_ONLY=true
```

Keep `IBKR_READ_ONLY=true` while testing account and position access. Set it to `false` only when you are ready to allow order submission through the existing preview and confirmation gates.

## Run locally

```bash
streamlit run app.py
```

Automation dry run:

```bash
python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run
```

Scan configured filing forms without generating a rebalance preview:

```bash
python -m core.auto_rebalancer --cik 0002045724 --scan-only --scan-forms "13F-HR,13F-HR/A,SC 13G"
```

CLI help:

```bash
python -m core.auto_rebalancer --help
```

## Safety gates

### Paper trading

`ALPACA_PAPER=true` is the default for Alpaca. For IBKR, paper/live mode is determined by the TWS or IB Gateway port you connect to: `7497` is the common paper port and `7496` is the common live port. Execution remains disabled until a preview exists, the preview hash matches, and the user checks the confirmation box.

### Live trading

Live trading requires:

```bash
ALPACA_PAPER=false
ALLOW_LIVE_TRADING=true
```

For IBKR live trading, connect to your live TWS/Gateway session and set:

```bash
BROKER=ibkr
IBKR_READ_ONLY=false
ALLOW_LIVE_TRADING=true
```

The UI also requires an additional live-trading checkbox and displays a large warning.

### Automatic execution

Automatic live execution is disabled unless all of these are true:

```bash
AUTO_EXECUTE=true
ALLOW_LIVE_TRADING=true
ALLOW_LIVE_AUTO_EXECUTE=true
ALPACA_PAPER=false
```

The default automation behavior is dry-run preview generation.

## Automatic filing scans

`SCAN_FORM_TYPES` controls which SEC forms the automation watches. The default is:

```bash
SCAN_FORM_TYPES="13F-HR,13F-HR/A"
```

You can add comparable filing types for detection and notification:

```bash
SCAN_FORM_TYPES="13F-HR,13F-HR/A,SC 13G,SC 13G/A,SC 13D,SC 13D/A"
```

Only 13F-HR filings are parsed into holdings and rebalance previews. Other forms are recorded in `state/last_filings.json` and can send notifications when a new accession number appears.

## Deployment notes

### Streamlit Community Cloud

Streamlit Community Cloud is suitable for the UI only. It should not be treated as a reliable scheduled job runner. Configure secrets through Streamlit secrets.

### Render cron job

Command:

```bash
python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run
```

Detection-only command:

```bash
python -m core.auto_rebalancer --cik 0002045724 --scan-only --scan-forms "13F-HR,13F-HR/A,SC 13G,SC 13D"
```

Use persistent disks if you want `state/last_filings.json` and `history/` to survive redeploys.

### Railway cron job

Command:

```bash
python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run
```

Configure environment variables in Railway and attach persistent storage for JSON state.

### GitHub Actions cron alternative

Use a scheduled workflow that installs dependencies and runs:

```bash
python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run
```

GitHub Actions ephemeral storage means local JSON state will not persist unless you commit artifacts elsewhere or use external storage.

## Runtime files

- `logs/app.jsonl` - structured events
- `history/rebalance_{cik}_{accession}.json` - rebalance previews
- `state/last_filings.json` - last seen accession numbers keyed by CIK and SEC form type

If deployed to an ephemeral filesystem, use persistent storage or an external state store.

## Troubleshooting

### Missing `EDGAR_IDENTITY`

SEC requests require a real identity. Set:

```bash
EDGAR_IDENTITY="Your Name your.email@example.com"
```

### Missing Alpaca keys

The app will still fetch holdings and calculate scaled targets. Portfolio comparison and execution remain disabled until `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are configured.

### Interactive Brokers connection

IBKR requires TWS or IB Gateway to be running and API access enabled. Check:

- `BROKER=ibkr`
- `IBKR_HOST` and `IBKR_PORT`
- `IBKR_CLIENT_ID`
- TWS/Gateway API settings allow socket clients
- Paper trading commonly uses port `7497`; live commonly uses `7496`
- `IBKR_READ_ONLY=true` prevents order submission while testing

### Missing tickers

Some 13F rows may not include tickers. These rows are displayed with warnings and excluded from generated orders.

### Options positions

Options are shown only when enabled in the UI and are excluded from auto-trading. The app does not convert options into underlying shares for trading.

### Price lookup failures

If yfinance cannot return a positive recent price, the symbol is skipped for trading and shown with a warning. The app does not silently use zero or stale substitute prices.

### Non-fractionable assets

Broker asset metadata is checked before preview generation. Non-fractionable buys are rounded down to whole shares. Sells are clipped to the current position.

### Insufficient buying power

The preview compares estimated buy notional against available buying power. Execution is disabled when buying power appears insufficient unless sell-first rebalancing is explicitly enabled and the preview includes sells.

## Tests

```bash
pytest
```

Current tests cover portfolio scaling/action generation and 13F value normalization.