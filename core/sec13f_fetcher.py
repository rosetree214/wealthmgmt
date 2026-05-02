from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd
import requests

from core.config import AppConfig, load_config
from utils.helpers import log_event, normalize_cik, parse_date


@dataclass(frozen=True)
class FilingMetadata:
    manager_name: str
    cik: str
    accession_number: str
    filing_date: date | None
    report_date: date | None
    form_type: str
    is_amendment: bool | None = None
    source: str = "edgartools"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("filing_date", "report_date"):
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return data


def normalize_13f_value_to_usd(
    values: Iterable[Any],
    *,
    filing_date: date | datetime | str | None,
    report_date: date | datetime | str | None,
    total_value_usd: float | None = None,
    metadata_units: str | None = None,
) -> tuple[list[float], str]:
    """Normalize 13F holding values to dollars with explicit confidence gates.

    SEC 13F information tables historically used value-in-thousands. Newer XML
    feeds and parser layers may expose dollar values. This function avoids a
    blanket multiplier by preferring explicit metadata, then total-value
    consistency, then filing/report-date convention.
    """

    numeric_values = [float(value or 0) for value in values]
    if any(value < 0 for value in numeric_values):
        raise ValueError("Cannot confidently normalize negative 13F values")

    units = (metadata_units or "").strip().lower()
    if units in {"usd", "dollar", "dollars"}:
        return numeric_values, "dollars_by_metadata"
    if units in {"thousand", "thousands", "thousands_usd", "x1000"}:
        return [value * 1000 for value in numeric_values], "thousands_by_metadata"

    raw_total = sum(numeric_values)
    if raw_total <= 0:
        raise ValueError("Cannot confidently normalize empty or zero 13F values")

    if total_value_usd and total_value_usd > 0:
        dollar_error = abs(raw_total - total_value_usd) / total_value_usd
        thousand_total = raw_total * 1000
        thousand_error = abs(thousand_total - total_value_usd) / total_value_usd
        if dollar_error <= 0.05 and dollar_error <= thousand_error:
            return numeric_values, "dollars_by_total_value"
        if thousand_error <= 0.05 and thousand_error < dollar_error:
            return [value * 1000 for value in numeric_values], "thousands_by_total_value"

    filing = parse_date(filing_date)
    report = parse_date(report_date)
    effective_date = filing or report
    if effective_date is None:
        raise ValueError("Cannot confidently normalize 13F values without dates or total value")

    # The SEC's modern structured data and parser ecosystem increasingly exposes
    # dollar values for recent filings. Legacy dates remain value-in-thousands.
    if effective_date >= date(2023, 1, 1):
        return numeric_values, "dollars_by_modern_date"
    return [value * 1000 for value in numeric_values], "thousands_by_legacy_date"


def _resolve_company(identifier: str, settings: AppConfig):
    from edgar import Company

    query = identifier.strip()
    if query.isdigit():
        query = normalize_cik(query)
    else:
        resolved = _resolve_cik_by_company_name(query, settings)
        if resolved:
            query = resolved
    return Company(query)


def _identifier_to_cik(identifier: str, settings: AppConfig) -> str:
    if identifier.strip().isdigit():
        return normalize_cik(identifier)
    resolved = _resolve_cik_by_company_name(identifier, settings)
    if resolved:
        return resolved
    raise ValueError("A numeric CIK is required when manager-name lookup fails")


def _resolve_cik_by_company_name(name: str, settings: AppConfig) -> str | None:
    try:
        if not settings.edgar_identity:
            raise ValueError("EDGAR_IDENTITY is required for SEC company-name lookup")
        headers = {"User-Agent": settings.edgar_identity}
        response = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=20)
        response.raise_for_status()
        needle = name.strip().lower()
        for record in response.json().values():
            title = str(record.get("title", "")).lower()
            if needle == title or needle in title:
                return normalize_cik(str(record.get("cik_str")))
    except Exception as exc:
        log_event("company_name_resolution_failed", details={"name": name, "error": str(exc)})
    return None


