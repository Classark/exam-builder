"""Signing in to a Classark account - the desktop side of the server's sign-in (the
Classark platform repo's EXAM_BUILDER_API.md; loopback redirect with PKCE, RFC 8252).

1. `start_sign_in` makes a PKCE verifier and a state, listens on 127.0.0.1 at a free port,
   and opens the system browser (not the app's own window) at classark.com/desktop/sign-in.
2. The teacher signs in there and clicks Continue; the page sends the browser back to
   http://127.0.0.1:<port>/callback?code=...&state=...
3. The code and verifier are exchanged at the server's /v1/classark/auth/token for a
   15-minute access token and a refresh token. Only the refresh token is kept, in the OS
   credential store (api_key.py's keyring); the access token stays in memory and is renewed
   from it (`access_token`). The server rotates the refresh token on every use.

Who's signed in (`account.json`: uid, email, name - nothing secret) sits in the config folder
beside settings.json. The server isn't deployed yet, so today signing in ends in a "couldn't
reach Classark" error; QBX_ACCOUNT_API / QBX_ACCOUNT_WEB point it at an emulator.
"""

import base64
import hashlib
import json
import logging
import os
import platform
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from qbx import api_key
from qbx import llm as llm_mod

log = logging.getLogger("qbx.account")

API_BASE = os.environ.get("QBX_ACCOUNT_API", "https://api.classark.com").rstrip("/")
WEB_BASE = os.environ.get("QBX_ACCOUNT_WEB", "https://classark.com").rstrip("/")
REFRESH_SECRET = "classark_refresh_token"
# how long the app waits for the browser to come back before giving up
SIGN_IN_TIMEOUT = 10 * 60
# renew the access token when less than this is left (the contract says 2 minutes)
RENEW_MARGIN = 120

_DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Classark Exam Builder</title>
<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0;
background:#f7f5f2;color:#1f2a44}main{text-align:center;max-width:28rem;padding:1rem}</style></head>
<body><main><h1>%s</h1><p>%s</p></main></body></html>"""


class AccountError(Exception):
    """Signing in, or renewing the sign-in, failed; the message is for the teacher."""


# -- opening the browser -------------------------------------------------------------------

_opener: Callable[[str], object] = webbrowser.open


def set_opener(opener: Callable[[str], object]) -> None:
    """The packaged app opens URLs outside its own job object (launcher.open_outside_app),
    or the browser would close with it."""
    global _opener
    _opener = opener


# -- the server's token endpoint -----------------------------------------------------------


def _post(path: str, body: dict, bearer: str | None = None) -> dict:
    """POST JSON to the Classark server; its errors use Anthropic's shape with a
    classark_code (EXAM_BUILDER_API.md)."""
    request = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {bearer}"} if bearer else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return json.loads(reply.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            error = json.loads(e.read()).get("error", {})
        except ValueError:
            error = {}
        raise AccountError(_message(e.code, error.get("classark_code"), error.get("message"))) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AccountError("Couldn't reach Classark - check your internet connection and try again.") from e


def _message(status: int, code: str | None, message: str | None) -> str:
    if code == "no_subscription":
        return "Your Classark account doesn't have a Classark Exam Builder subscription."
    if code in ("session_revoked", "token_invalid", "token_expired") or status == 401:
        return "Your sign-in has ended - please sign in again."
    if code == "account_disabled":
        return "This Classark account is disabled."
    if status == 429:
        return "Too many sign-in attempts - wait a minute and try again."
    return message or f"Classark couldn't sign you in (error {status})."


# -- state ---------------------------------------------------------------------------------

_lock = threading.Lock()
_state: dict = {"status": "signed_out", "error": None, "url": None}
_access: dict = {"token": None, "expires_at": 0.0}
_server: ThreadingHTTPServer | None = None


def _account_path():
    return llm_mod.CONFIG_DIR / "account.json"


def _saved_account() -> dict | None:
    try:
        data = json.loads(_account_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("uid") else None
    except (OSError, ValueError):
        return None


def status() -> dict:
    """For /api/state: signed_out, waiting (the browser is open) or signed_in, with who."""
    with _lock:
        state = dict(_state)
    account = _saved_account()
    if state["status"] != "waiting":
        state["status"] = "signed_in" if account and api_key.get_secret(REFRESH_SECRET) else "signed_out"
    if account and state["status"] == "signed_in":
        state.update(email=account.get("email"), name=account.get("name"))
    return state


def _signed_in(reply: dict) -> None:
    if not reply.get("refresh_token") or not reply.get("access_token"):
        raise AccountError("Classark's reply had no sign-in in it.")
    api_key.set_secret(REFRESH_SECRET, reply["refresh_token"])
    account = reply.get("account") or {}
    llm_mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _account_path().write_text(
        json.dumps({"uid": account.get("uid"), "email": account.get("email"), "name": account.get("name")}, indent=1),
        encoding="utf-8",
    )
    with _lock:
        _access.update(token=reply["access_token"], expires_at=time.time() + float(reply.get("expires_in", 900)))


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return verifier, challenge


def start_sign_in() -> dict:
    """Opens the browser at Classark's sign-in page; the result arrives at the callback."""
    global _server
    cancel_sign_in()
    verifier, challenge = _pkce()
    state_token = secrets.token_urlsafe(24)

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):  # the window has no console; and the URL holds the code
            pass

        def do_GET(self):  # noqa: N802 - the stdlib's name
            url = urllib.parse.urlparse(self.path)
            if url.path != "/callback":
                self.send_error(404)
                return
            query = urllib.parse.parse_qs(url.query)
            code, got_state = (query.get("code") or [""])[0], (query.get("state") or [""])[0]
            if not code or not secrets.compare_digest(got_state, state_token):
                self._page(400, "Sign-in didn't match", "Go back to Classark Exam Builder and try again.")
                return
            try:
                _signed_in(_post("/v1/classark/auth/token",
                                 {"grant_type": "authorization_code", "code": code, "code_verifier": verifier}))
                error = None
            except AccountError as e:
                error = str(e)
            # the app's state first, then the page: the app may look the moment the page loads
            with _lock:
                _state.update(status="signed_out" if error else "signed_in", error=error, url=None)
            if error:
                self._page(400, "Sign-in failed", error)
            else:
                self._page(200, "You're signed in", "You can close this tab and go back to Classark Exam Builder.")
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def _page(self, code: int, title: str, text: str) -> None:
            body = (_DONE_PAGE % (title, text)).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
    port = server.server_address[1]
    url = f"{WEB_BASE}/desktop/sign-in?" + urllib.parse.urlencode(
        {"port": port, "state": state_token, "code_challenge": challenge, "device": platform.node() or "Computer"}
    )
    with _lock:
        _server = server
        _state.update(status="waiting", error=None, url=url)
    threading.Thread(target=server.serve_forever, daemon=True, name="sign-in callback").start()

    def give_up():
        time.sleep(SIGN_IN_TIMEOUT)
        with _lock:
            if _server is server and _state["status"] == "waiting":
                _state.update(status="signed_out", error="Sign-in timed out - try again.", url=None)
            else:
                return
        server.shutdown()

    threading.Thread(target=give_up, daemon=True).start()
    try:
        _opener(url)
    except Exception:  # the page shows the link to open by hand
        log.warning("couldn't open the browser for sign-in")
    return status()


