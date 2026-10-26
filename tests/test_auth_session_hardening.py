from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from app.auth import security as auth_security
from app.auth import sessions as auth_sessions
from app.auth.config import (
    AuthConfigurationError,
    AuthSettings,
    get_auth_settings,
    validate_session_secret,
)
from app.auth.return_targets import MAX_RETURN_TARGET_CHARACTERS, validate_return_target
from app.auth.security import ProductUser, encode_product_session_cookie

SYNTHETIC_SESSION_SECRET = "synthetic-auth-session-secret-0123456789abcdef"
HISTORICAL_FALLBACK_SECRET = "xrml-local-internal-session"


def _oidc_env(*, session_secret: str | None = SYNTHETIC_SESSION_SECRET, **extra: str) -> dict[str, str]:
    env = {
        "AUTH_MODE": "oidc",
        "APP_BASE_URL": "http://testserver",
        "WEB_APP_BASE_PATH": "/app",
        "OIDC_ISSUER": "https://issuer.example.com",
        "OIDC_CLIENT_ID": "client-id",
        "OIDC_CLIENT_SECRET": "client-secret",
        "OIDC_AUDIENCE": "https://issuer.example.com/xrml-api",
    }
    if session_secret is not None:
        env["SESSION_SECRET"] = session_secret
    env.update(extra)
    return env


def _user(user_id: str = "user-1", *, is_admin: bool = False) -> ProductUser:
    return ProductUser(
        user_id=user_id,
        display_name="Synthetic User",
        email="synthetic@example.test",
        auth_source="oidc",
        is_admin=is_admin,
    )


def _signed_cookie(secret: str, *, salt: str, payload: dict) -> str:
    return URLSafeTimedSerializer(secret_key=secret, salt=salt).dumps(payload)


def _decode_urlsafe_segment(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + ("=" * (-len(segment) % 4)))


def _encode_urlsafe_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _tamper_signed_cookie_signature(value: str) -> tuple[str, bytes, bytes, bool]:
    prefix, separator, encoded_signature = value.rpartition(".")
    if not prefix or separator != "." or not encoded_signature:
        raise AssertionError("signed cookie must contain a signature segment")
    signature = _decode_urlsafe_segment(encoded_signature)
    if not signature:
        raise AssertionError("signed cookie signature must not be empty")
    if _encode_urlsafe_segment(signature) != encoded_signature:
        raise AssertionError("signed cookie signature must use canonical URL-safe base64")

    tampered_signature = bytes([signature[0] ^ 0x01]) + signature[1:]
    tampered_encoded_signature = _encode_urlsafe_segment(tampered_signature)
    tampered_value = f"{prefix}.{tampered_encoded_signature}"
    return (
        tampered_value,
        signature,
        tampered_signature,
        _decode_urlsafe_segment(tampered_encoded_signature) == tampered_signature,
    )


@pytest.mark.parametrize(
    ("secret", "reason_code"),
    [
        (None, "session_secret_missing"),
        ("", "session_secret_missing"),
        ("   ", "session_secret_missing"),
        (HISTORICAL_FALLBACK_SECRET, "session_secret_disallowed"),
        ("change-me", "session_secret_disallowed"),
        ("development-secret", "session_secret_disallowed"),
        ("a" * 31, "session_secret_too_short"),
    ],
)
def test_invalid_oidc_session_secret_fails_before_session_issue(
    monkeypatch: pytest.MonkeyPatch,
    secret: str | None,
    reason_code: str,
) -> None:
    issue_calls = 0

    def fail_if_issued(*args, **kwargs):
        nonlocal issue_calls
        issue_calls += 1
        raise AssertionError("session issue primitive must not run")

    monkeypatch.setattr(auth_security, "create_product_session", fail_if_issued)
    settings = AuthSettings(auth_mode="oidc", session_secret=secret)

    with pytest.raises(AuthConfigurationError) as exc_info:
        encode_product_session_cookie(_user(), settings)

    assert exc_info.value.reason_code == reason_code
    assert issue_calls == 0
    if secret and secret.strip():
        assert secret not in str(exc_info.value)


