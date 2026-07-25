"""ISIN -> Yahoo Finance symbol, so the user does not have to type the ticker.

Two steps, both best effort. If neither resolves, the function returns None and
the symbol stays a manual field:

  1. yfinance.Search(isin): every symbol Yahoo suggests is probed with a price
     fetch, preferring symbols quoted in EUR (.F/.DE/.MC/.AS/.MI/.PA ...). This
     is what resolves funds (0P....F), which OpenFIGI does not return.
  2. OpenFIGI (https://www.openfigi.com/api, free): maps ISIN -> ticker +
     exchange code, converted to a Yahoo symbol with the exchange suffix. Good
     for listed ETFs and shares.

Crypto does not go through here: see prices.crypto_symbol (BTC -> BTC-EUR).
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger("symbol_lookup")

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
TIMEOUT = 15

# OpenFIGI exchange code -> Yahoo Finance suffix. The common European venues,
# easy to extend.
EXCH_TO_YAHOO_SUFFIX = {
    "GR": ".DE", "GY": ".DE", "GF": ".DE",   # Germany (Xetra / Frankfurt)
    "SM": ".MC",                              # Spain (BME Madrid)
    "FP": ".PA",                              # France (Euronext Paris)
    "NA": ".AS",                              # Netherlands (Euronext Amsterdam)
    "BB": ".BR",                              # Belgium (Euronext Brussels)
    "LN": ".L",                               # United Kingdom (LSE)
    "IM": ".MI",                              # Italy (Borsa Italiana)
    "SW": ".SW", "VX": ".SW",                 # Switzerland
    "PL": ".LS",                              # Portugal (Euronext Lisbon)
    "US": "", "UN": "", "UW": "", "UQ": "",   # United States (no suffix)
}

# Preferred venues when OpenFIGI returns several: EUR and Europe first.
_PREF_ORDER = ["GR", "GY", "GF", "SM", "FP", "NA", "BB", "IM", "SW", "LN",
               "US", "UN", "UW", "UQ"]

_cache: dict[str, str | None] = {}

# Yahoo suffixes of venues quoted in EUR.
_EUR_SUFFIXES = (".F", ".DE", ".MC", ".AS", ".MI", ".PA", ".BR", ".LS", ".VI", ".SW")


def _yahoo_search(isin: str) -> str | None:
    """Resolve through yfinance.Search by probing prices, EUR symbols first."""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        quotes = getattr(yf.Search(isin), "quotes", None) or []
    except Exception as exc:
        log.warning("yfinance.Search failed for %s: %s", isin, exc)
        return None

    from app.services.prices import fetch_price

    symbols = [q.get("symbol") for q in quotes if q.get("symbol")]
    # Stable sort: EUR venues first, order inside each group untouched.
    symbols.sort(key=lambda s: 0 if s.upper().endswith(_EUR_SUFFIXES) else 1)
    for sym in symbols:
        try:
            if fetch_price(sym) is not None:
                return sym
        except Exception:
            continue
    return None


def _to_yahoo(ticker: str, exch_code: str) -> str | None:
    if not ticker:
        return None
    if exch_code not in EXCH_TO_YAHOO_SUFFIX:
        return None
    base = ticker.strip().upper().replace(" ", "-")
    return f"{base}{EXCH_TO_YAHOO_SUFFIX[exch_code]}"


def _pick(data: list[dict]) -> str | None:
    """Choose the best OpenFIGI match and map it to a Yahoo symbol."""
    candidates = [d for d in data if d.get("ticker")]
    if not candidates:
        return None

    def rank(d: dict) -> int:
        ec = d.get("exchCode", "")
        return _PREF_ORDER.index(ec) if ec in _PREF_ORDER else len(_PREF_ORDER)

    for d in sorted(candidates, key=rank):
        sym = _to_yahoo(d.get("ticker", ""), d.get("exchCode", ""))
        if sym is not None:
            return sym
    return None


def _openfigi(isin: str) -> str | None:
    """Map ISIN -> Yahoo symbol through OpenFIGI. None if it fails."""
    headers = {"Content-Type": "application/json"}
    if settings.openfigi_api_key:
        headers["X-OPENFIGI-APIKEY"] = settings.openfigi_api_key
    try:
        r = httpx.post(OPENFIGI_URL, headers=headers,
                       json=[{"idType": "ID_ISIN", "value": isin}], timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning("OpenFIGI returned %s for %s", r.status_code, isin)
            return None
        payload = r.json()
        data = (payload[0].get("data") if payload and isinstance(payload, list) else None) or []
        return _pick(data)
    except Exception as exc:
        log.warning("OpenFIGI failed for %s: %s", isin, exc)
        return None


def isin_to_yahoo(isin: str) -> str | None:
    """Resolve an ISIN to a Yahoo symbol. None when it cannot be resolved."""
    isin = (isin or "").strip().upper()
    if len(isin) != 12:
        return None
    if isin in _cache:
        return _cache[isin]

    sym = _yahoo_search(isin) or _openfigi(isin)
    _cache[isin] = sym
    return sym
