import base64
import hashlib
import json
import time
from functools import partial
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.auth.client import eurofurence
from app.config import settings
from app.database import Base, get_db
from app.identity.models import ExternalIdentity, LocalUser
from app.identity.service import resolve_user
from app.main import app


class Provider:
    """HTTP-level provider double; Authlib itself is never mocked."""

    def __init__(self, key):
        self.key = key
        self.signing_key = key
        self.claims = {}
        self.omit = set()
        self.token_updates = {}
        self.algorithm = "RS256"
        self.failure = None
        self.metadata = {
            "issuer": "https://identity.example/",
            "authorization_endpoint": "https://identity.example/authorize",
            "token_endpoint": "https://identity.example/token",
            "jwks_uri": "https://identity.example/jwks",
            "id_token_signing_alg_values_supported": ["RS256", "RS512"],
        }
        self.query = None
        self.token_requests = []

    def handle(self, request):
        path = request.url.path
        if self.failure == path:
            raise httpx2.ConnectError("sensitive upstream detail", request=request)
        if path == "/.well-known/openid-configuration":
            return httpx2.Response(200, json=self.metadata)
        if path == "/jwks":
            return httpx2.Response(
                200, json={"keys": [self.key.as_dict(private=False)]}
            )
        assert path == "/token"
        fields = parse_qs(request.content.decode())
        self.token_requests.append(fields)
        assert fields["grant_type"] == ["authorization_code"]
        assert fields["client_id"] == ["test-client"]
        assert fields["client_secret"] == ["test-client-secret"]
        assert fields["redirect_uri"] == [settings.oidc_redirect_uri]
        verifier = fields["code_verifier"][0]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if challenge != self.query["code_challenge"][0]:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        claims = {
            "iss": "https://identity.example/",
            "sub": "person-123",
            "aud": "test-client",
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
            "nonce": self.query["nonce"][0],
            "email": "person@example.test",
            "name": "Mutable name",
            "roles": ["ADMIN"],
            "groups": ["BADGE_STAFF"],
        }
        claims.update(self.claims)
        for name in self.omit:
            claims.pop(name, None)
        token = jwt.encode(
            {"alg": self.algorithm, "kid": "test-key"}, claims, self.signing_key
        )
        result = {
            "access_token": "private-access-token",
            "token_type": "Bearer",
            "id_token": token,
        }
        result.update(self.token_updates)
        return httpx2.Response(200, json=result)


@pytest.fixture(scope="session")
def key():
    return RSAKey.generate_key(parameters={"kid": "test-key"})


@pytest.fixture
def provider(monkeypatch, key):
    provider = Provider(key)
    monkeypatch.setattr(eurofurence, "server_metadata", {})
    monkeypatch.setattr(
        eurofurence,
        "client_cls",
        partial(AsyncOAuth2Client, transport=httpx2.MockTransport(provider.handle)),
    )
    return provider


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(db, provider):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client
    app.dependency_overrides.clear()


def begin(client, provider):
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302
    provider.query = parse_qs(urlparse(response.headers["location"]).query)
    return provider.query


def finish(client, provider):
    return client.get(
        "/auth/callback",
        params={"code": "test-code", "state": provider.query["state"][0]},
        follow_redirects=False,
    )


def session_data(client):
    return json.loads(
        base64.b64decode(client.cookies.get("creator_session").split(".")[0])
    )


