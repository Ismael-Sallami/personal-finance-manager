"""Minimal Enable Banking client (PSD2, Account Information only).

Every call is signed with a short-lived (1 h) RS256 JWT, made with the
application's private key (`EB_PRIVATE_KEY`) and `kid` = `EB_APP_ID`. The key
never leaves this module.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import jwt

from app.config import settings

API = "https://api.enablebanking.com"
COUNTRY = "ES"  # change it to the country of your banks
TIMEOUT = 30


class EBError(RuntimeError):
    pass


_PEM = re.compile(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", re.S)


def _key() -> str:
    """A well-formed PEM even if it was pasted on one line, with spaces, with
    literal '\\n' or with quotes (it happens when pasting it in a host's panel)."""
    k = settings.eb_private_key.strip().strip('"').strip("'").replace("\\n", "\n")
    m = _PEM.search(k)
    if not m:
        raise EBError("EB_PRIVATE_KEY does not look like a PEM (missing -----BEGIN ... KEY-----)")
    kind, body = m.group(1), re.sub(r"\s+", "", m.group(2))
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {kind}-----\n{lines}\n-----END {kind}-----\n"


def _headers() -> dict:
    now = int(time.time())
    token = jwt.encode(
        {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
         "iat": now, "exp": now + 3600},
        _key(), algorithm="RS256", headers={"kid": settings.eb_app_id.strip()},
    )
    return {"Authorization": f"Bearer {token}"}


def _call(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, API + path, headers=_headers(), timeout=TIMEOUT, **kw)
    if r.is_error:
        raise EBError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def aspsps(country: str = COUNTRY) -> list[dict]:
    """Banks available in a country (2-letter ISO code)."""
    return _call("GET", "/aspsps", params={"country": country.upper()})["aspsps"]


def aspsp(bank: str, country: str = COUNTRY) -> dict:
    """The bank's record (exact name, maximum consent validity...)."""
    for b in aspsps(country):
        if b["name"].lower() == bank.lower():
            return b
    raise EBError(f"bank not found: {bank} ({country})")


def start_auth(bank: str, redirect_url: str, state: str, country: str = COUNTRY) -> str:
    """Ask for authorisation and return the URL the user opens at their bank."""
    b = aspsp(bank, country)
    secs = b.get("maximum_consent_validity") or 90 * 86400
    until = datetime.now(timezone.utc) + timedelta(seconds=secs - 3600)
    psu = "personal" if "personal" in (b.get("psu_types") or ["personal"]) else b["psu_types"][0]
    r = _call("POST", "/auth", json={
        "access": {"valid_until": until.isoformat()},
        "aspsp": {"name": b["name"], "country": country.upper()},
        "state": state,
        "redirect_url": redirect_url,
        "psu_type": psu,
    })
    return r["url"]


def create_session(code: str) -> dict:
    return _call("POST", "/sessions", json={"code": code})


def get_session(session_id: str) -> dict:
    return _call("GET", f"/sessions/{session_id}")


def balances(uid: str) -> list[dict]:
    return _call("GET", f"/accounts/{uid}/balances").get("balances", [])


def transactions(uid: str, date_from: date) -> list[dict]:
    """Every movement since `date_from`, following the pagination."""
    out: list[dict] = []
    params: dict = {"date_from": date_from.isoformat()}
    for _ in range(50):  # defensive cap on pages
        r = _call("GET", f"/accounts/{uid}/transactions", params=params)
        out.extend(r.get("transactions") or [])
        key = r.get("continuation_key")
        if not key:
            break
        params = {"date_from": date_from.isoformat(), "continuation_key": key}
    return out


def delete_session(session_id: str) -> None:
    """Revoke the consent at Enable Banking (that account can no longer be read)."""
    r = httpx.delete(f"{API}/sessions/{session_id}", headers=_headers(), timeout=TIMEOUT)
    if r.is_error and r.status_code != 404:  # 404 = it was already gone
        raise EBError(f"DELETE /sessions -> {r.status_code}: {r.text[:300]}")
