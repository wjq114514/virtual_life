"""WebUI session cookie: remember the administrative token without storing it."""

import time

import pytest
from fastapi.testclient import TestClient
from virtual_life import webui_session
from virtual_life.api import create_app
from virtual_life.settings import Settings

TOKEN = "test-token"


@pytest.fixture
def client(tmp_path):
    # No default Authorization header: these checks exercise the cookie path.
    settings = Settings(database_path=str(tmp_path / "runtime.sqlite3"), api_token=TOKEN)
    with TestClient(create_app(settings)) as client:
        yield client


def cookie_value(response):
    for header in response.headers.get_list("set-cookie"):
        if header.startswith(webui_session.COOKIE_NAME + "="):
            return header.split(";", 1)[0].split("=", 1)[1]
    return None


def test_session_cookie_grants_admin_without_a_bearer_token(client):
    response = client.post("/v1/session", json={"token": TOKEN})
    assert response.status_code == 200, response.text
    assert response.json()["active"] is True
    assert response.json()["expires_at"]
    cookie = cookie_value(response)
    assert cookie and TOKEN not in cookie

    lower = response.headers["set-cookie"].lower()
    for flag in (webui_session.COOKIE_NAME, "httponly", "samesite=strict", "max-age=", "path=/"):
        assert flag in lower
    # Plain HTTP deployment; Secure is only attached over HTTPS.
    assert "; secure" not in lower

    # No Authorization header at all: the browser sends the cookie itself.
    assert client.get("/v1/capabilities").status_code == 200
    assert client.get("/v1/nodes").status_code == 200
    created = client.post(
        "/v1/nodes",
        json={
            "node_id": "desk",
            "label": "desk",
            "kind": "desktop",
            "capabilities": ["text.display"],
        },
    )
    assert created.status_code == 201, created.text


def test_session_cookie_rejects_unknown_tokens(client):
    for token in ("wrong", "vl_reporter_deadbeef", "Bearer " + TOKEN):
        response = client.post("/v1/session", json={"token": token})
        assert response.status_code == 401, response.text
    assert client.post("/v1/session", json={"token": ""}).status_code == 422
    assert client.post("/v1/session", json={}).status_code == 422


def test_delete_forgets_the_session(client):
    client.post("/v1/session", json={"token": TOKEN})
    assert client.get("/v1/nodes").status_code == 200
    response = client.delete("/v1/session")
    assert response.status_code == 200
    assert response.json() == {"active": False, "expires_at": None}
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/v1/nodes").status_code == 401


def test_bearer_token_still_wins_and_wrong_tokens_are_not_downgraded(client):
    # Without a session the header is the only credential.
    assert client.get("/v1/nodes").status_code == 401
    assert client.get("/v1/nodes", headers={"Authorization": "Bearer "}).status_code == 401
    client.post("/v1/session", json={"token": TOKEN})
    response = client.get("/v1/nodes", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    # A wrong credential is never silently replaced by the remembered one.
    response = client.get("/v1/nodes", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert client.get("/v1/nodes", headers={"Authorization": "Token abc"}).status_code == 401
    # No credential text at all, which is how the WebUI issues every call.
    assert client.get("/v1/nodes", headers={"Authorization": "Bearer "}).status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        "",
        "v1.1.deadbeef",
        "v1.deadbeef",
        "v2.1.deadbeef",
        "v1.1.deadbeef.1",
        "v1.-1.deadbeef",
    ],
)
def test_malformed_cookies_are_rejected(client, value):
    client.cookies.set(webui_session.COOKIE_NAME, value)
    assert client.get("/v1/nodes").status_code == 401


def test_signature_expiry_and_rotation_boundaries():
    now = time.time()
    future = webui_session.sign_session_cookie(TOKEN, int(now) + 86_400)
    assert webui_session.verify_session_cookie(TOKEN, future, at=now)
    # The digest is bound to the expiry it signs.
    assert not webui_session.verify_session_cookie(TOKEN, "v1.1.deadbeef", at=0)
    assert not webui_session.verify_session_cookie(TOKEN, "v1.1." + "0" * 64, at=0)
    # The cookie cannot outlive the expiry it carries.
    past = webui_session.sign_session_cookie(TOKEN, int(now) - 60)
    assert webui_session.verify_session_cookie(TOKEN, past, at=now - 120)
    assert not webui_session.verify_session_cookie(TOKEN, past, at=now)
    # Rotating the token invalidates every session without touching any store.
    assert not webui_session.verify_session_cookie("rotated", future, at=now)
    assert not webui_session.verify_session_cookie(TOKEN, TOKEN + future, at=now)


def test_disabled_persistence_refuses_to_remember(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "disabled.sqlite3"),
        api_token=TOKEN,
        webui_session_days=0,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/session", json={"token": TOKEN})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "session_disabled"


def test_zero_lifetime_invalidates_cookies_minted_before_the_change(tmp_path):
    """Disabling persistence must cut off sessions issued while it was on."""
    minted = webui_session.sign_session_cookie(
        TOKEN, int(time.time()) + webui_session.SECONDS_PER_DAY
    )
    assert webui_session.verify_session_cookie(TOKEN, minted)
    settings = Settings(
        database_path=str(tmp_path / "zero.sqlite3"),
        api_token=TOKEN,
        webui_session_days=0,
    )
    with TestClient(create_app(settings)) as client:
        client.cookies.set(webui_session.COOKIE_NAME, minted)
        assert client.get("/v1/nodes").status_code == 401
        # Bearer authentication is untouched by the switch.
        assert (
            client.get("/v1/nodes", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
        )


def test_session_cookie_serves_https_only(tmp_path):
    settings = Settings(database_path=str(tmp_path / "https.sqlite3"), api_token=TOKEN)
    with TestClient(create_app(settings), base_url="https://localhost") as client:
        response = client.post("/v1/session", json={"token": TOKEN})
    assert response.status_code == 200
    assert "; secure" in response.headers["set-cookie"].lower()


def test_session_days_configuration(tmp_path, monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("VIRTUAL_LIFE_"):
            monkeypatch.delenv(key)
    path = tmp_path / "settings.toml"
    path.write_text("[runtime]\nwebui_session_days = 7\n", encoding="utf8")
    assert Settings.load(path).webui_session_days == 7
    monkeypatch.setenv("VIRTUAL_LIFE_WEBUI_SESSION_DAYS", "14")
    assert Settings.load(path).webui_session_days == 14
    for days in (-1, 3651, "soon"):
        with pytest.raises(ValueError):
            Settings(
                database_path=str(tmp_path / "bad.sqlite3"),
                api_token=TOKEN,
                webui_session_days=days,
            )
