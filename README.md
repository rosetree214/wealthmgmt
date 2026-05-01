# 13F Mirror Trader

A personal-use Streamlit web app that mirrors the latest **Form 13F-HR** holdings of a
selected investment manager into a target portfolio size, compares the result against
your current Alpaca portfolio, previews trades, and (optionally) executes them through
Alpaca with strict safety gates.

> **Default fund:** Situational Awareness LP — CIK `0002045724`.

---

## ⚠️ Disclaimers (read first)

- **This is software, not investment advice.** Mirroring 13F filings is not a
  recommended trading strategy.
- **13F filings are stale.** Form 13F-HR is filed up to 45 days after quarter-end.
  By the time you see a holding, it may already be weeks or months old.
- **13F filings are incomplete.** They generally exclude shorts, cash, fixed income,
  most non-U.S. equities, and the true intent of options trades.
- **Paper trading by default.** All trading defaults to **PAPER** (Alpaca paper
  account) and **DRY-RUN** (preview only).
- **Live trading is opt-in three times over.** It requires `ALPACA_PAPER=false`
  *and* `ALLOW_LIVE_TRADING=true` *and* an explicit checkbox in the UI. Automatic
  live execution additionally requires `ALLOW_LIVE_AUTO_EXECUTE=true` and
  `AUTO_EXECUTE=true`.
- **Use at your own risk.** Past holdings are not predictive of future performance.

## What it does

- Resolves a fund (by CIK or name) and pulls the latest 13F-HR via `edgartools`.
- Normalizes 13F values from "thousands" to dollars correctly per filing date and
  consistency checks. Fails closed when ambiguous.
- Parses holdings into a clean DataFrame (ticker, issuer, CUSIP, FIGI, class,
  put/call, value, shares, fund weight).
- Excludes options from auto-trading (toggle to view them in the table).
- Scales each holding to a target portfolio size and computes target shares using
  Alpaca market data (with `yfinance` fallback).
- Compares against current Alpaca positions and produces BUY / SELL / HOLD / SKIP
  actions with a configurable minimum-trade-notional threshold.
- Generates a hashed preview; execution requires a matching hash.
- Submits sells before buys; logs every order and response.
- Detects new 13F filings via accession number and saves dry-run previews to
  `history/`.
- Optional APScheduler in-app or standalone CLI for cron-style automation.

## What it does **not** do

- It does not replicate options strategies. Calls and puts are excluded from
  generated orders. (A toggle exists to display them in the holdings table only.)
- It does not infer underlying-equity exposure from options.
- It does not include short positions, cash management, or non-equity instruments.
- It does not enable margin, complex order types, or after-hours trading.
- It does not auto-execute live trades unless you explicitly flip three flags.

## Project layout

```
app.py                      # Streamlit dashboard
core/
  config.py                 # Typed config from .env / Streamlit secrets
  sec13f_fetcher.py         # edgartools + SEC submissions JSON; normalization
  portfolio_scaler.py       # Pure scaling math
  trade_executor.py         # alpaca-py integration; order submission
  auto_rebalancer.py        # New-filing detection, scheduler, CLI
utils/
  helpers.py                # Logging, JSON state, notifications, formatters
tests/
  test_portfolio_scaler.py
  test_value_normalization.py
.streamlit/
  config.toml               # Dark theme
  secrets.toml.example
.env.example
requirements.txt
state/                      # last_filings.json (created at runtime)
history/                    # Saved rebalance previews (created at runtime)
logs/                        # JSONL event log (created at runtime)
```

## Setup

### 1. Python environment

Requires **Python 3.11+**.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configure environment

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
$EDITOR .env
```

Minimum to fetch holdings:

```ini
EDGAR_IDENTITY="Your Name your.email@example.com"
DEFAULT_CIK=0002045724
TARGET_PORTFOLIO_SIZE=10000
```

To enable Alpaca paper trading also set:

```ini
ALPACA_API_KEY="..."
ALPACA_SECRET_KEY="..."
ALPACA_PAPER=true
```

For Streamlit Cloud, copy `.streamlit/secrets.toml.example` to
`.streamlit/secrets.toml` and use that instead — secrets take precedence over
`.env`.

### 3. Run the app

```bash
streamlit run app.py
```

### 4. CLI rebalancer

```bash
# One-shot dry-run
python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run