def test_callback_redirect_clears_visible_parameters_and_ignores_return_targets(
    client, provider, caplog
):
    begin(client, provider)
    response = client.get(
        "/auth/callback",
        params={
            "code": "test-code",
            "state": provider.query["state"][0],
            "next": "https://attacker.example",
            "return_to": "//attacker.example",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert response.content == b""
    assert (
        "test-code" not in caplog.text and provider.query["state"][0] not in caplog.text
    )
    page = client.get(response.headers["location"])
    assert str(page.url) == "http://127.0.0.1:8000/"
    assert "Logout" in page.text and "Your application" in page.text
    assert "private-access-token" not in str(session_data(client))
    assert 'href="/admin/applications"' not in page.text


def test_login_uses_code_flow_with_pkce(client, provider):
    query = begin(client, provider)
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8000/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"][0] and query["state"][0]
    assert finish(client, provider).status_code == 303
    assert provider.token_requests


def test_callback_stores_only_local_user_and_logout_clears_session(
    client, provider, db
):
    assert client.get("/auth/me").status_code == 401
    begin(client, provider)
    response = finish(client, provider)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert response.content == b""
    user_id = session_data(client)["user_id"]
    assert session_data(client) == {
        "user_id": user_id,
        "identity_key": db.get(LocalUser, user_id).session_key,
    }
    assert client.get("/auth/me").json() == {"user_id": user_id}
    identity = db.scalar(select(ExternalIdentity))
    assert (identity.issuer, identity.subject, identity.user_id) == (
        settings.oidc_issuer_url,
        "person-123",
        user_id,
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "secure" not in cookie
    from test_applications import csrf

    assert client.post("/auth/logout").status_code == 403
    assert (
        client.post("/auth/logout", data={"csrf_token": csrf(client, "/")}).status_code
        == 200
    )
    assert client.get("/auth/me").status_code == 401


def test_repeat_login_ignores_mutable_profile_and_roles(client, provider, db):
    begin(client, provider)
    assert finish(client, provider).status_code == 303
    user_id = session_data(client)["user_id"]
    provider.claims.update(
        email="changed@example.test", name="Changed", roles=["BADGE_STAFF"]
    )
    begin(client, provider)
    assert finish(client, provider).status_code == 303
    assert session_data(client)["user_id"] == user_id
    assert db.scalar(select(func.count()).select_from(LocalUser)) == 1
    provider.claims["sub"] = "another-person"
    begin(client, provider)
    assert finish(client, provider).status_code == 303
    assert session_data(client)["user_id"] != user_id


@pytest.mark.parametrize(
    "claim,value",
    [
        ("iss", "https://attacker.example/"),
        ("aud", "another-client"),
        ("nonce", "wrong"),
        ("exp", 1),
        ("iat", 9999999999),
        ("sub", ""),
        ("sub", 123),
    ],
)
def test_invalid_claims_rejected(client, provider, db, claim, value):
    provider.claims[claim] = value
    begin(client, provider)
    assert finish(client, provider).status_code == 401
    assert client.get("/auth/me").status_code == 401
    assert db.scalar(select(func.count()).select_from(LocalUser)) == 0


@pytest.mark.parametrize("claim", ["iss", "sub", "aud", "exp", "iat", "nonce"])
def test_missing_claim_rejected(client, provider, claim):
    provider.omit.add(claim)
    begin(client, provider)
    assert finish(client, provider).status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_nonce_cannot_be_disabled_by_provider_claim(client, provider):
    provider.claims.update(nonce_supported=False, nonce="wrong")
    begin(client, provider)
    assert finish(client, provider).status_code == 401


def test_bad_signature_rejected(client, provider):
    provider.signing_key = RSAKey.generate_key(parameters={"kid": "test-key"})
    begin(client, provider)
    assert finish(client, provider).status_code == 401


def test_unsupported_algorithm_rejected(client, provider):
    provider.algorithm = "RS512"
    begin(client, provider)
    assert finish(client, provider).status_code == 401


@pytest.mark.parametrize("token", [None, "", "not-a-jwt"])
def test_missing_or_malformed_id_token_rejected(client, provider, token):
    provider.token_updates["id_token"] = token
    begin(client, provider)
    assert finish(client, provider).status_code in {401, 503}
    assert client.get("/auth/me").status_code == 401


@pytest.mark.parametrize(
    "params", [{}, {"code": "test-code"}, {"code": "test-code", "state": "wrong"}]
)
def test_invalid_state_never_exchanges_code(client, provider, params):
    begin(client, provider)
    assert client.get("/auth/callback", params=params).status_code == 401
    assert not provider.token_requests
    assert client.get("/auth/me").status_code == 401


def test_missing_code_rejected(client, provider):
    begin(client, provider)
    assert (
        client.get(
            "/auth/callback", params={"state": provider.query["state"][0]}
        ).status_code
        == 401
    )
    assert not provider.token_requests


def test_callback_replay_rejected(client, provider):
    begin(client, provider)
    assert finish(client, provider).status_code == 303
    assert finish(client, provider).status_code == 401
    assert len(provider.token_requests) == 1


def test_denied_login_does_not_expose_provider_description(client, provider, caplog):
    begin(client, provider)
    response = client.get(
        "/auth/callback",
        params={
            "state": provider.query["state"][0],
            "error": "access_denied",
            "error_description": "private-provider-detail",
        },
    )
    assert response.status_code == 401
    assert "private-provider-detail" not in response.text
    assert "private-provider-detail" not in caplog.text
    assert client.get("/auth/me").status_code == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "https://attacker.example/"),
        ("issuer", None),
        ("jwks_uri", None),
        ("token_endpoint", "http://identity.example/token"),
    ],
)
def test_invalid_discovery_rejected_before_redirect(client, provider, field, value):
    provider.metadata[field] = value
    assert client.get("/auth/login").status_code == 503
    assert not provider.token_requests


@pytest.mark.parametrize(
    "path", ["/.well-known/openid-configuration", "/token", "/jwks"]
)
def test_provider_network_errors_fail_closed(client, provider, path, caplog):
    if path != "/.well-known/openid-configuration":
        begin(client, provider)
    provider.failure = path
    response = (
        client.get("/auth/login") if not provider.query else finish(client, provider)
    )
    assert response.status_code == 503
    assert "sensitive upstream detail" not in response.text + caplog.text
    assert client.get("/auth/me").status_code == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("oidc_client_id", None),
        ("oidc_client_id", "INSERT_CLIENT_ID"),
        ("oidc_redirect_uri", None),
        ("oidc_issuer_url", None),
    ],
)
def test_login_is_unavailable_when_oidc_is_not_configured(
    client, monkeypatch, field, value
):
    monkeypatch.setattr(settings, field, value)
    assert client.get("/auth/login").status_code == 503


