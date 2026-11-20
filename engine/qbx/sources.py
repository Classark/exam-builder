"""Finding a paper's source PDFs after its data folder has moved.

paper.json records each source PDF by absolute path. A data folder copied to another
computer (or drive, or renamed) keeps those paths, and every one then points nowhere: a
composed exam printed "its question paper PDF ... is missing from the papers folder" for
every question on a friend's machine, though the PDFs were right there in its own papers
folder. `locate` finds the file under the papers folder in use now instead.
"""

import os
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

# same default as main.PAPERS_DIR: <repo>/papers
_DEFAULT_PAPERS_DIR = Path(__file__).resolve().parent.parent.parent / "papers"


def papers_dir() -> Path:
    return Path(os.environ.get("QBX_PAPERS_DIR", _DEFAULT_PAPERS_DIR))


def locate(path: str) -> str:
    """`path` if it's there; else the same file under today's papers folder - kept at its
    place below the old "papers" folder (Edexcel's sitting subfolders), or by name at the
    top - else `path` unchanged, so callers report it missing as before."""
    if not path or os.path.exists(path):
        return path
    # recorded on Windows or macOS, read on either
    parts = (PureWindowsPath(path) if "\\" in path else PurePosixPath(path)).parts
    root = papers_dir()
    tails = []
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() == "papers":
            tails.append(parts[i + 1 :])
            break
    tails.append(parts[-1:])
    for tail in tails:
        candidate = root.joinpath(*tail) if tail else None
        if candidate is not None and candidate.is_file():
            return str(candidate)
    # moved into a subfolder of the papers folder (user-reported: every PDF moved into
    # "papers\Chemistry IGCSE", and every composed question read "missing")
    found = _by_name(root).get(parts[-1].lower()) if parts else None
    return str(found) if found is not None and found.is_file() else path


# every PDF under the papers folder by file name, re-read at most every _RESCAN seconds
_RESCAN = 10.0
_names: dict[str, Path] = {}
_names_root: Path | None = None
_names_read = 0.0
_names_lock = threading.Lock()


def _by_name(root: Path) -> dict[str, Path]:
    global _names, _names_root, _names_read
    with _names_lock:
        if _names_root != root or time.monotonic() - _names_read > _RESCAN:
            names: dict[str, Path] = {}
            if root.is_dir():
                for pdf in sorted(root.rglob("*.pdf")):
                    names.setdefault(pdf.name.lower(), pdf)
            _names, _names_root, _names_read = names, root, time.monotonic()
        return _names
