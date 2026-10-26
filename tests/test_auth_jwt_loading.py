"""Local startup avoids OIDC crypto loading; actual token verification stays strict."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from app.auth import security
from app.auth.config import AuthSettings


def test_local_application_starts_without_loading_oidc_crypto(tmp_path: Path) -> None:
    # A fresh interpreter is required: other auth tests legitimately load JWT.
    script = textwrap.dedent("""
        import sys
        import importlib.abc
        class RejectOidcCrypto(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.')[0] in {'jwt', 'cryptography'}:
                    raise AssertionError('local startup loaded OIDC crypto: ' + fullname)
        sys.meta_path.insert(0, RejectOidcCrypto())
        def no_network(event, args):
            if event == 'socket.connect':
                raise AssertionError('startup attempted a network connection')
        sys.addaudithook(no_network)
        from app.local import create_local_app
        from app.auth.security import require_product_user
        from starlette.requests import Request
        app = create_local_app()
        user = require_product_user(Request({'type': 'http', 'headers': []}))
        assert user.auth_source == 'disabled' and user.is_admin
        assert app is not None
    """)
    env = {**os.environ, "AUTH_MODE": "disabled", "XRML_DATA_DIR": str(tmp_path / "data"),
           "TOPIC_SCHEDULER_MODE": "disabled", "TOPIC_SCHEDULER_ENABLED": "0",
           "XRML_RUN_EXECUTOR_MODE": "disabled", "XRML_PROVIDER": "disabled"}
    completed = subprocess.run(
        [sys.executable, "-c", script], env=env,
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.fixture()
def oidc_verifier(monkeypatch: pytest.MonkeyPatch):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = AuthSettings(auth_mode="oidc", oidc_issuer="https://issuer.example.test")
    security.clear_auth_runtime_caches()
    monkeypatch.setattr(security, "_oidc_metadata", lambda _: {
        "issuer": settings.oidc_issuer, "jwks_uri": "https://issuer.example.test/jwks",
    })
    # Replace only remote key retrieval; verify real signed tokens with PyJWT.
    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt",
                        lambda self, token: SimpleNamespace(key=key.public_key()))
    claims = {"sub": "synthetic-user", "iss": settings.oidc_issuer,
              "aud": "xrml-test", "exp": int(time.time()) + 300, "nonce": "test-nonce"}
    yield jwt, key, settings, claims
    security.clear_auth_runtime_caches()


def test_oidc_still_verifies_real_signature_and_claims(oidc_verifier) -> None:
    jwt, key, settings, claims = oidc_verifier
    token = jwt.encode(claims, key, algorithm="RS256")
    assert security._decode_oidc_token(
        token, settings=settings, audience="xrml-test", nonce="test-nonce",
    ) == claims


@pytest.mark.parametrize("invalid", ["signature", "issuer", "audience", "expiry", "nonce", "algorithm"])
def test_oidc_rejects_invalid_signed_tokens(oidc_verifier, invalid: str) -> None:
    jwt, key, settings, claims = oidc_verifier
    if invalid == "issuer":
        claims["iss"] = "https://wrong.example.test"
    elif invalid == "audience":
        claims["aud"] = "different-app"
    elif invalid == "expiry":
        claims["exp"] = int(time.time()) - 60
    elif invalid == "nonce":
        claims["nonce"] = "wrong-nonce"
    elif invalid == "signature":
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = (jwt.encode(claims, "synthetic-hmac-key-at-least-thirty-two-bytes", algorithm="HS256")
             if invalid == "algorithm" else jwt.encode(claims, key, algorithm="RS256"))
    expected = RuntimeError if invalid == "nonce" else jwt.InvalidTokenError
    with pytest.raises(expected):
        security._decode_oidc_token(token, settings=settings, audience="xrml-test", nonce="test-nonce")
