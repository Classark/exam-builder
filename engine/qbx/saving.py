"""Where the app saves what a teacher makes (user-decided): its own folder, not the browser's
downloads.

Exams, topic booklets and the source code are written straight to disk by the app, never
through the window's browser engine (whose download bar the user didn't want), into a save
folder - `Documents\\Classark Exam Builder` unless the teacher picks another. The teacher
chooses it once, right after signing in (`chosen`), and can change it in Settings; it's the
only setting a release build has.

Each exam gets a folder of its own, named after it, holding its question paper, mark scheme
and sources list. Saving an exam again after changing it first moves the files of the last
save into `Old versions\\Version n (date)` - the date that version was saved - so nothing a
teacher printed is overwritten. Saving an unchanged exam replaces its files in place. What
counts as "changed" is the exam's content (`fingerprint`), kept in the folder's
`.exam-builder.json` beside the list of files this app wrote, so a teacher's own files in the
folder are never moved.

Every save is listed (config/downloads.json, newest first) for the top bar's Saved files
list, with Open and Show in folder. Only files on that list, or the save folder itself, are
ever opened - the paths come from the UI.
"""

import datetime
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from qbx import llm as llm_mod

log = logging.getLogger("qbx.saving")

APP_FOLDER = "Classark Exam Builder"
OLD_VERSIONS = "Old versions"
MANIFEST = ".exam-builder.json"
RECENT_LIMIT = 50
# Windows' reserved characters and names, and trailing dots/spaces, which it drops silently
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

_lock = threading.Lock()


class SaveError(Exception):
    """Saving failed; the message is for the teacher."""


# -- the save folder -----------------------------------------------------------------------


def documents_folder() -> Path:
    """The user's Documents folder, wherever Windows has it (it can be moved, or synced
    by OneDrive)."""
    if sys.platform == "win32":
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(32768)
            # CSIDL_PERSONAL (5): Documents, following any move the user made
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer) == 0 and buffer.value:
                return Path(buffer.value)
        except (OSError, AttributeError):
            pass
    return Path.home() / "Documents"


def default_folder() -> Path:
    return documents_folder() / APP_FOLDER


def _prefs_path() -> Path:
    return llm_mod.CONFIG_DIR / "preferences.json"


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def status() -> dict:
    """The save folder, and whether the teacher has chosen it yet (asked once, after
    signing in)."""
    prefs = _read_json(_prefs_path(), {})
    folder = prefs.get("save_folder") or str(default_folder())
    return {
        "folder": folder,
        "chosen": bool(prefs.get("save_folder_chosen")),
        "default": str(default_folder()),
        "can_pick": _picker is not None,
    }


def folder() -> Path:
    return Path(status()["folder"])


def set_folder(path: str) -> dict:
    """Saves the teacher's choice, once the folder exists and can be written to."""
    text = (path or "").strip().strip('"')
    if not text:
        raise SaveError("Choose a folder to save into.")
    target = Path(os.path.expandvars(os.path.expanduser(text)))
    if not target.is_absolute():
        raise SaveError("Give the folder's full location, e.g. C:\\Users\\you\\Documents\\Exams.")
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".classark-write-test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as e:
        raise SaveError(f"The app can't save into {target}: {e.strerror or e}.") from e
    with _lock:
        prefs = _read_json(_prefs_path(), {})
        prefs.update(save_folder=str(target), save_folder_chosen=True)
        _write_json(_prefs_path(), prefs)
    return status()


# -- the window's folder picker and the system's file opener (set by the packaged app) -------

_picker: Callable[[str], str | None] | None = None


def _default_open(target: str) -> None:
    if sys.platform == "win32":
        os.startfile(target)  # noqa: S606 - a file or folder the app itself wrote
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", target], check=False)


_opener: Callable[[str], object] = _default_open


def set_folder_picker(picker: Callable[[str], str | None] | None) -> None:
    """The app window's own folder dialog (pywebview), starting at the given folder; it
    returns the folder chosen or None. Without one - running in a browser - the UI asks
    for the location as text."""
    global _picker
    _picker = picker


def set_opener(opener: Callable[[str], object]) -> None:
    """launcher.open_outside_app: a PDF reader or Explorer the app starts must not close
    with it."""
    global _opener
    _opener = opener


def pick_folder() -> str | None:
    if _picker is None:
        return None
    start = folder()
    return _picker(str(start if start.exists() else documents_folder()))


# -- names -------------------------------------------------------------------------------------


def safe_name(name: str, fallback: str = "Exam") -> str:
    """A file or folder name Windows and macOS both accept, close to what the teacher typed."""
    text = _UNSAFE_RE.sub(" ", name or "")
    text = " ".join(text.split()).rstrip(". ")[:100].rstrip(". ")
    if not text or text.upper().split(".")[0] in _RESERVED:
        text = f"{fallback} {text}".strip() if text else fallback
    return text


def fingerprint(content) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]


# -- saving --------------------------------------------------------------------------------------


def _date(timestamp: float) -> str:
    when = datetime.datetime.fromtimestamp(timestamp)
    return f"{when.day} {when:%b %Y}"


