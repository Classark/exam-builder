"""Finding a paper's source PDFs after its data folder has moved.

paper.json records each source PDF by absolute path. A data folder copied to another
computer (or drive, or renamed) keeps those paths, and every one then points nowhere: a
composed exam printed "its question paper PDF ... is missing from the papers folder" for
every question on a friend's machine, though the PDFs were right there in its own papers
folder. `locate` finds the file under the papers folder in use now instead.
"""

import os
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
    return path
