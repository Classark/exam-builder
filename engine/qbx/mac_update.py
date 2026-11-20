"""Updates the Mac app by itself (user-asked: teachers shouldn't download and set up every
new version). Windows needs none of this - the Microsoft Store updates it.

Each Mac release is uploaded to Classark's downloads bucket (Cloudflare R2, no egress
charges; .github/workflows/mac.yml) as `RELEASES/<version>/`: its DMGs, each kind of Mac's
zipped app, and one small feed per kind of Mac (package.py mac):

    mac-arm64.json / mac-x86_64.json
        {"version": "1.0.1", "file": "ClassarkExamBuilder-1.0.1-mac-arm64.zip",
         "sha256": "...", "size": 92345678}

Uploading rolls nothing out. Which version Macs move to is config/app's
`examBuilderMacVersion`, set in the admin dashboard's App versions window
(qbx/app_version.py reads it hourly); empty means no update. When it's newer than this copy,
a release build running as an app bundle reads that version's feed (`file` is relative to
it), downloads the zip into its own `updates` folder and checks it before offering it:

- the download's size and SHA-256 are the feed's;
- the app inside is this app (bundle id), the version the feed says, intact
  (`codesign --verify --deep --strict`), and signed by the same Apple team as the copy
  that's running - so a feed or download that isn't ours can't install anything. An
  unsigned copy never updates itself.

"Restart to update" then hands over to a small script that waits for this app to quit,
moves the old app into `updates`, moves the new one into its place, opens it, and deletes
the old one - or puts the old one back if the swap failed.

**The teacher's work is never touched** (user-asked): papers, parsed papers, exams, tags,
settings and the sign-in live in ~/Library/Application Support/ClassarkExamBuilder, never
inside the app (launcher.data_dir). The only things this module writes or removes are its
own `updates` folder and the app bundle it replaces.

Where the app can't replace itself - a folder the teacher can't write to (a managed
school Mac), run straight from the DMG, or macOS's App Translocation - it says so, and the
"Please update" screen's download link (app_version) is the way.

`QBX_MAC_FEED` points it at one feed instead ("{arch}" filled in; "" off), for testing;
`QBX_MAC_RELEASES` at another bucket.
"""

import hashlib
import json
import logging
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from functools import cache
from pathlib import Path
from typing import Callable

from qbx import app_version, edition, legal

RELEASES = "https://downloads.classark.com/exam-builder/mac"
CHECK_SECONDS = 3600.0
_CHUNK = 1 << 16

log = logging.getLogger("qbx.mac_update")
_lock = threading.Lock()
_state: dict = {"status": "idle", "version": None, "done": 0, "total": 0, "error": None, "checked_at": 0.0}
_checking = False
_ready_app: Path | None = None
_quit: Callable[[], None] | None = None


# -- where things are -----------------------------------------------------------------------


def set_quitter(quit_app: Callable[[], None]) -> None:
    """How to close the app once the swap is handed over (the launcher's window)."""
    global _quit
    _quit = quit_app


def updates_dir() -> Path:
    if os.environ.get("QBX_UPDATES_DIR"):
        return Path(os.environ["QBX_UPDATES_DIR"])
    from qbx.llm import CONFIG_DIR

    return CONFIG_DIR.parent / "updates"


def running_app() -> Path | None:
    """The .app bundle this copy runs from, or None (not frozen, not a Mac, not a bundle)."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    app = Path(sys.executable).resolve().parents[2]
    return app if app.suffix == ".app" else None


@cache
def arch() -> str:
    """Which build this Mac should run: an Intel copy running under Rosetta moves to the
    Apple Silicon build."""
    if platform.machine() == "arm64":
        return "arm64"
    try:
        translated = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True)
        if translated.stdout.strip() == "1":
            return "arm64"
    except OSError:
        pass
    return "x86_64"


def rolled_out() -> str | None:
    """The Mac version the admin dashboard has rolled out (config/app), or None."""
    return app_version.status().get("mac_version") or None


def feed_url() -> str:
    """This Mac's feed for the rolled-out version, or "" when there's nothing to look at."""
    if "QBX_MAC_FEED" in os.environ:
        template = os.environ["QBX_MAC_FEED"]
    else:
        version = rolled_out()
        base = os.environ.get("QBX_MAC_RELEASES", RELEASES).rstrip("/")
        if not version or not base or not app_version.older(legal.version(), version):
            return ""
        template = f"{base}/{urllib.parse.quote(version)}/mac-{{arch}}.json"
    return template.replace("{arch}", arch()) if template else ""



def _blocked(app: Path) -> str | None:
    """Why this copy can't replace itself, or None."""
    text = str(app)
    if "/AppTranslocation/" in text or text.startswith("/Volumes/"):
        return "Move Classark Exam Builder into your Applications folder to have it update itself."
    if not (os.access(app.parent, os.W_OK) and os.access(app, os.W_OK)):
        return "This Mac doesn't let the app replace itself - download the update instead."
    return None


