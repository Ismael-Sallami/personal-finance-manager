"""Enable Banking private key: tolerant to how it gets pasted in a host's panel."""
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings
from app.services import enablebanking as eb


@pytest.fixture(scope="module")
def pem():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()


@pytest.mark.parametrize("mangle", [
    lambda p: p,                                   # as is
    lambda p: p.replace("\n", " "),                # one line with spaces
    lambda p: p.replace("\n", ""),                 # all glued together
    lambda p: p.replace("\n", "\\n"),              # literal \n
    lambda p: f'"{p}"',                            # quoted
    lambda p: p.replace("\n", "\r\n"),             # Windows line breaks
])
def test_key_is_rebuilt(monkeypatch, pem, mangle):
    monkeypatch.setattr(settings, "eb_private_key", mangle(pem), raising=False)
    monkeypatch.setattr(settings, "eb_app_id", "app", raising=False)
    token = eb._headers()["Authorization"].split()[1]
    assert jwt.get_unverified_header(token)["kid"] == "app"


def test_invalid_key_gives_a_clear_error(monkeypatch):
    monkeypatch.setattr(settings, "eb_private_key", "garbage", raising=False)
    with pytest.raises(eb.EBError, match="does not look like a PEM"):
        eb._key()


def test_modes(monkeypatch):
    monkeypatch.setattr(settings, "eb_app_id", "", raising=False)
    monkeypatch.setattr(settings, "eb_private_key", "", raising=False)
    monkeypatch.setattr(settings, "entry_mode", "auto", raising=False)
    assert settings.mode == "manual"
    monkeypatch.setattr(settings, "eb_app_id", "app", raising=False)
    monkeypatch.setattr(settings, "eb_private_key", "key", raising=False)
    assert settings.mode == "bank"
    monkeypatch.setattr(settings, "entry_mode", "manual", raising=False)
    assert settings.mode == "manual"  # forced off even with credentials