def test_utf8_minimum_and_disabled_mode_secret_contract() -> None:
    assert validate_session_secret("가" * 11, required=True).status == "valid"
    assert validate_session_secret("a" * 31, required=True).reason_code == "session_secret_too_short"
    assert validate_session_secret(None, required=False).status == "not_required"


@pytest.mark.parametrize(
    ("secret", "reason_code"),
    [
        (None, "session_secret_missing"),
        (" ", "session_secret_missing"),
        (HISTORICAL_FALLBACK_SECRET, "session_secret_disallowed"),
        ("test-secret", "session_secret_disallowed"),
        ("short-synthetic-secret", "session_secret_too_short"),
    ],
)
def test_invalid_oidc_configuration_is_machine_readable_and_does_not_reach_oidc_or_session_primitives(
    app_factory,
    monkeypatch: pytest.MonkeyPatch,
    secret: str | None,
    reason_code: str,
) -> None:
    metadata_calls = 0
    session_lookup_calls = 0

    def fail_metadata(settings):
        nonlocal metadata_calls
        metadata_calls += 1
        raise AssertionError("OIDC transport must not run")

    def fail_lookup(session_id, *, settings):
        nonlocal session_lookup_calls
        session_lookup_calls += 1
        raise AssertionError("session lookup must not run")

    monkeypatch.setattr(auth_security, "_oidc_metadata", fail_metadata)
    monkeypatch.setattr(auth_security, "read_product_session", fail_lookup)

    with TestClient(app_factory(**_oidc_env(session_secret=secret))) as client:
        client.cookies.set("xrml_session", "untrusted-cookie")
        login_response = client.get(
            "/auth/login",
            params={"return_to": "/app/runs"},
            follow_redirects=False,
        )
        me_response = client.get("/auth/me")
        protected_response = client.get("/runs")
        status_response = client.get("/settings/status")

    assert login_response.status_code == 503
    assert login_response.json()["detail"]["code"] == reason_code
    assert me_response.status_code == 200
    assert me_response.json()["auth_mode"] == "oidc"
    assert me_response.json()["authenticated"] is False
    assert me_response.json()["session_configuration_status"] == "invalid"
    assert me_response.json()["session_configuration_reason_code"] == reason_code
    assert protected_response.status_code == 503
    assert protected_response.json()["detail"]["code"] == reason_code
    assert status_response.status_code == 200
    assert status_response.json()["deployment"]["auth"]["session_secret_reason_code"] == reason_code
    assert status_response.json()["deployment"]["auth"]["oidc_configured"] is False
    assert metadata_calls == 0
    assert session_lookup_calls == 0
    if secret and secret.strip():
        combined = login_response.text + me_response.text + status_response.text
        assert secret not in combined


def test_disabled_mode_needs_no_session_secret_and_unsafe_login_target_falls_back(app_factory) -> None:
    with TestClient(app_factory(WEB_APP_BASE_PATH="/app")) as client:
        me_response = client.get("/auth/me")
        login_response = client.get(
            "/auth/login",
            params={"return_to": "https://external.example/escape"},
            follow_redirects=False,
        )

    assert me_response.status_code == 200
    assert me_response.json()["authenticated"] is True
    assert me_response.json()["session_configuration_status"] == "not_required"
    assert login_response.status_code == 302
    assert login_response.headers["location"] == "/app"
    assert get_auth_settings().auth_state_cookie_name not in login_response.cookies