def test_localhost_callback_is_preserved_exactly(client, provider, monkeypatch):
    monkeypatch.setattr(
        settings, "oidc_redirect_uri", "http://localhost:8000/auth/callback"
    )
    assert begin(client, provider)["redirect_uri"] == [
        "http://localhost:8000/auth/callback"
    ]


def test_database_failure_does_not_authenticate(client, provider, db, monkeypatch):
    begin(client, provider)
    monkeypatch.setattr(
        db,
        "scalar",
        lambda *a, **k: (_ for _ in ()).throw(OperationalError("", {}, Exception())),
    )
    assert finish(client, provider).status_code == 503
    assert client.get("/auth/me").status_code == 401


def test_same_subject_different_issuers_are_distinct(db):
    first = resolve_user(db, "https://one.example/", "same")
    second = resolve_user(db, "https://two.example/", "same")
    assert first.id != second.id
    assert resolve_user(db, "https://one.example/", "same").id == first.id
    db.add(
        ExternalIdentity(
            user_id=second.id, issuer="https://one.example/", subject="same"
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_deleted_local_user_is_not_authenticated(client, provider, db):
    begin(client, provider)
    assert finish(client, provider).status_code == 303
    user_id = session_data(client)["user_id"]
    db.delete(db.scalar(select(ExternalIdentity)))
    db.delete(db.get(LocalUser, user_id))
    db.commit()
    assert client.get("/auth/me").status_code == 401


def replace_session(client, data):
    from itsdangerous import TimestampSigner

    payload = base64.b64encode(json.dumps(data).encode())
    cookie = (
        TimestampSigner(settings.session_secret.get_secret_value())
        .sign(payload)
        .decode()
    )
    client.cookies.set("creator_session", cookie, domain="127.0.0.1", path="/")


@pytest.mark.parametrize("change", ["expired", "missing_nonce", "missing_verifier"])
def test_incomplete_or_expired_transaction_rejected(client, provider, change):
    begin(client, provider)
    data = session_data(client)
    transaction = next(iter(data.values()))
    if change == "expired":
        transaction["exp"] = 1
    else:
        transaction["data"].pop(
            "nonce" if change == "missing_nonce" else "code_verifier"
        )
    replace_session(client, data)
    assert finish(client, provider).status_code == 401
    assert not provider.token_requests


def test_wrong_pkce_verifier_is_rejected_by_token_endpoint(client, provider):
    begin(client, provider)
    data = session_data(client)
    next(iter(data.values()))["data"]["code_verifier"] = "x" * 48
    replace_session(client, data)
    assert finish(client, provider).status_code == 401
    assert client.get("/auth/me").status_code == 401


def test_discovery_issuer_checked_again_on_callback(client, provider):
    begin(client, provider)
    eurofurence.server_metadata["issuer"] = "https://attacker.example/"
    assert finish(client, provider).status_code == 401
    assert not provider.token_requests


def test_tampered_cookie_is_not_authenticated(client, provider):
    begin(client, provider)
    assert finish(client, provider).status_code == 303
    cookie = client.cookies.get("creator_session")
    payload, timestamp, signature = cookie.split(".")
    payload = base64.b64encode(b'{"user_id":999}').decode()
    client.cookies.set(
        "creator_session",
        f"{payload}.{timestamp}.{signature}",
        domain="127.0.0.1",
        path="/",
    )
    assert client.get("/auth/me").status_code == 401


def test_production_cookie_flags(client, provider, monkeypatch):
    from starlette.middleware.sessions import SessionMiddleware

    middleware = next(
        item for item in app.user_middleware if item.cls is SessionMiddleware
    )
    monkeypatch.setitem(middleware.kwargs, "https_only", True)
    monkeypatch.setattr(app, "middleware_stack", None)
    try:
        response = client.get("/auth/login", follow_redirects=False)
        assert response.status_code == 302
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    finally:
        app.middleware_stack = None


def test_production_rejects_http_callback(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    assert client.get("/auth/login").status_code == 503


def test_failed_identity_insert_rolls_back_provisional_user(
    client, provider, db, monkeypatch
):
    def fail_commit():
        raise IntegrityError("test constraint failure", {}, Exception())

    monkeypatch.setattr(db, "commit", fail_commit)
    begin(client, provider)
    assert finish(client, provider).status_code == 503
    assert db.scalar(select(func.count()).select_from(LocalUser)) == 0
    assert db.scalar(select(func.count()).select_from(ExternalIdentity)) == 0
    assert client.get("/auth/me").status_code == 401