def cancel_sign_in() -> None:
    global _server
    with _lock:
        server, _server = _server, None
        if _state["status"] == "waiting":
            _state.update(status="signed_out", url=None)
    if server is not None:
        threading.Thread(target=server.shutdown, daemon=True).start()


def access_token() -> str:
    """A current access token for the Classark server, renewed from the stored refresh
    token when it's close to expiring. Raises AccountError when not signed in."""
    with _lock:
        if _access["token"] and _access["expires_at"] - time.time() > RENEW_MARGIN:
            return _access["token"]
    refresh = api_key.get_secret(REFRESH_SECRET)
    if not refresh:
        raise AccountError("Sign in to your Classark account to use Claude.")
    try:
        reply = _post("/v1/classark/auth/token", {"grant_type": "refresh_token", "refresh_token": refresh})
    except AccountError:
        # a refused refresh means the session is over (revoked, expired, reused): forget it
        # so the app asks to sign in again, rather than failing the same way every call
        _forget()
        raise
    _signed_in(reply)
    return reply["access_token"]


def _forget() -> None:
    api_key.delete_secret(REFRESH_SECRET)
    try:
        _account_path().unlink()
    except OSError:
        pass
    with _lock:
        _access.update(token=None, expires_at=0.0)


def sign_out() -> None:
    """Ends this computer's session on the server (best effort) and forgets it here."""
    cancel_sign_in()
    with _lock:
        token = _access["token"] if _access["expires_at"] > time.time() else None
    if token:
        try:
            _post("/v1/classark/auth/revoke", {}, bearer=token)
        except AccountError:
            log.info("couldn't tell Classark about the sign-out; the session expires on its own")
    _forget()
    with _lock:
        _state.update(status="signed_out", error=None, url=None)


def web_url(path: str) -> str:
    """A page on classark.com (the subscription and credit pages)."""
    return WEB_BASE + path


def open_web(path: str) -> None:
    _opener(web_url(path))
