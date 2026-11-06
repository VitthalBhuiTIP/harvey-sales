"""The dashboard hosted on cPanel/Passenger (passenger_wsgi.py) under a URL
prefix and a public host name: HTTPS only, token still required, and the
loopback-only default unchanged when nothing is configured."""

import importlib
import io
import sys

import pytest

import harvey.dashboard as dash
from harvey import dashboard_auth as auth

HOST = "sales.example.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    return {"token": dash.token_store.get()}


def test_public_hosts_default_to_none(monkeypatch):
    monkeypatch.delenv(auth.PUBLIC_HOSTS_ENV, raising=False)
    assert auth.public_hosts_from_env() == frozenset()


def test_public_hosts_are_parsed(monkeypatch):
    monkeypatch.setenv(auth.PUBLIC_HOSTS_ENV, " Sales.Example.com, www.example.com ,")
    assert auth.public_hosts_from_env() == {"sales.example.com", "www.example.com"}


@pytest.mark.parametrize("bad", ["https://example.com", "example.com:443", "example.com/x",
                                 "localhost", "127.0.0.1"])
def test_malformed_public_host_stops_startup(monkeypatch, bad):
    monkeypatch.setenv(auth.PUBLIC_HOSTS_ENV, bad)
    with pytest.raises(ValueError):
        auth.public_hosts_from_env()


@pytest.fixture
def wsgi(env, monkeypatch):
    """passenger_wsgi.application, imported as Passenger would."""
    monkeypatch.setenv(auth.PUBLIC_HOSTS_ENV, HOST)
    monkeypatch.setenv("HARVEY_BASE_PATH", "/harvey-sales")
    monkeypatch.setattr(dash.app, "middleware_stack", None)  # rebuild with the env above
    sys.modules.pop("passenger_wsgi", None)
    module = importlib.import_module("passenger_wsgi")
    yield module.application
    monkeypatch.setattr(dash.app, "middleware_stack", None)
    sys.modules.pop("passenger_wsgi", None)


def call(app, path, *, method="GET", scheme="https", host=HOST, headers=None, body=b""):
    script, _, rest = path.partition("/harvey-sales")
    environ = {
        "REQUEST_METHOD": method, "SCRIPT_NAME": "/harvey-sales", "PATH_INFO": rest,
        "QUERY_STRING": "", "SERVER_NAME": host, "SERVER_PORT": "443",
        "SERVER_PROTOCOL": "HTTP/1.1", "wsgi.url_scheme": scheme,
        "wsgi.input": io.BytesIO(body), "wsgi.errors": io.StringIO(),
        "wsgi.version": (1, 0), "wsgi.multithread": True, "wsgi.multiprocess": False,
        "wsgi.run_once": False, "HTTP_HOST": host, "CONTENT_LENGTH": str(len(body)),
    }
    for k, v in (headers or {}).items():
        environ["HTTP_" + k.upper().replace("-", "_")] = v
    out = {}

    def start_response(status, response_headers, exc_info=None):
        out["status"] = int(status.split()[0])
        out["headers"] = {k.lower(): v for k, v in response_headers}

    out["body"] = b"".join(app(environ, start_response))
    return out


def test_shell_is_served_under_the_prefix(wsgi):
    r = call(wsgi, "/harvey-sales/")
    assert r["status"] == 200
    assert b'src="/harvey-sales/static/auth.js"' in r["body"]
    assert b'href="/harvey-sales/static/app.css"' in r["body"]
    assert call(wsgi, "/harvey-sales/static/app.js")["status"] == 200


def test_bare_prefix_redirects_to_trailing_slash(wsgi):
    r = call(wsgi, "/harvey-sales")
    assert r["status"] == 301 and r["headers"]["location"] == "/harvey-sales/"


def test_api_still_needs_the_token(wsgi, env):
    assert call(wsgi, "/harvey-sales/api/stats")["status"] == 401
    r = call(wsgi, "/harvey-sales/api/stats",
             headers={"Authorization": f"Bearer {env['token']}"})
    assert r["status"] == 200


def test_plain_http_is_refused(wsgi):
    r = call(wsgi, "/harvey-sales/", scheme="http")
    assert r["status"] == 400 and b"https_required" in r["body"]


def test_other_hosts_are_still_refused(wsgi):
    r = call(wsgi, "/harvey-sales/", host="evil.example")
    assert r["status"] == 400 and b"bad_host" in r["body"]


def test_https_origin_is_accepted_and_others_refused(wsgi, env):
    auth_h = {"Authorization": f"Bearer {env['token']}", "Content-Type": "application/json"}
    ok = call(wsgi, "/harvey-sales/api/stats",
              headers={**auth_h, "Origin": f"https://{HOST}", "Sec-Fetch-Site": "same-origin"})
    assert ok["status"] == 200
    bad = call(wsgi, "/harvey-sales/api/stats",
               headers={**auth_h, "Origin": f"http://{HOST}", "Sec-Fetch-Site": "same-origin"})
    assert bad["status"] == 403


def test_local_dashboard_is_unchanged(env, monkeypatch):
    monkeypatch.delenv("HARVEY_BASE_PATH", raising=False)
    html = (dash.WEB_DIR / "index.html").read_text(encoding="utf-8")
    import asyncio
    assert asyncio.run(dash.dashboard()) == html
