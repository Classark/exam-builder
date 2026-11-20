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


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    """A fake Classark proxy: /v1/messages, topic lists, balance and the token endpoint."""
    seen = []
    state = {"expire_once": True, "credit": True}

    class Server(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, reply, headers=()):
            data = json.dumps(reply).encode()
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, code, kind, classark_code):
            self._send(code, {"type": "error", "error": {"type": kind, "message": "x", "classark_code": classark_code}})

        def do_GET(self):  # noqa: N802
            seen.append(("GET", self.path, dict(self.headers)))
            if self.path == "/v1/classark/topics/0620":
                if self.headers.get("If-None-Match") == '"0620@3"':
                    self.send_response(304)
                    self.end_headers()
                    return
                self._send(200, {"subject": "0620", "version": 3, "topics": [
                    {"id": "1", "name": "States of matter", "parent": None, "outcomes": ["describe the states"]}]},
                    [("ETag", '"0620@3"')])
            elif self.path.startswith("/v1/classark/topics/"):
                self._error(404, "not_found_error", "unknown_subject")
            elif self.path == "/v1/classark/balance":
                self._send(200, {"currency": "USD", "available_micros": 4200000})

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            seen.append(("POST", self.path, dict(self.headers), body))
            if self.path == "/v1/classark/auth/token":
                self._send(200, {"access_token": "at-new", "expires_in": 900, "refresh_token": "rt-new"})
            elif self.path == "/v1/messages":
                if self.headers.get("Authorization") == "Bearer at-old" and state["expire_once"]:
                    state["expire_once"] = False
                    return self._error(401, "authentication_error", "token_expired")
                if not state["credit"]:
                    return self._error(402, "insufficient_credit", "out_of_credit")
                self._send(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                                 "content": [{"type": "text", "text": '{"ok": true}'}], "stop_reason": "end_turn",
                                 "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(account, "API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(llm_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setenv("QBX_EDITION", "release")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-be-sent")
    api_key.set_backend(_MemoryKeyring())
    llm_mod.set_llm_override(None)
    api_key.set_secret(account.REFRESH_SECRET, "rt-old")
    llm_mod.CONFIG_DIR.mkdir(parents=True)
    (llm_mod.CONFIG_DIR / "account.json").write_text(json.dumps({"uid": "u1"}))
    account._access.update(token="at-old", expires_at=9e12)
    account._state.update(status="signed_out", error=None, url=None)
    yield seen, state
    server.shutdown()
    api_key.set_backend(None)



def test_claude_through_the_classark_proxy(proxy):
    from pydantic import BaseModel

    from qbx import topics

    class Ok(BaseModel):
        ok: bool

    seen, state = proxy
    llm = llm_mod.get_llm("tag")
    with llm_mod.cache_label("0620_42_MJ_23"):
        assert llm.json("hi", Ok).ok
    posts = [s for s in seen if s[0] == "POST" and s[1] == "/v1/messages"]
    # the first try's token had expired: renewed once, then sent again
    assert [p[2]["Authorization"] for p in posts] == ["Bearer at-old", "Bearer at-new"]
    headers = posts[-1][2]
    assert headers["X-Classark-Job"] == "tag" and headers["X-Classark-Cache"] == "tag:0620_42_MJ_23"
    assert headers["X-Classark-Client"].startswith("exam-builder/")
    assert "X-Api-Key" not in headers and "anthropic-beta" not in {k.lower() for k in headers}
    assert api_key.get_secret(account.REFRESH_SECRET) == "rt-new"

    # out of credit pauses a run (a usage limit), in the teacher's words
    state["credit"] = False
    with pytest.raises(llm_mod.UsageLimitError, match="out of Claude credit"):
        llm_mod.get_llm("chat").json("hi", Ok)

    # topic lists come from the server, kept with their ETag and re-checked with it
    listed = topics.load_topics("0620").topics
    assert [t.name for t in listed] == ["States of matter"] and listed[0].outcomes == ["describe the states"]
    topics._checked.clear()
    assert topics.load_topics("0620").topics[0].id == "1"  # a 304: the kept copy
    assert [s[2].get("If-None-Match") for s in seen if s[1] == "/v1/classark/topics/0620"] == [None, '"0620@3"']
    assert topics.load_topics("9999").topics == []  # the server has none
    assert account.balance()["available_micros"] == 4200000


def test_the_page_the_browser_comes_back_to_is_filled_and_escaped():
    ok = account._done_page(True, "You're signed in", "Go back to <the app>.")
    assert 'class="card ok"' in ok and "polyline" in ok and "You&#x27;re signed in" in ok
    assert "&lt;the app&gt;" in ok and "{title}" not in ok and "{text}" not in ok
    bad = account._done_page(False, "Sign-in failed", "Try again.")
    assert 'class="card bad"' in bad and "<line" in bad


def test_the_app_opens_urls_and_folders_without_a_shell():
    # `cmd /c start "" <url>` cut the sign-in URL at its first "&", and classark.com said
    # "This sign-in link is not valid" (user-reported); the in-app link always worked
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "launcher", Path(__file__).resolve().parents[2] / "packaging" / "launcher.py"
    )
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    url = "https://classark.com/desktop/sign-in?code_challenge=abc&state=xyz&port=51234"
    assert launcher.open_command(url) == ["rundll32", "url.dll,FileProtocolHandler", url]
    assert launcher.open_command(r"C:\Exams\Chemistry & Physics") == ["explorer", r"C:\Exams\Chemistry & Physics"]
    assert "cmd" not in launcher.open_command(url) + launcher.open_command(r"C:\x")