def enabled() -> bool:
    """Whether this copy updates itself at all: a release build running as an app bundle."""
    return running_app() is not None and edition.edition() == "release"


# -- checks -------------------------------------------------------------------------------


def _team(app: Path) -> str | None:
    """The Apple team that signed `app`, or None (unsigned, ad hoc, or not readable)."""
    shown = subprocess.run(["codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True)
    found = re.search(r"^TeamIdentifier=(\S+)$", shown.stderr, re.MULTILINE)
    team = found.group(1) if found else None
    return None if team in (None, "not", "not set") else team


def _bundle_info(app: Path) -> dict:
    with open(app / "Contents" / "Info.plist", "rb") as f:
        return plistlib.load(f)


def verify_app(new: Path, current: Path, version: str) -> None:
    """Raises UpdateError unless `new` is this app, at `version`, intact and signed by the
    same team as `current`."""
    try:
        info, own = _bundle_info(new), _bundle_info(current)
    except (OSError, plistlib.InvalidFileException) as e:
        raise UpdateError("The update isn't a complete app.") from e
    if info.get("CFBundleIdentifier") != own.get("CFBundleIdentifier"):
        raise UpdateError("The update is a different app.")
    if info.get("CFBundleShortVersionString") != version:
        raise UpdateError("The update isn't the version it says it is.")
    team = _team(current)
    if not team:
        raise UpdateError("This copy isn't signed, so it can't check an update.")
    checked = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(new)], capture_output=True, text=True)
    if checked.returncode != 0:
        raise UpdateError("The update's signature is broken.")
    if _team(new) != team:
        raise UpdateError("The update isn't signed by Classark.")


class UpdateError(Exception):
    pass


# -- the background check -----------------------------------------------------------------


def _set(**changes) -> None:
    with _lock:
        _state.update(changes)


def _read_feed(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "ClassarkExamBuilder"})
    with urllib.request.urlopen(request, timeout=20) as reply:
        feed = json.loads(reply.read().decode("utf-8"))
    if not all(isinstance(feed.get(k), str) for k in ("version", "file", "sha256")) or not isinstance(feed.get("size"), int):
        raise UpdateError("The update feed isn't readable.")
    if "/" in feed["file"] or "\\" in feed["file"] or not feed["file"].endswith(".zip"):
        raise UpdateError("The update feed names an unexpected file.")
    return feed


def _download(url: str, to: Path, size: int, sha256: str) -> None:
    digest = hashlib.sha256()
    done = 0
    request = urllib.request.Request(url, headers={"User-Agent": "ClassarkExamBuilder"})
    with urllib.request.urlopen(request, timeout=60) as reply, open(to, "wb") as out:
        while chunk := reply.read(_CHUNK):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            _set(done=done)
            if done > size:
                raise UpdateError("The update is larger than it should be.")
    if done != size or digest.hexdigest() != sha256.lower():
        raise UpdateError("The update didn't download completely - it'll be tried again.")


def _unpack(archive: Path, to: Path) -> None:
    # ditto keeps what a zip can't on its own: the bundle's symlinks, modes and signature
    subprocess.run(["ditto", "-x", "-k", str(archive), str(to)], check=True, capture_output=True)


def _clear(folder: Path) -> None:
    """Empty the updates folder - it's this module's own, nothing else is ever in it."""
    if folder.is_dir():
        for item in folder.iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)