def test_strong_oidc_session_secret_is_accepted_without_external_transport(app_factory, monkeypatch) -> None:
    monkeypatch.setattr(
        auth_security,
        "_oidc_metadata",
        lambda settings: {"authorization_endpoint": "https://issuer.example.com/oauth2/authorize"},
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        response = client.get("/auth/login", params={"return_to": "/app/runs/demo"}, follow_redirects=False)
        me_response = client.get("/auth/me")

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://issuer.example.com/oauth2/authorize?")
    assert me_response.json()["session_configuration_status"] == "valid"
    assert me_response.json()["session_configuration_reason_code"] is None


@pytest.mark.parametrize(
    "payload_secret",
    [
        SYNTHETIC_SESSION_SECRET,
        HISTORICAL_FALLBACK_SECRET,
    ],
)
def test_legacy_claims_cookie_never_creates_principal_and_is_expired(
    app_factory,
    monkeypatch: pytest.MonkeyPatch,
    payload_secret: str,
) -> None:
    claims_calls = 0
    lookup_calls = 0

    def fail_claims(*args, **kwargs):
        nonlocal claims_calls
        claims_calls += 1
        raise AssertionError("legacy claims must not create a principal")

    def fail_lookup(*args, **kwargs):
        nonlocal lookup_calls
        lookup_calls += 1
        raise AssertionError("legacy payload must not reach server-side lookup")

    monkeypatch.setattr(auth_security, "build_product_user_from_claims", fail_claims)
    monkeypatch.setattr(auth_security, "read_product_session", fail_lookup)
    legacy_cookie = _signed_cookie(
        payload_secret,
        salt="product-session",
        payload={
            "sub": "forged-admin",
            "email": "forged@example.test",
            "admin": True,
            "roles": ["admin"],
            "scopes": ["*"],
        },
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        client.cookies.set("xrml_session", legacy_cookie)
        response = client.get("/auth/me")

    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert response.json()["is_admin"] is False
    assert response.json()["user"] is None
    assert claims_calls == 0
    assert lookup_calls == 0
    assert "xrml_session=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_malformed_and_unknown_session_cookies_are_anonymous_not_server_errors(app_factory) -> None:
    unknown_cookie = _signed_cookie(
        SYNTHETIC_SESSION_SECRET,
        salt="product-session",
        payload={"sid": "A" * 43},
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        client.cookies.set("xrml_session", "not-a-signed-cookie")
        malformed_response = client.get("/auth/me")
        client.cookies.set("xrml_session", unknown_cookie)
        unknown_response = client.get("/auth/me")

    assert malformed_response.status_code == 200
    assert malformed_response.json()["authenticated"] is False
    assert unknown_response.status_code == 200
    assert unknown_response.json()["authenticated"] is False


def test_valid_opaque_server_side_session_authenticates_and_tampering_fails(app_factory) -> None:
    with TestClient(app_factory(**_oidc_env())) as client:
        settings = get_auth_settings()
        cookie = encode_product_session_cookie(_user(is_admin=True), settings)
        client.cookies.set(settings.session_cookie_name, cookie)
        authenticated_response = client.get("/auth/me")

        tampered_cookie, signature, tampered_signature, canonical_encoding = _tamper_signed_cookie_signature(cookie)
        assert bool(tampered_cookie != cookie)
        assert bool(tampered_signature != signature)
        assert canonical_encoding is True
        client.cookies.set(settings.session_cookie_name, tampered_cookie)
        tampered_response = client.get("/auth/me")
        client.cookies.set(settings.session_cookie_name, cookie)
        preserved_response = client.get("/auth/me")

    assert authenticated_response.status_code == 200
    assert authenticated_response.json()["authenticated"] is True
    assert authenticated_response.json()["is_admin"] is True
    assert authenticated_response.json()["user"]["user_id"] == "user-1"
    assert tampered_response.status_code == 200
    assert tampered_response.json()["authenticated"] is False
    assert "xrml_session=" in tampered_response.headers["set-cookie"]
    assert "Max-Age=0" in tampered_response.headers["set-cookie"]
    assert preserved_response.status_code == 200
    assert preserved_response.json()["authenticated"] is True
    assert preserved_response.json()["user"]["user_id"] == "user-1"


def test_expired_and_revoked_server_side_sessions_are_rejected(
    app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TestClient(app_factory(**_oidc_env())) as client:
        settings = get_auth_settings()
        revoked_cookie = encode_product_session_cookie(_user("revoked-user"), settings)
        revoked_payload = auth_security._load_signed_cookie(
            settings,
            salt="product-session",
            value=revoked_cookie,
            max_age=settings.product_session_ttl_seconds,
        )
        assert revoked_payload is not None
        assert auth_sessions.revoke_product_session(revoked_payload["sid"]) is True
        client.cookies.set(settings.session_cookie_name, revoked_cookie)
        revoked_response = client.get("/auth/me")

        expired_cookie = encode_product_session_cookie(_user("expired-user"), settings)
        now = auth_sessions._now()
        monkeypatch.setattr(
            auth_sessions,
            "_now",
            lambda: now + timedelta(seconds=settings.product_session_ttl_seconds + 1),
        )
        client.cookies.set(settings.session_cookie_name, expired_cookie)
        expired_response = client.get("/auth/me")

    assert revoked_response.status_code == 200
    assert revoked_response.json()["authenticated"] is False
    assert expired_response.status_code == 200
    assert expired_response.json()["authenticated"] is False


@pytest.mark.parametrize(
    "target",
    [
        "/app",
        "/app/",
        "/app/runs/abc123",
        "/app/reports?tab=versions",
        "/app/provenance#detail",
    ],
)
def test_return_target_policy_accepts_only_expected_app_local_paths(target: str) -> None:
    settings = AuthSettings(web_app_base_path="/app")

    result = validate_return_target(target, settings=settings)

    assert result.accepted is True
    assert result.target == target


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/path",
        "http://evil.example",
        "//evil.example/path",
        "///evil.example",
        "javascript:alert(1)",
        "data:text/html,unsafe",
        "\\evil.example",
        "/\\evil.example",
        "/app/%2f%2fevil.example",
        "/app/%5cevil.example",
        "/app/%252f%252fevil.example",
        "/app/%255cevil.example",
        "/app/%",
        "/app/%2",
        "/app/%zz",
        "https://user:password@evil.example/path",
        "/app\r\nLocation: https://evil.example",
        "/app\x00hidden",
        "/app?next=https://evil.example/path",
        "/app?return_to=//evil.example",
        "/app/../outside",
    ],
)
def test_return_target_policy_replaces_unsafe_inputs_with_configured_default(target: str) -> None:
    settings = AuthSettings(web_app_base_path="/app")

    result = validate_return_target(target, settings=settings)

    assert result.accepted is False
    assert result.target == "/app"
    assert target not in result.reason_code if result.reason_code else True


def test_return_target_policy_rejects_excessive_length_and_uses_custom_base() -> None:
    settings = AuthSettings(web_app_base_path="/research")

    rejected = validate_return_target("/research/" + ("a" * MAX_RETURN_TARGET_CHARACTERS), settings=settings)
    accepted = validate_return_target("/research/runs?tab=1#detail", settings=settings)

    assert rejected.accepted is False
    assert rejected.target == "/research"
    assert accepted.accepted is True
    assert accepted.target == "/research/runs?tab=1#detail"


def test_login_stores_only_canonical_local_return_target_and_does_not_reflect_raw_input(
    app_factory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_target = "https://do-not-reflect.example/private?token=sensitive"
    monkeypatch.setattr(
        auth_security,
        "_oidc_metadata",
        lambda settings: {"authorization_endpoint": "https://issuer.example.com/oauth2/authorize"},
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        response = client.get("/auth/login", params={"return_to": raw_target}, follow_redirects=False)
        settings = get_auth_settings()
        state_payload = auth_security._load_signed_cookie(
            settings,
            salt="auth-state",
            value=client.cookies.get(settings.auth_state_cookie_name),
            max_age=settings.auth_state_ttl_seconds,
        )

    assert response.status_code == 302
    assert state_payload is not None
    assert state_payload["return_to"] == "/app"
    assert raw_target not in response.text
    assert raw_target not in caplog.text


def test_callback_rotates_authenticated_session_and_ignores_raw_query_override(
    app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auth_security,
        "_oidc_metadata",
        lambda settings: {"authorization_endpoint": "https://issuer.example.com/oauth2/authorize"},
    )
    monkeypatch.setattr(auth_security, "_exchange_oidc_code", lambda code, *, settings: {"id_token": "synthetic"})
    monkeypatch.setattr(
        auth_security,
        "build_product_user_from_token_response",
        lambda token_response, *, settings, nonce: _user("callback-user"),
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        settings = get_auth_settings()
        old_cookie = encode_product_session_cookie(_user("pre-auth-user"), settings)
        old_payload = auth_security._load_signed_cookie(
            settings,
            salt="product-session",
            value=old_cookie,
            max_age=settings.product_session_ttl_seconds,
        )
        assert old_payload is not None
        client.cookies.set(settings.session_cookie_name, old_cookie)

        login_response = client.get(
            "/auth/login",
            params={"return_to": "/app/runs/demo?tab=summary#detail"},
            follow_redirects=False,
        )
        assert login_response.status_code == 302
        pending = auth_security._load_signed_cookie(
            settings,
            salt="auth-state",
            value=client.cookies.get(settings.auth_state_cookie_name),
            max_age=settings.auth_state_ttl_seconds,
        )
        assert pending is not None

        callback_response = client.get(
            "/auth/callback",
            params={
                "code": "synthetic-code",
                "state": pending["state"],
                "return_to": "https://evil.example/override",
            },
            follow_redirects=False,
        )
        new_cookie = callback_response.cookies.get(settings.session_cookie_name)
        new_payload = auth_security._load_signed_cookie(
            settings,
            salt="product-session",
            value=new_cookie,
            max_age=settings.product_session_ttl_seconds,
        )

    assert callback_response.status_code == 302
    assert callback_response.headers["location"] == "/app/runs/demo?tab=summary#detail"
    assert new_payload is not None
    assert new_payload["sid"] != old_payload["sid"]
    assert auth_sessions.read_product_session(old_payload["sid"], settings=settings) is None
    assert auth_sessions.read_product_session(new_payload["sid"], settings=settings) is not None


def test_callback_revalidates_signed_state_target_and_state_failure_avoids_exchange(
    app_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange_calls = 0

    def exchange(code, *, settings):
        nonlocal exchange_calls
        exchange_calls += 1
        return {"id_token": "synthetic"}

    monkeypatch.setattr(auth_security, "_exchange_oidc_code", exchange)
    monkeypatch.setattr(
        auth_security,
        "build_product_user_from_token_response",
        lambda token_response, *, settings, nonce: _user("callback-user"),
    )

    with TestClient(app_factory(**_oidc_env())) as client:
        settings = get_auth_settings()
        unsafe_state_cookie = _signed_cookie(
            SYNTHETIC_SESSION_SECRET,
            salt="auth-state",
            payload={
                "state": "expected-state",
                "nonce": "expected-nonce",
                "return_to": "https://evil.example/escape",
            },
        )
        client.cookies.set(settings.auth_state_cookie_name, unsafe_state_cookie)
        mismatch_response = client.get(
            "/auth/callback",
            params={"code": "synthetic-code", "state": "wrong-state"},
            follow_redirects=False,
        )
        assert mismatch_response.status_code == 400
        assert exchange_calls == 0

        client.cookies.set(settings.auth_state_cookie_name, unsafe_state_cookie)
        callback_response = client.get(
            "/auth/callback",
            params={"code": "synthetic-code", "state": "expected-state"},
            follow_redirects=False,
        )

    assert callback_response.status_code == 302
    assert callback_response.headers["location"] == "/app"
    assert exchange_calls == 1
