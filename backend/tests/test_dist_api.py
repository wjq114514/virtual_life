"""Distribution endpoints: publishing and fetching the desktop host client."""

import hashlib

from fastapi.testclient import TestClient
from virtual_life.api import create_app
from virtual_life.dist_api import is_wheel_name, version_key
from virtual_life.settings import Settings

WHEEL = "virtual_life_linux-0.1.0-py3-none-any.whl"
OTHER = "virtual_life_linux-0.1.1-py3-none-any.whl"
PAYLOAD = b"fake wheel payload"


def make_client(tmp_path, *, secret="", directory=None):
    settings = Settings(
        database_path=str(tmp_path / "runtime.sqlite3"),
        api_token="test-token",
        dist_directory=str(directory or tmp_path / "dist"),
        dist_upload_secret=secret,
    )
    return TestClient(create_app(settings), headers={"Authorization": "Bearer test-token"})


def test_manifest_reports_empty_store(tmp_path):
    response = make_client(tmp_path).get("/v1/dist/manifest.json")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "dist_empty"


def test_manifest_requires_the_runtime_token(tmp_path):
    response = TestClient(create_app(Settings(database_path=str(tmp_path / "db.sqlite3")))).get(
        "/v1/dist/manifest.json"
    )
    assert response.status_code == 401


def test_upload_is_disabled_without_a_secret(tmp_path):
    response = make_client(tmp_path).post(
        "/v1/dist/packages",
        data={"upload_secret": "nope"},
        files={"file": (WHEEL, PAYLOAD)},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dist_upload_disabled"


def test_upload_and_download_roundtrip(tmp_path):
    store = tmp_path / "dist"
    handle = make_client(tmp_path, secret="dist-secret", directory=store)
    response = handle.post(
        "/v1/dist/packages",
        data={"upload_secret": "dist-secret"},
        files={"file": (WHEEL, PAYLOAD)},
    )
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["version"] == "0.1.0"
    assert entry["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert entry["size_bytes"] == len(PAYLOAD)

    manifest = handle.get("/v1/dist/manifest.json")
    assert manifest.status_code == 200
    body = manifest.json()
    assert body["dist_version"] == "1.0"
    assert body["default_version"] == "0.1.0"
    assert [item["wheel"] for item in body["packages"]] == [WHEEL]

    download = handle.get(f"/v1/dist/packages/{WHEEL}")
    assert download.status_code == 200
    assert download.content == PAYLOAD
    assert download.headers["content-disposition"].endswith(f'filename="{WHEEL}"')


def test_reupload_advances_the_default_version(tmp_path):
    handle = make_client(tmp_path, secret="s", directory=tmp_path / "dist")
    for name, payload in ((WHEEL, PAYLOAD), (OTHER, PAYLOAD + b"x")):
        response = handle.post(
            "/v1/dist/packages", data={"upload_secret": "s"}, files={"file": (name, payload)}
        )
        assert response.status_code == 201, response.text
    body = handle.get("/v1/dist/manifest.json").json()
    assert body["default_version"] == "0.1.1"
    assert [item["version"] for item in body["packages"]] == ["0.1.0", "0.1.1"]


def test_wrong_secret_and_bad_names_are_rejected(tmp_path):
    handle = make_client(tmp_path, secret="right", directory=tmp_path / "dist")
    assert (
        handle.post(
            "/v1/dist/packages",
            data={"upload_secret": "wrong"},
            files={"file": (WHEEL, PAYLOAD)},
        ).status_code
        == 403
    )
    assert (
        handle.post(
            "/v1/dist/packages",
            data={"upload_secret": "right"},
            files={"file": ("notes.txt", b"hello")},
        ).status_code
        == 400
    )


def test_download_rejects_unpublished_and_traversal_names(tmp_path):
    handle = make_client(tmp_path, secret="right", directory=tmp_path / "dist")
    assert handle.get(f"/v1/dist/packages/{WHEEL}").status_code == 404
    # The HTTP client normalizes ".." segments before routing, so traversal never
    # reaches the handler; the filename guard rejects it regardless.
    assert handle.get("/v1/dist/packages/not-a-wheel.whl").status_code == 400
    for name in ("../etc/passwd", "../../etc/passwd", "a/b.whl", "..", ""):
        assert is_wheel_name(name) is False


def test_package_limit_is_enforced(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "db.sqlite3"),
        api_token="test-token",
        dist_directory=str(tmp_path / "dist"),
        dist_upload_secret="s",
        dist_max_packages=1,
    )
    handle = TestClient(create_app(settings), headers={"Authorization": "Bearer test-token"})
    assert (
        handle.post(
            "/v1/dist/packages",
            data={"upload_secret": "s"},
            files={"file": (WHEEL, PAYLOAD)},
        ).status_code
        == 201
    )
    response = handle.post(
        "/v1/dist/packages",
        data={"upload_secret": "s"},
        files={"file": (OTHER, PAYLOAD)},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dist_limit_exceeded"


def test_size_limit_is_enforced(tmp_path):
    settings = Settings(
        database_path=str(tmp_path / "db.sqlite3"),
        api_token="test-token",
        dist_directory=str(tmp_path / "dist"),
        dist_upload_secret="s",
        dist_max_bytes=16,
    )
    handle = TestClient(create_app(settings), headers={"Authorization": "Bearer test-token"})
    response = handle.post(
        "/v1/dist/packages",
        data={"upload_secret": "s"},
        files={"file": (WHEEL, PAYLOAD)},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "dist_too_large"


def test_version_key_orders_prereleases_below_releases():
    ordered = sorted(
        {"0.1.0", "0.1.1", "1.0.0", "0.10.0", "0.1.0rc1", "0.1.0rc2"},
        key=version_key,
    )
    # A release outranks its own prereleases, and 0.10.0 sorts after 0.1.1.
    assert ordered == ["0.1.0rc1", "0.1.0rc2", "0.1.0", "0.1.1", "0.10.0", "1.0.0"]