# Help
python -m core.auto_rebalancer --help
```

To allow execution from the CLI you must:

1. set `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`,
2. choose a mode (paper or live), and
3. invoke `--no-dry-run`. For live mode you additionally need `ALPACA_PAPER=false`
   and `ALLOW_LIVE_TRADING=true`. For automatic live execution you also need
   `ALLOW_LIVE_AUTO_EXECUTE=true` and `AUTO_EXECUTE=true`.

### 5. Run tests

```bash
pytest -q
```

## Deployment

> ⚠️ Local JSON state (`state/`, `history/`, `logs/`) requires **persistent
> storage**. On ephemeral platforms, mount a persistent volume or substitute a
> database. Without persistence, the new-filing detector cannot remember the last
> seen accession number and will trigger on every run.

### Streamlit Community Cloud (UI only)

Streamlit Cloud is suitable for the dashboard but not for cron jobs. It has no
persistent file system across redeploys.

1. Push your repo to GitHub.
2. Create a new app on share.streamlit.io pointing at `app.py`.
3. Add secrets via the Streamlit Cloud secrets editor (paste `.streamlit/secrets.toml`).
4. Treat it as a UI-only deployment; run the cron from a separate host.

### Render cron job

Create a Render Cron Job with:

- Build command: `pip install -r requirements.txt`
- Schedule: `0 * * * *`
- Command:
  ```
  python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run
  ```
- Persistent disk attached at `/opt/render/project/src/state` and `history` and `logs`,
  or use environment-backed storage.

### Railway cron job

Add a `cron` service:

```
*/60 * * * * python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run
```

Mount a Railway volume for `state/`, `history/`, and `logs/`.

### GitHub Actions (no execution)

`.github/workflows/rebalance.yml`:

```yaml
on:
  schedule:
    - cron: "0 * * * *"
jobs:
  rebalance:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r requirements.txt
      - env:
          EDGAR_IDENTITY: ${{ secrets.EDGAR_IDENTITY }}
        run: |
          python -m core.auto_rebalancer --cik 0002045724 --portfolio-size 10000 --once --dry-run
      - uses: actions/upload-artifact@v4
        with:
          name: history
          path: history/
```

This is suitable for **dry-run** only — Actions has no persistent state between
runs, so live execution is unsafe here.

### Local crontab

```cron
0 * * * * cd /path/to/13f-mirror && /path/to/venv/bin/python -m core.auto_rebalancer \
  --cik 0002045724 --portfolio-size 10000 --once --dry-run >> logs/cron.log 2>&1
```

## Troubleshooting

### Missing `EDGAR_IDENTITY`

You'll see an error in the sidebar like
`EDGAR_IDENTITY is not set. SEC fair-access policy requires identifying the requester.`
Set the variable in `.env` or `.streamlit/secrets.toml` to a string like
`"Your Name you@example.com"`. SEC throttles or blocks anonymous requests.

### Missing Alpaca keys

The app remains read-only. You can still fetch a 13F filing, see scaled targets,
and inspect a (price-only) target portfolio. Portfolio comparison and execution
are disabled.

### Missing tickers

Some 13F line items lack a usable ticker (e.g., warrants, preferreds, private
placements). These are shown in the holdings table but excluded from trade
generation. The UI shows a count.

### Price lookup failures

The app prefers Alpaca market data and falls back to `yfinance`. If neither
returns a positive price for a symbol, that symbol is **skipped** for trading and
shown with a warning. The app will not silently substitute zero or stale prices.

### Non-fractionable assets

For symbols Alpaca marks as `fractionable=false` (e.g., BRK.A), buys are floored
to whole shares. If your target is less than 1 share, the order is suppressed.

### Insufficient buying power

If the estimated buy notional exceeds your Alpaca buying power and there are no
sells in the preview, the **Execute** button is disabled. Either reduce the
target portfolio size or enable mode that produces sells (e.g., already holding
positions outside the target portfolio with full-account rebalance ON).

### "Failed to normalize 13F value"

The normalizer fails closed when neither the dollars nor the thousands
interpretation produces a plausible AUM. Inspect `logs/app.jsonl` for the raw
total and dates; this almost always indicates a parsing problem (e.g., zero
total) rather than a real ambiguity.

## Safety summary (TL;DR)

| Flag                     | Default | Effect                                       |
|--------------------------|---------|----------------------------------------------|
| `ALPACA_PAPER`           | `true`  | Paper trading endpoint                       |
| `ALLOW_LIVE_TRADING`     | `false` | Required to submit live orders               |
| `ALLOW_LIVE_AUTO_EXECUTE`| `false` | Required for *automatic* live execution      |
| `AUTO_EXECUTE`           | `false` | Required for the rebalancer to place orders  |
| Mirror-only mode         | `on`    | Never sells unrelated positions              |
| Full-account rebalance   | `off`   | Opt-in; sells positions not in 13F           |
| Dry-run                  | `on`    | CLI default; orders not submitted            |
| Preview hash             | enforced| Execute requires matching hash               |