def _archive(target: Path, manifest: dict) -> str | None:
    """Moves the files of the last save into Old versions\\Version n (its date); returns the
    new folder's name, or None if there was nothing to move."""
    present = [target / name for name in manifest.get("files", []) if (target / name).exists()]
    if not present:
        return None
    version = int(manifest.get("version", 1))
    label = f"Version {version} ({_date(float(manifest.get('saved_at', 0)) or os.path.getmtime(present[0]))})"
    old = target / OLD_VERSIONS / label
    n = 2
    while old.exists():  # two saves of one version on one day can't collide
        old = target / OLD_VERSIONS / f"{label} {n}"
        n += 1
    old.mkdir(parents=True)
    for path in present:
        try:
            shutil.move(str(path), str(old / path.name))
        except OSError as e:
            raise SaveError(_locked_message(path, e)) from e
    return old.name


def _locked_message(path: Path, error: OSError) -> str:
    if isinstance(error, PermissionError):
        return f"{path.name} is open in another program - close it, then save again."
    return f"Couldn't save {path.name}: {error.strerror or error}."


def save_files(kind: str, title: str, subfolder: str | None, files: dict[str, bytes], content_print: str) -> dict:
    """Writes `files` (file name -> bytes) into the save folder's `<subfolder>\\<title>`
    folder, first moving the last save's files into Old versions if `content_print`
    differs from theirs. Returns where they went, for the UI to say."""
    with _lock:
        root = folder()
        parent = root / subfolder if subfolder else root
        target = parent / safe_name(title)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SaveError(f"Couldn't make the folder {target}: {e.strerror or e}. Choose another save folder in Settings.") from e
        manifest = _read_json(target / MANIFEST, {})
        archived = None
        version = int(manifest.get("version", 0)) or 1
        if manifest.get("fingerprint") and manifest["fingerprint"] != content_print:
            archived = _archive(target, manifest)
            version += 1
            manifest = {}
        written = []
        for name, data in files.items():
            path = target / name
            try:
                path.write_bytes(data)
            except OSError as e:
                raise SaveError(_locked_message(path, e)) from e
            written.append(path)
        names = sorted({*manifest.get("files", []), *(p.name for p in written)})
        _unhide(target / MANIFEST)  # replacing a hidden file can be refused on Windows
        _write_json(target / MANIFEST, {"fingerprint": content_print, "version": version,
                                        "saved_at": datetime.datetime.now().timestamp(), "files": names})
        _hide(target / MANIFEST)
        _remember(kind, title, written)
    return {"folder": str(target), "files": [{"name": p.name, "path": str(p)} for p in written],
            "archived": archived, "version": version}


def _attributes(path: Path, value: int) -> None:
    if sys.platform == "win32" and path.exists():
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(path), value)
        except (OSError, AttributeError):
            pass


def _hide(path: Path) -> None:
    _attributes(path, 0x2)  # FILE_ATTRIBUTE_HIDDEN


def _unhide(path: Path) -> None:
    _attributes(path, 0x80)  # FILE_ATTRIBUTE_NORMAL


# -- the Saved files list ------------------------------------------------------------------------


def _recent_path() -> Path:
    return llm_mod.CONFIG_DIR / "downloads.json"


def _remember(kind: str, title: str, paths: list[Path]) -> None:
    recent = _read_json(_recent_path(), [])
    now = datetime.datetime.now().timestamp()
    fresh = [{"kind": kind, "title": title, "name": p.name, "path": str(p), "saved_at": now} for p in paths]
    names = {e["path"] for e in fresh}
    _write_json(_recent_path(), (fresh + [e for e in recent if e.get("path") not in names])[:RECENT_LIMIT])


def recent() -> list[dict]:
    """The latest saves, newest first, each marked whether it's still where it was put."""
    return [{**e, "exists": Path(e["path"]).exists()} for e in _read_json(_recent_path(), []) if e.get("path")]


def _known(path: str) -> Path:
    """Only paths the app saved (or its save folder) are opened: they come from the UI."""
    if path == str(folder()):
        return folder()
    if any(e.get("path") == path for e in _read_json(_recent_path(), [])):
        return Path(path)
    raise SaveError("That file isn't one the app saved.")


def open_saved(path: str) -> None:
    target = _known(path)
    if not target.exists():
        raise SaveError(f"{target.name} isn't there any more - it was moved or deleted.")
    _opener(str(target))


def show_in_folder(path: str) -> None:
    """Opens the folder with the file selected (Explorer, Finder)."""
    target = _known(path)
    if not target.exists():
        if target.parent.exists():
            _opener(str(target.parent))
            return
        raise SaveError(f"{target.name} isn't there any more - it was moved or deleted.")
    if target.is_dir():
        _opener(str(target))
    elif sys.platform == "win32":
        # The command line is written out, not passed as a list: subprocess would quote the
        # whole "/select,<path>" argument, and Explorer reads that only when the path has no
        # spaces - every save path has them ("Classark Exam Builder"), so Show in folder
        # opened nothing (user-reported). Explorer wants /select,"<path>".
        command = f'explorer /select,"{target}"'
        flags = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        try:  # outside the app's job, or Explorer's window would close with the app
            subprocess.Popen(command, creationflags=flags)
        except OSError:
            subprocess.Popen(command)
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", str(target)], check=False)
    else:
        _opener(str(target.parent))