def check_now() -> None:
    """Read the feed; download, unpack and verify a newer version. Runs in the background."""
    global _checking, _ready_app
    try:
        current = running_app()
        if current is None:
            return
        url = feed_url()
        if not url:  # nothing rolled out, or this copy already has it
            _clear(updates_dir())
            _set(status="idle", version=None, error=None)
            return
        feed = _read_feed(url)
        version = feed["version"]
        wanted = rolled_out()
        if "QBX_MAC_FEED" not in os.environ and wanted and version != wanted:
            raise UpdateError(f"The update feed for {wanted} lists version {version}.")
        if not app_version.older(legal.version(), version):
            _clear(updates_dir())  # the last update, now running: nothing left to keep
            _set(status="idle", version=None, error=None)
            return
        blocked = _blocked(current)
        if blocked:
            _set(status="manual", version=version, error=blocked)
            return
        folder = updates_dir()
        staged = folder / version / current.name
        marker = folder / version / "verified"
        if marker.exists() and marker.read_text(encoding="utf-8") == feed["sha256"] and staged.is_dir():
            _ready_app = staged
            _set(status="ready", version=version, error=None)
            return
        _clear(folder)
        (folder / version).mkdir(parents=True, exist_ok=True)
        archive = folder / version / feed["file"]
        _set(status="downloading", version=version, done=0, total=feed["size"], error=None)
        _download(urllib.parse.urljoin(url, feed["file"]), archive, feed["size"], feed["sha256"])
        unpacked = folder / version / "unpacked"
        _unpack(archive, unpacked)
        apps = [p for p in unpacked.iterdir() if p.suffix == ".app"]
        if len(apps) != 1:
            raise UpdateError("The update doesn't hold the app.")
        verify_app(apps[0], current, version)
        apps[0].rename(staged)
        shutil.rmtree(unpacked, ignore_errors=True)
        archive.unlink(missing_ok=True)
        marker.write_text(feed["sha256"], encoding="utf-8")
        _ready_app = staged
        _set(status="ready", version=version, error=None)
        log.info("version %s downloaded and checked, ready to install", version)
    except UpdateError as e:
        log.warning("update not installed: %s", e)
        _set(status="failed", error=str(e))
    except Exception as e:  # offline, GitHub down, no release yet: quietly try again later
        log.info("couldn't check for an update: %s", e)
        with _lock:
            if _state["status"] == "downloading":
                _state.update(status="failed", error="The update didn't download - it'll be tried again.")
    finally:
        with _lock:
            _state["checked_at"] = time.time()
        _checking = False


def status() -> dict | None:
    """{status, version, done, total, error} - "downloading", "ready" (restart to update),
    "manual" (download it by hand: `error` says why), "failed", or "idle" - and a check in
    the background when the last is over CHECK_SECONDS old. None where this copy doesn't
    update itself (Windows, a debug build, running from source)."""
    global _checking
    if not enabled():
        return None
    with _lock:
        due = (
            time.time() - _state["checked_at"] > CHECK_SECONDS
            and not _checking
            and _state["status"] not in ("downloading", "ready")
        )
        if due:
            _checking = True
        shown = {k: _state[k] for k in ("status", "version", "done", "total", "error")}
    if due:
        threading.Thread(target=check_now, daemon=True, name="mac-update").start()
    return shown


# -- installing ---------------------------------------------------------------------------

# Waits for the app to quit, then swaps the bundles; never touches anything else.
# $1 pid of the running app, $2 the app to replace, $3 the verified new app, $4 where the
# old one waits (inside the updates folder) until the new one is in place.
SWAP_SCRIPT = """#!/bin/sh
pid="$1"; app="$2"; new="$3"; old="$4"
while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done
rm -rf "$old"
if mv "$app" "$old"; then
  if mv "$new" "$app"; then
    rm -rf "$old"
  else
    rm -rf "$app"
    mv "$old" "$app"
  fi
fi
open "$app"
"""


def install() -> None:
    """Hand the swap to SWAP_SCRIPT and close the app. Raises UpdateError when there's
    nothing verified to install."""
    current, staged = running_app(), _ready_app
    with _lock:
        ready = _state["status"] == "ready"
    if current is None or staged is None or not ready or not staged.is_dir():
        raise UpdateError("There's no update ready to install.")
    folder = updates_dir()
    script = folder / "install.sh"
    script.write_text(SWAP_SCRIPT, encoding="utf-8")
    old = folder / "previous.app"
    with open(folder / "install.log", "ab") as out:
        subprocess.Popen(
            ["/bin/sh", str(script), str(os.getpid()), str(current), str(staged), str(old)],
            start_new_session=True,
            stdout=out,
            stderr=out,
        )
    log.info("installing version %s: the app closes and opens again", _state["version"])
    if _quit is not None:
        threading.Timer(0.3, _quit).start()
