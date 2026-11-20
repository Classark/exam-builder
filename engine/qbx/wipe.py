"""Delete all my data (user-asked, Settings): everything this app keeps about the teacher,
so a computer can be handed on, or the app removed cleanly - deleting a Mac app never
removes its Application Support folder, and nothing can run when it's dragged to the Trash.

What goes: the whole data folder (launcher.data_dir) - papers, parsed papers, exams,
tags, syllabus lists, settings, the downloaded update - plus what's kept outside it: the
Classark sign-in (signed out on the server too) and any API key in the credential store.
What stays: the PDFs the app saved for the teacher (Documents\\Classark Exam Builder, or
wherever they chose) - those are the teacher's own documents, not the app's.

Only the packaged app can do this: the launcher hands over the data folder
(`set_app`), and a folder that doesn't look like one of the app's own is refused - so a
copy running from source, whose papers folder is the repository's, never deletes anything.

Most of the folder goes at once. What the running app holds open (its log, and the window's
browser storage) goes in a small process that waits for the app to quit, then removes the
folder; the app closes itself straight after.
"""

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("qbx.wipe")

# the folder names the launcher uses (launcher.DATA_FOLDER_NAME, APP_DATA_NAME)
FOLDER_NAMES = ("ClassarkExamBuilder data", "ClassarkExamBuilder")
# held open while the app runs: removed after it quits
_IN_USE = {"app.log", "webview"}

_folder: Path | None = None
_quit: Callable[[], None] | None = None


class WipeError(Exception):
    pass


def set_app(folder: Path, quit_app: Callable[[], None]) -> None:
    """The packaged app's data folder, and how to close the app (packaging/launcher.py)."""
    global _folder, _quit
    _folder, _quit = Path(folder).resolve(), quit_app


def data_folder() -> Path | None:
    """The data folder this copy may delete, or None (running from source, or a folder
    that isn't recognisably the app's own)."""
    folder = _folder
    if folder is None or folder.name not in FOLDER_NAMES or not folder.is_dir():
        return None
    if folder == Path.home().resolve() or len(folder.parts) < 3:
        return None
    if not any((folder / name).exists() for name in ("out", "config", "papers", "quizzes")):
        return None
    return folder


def available() -> bool:
    return data_folder() is not None


def _forget_credentials() -> None:
    from qbx import account, api_key

    try:
        account.sign_out()
    except Exception:  # noqa: BLE001 - offline or already out: forgetting locally is what matters
        log.exception("couldn't sign out before deleting the data")
        account.forget_sign_in()
    try:
        api_key.clear_api_key()
    except Exception:  # noqa: BLE001
        log.exception("couldn't remove the saved API key")


def _remove_after_exit(folder: Path) -> None:
    """A process outside the app that waits for it to quit, then removes `folder`."""
    pid = os.getpid()
    if sys.platform == "win32":
        quoted = str(folder).replace("'", "''")
        command = [
            "powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command",
            f"Wait-Process -Id {pid} -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1; "
            f"Remove-Item -LiteralPath '{quoted}' -Recurse -Force -ErrorAction SilentlyContinue",
        ]
        flags = subprocess.CREATE_NO_WINDOW
        try:  # outside the app's job, or it would end with the app (launcher._end_children_with_app)
            subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except OSError:
            subprocess.Popen(command, creationflags=flags)
        return
    subprocess.Popen(
        ["/bin/sh", "-c", 'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; rm -rf "$2"', "sh", str(pid), str(folder)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def delete_all() -> None:
    """Sign out, delete the data folder, and close the app. Raises WipeError when this copy
    can't (not the packaged app)."""
    folder = data_folder()
    if folder is None:
        raise WipeError("Only the installed app can delete its data.")
    log.warning("deleting all of this computer's data at the teacher's request: %s", folder)
    _forget_credentials()
    for item in folder.iterdir():
        if item.name in _IN_USE:
            continue
        try:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError:
            log.warning("couldn't delete %s now - it goes once the app has closed", item)
    _remove_after_exit(folder)
    if _quit is not None:
        threading.Timer(0.5, _quit).start()