def _call_first(obj: Any, names: list[str], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        attr = getattr(obj, name, None)
        if callable(attr):
            try:
                return attr(*args, **kwargs)
            except TypeError:
                continue
    return None


def _latest_13f_from_edgartools(identifier: str, settings: AppConfig) -> tuple[Any, FilingMetadata]:
    from edgar import set_identity

    if not settings.edgar_identity:
        raise ValueError("EDGAR_IDENTITY is required to fetch SEC filings")
    set_identity(settings.edgar_identity)

    company = _resolve_company(identifier, settings)
    filings = _call_first(company, ["get_filings"], form="13F-HR") or _call_first(
        company, ["get_filings"], form=["13F-HR", "13F-HR/A"]
    )
    if filings is None:
        raise ValueError("Could not query edgartools filings for the selected manager")

    latest = _call_first(filings, ["latest"])
    if latest is None:
        try:
            latest = next(iter(filings))
        except StopIteration as exc:
            raise ValueError("No 13F-HR filings found for the selected manager") from exc

    accession = (
        getattr(latest, "accession_number", None)
        or getattr(latest, "accession_no", None)
        or getattr(latest, "accession", None)
        or ""
    )
    filing_date = parse_date(getattr(latest, "filing_date", None) or getattr(latest, "filed", None))
    report_date = parse_date(
        getattr(latest, "report_date", None)
        or getattr(latest, "period_of_report", None)
        or getattr(latest, "period", None)
    )
    form_type = str(getattr(latest, "form", None) or getattr(latest, "form_type", None) or "13F-HR")
    cik = normalize_cik(getattr(company, "cik", None) or identifier)
    manager_name = str(getattr(company, "name", None) or getattr(latest, "company", None) or identifier)

    metadata = FilingMetadata(
        manager_name=manager_name,
        cik=cik,
        accession_number=str(accession),
        filing_date=filing_date,
        report_date=report_date,
        form_type=form_type,
        is_amendment="/A" in form_type,
    )
    return latest, metadata


def get_latest_filing_metadata(identifier: str, settings: AppConfig | None = None) -> FilingMetadata:
    settings = settings or load_config()
    try:
        _, metadata = _latest_13f_from_edgartools(identifier, settings)
        return metadata
    except Exception as exc:
        log_event("edgartools_metadata_failed", cik=identifier, details={"error": str(exc)})
        if not settings.edgar_identity:
            raise ValueError("EDGAR_IDENTITY is required for SEC submissions fallback") from exc
        cik = _identifier_to_cik(identifier, settings)
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        headers = {"User-Agent": settings.edgar_identity}
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        for index, form in enumerate(forms):
            if str(form).startswith("13F-HR"):
                return FilingMetadata(
                    manager_name=payload.get("name") or identifier,
                    cik=cik,
                    accession_number=recent.get("accessionNumber", [""])[index],
                    filing_date=parse_date(recent.get("filingDate", [None])[index]),
                    report_date=parse_date(recent.get("reportDate", [None])[index]),
                    form_type=form,
                    is_amendment="/A" in form,
                    source="sec_submissions_json",
                )
        raise ValueError("No 13F-HR filings found in SEC submissions JSON")


def _extract_holdings_from_filing(filing: Any) -> pd.DataFrame:
    obj = _call_first(filing, ["obj", "xbrl", "thirteenf"])
    candidates = [obj, filing]
    for candidate in candidates:
        if candidate is None:
            continue
        table = _call_first(candidate, ["infotable", "information_table", "holdings", "get_holdings"])
        if table is None:
            continue
        if isinstance(table, pd.DataFrame):
            return table.copy()
        if hasattr(table, "to_dataframe"):
            return table.to_dataframe()
        try:
            return pd.DataFrame(table)
        except Exception:
            continue
    raise ValueError("edgartools did not expose a parseable 13F holdings table")


def _pick(row: pd.Series, names: list[str]) -> Any:
    lowered = {str(key).lower().replace(" ", "_"): key for key in row.index}
    for name in names:
        key = lowered.get(name.lower().replace(" ", "_"))
        if key is not None:
            return row[key]
    return None


def clean_holdings(
    raw: pd.DataFrame,
    metadata: FilingMetadata,
    *,
    total_value_usd: float | None = None,
    metadata_units: str | None = None,
) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    raw_values: list[float] = []
    for _, row in raw.iterrows():
        raw_value = _pick(row, ["value", "value_usd", "reported_value", "market_value"])
        raw_values.append(float(raw_value or 0))

    normalized_values, convention = normalize_13f_value_to_usd(
        raw_values,
        filing_date=metadata.filing_date,
        report_date=metadata.report_date,
        total_value_usd=total_value_usd,
        metadata_units=metadata_units,
    )
    log_event(
        "13f_value_normalized",
        cik=metadata.cik,
        accession_number=metadata.accession_number,
        details={"convention": convention},
    )

    for idx, (_, row) in enumerate(raw.iterrows()):
        put_call = _pick(row, ["put_call", "putcall", "put_call_indicator"])
        ticker = _pick(row, ["ticker", "symbol"])
        cusip = _pick(row, ["cusip"])
        shares = _pick(row, ["shares", "sshprnamt", "share_amount", "principal_amount"])
        record = {
            "ticker": str(ticker).upper().strip() if ticker not in (None, "") else None,
            "company_name": _pick(row, ["name_of_issuer", "issuer", "company_name", "nameofissuer"]),
            "cusip": str(cusip).strip() if cusip not in (None, "") else None,
            "figi": _pick(row, ["figi"]),
            "security_class": _pick(row, ["title_of_class", "class", "titleofclass"]),
            "put_call": str(put_call).upper().strip() if put_call not in (None, "") else None,
            "reported_value_usd": normalized_values[idx],
            "reported_shares": float(shares or 0),
        }
        record["is_option"] = record["put_call"] in {"PUT", "CALL"}
        record["warnings"] = ""
        warnings = []
        if not record["ticker"]:
            warnings.append("Missing ticker; excluded from trading")
        if record["is_option"]:
            warnings.append("Options excluded from auto-trading")
        record["warnings"] = "; ".join(warnings)
        records.append(record)

    holdings = pd.DataFrame(records)
    total = holdings["reported_value_usd"].sum()
    if total <= 0:
        raise ValueError("13F holdings total value is zero after normalization")
    holdings["original_fund_weight"] = holdings["reported_value_usd"] / total
    holdings["is_trade_eligible"] = [
        bool(ticker) and not bool(is_option)
        for ticker, is_option in zip(holdings["ticker"], holdings["is_option"], strict=True)
    ]
    holdings["is_trade_eligible"] = holdings["is_trade_eligible"].astype(object)
    return holdings.sort_values("original_fund_weight", ascending=False).reset_index(drop=True)


def _extract_total_value_usd(filing: Any, raw: pd.DataFrame | None = None) -> float | None:
    for obj in [filing, getattr(filing, "summary", None), getattr(filing, "cover_page", None)]:
        if obj is None:
            continue
        for attr in ("total_value_usd", "total_value", "table_value_total", "aggregate_value"):
            value = getattr(obj, attr, None)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
    if raw is not None:
        for column in ("total_value_usd", "total_value"):
            if column in raw.columns:
                values = pd.to_numeric(raw[column], errors="coerce").dropna()
                if not values.empty:
                    return float(values.iloc[0])
    return None


def _extract_value_units(filing: Any, raw: pd.DataFrame | None = None) -> str | None:
    for obj in [filing, getattr(filing, "summary", None), getattr(filing, "cover_page", None)]:
        if obj is None:
            continue
        for attr in ("value_units", "units", "value_unit", "table_value_units"):
            value = getattr(obj, attr, None)
            if value not in (None, ""):
                return str(value)
    if raw is not None:
        for column in ("value_units", "units", "value_unit"):
            if column in raw.columns:
                values = raw[column].dropna()
                if not values.empty:
                    return str(values.iloc[0])
    return None


def fetch_latest_13f_holdings(identifier: str, settings: AppConfig | None = None) -> tuple[FilingMetadata, pd.DataFrame]:
    settings = settings or load_config()
    filing, metadata = _latest_13f_from_edgartools(identifier, settings)
    raw = _extract_holdings_from_filing(filing)
    metadata_units = _extract_value_units(filing, raw)
    total_value_usd = _extract_total_value_usd(filing, raw)
    holdings = clean_holdings(raw, metadata, total_value_usd=total_value_usd, metadata_units=metadata_units)
    log_event(
        "13f_holdings_fetched",
        cik=metadata.cik,
        accession_number=metadata.accession_number,
        details={"rows": len(holdings)},
    )
    return metadata, holdings
