"""Signing in to a Classark account (qbx/account.py) against a fake Classark token server,
and the AGPL notices and source download (qbx/legal.py)."""

import io
import json
import sys
import threading
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from qbx import account, api_key, legal
from qbx import llm as llm_mod


class _MemoryKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


@pytest.fixture
def classark(tmp_path, monkeypatch):
    """A fake Classark server: the token endpoint (both grants) and revoke."""
    calls = []

    class Server(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            calls.append((self.path, body, self.headers.get("Authorization")))
            if self.path == "/v1/classark/auth/revoke":
                reply, code = {}, 200
            elif body.get("grant_type") == "authorization_code" and body.get("code") == "good":
                reply, code = {"access_token": "at1", "expires_in": 900, "refresh_token": "rt1",
                               "account": {"uid": "u1", "email": "t@school.org", "name": "T"}}, 200
            elif body.get("grant_type") == "refresh_token" and body.get("refresh_token") == "rt1":
                reply, code = {"access_token": "at2", "expires_in": 900, "refresh_token": "rt2", "account": {"uid": "u1"}}, 200
            else:
                reply, code = {"type": "error", "error": {"type": "authentication_error", "classark_code": "session_revoked"}}, 401
            data = json.dumps(reply).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(account, "API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(llm_mod, "CONFIG_DIR", tmp_path / "config")
    api_key.set_backend(_MemoryKeyring())
    opened = []
    account.set_opener(opened.append)
    account._access.update(token=None, expires_at=0.0)
    account._state.update(status="signed_out", error=None, url=None)
    yield calls, opened
    account.cancel_sign_in()
    server.shutdown()
    api_key.set_backend(None)


def _come_back(url: str, code: str, state: str | None = None) -> int:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    back = f"http://127.0.0.1:{query['port'][0]}/callback?" + urllib.parse.urlencode(
        {"code": code, "state": state or query["state"][0]}
    )
    try:
        return urllib.request.urlopen(back, timeout=10).status
    except urllib.error.HTTPError as e:
        return e.code


def test_signing_in_through_the_browser_keeps_only_the_refresh_token(classark):
    calls, opened = classark
    assert account.start_sign_in()["status"] == "waiting"
    url = opened[0]
    assert url.startswith(account.WEB_BASE + "/desktop/sign-in?") and "code_challenge=" in url

    assert _come_back(url, "good", state="forged") == 400  # a different state is refused
    assert account.status()["status"] == "waiting"
    assert _come_back(url, "good") == 200

    status = account.status()
    assert status["status"] == "signed_in" and status["email"] == "t@school.org"
    exchange = calls[-1][1]
    assert exchange["grant_type"] == "authorization_code" and len(exchange["code_verifier"]) >= 43
    assert api_key.get_secret(account.REFRESH_SECRET) == "rt1"
    assert "rt1" not in (llm_mod.CONFIG_DIR / "account.json").read_text()  # nothing secret on disk

    assert account.access_token() == "at1"  # fresh, from memory
    account._access["expires_at"] = 0
    assert account.access_token() == "at2"  # renewed, and the refresh token rotated
    assert api_key.get_secret(account.REFRESH_SECRET) == "rt2"

    account.sign_out()
    assert calls[-1][0] == "/v1/classark/auth/revoke" and calls[-1][2] == "Bearer at2"
    assert account.status()["status"] == "signed_out" and api_key.get_secret(account.REFRESH_SECRET) is None


def test_a_refused_refresh_signs_the_computer_out(classark):
    api_key.set_secret(account.REFRESH_SECRET, "stolen-and-rotated")
    llm_mod.CONFIG_DIR.mkdir(parents=True)
    (llm_mod.CONFIG_DIR / "account.json").write_text(json.dumps({"uid": "u1"}))
    with pytest.raises(account.AccountError, match="sign in again"):
        account.access_token()
    assert account.status()["status"] == "signed_out"


def test_a_bad_code_reports_the_error(classark):
    _, opened = classark
    account.start_sign_in()
    assert _come_back(opened[0], "bad") == 400
    status = account.status()
    assert status["status"] == "signed_out" and "sign in again" in status["error"]


def test_the_source_download_is_this_version_without_notes():
    names = zipfile.ZipFile(io.BytesIO(legal.make_source_zip())).namelist()
    top = f"ClassarkExamBuilder-{legal.version()}-source/"
    assert all(n.startswith(top) for n in names)
    files = {n[len(top):] for n in names}
    assert {"LICENSE", "NOTICE.txt", "engine/qbx/legal.py", "ui/src/App.tsx", "package.py"} <= files
    assert not any(f.startswith((".idea/", ".claude/")) or f == "CLAUDE.md" for f in files)


def test_about_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    monkeypatch.setattr(legal, "make_third_party_text", lambda repo=legal.REPO: "notices")
    sys.modules.pop("main", None)
    import main

    client = TestClient(main.app)
    about = client.get("/api/about").json()
    assert about["license"] == legal.LICENSE_NAME and "WITHOUT ANY WARRANTY" in about["notice"]
    assert "AFFERO" in client.get("/api/about/license").text
    reply = client.get("/api/about/source.zip")
    assert reply.headers["content-type"] == "application/zip" and "source.zip" in reply.headers["content-disposition"]
    assert client.get("/api/state").json()["account"]["status"] in ("signed_out", "signed_in")
