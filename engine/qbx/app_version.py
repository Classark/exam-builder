"""The oldest version of this app still allowed, from Classark's own config (user-asked:
the same switch the mobile apps use).

Firestore's `config/app` document - edited in the admin dashboard's App versions window,
readable by anyone (firestore.rules) - holds the mobile apps' minimums, and for this app:

    minExamBuilderVersion   "0.2.0": an older copy shows "Please update" and goes no further
    examBuilderUpdateUrl    where the update is - the Microsoft Store page
    examBuilderMacUpdateUrl optional: where a Mac finds it (the DMG download page); a Mac
                            uses examBuilderUpdateUrl when it isn't set
    examBuilderMacVersion   the Mac release rolled out, "1.0.1": a Mac copy older than it
                            downloads it from Classark's downloads bucket and offers to
                            restart into it (qbx/mac_update.py). Empty or missing: no update

Read through Firestore's public REST endpoint at start and every CHECK_SECONDS, before
anyone signs in; nothing about the computer or teacher is sent. A missing field, or no
answer (offline, Google unreachable), means no minimum: the app never locks itself out
for want of a connection. Only release builds are held to it (`required` is reported to a
debug build too, but it carries on). `QBX_CONFIG_URL` points it elsewhere, for testing.
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.request

from qbx import legal

CONFIG_URL = "https://firestore.googleapis.com/v1/projects/theplatform-sw/databases/(default)/documents/config/app"
CHECK_SECONDS = 3600.0

log = logging.getLogger("qbx.app_version")
_lock = threading.Lock()
_state: dict = {"checked_at": 0.0, "minimum": None, "url": None, "mac_url": None, "mac_version": None}
_checking = False


def _parts(version: str) -> tuple[int, ...]:
    out = []
    for piece in version.strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple((out + [0, 0, 0])[:3])


def older(version: str, minimum: str) -> bool:
    """'0.1.9' is older than '0.1.10'; missing parts count as 0."""
    return _parts(version) < _parts(minimum)


def _field(fields: dict, name: str) -> str | None:
    value = fields.get(name) or {}
    found = value.get("stringValue") or value.get("integerValue")
    return str(found).strip() if found else None


def _fetch() -> None:
    global _checking
    try:
        url = os.environ.get("QBX_CONFIG_URL", CONFIG_URL)
        if not url:  # switched off (the tests)
            with _lock:
                _state["checked_at"] = time.time()
            return
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ClassarkExamBuilder"}), timeout=15) as reply:
            fields = json.loads(reply.read().decode("utf-8")).get("fields", {})
        with _lock:
            _state.update(
                checked_at=time.time(),
                minimum=_field(fields, "minExamBuilderVersion"),
                url=_field(fields, "examBuilderUpdateUrl"),
                mac_url=_field(fields, "examBuilderMacUpdateUrl"),
                mac_version=_field(fields, "examBuilderMacVersion"),
            )
    except Exception as e:  # offline, or no such document: no minimum, try again later
        log.info("couldn't read the minimum version: %s", e)
        with _lock:
            _state["checked_at"] = time.time()
    finally:
        _checking = False


def status() -> dict:
    """{version, minimum, url, required, mac_version} - and a fresh check in the background when the
    last is over CHECK_SECONDS old. Never waits on the network."""
    global _checking
    with _lock:
        due = time.time() - _state["checked_at"] > CHECK_SECONDS and not _checking
        if due:
            _checking = True
        minimum, url, mac_version = _state["minimum"], _state["url"], _state["mac_version"]
        if sys.platform == "darwin" and _state["mac_url"]:
            url = _state["mac_url"]
    if due:
        threading.Thread(target=_fetch, daemon=True, name="app-version").start()
    current = legal.version()
    return {
        "version": current,
        "minimum": minimum,
        "url": url,
        "required": bool(minimum) and older(current, minimum),
        "mac_version": mac_version,
    }
