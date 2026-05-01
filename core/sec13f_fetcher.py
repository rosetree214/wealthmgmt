"""SEC 13F-HR fetching and parsing.

Primary source: `edgartools` (https://github.com/dgunning/edgartools).
Secondary fallback: SEC submissions JSON endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional, Tuple

import pandas as pd
import requests

from core.config import AppConfig, normalize_cik
from utils.helpers import log_event, safe_float, safe_int

# SEC final rule on Form 13F technical amendments removed the "in thousands"
# convention; values reported with period of report on or after this cutoff
# are expected to be in actual U.S. dollars. Earlier filings are typically
# reported in thousands. We use the cutoff as a *prior*, then sanity-check.
DOLLAR_REPORTING_CUTOFF = date(2023, 1, 3)

# Sanity-check thresholds for the consistency-based normalizer.
# 13F filers must have ≥ $100M AUM, but holdings can be much larger.
PLAUSIBLE_AUM_MIN = 75_000_000.0       # below this, we suspect "thousands"
PLAUSIBLE_AUM_MAX = 5_000_000_000_000.0  # above this, we suspect already in dollars

SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


@dataclass
class FilingMetadata:
    cik: str
    manager_name: Optional[str]
    accession_number: Optional[str]
    filing_date: Optional[str]
    report_date: Optional[str]
    form_type: Optional[str]
    is_amendment: bool = False
    source: str = "edgartools"

    def to_dict(self) -> dict:
        return {
            "cik": self.cik,
            "manager_name": self.manager_name,
            "accession_number": self.accession_number,
            "filing_date": self.filing_date,
            "report_date": self.report_date,
            "form_type": self.form_type,
            "is_amendment": self.is_amendment,
            "source": self.source,
        }


@dataclass
class HoldingsResult:
    metadata: FilingMetadata
    holdings: pd.DataFrame
    total_value_usd: float
    normalization_convention: str  # "dollars" or "thousands"
    normalization_reason: str
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def configure_edgar_identity(cfg: AppConfig) -> bool:
    """Configure edgartools identity. Returns True iff configured."""
    if not cfg.edgar_identity:
        return False
    try:
        from edgar import set_identity  # type: ignore
        set_identity(cfg.edgar_identity)
        return True
    except ImportError:
        log_event("edgar.import_error", message="edgartools not installed")
        return False


# ---------------------------------------------------------------------------
# Value normalization
# ---------------------------------------------------------------------------
def _is_amendment(form_type: Optional[str]) -> bool:
    return bool(form_type and "/A" in form_type.upper())


def _coerce_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def normalize_13f_value_to_usd(
    raw_total_value: float,
    *,
    filing_date: Any = None,
    report_date: Any = None,
    metadata_unit_hint: Optional[str] = None,
    n_positions: Optional[int] = None,
) -> Tuple[float, str, str]:
    """Determine whether 13F values are in dollars or thousands and convert.

    Returns ``(normalized_total_usd, convention, reason)`` where ``convention``
    is ``"dollars"`` or ``"thousands"``. Raises ``ValueError`` if the value
    cannot be normalized confidently (fail-closed behavior).
    """
    if raw_total_value is None or raw_total_value <= 0:
        raise ValueError("raw_total_value must be a positive number")

    fd = _coerce_date(filing_date)
    rd = _coerce_date(report_date)

    # 1. Explicit metadata hint wins.
    if metadata_unit_hint:
        hint = metadata_unit_hint.strip().lower()
        if hint in {"dollars", "usd", "$"}:
            return raw_total_value, "dollars", "explicit metadata hint=dollars"
        if hint in {"thousands", "k", "$000"}:
            return raw_total_value * 1000.0, "thousands", "explicit metadata hint=thousands"

    # 2. Filing date prior. Form 13F amendments effective 2023-01-03 require
    # values in actual dollars. Use the more reliable of report_date / filing_date.
    cutoff_basis = rd or fd
    prior: Optional[str] = None
    if cutoff_basis is not None:
        prior = "dollars" if cutoff_basis >= DOLLAR_REPORTING_CUTOFF else "thousands"

    # 3. Sanity check using plausible AUM ranges.
    as_dollars_total = raw_total_value
    as_thousands_total = raw_total_value * 1000.0

    plausible_dollars = PLAUSIBLE_AUM_MIN <= as_dollars_total <= PLAUSIBLE_AUM_MAX
    plausible_thousands = PLAUSIBLE_AUM_MIN <= as_thousands_total <= PLAUSIBLE_AUM_MAX

    # If only one interpretation is plausible, accept it.
    if plausible_dollars and not plausible_thousands:
        return as_dollars_total, "dollars", "sanity-check: only dollars within plausible AUM range"
    if plausible_thousands and not plausible_dollars:
        return as_thousands_total, "thousands", "sanity-check: only thousands within plausible AUM range"

    # 4. If both are plausible, use the date prior.
    if prior == "dollars" and plausible_dollars:
        return as_dollars_total, "dollars", f"date prior (cutoff {DOLLAR_REPORTING_CUTOFF}) and plausible"
    if prior == "thousands" and plausible_thousands:
        return as_thousands_total, "thousands", f"date prior (cutoff {DOLLAR_REPORTING_CUTOFF}) and plausible"

    # 5. Neither plausible: fail closed.
    raise ValueError(
        "Cannot confidently normalize 13F total value: "
        f"raw={raw_total_value}, filing_date={fd}, report_date={rd}. "
        f"Plausible-as-dollars={plausible_dollars}, plausible-as-thousands={plausible_thousands}."
    )


# ---------------------------------------------------------------------------
# Latest filing detection
# ---------------------------------------------------------------------------
def _http_headers(cfg: AppConfig) -> dict:
    ua = cfg.edgar_identity or "13F Mirror Trader contact@example.com"
    return {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}


def fetch_latest_filing_metadata(cik: str, cfg: AppConfig) -> FilingMetadata:
    """Return metadata for the latest 13F-HR (or 13F-HR/A) filing for ``cik``.

    Tries edgartools first, falls back to the SEC submissions JSON endpoint.
    """
    cik10 = normalize_cik(cik)

    # 1. edgartools
    if configure_edgar_identity(cfg):
        try:
            from edgar import Company  # type: ignore

            company = Company(int(cik10))
            filings = company.get_filings(form=["13F-HR", "13F-HR/A"])
            if filings is not None and len(filings) > 0:
                latest = filings[0]
                form_type = getattr(latest, "form", None) or "13F-HR"
                manager_name = (
                    getattr(company, "name", None)
                    or getattr(company, "company_name", None)
                )
                report_date = getattr(latest, "report_date", None) or getattr(
                    latest, "period_of_report", None
                )
                return FilingMetadata(
                    cik=cik10,
                    manager_name=str(manager_name) if manager_name else None,
                    accession_number=str(getattr(latest, "accession_number", "") or "") or None,
                    filing_date=str(getattr(latest, "filing_date", "") or "") or None,
                    report_date=str(report_date) if report_date else None,
                    form_type=str(form_type),
                    is_amendment=_is_amendment(str(form_type)),
                    source="edgartools",
                )
        except Exception as exc:
            log_event("edgar.latest_error", cik=cik10, error=str(exc))

    # 2. SEC submissions JSON fallback
    try:
        resp = requests.get(
            SEC_SUBMISSIONS_URL.format(cik=cik10),
            headers=_http_headers(cfg),
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accs = recent.get("accessionNumber", [])
        fdates = recent.get("filingDate", [])
        rdates = recent.get("reportDate", [])
        for i, form in enumerate(forms):
            if form in ("13F-HR", "13F-HR/A"):
                return FilingMetadata(
                    cik=cik10,
                    manager_name=data.get("name"),
                    accession_number=accs[i] if i < len(accs) else None,
                    filing_date=fdates[i] if i < len(fdates) else None,
                    report_date=rdates[i] if i < len(rdates) else None,
                    form_type=form,
                    is_amendment=_is_amendment(form),
                    source="sec_json",
                )
        raise RuntimeError(f"No 13F-HR filing found for CIK {cik10}")
    except requests.RequestException as exc:
        raise RuntimeError(f"SEC submissions request failed for {cik10}: {exc}") from exc


# ---------------------------------------------------------------------------
# Resolve fund name -> CIK
# ---------------------------------------------------------------------------
def resolve_fund_to_cik(query: str, cfg: AppConfig) -> Optional[str]:
    """Resolve either a CIK or a fund name to a 10-digit CIK."""
    q = (query or "").strip()
    if not q:
        return None
    digits = q.replace("-", "").replace(" ", "")
    if digits.lower().startswith("cik"):
        digits = digits[3:].lstrip("-_")
    if digits.isdigit():
        try:
            return normalize_cik(digits)
        except ValueError:
            return None

    # Name lookup: prefer edgartools, fall back to SEC company tickers list.
    if configure_edgar_identity(cfg):
        try:
            from edgar import find_company  # type: ignore

            results = find_company(q)
            if results is not None and len(results) > 0:
                first = results[0]
                cik = getattr(first, "cik", None)
                if cik is not None:
                    return normalize_cik(cik)
        except Exception as exc:
            log_event("edgar.find_company_error", query=q, error=str(exc))

    try:
        resp = requests.get(
            "https://www.sec.gov/cgi-bin/browse-edgar",
            params={"action": "getcompany", "company": q, "type": "13F", "output": "atom"},
            headers=_http_headers(cfg),
            timeout=20,
        )
        if resp.ok:
            import re

            m = re.search(r"CIK=(\d{4,10})", resp.text)
            if m:
                return normalize_cik(m.group(1))
    except requests.RequestException as exc:
        log_event("sec.browse_edgar_error", query=q, error=str(exc))

    return None


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------
_COL_ALIASES = {
    "ticker": ["ticker", "symbol", "tickerSymbol"],
    "issuer": ["issuer", "name_of_issuer", "nameOfIssuer", "issuerName", "Issuer", "name"],
    "cusip": ["cusip", "Cusip", "CUSIP"],
    "figi": ["figi", "FIGI", "compositeFigi"],
    "security_class": ["title_of_class", "titleOfClass", "class", "securityClass"],
    "put_call": ["put_call", "putCall", "PutCall", "option_type"],
    "value": ["value", "Value", "marketValue", "valueOfHolding", "value_usd"],
    "shares": ["shares", "shrsOrPrnAmt", "sharesOrPrincipal", "shrsOrPrnAmtType",
               "sshPrnamt", "ssh_prnamt", "principalAmount"],
    "shares_type": ["sshPrnamtType", "ssh_prnamt_type", "shrsOrPrnAmtType", "shares_type"],
}


def _pick(row: Mapping, names: list[str]) -> Any:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def _normalize_holdings_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(
            columns=[
                "ticker", "issuer", "cusip", "figi", "security_class",
                "put_call", "shares", "shares_type", "value_raw",
            ]
        )
    rows = []
    for _, raw in df.iterrows():
        record = {col: raw.get(col) for col in df.columns}
        rows.append(
            {
                "ticker": _pick(record, _COL_ALIASES["ticker"]),
                "issuer": _pick(record, _COL_ALIASES["issuer"]),
                "cusip": _pick(record, _COL_ALIASES["cusip"]),
                "figi": _pick(record, _COL_ALIASES["figi"]),
                "security_class": _pick(record, _COL_ALIASES["security_class"]),
                "put_call": _pick(record, _COL_ALIASES["put_call"]),
                "shares": safe_float(_pick(record, _COL_ALIASES["shares"])),
                "shares_type": _pick(record, _COL_ALIASES["shares_type"]),
                "value_raw": safe_float(_pick(record, _COL_ALIASES["value"])),
            }
        )
    out = pd.DataFrame(rows)
    # Normalize string casing on a few columns.
    for col in ("ticker", "cusip", "put_call", "shares_type", "security_class"):
        if col in out.columns:
            out[col] = out[col].astype("object").apply(
                lambda v: v.strip().upper() if isinstance(v, str) else v
            )
    return out


def fetch_holdings(cik: str, cfg: AppConfig) -> HoldingsResult:
    """Fetch and parse the latest 13F-HR holdings for ``cik``."""
    cik10 = normalize_cik(cik)
    metadata = fetch_latest_filing_metadata(cik10, cfg)

    if not configure_edgar_identity(cfg):
        raise RuntimeError(
            "EDGAR_IDENTITY is not configured. Set it in .env or Streamlit secrets."
        )

    from edgar import Company  # type: ignore

    company = Company(int(cik10))
    filings = company.get_filings(form=["13F-HR", "13F-HR/A"])
    if filings is None or len(filings) == 0:
        raise RuntimeError(f"No 13F-HR filings available for CIK {cik10}")

    # Match the metadata accession number if possible; else use the latest.
    target = filings[0]
    if metadata.accession_number:
        for f in filings:
            if str(getattr(f, "accession_number", "")) == metadata.accession_number:
                target = f
                break

    obj = target.obj()
    raw_df = None
    metadata_unit_hint: Optional[str] = None

    for attr in ("infotable", "holdings", "to_pandas", "data"):
        if hasattr(obj, attr):
            try:
                candidate = getattr(obj, attr)
                if callable(candidate):
                    candidate = candidate()
                if isinstance(candidate, pd.DataFrame) and len(candidate) > 0:
                    raw_df = candidate
                    break
            except Exception:
                continue

    if raw_df is None:
        raise RuntimeError("edgartools returned no parsable holdings table")

    # Some edgartools versions expose the reporting unit via attributes.
    for attr in ("value_unit", "valueUnit", "reporting_unit"):
        if hasattr(obj, attr):
            try:
                metadata_unit_hint = str(getattr(obj, attr) or "")
                break
            except Exception:
                pass

    holdings = _normalize_holdings_dataframe(raw_df)

    raw_total = float(holdings["value_raw"].fillna(0).sum()) if "value_raw" in holdings else 0.0
    if raw_total <= 0:
        raise RuntimeError("Total raw 13F value is non-positive; refusing to proceed")

    normalized_total, convention, reason = normalize_13f_value_to_usd(
        raw_total,
        filing_date=metadata.filing_date,
        report_date=metadata.report_date,
        metadata_unit_hint=metadata_unit_hint,
        n_positions=len(holdings),
    )

    multiplier = 1000.0 if convention == "thousands" else 1.0
    holdings["value_usd"] = holdings["value_raw"].fillna(0) * multiplier
    total_value_usd = float(holdings["value_usd"].sum())
    holdings["fund_weight"] = (
        holdings["value_usd"] / total_value_usd if total_value_usd > 0 else 0.0
    )

    holdings = holdings.sort_values("fund_weight", ascending=False).reset_index(drop=True)

    warnings: list[str] = []
    missing_tickers = int(holdings["ticker"].isna().sum() + (holdings["ticker"] == "").sum())
    if missing_tickers > 0:
        warnings.append(
            f"{missing_tickers} holdings have no ticker (often private placements / non-equity); "
            "they are excluded from trading by default."
        )
    options = holdings["put_call"].fillna("").astype(str).str.upper().isin({"PUT", "CALL"})
    n_options = int(options.sum())
    if n_options:
        warnings.append(
            f"{n_options} options positions (PUT/CALL) detected; excluded from auto-trading."
        )

    log_event(
        "filing.parsed",
        cik=cik10,
        accession=metadata.accession_number,
        n_holdings=len(holdings),
        total_value_usd=total_value_usd,
        normalization=convention,
        normalization_reason=reason,
        unit_hint=metadata_unit_hint,
    )

    return HoldingsResult(
        metadata=metadata,
        holdings=holdings,
        total_value_usd=total_value_usd,
        normalization_convention=convention,
        normalization_reason=reason,
        warnings=warnings,
    )


