"""ISIN -> Yahoo symbol tests (OpenFIGI mocked, no network)."""
import httpx
import pytest

from app.services import symbol_lookup as sl


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    sl._cache.clear()
    # By default step 1 (yfinance.Search) resolves nothing, so these tests
    # exercise the OpenFIGI fallback. The Search tests override it.
    monkeypatch.setattr(sl, "_yahoo_search", lambda isin: None)
    yield
    sl._cache.clear()


def test_maps_xetra_to_de(monkeypatch):
    def fake_post(url, **kw):
        return _FakeResp(200, [{"data": [{"ticker": "SXR8", "exchCode": "GY"}]}])
    monkeypatch.setattr(httpx, "post", fake_post)
    assert sl.isin_to_yahoo("IE00B5BMR087") == "SXR8.DE"


def test_prefers_europe_over_us(monkeypatch):
    def fake_post(url, **kw):
        return _FakeResp(200, [{"data": [
            {"ticker": "AAPL", "exchCode": "US"},
            {"ticker": "APC", "exchCode": "GY"},
        ]}])
    monkeypatch.setattr(httpx, "post", fake_post)
    # GY (.DE) ranks above US in _PREF_ORDER
    assert sl.isin_to_yahoo("US0378331005") == "APC.DE"


def test_us_has_no_suffix(monkeypatch):
    def fake_post(url, **kw):
        return _FakeResp(200, [{"data": [{"ticker": "AAPL", "exchCode": "US"}]}])
    monkeypatch.setattr(httpx, "post", fake_post)
    assert sl.isin_to_yahoo("US0378331005") == "AAPL"


def test_invalid_isin():
    assert sl.isin_to_yahoo("XXX") is None
    assert sl.isin_to_yahoo("") is None


def test_no_match(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _FakeResp(200, [{"data": []}]))
    assert sl.isin_to_yahoo("IE00B5BMR087") is None


def test_network_error_returns_none(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(httpx, "post", boom)
    assert sl.isin_to_yahoo("IE00B5BMR087") is None


def test_result_is_cached(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        return _FakeResp(200, [{"data": [{"ticker": "SXR8", "exchCode": "GY"}]}])
    monkeypatch.setattr(httpx, "post", fake_post)
    sl.isin_to_yahoo("IE00B5BMR087")
    sl.isin_to_yahoo("IE00B5BMR087")
    assert calls["n"] == 1  # the second call comes from the cache


# --- Step 1: yfinance.Search, which resolves funds (0P...) ---

class _FakeSearch:
    def __init__(self, query):
        self.quotes = _FakeSearch.QUOTES

    QUOTES: list = []


_real_search = sl._yahoo_search  # captured at import, before any stub


def test_search_resolves_a_fund_in_eur(monkeypatch):
    # Yahoo Search returns the EUR share class (.F) and a USD one; EUR wins.
    quotes = [{"symbol": "0P0001CJGW"}, {"symbol": "0P0001CLDK.F"}]
    quoted = {"0P0001CJGW": 15.0, "0P0001CLDK.F": 13.63}
    monkeypatch.setattr(sl, "_yahoo_search", _real_search)
    import yfinance
    from decimal import Decimal
    from app.services import prices
    _FakeSearch.QUOTES = quotes
    monkeypatch.setattr(yfinance, "Search", _FakeSearch, raising=False)
    monkeypatch.setattr(prices, "fetch_price",
                        lambda s: (Decimal(str(quoted[s])) if quoted.get(s) else None))
    assert sl.isin_to_yahoo("IE00BYX5NX33") == "0P0001CLDK.F"


def test_search_without_prices_falls_back_to_openfigi(monkeypatch):
    monkeypatch.setattr(sl, "_yahoo_search", _real_search)
    import yfinance
    from app.services import prices
    _FakeSearch.QUOTES = [{"symbol": "FOO.XX"}]
    monkeypatch.setattr(yfinance, "Search", _FakeSearch, raising=False)
    monkeypatch.setattr(prices, "fetch_price", lambda s: None)
    monkeypatch.setattr(httpx, "post",
                        lambda url, **kw: _FakeResp(200, [{"data": [{"ticker": "SXR8", "exchCode": "GY"}]}]))
    assert sl.isin_to_yahoo("IE00B5BMR087") == "SXR8.DE"
