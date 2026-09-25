"""A desktop session must not become an unauthenticated local web API."""

import io
import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from image23mf.macos.desktop import COOKIE_NAME, DesktopBoundary, read_configuration

TOKEN = "a" * 64


def client():
    async def secret(request):
        return JSONResponse({"private": True})

    app = DesktopBoundary(
        Starlette(routes=[Route("/api/private", secret, methods=["GET", "POST"])]),
        token=TOKEN,
        port=54321,
    )
    return TestClient(app, base_url="http://127.0.0.1:54321")


def test_desktop_accepts_its_session_and_applies_frame_protection():
    with client() as c:
        response = c.post(
            "/api/private",
            headers={
                "Cookie": f"{COOKIE_NAME}={TOKEN}",
                "Origin": "http://127.0.0.1:54321",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert response.status_code == 200
        assert response.json() == {"private": True}
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Cookie": f"{COOKIE_NAME}=wrong"},
        {"Cookie": "malformed; =cookie"},
        {"Cookie": f"{COOKIE_NAME}={TOKEN}", "Host": "attacker.example"},
        {"Cookie": f"{COOKIE_NAME}={TOKEN}", "Origin": "http://localhost:54321"},
        {"Cookie": f"{COOKIE_NAME}={TOKEN}", "Origin": "https://attacker.example"},
        {"Cookie": f"{COOKIE_NAME}={TOKEN}", "Origin": "null"},
        {"Cookie": f"{COOKIE_NAME}={TOKEN}", "Sec-Fetch-Site": "cross-site"},
        {"Cookie": f"{COOKIE_NAME}={TOKEN}", "Sec-Fetch-Site": "same-site"},
    ],
)
def test_desktop_denies_missing_session_rebinding_and_cross_origin(headers):
    with client() as c:
        response = c.post("/api/private", headers=headers)
        assert response.status_code == 403
        assert "private" not in response.text


def test_configuration_requires_absolute_paths_and_a_strong_session_token(tmp_path):
    good = {"token": TOKEN, **{k: str(tmp_path / k) for k in ["home", "workspace", "web_root"]}}
    assert read_configuration(io.StringIO(json.dumps(good))) == good
    for patch in [{"token": "short"}, {"workspace": "relative"}, {"web_root": None}]:
        with pytest.raises(ValueError):
            read_configuration(io.StringIO(json.dumps({**good, **patch})))
