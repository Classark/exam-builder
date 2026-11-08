"""The app's legal notices and its source code offer (AGPL-3.0, user-decided).

Classark Exam Builder is built on PyMuPDF, which is licensed under the GNU AGPL-3.0, so the
app is conveyed under the AGPL too: every copy carries the licence, the notices of what it
bundles, and the complete source of that exact version. The source goes *inside* the
build (`source.zip`, written by package.py), not on a website - AGPL section 6(a) lets the
object code be "accompanied by the Corresponding Source", which can't break, go stale, or
point at a different version than the one installed.

What the source holds (`source_files`): every file git tracks - the engine, the UI's
source, the build scripts and installers - except notes and IDE settings, which aren't
needed to build or run it. Not in it, and not covered by the AGPL: the Classark server
(a separate program, reached over the network, that doesn't use PyMuPDF), and people's
papers, exams and settings. The licence covers the code, not the Classark name or logo.

In a build, everything here reads from the `legal/` folder beside this file. Running from
source, it's assembled from the repository on demand.
"""

import importlib.metadata
import io
import subprocess
import tomllib
import zipfile
from pathlib import Path

LEGAL_DIR = Path(__file__).with_name("legal")
REPO = Path(__file__).resolve().parents[2]
PRODUCT = "Classark Exam Builder"
COPYRIGHT = "Copyright (C) 2026 Classark"
LICENSE_NAME = "GNU Affero General Public License v3.0"

# tracked files left out of the source download: notes and IDE settings, not needed to build
_EXCLUDE_PREFIXES = (".idea/", ".claude/", "CLAUDE.md", "root.iml")
# what the UI bundles from npm, with what it pulls in
UI_PACKAGES = ["react", "react-dom", "scheduler", "lucide-react"]

# the notice's body, shown under the product name and copyright in About & licences
NOTICE_BODY = (
    "This program is free software: you can redistribute it and/or modify it under the terms "
    "of the GNU Affero General Public License as published by the Free Software Foundation, "
    "either version 3 of the License, or (at your option) any later version.\n\n"
    "This program is distributed in the hope that it will be useful, but WITHOUT ANY "
    "WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A "
    "PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.\n\n"
    "The complete source code of this version is included with it: use \"Download source "
    "code\" in About & licences.\n\n"
    "\"Classark\" and the Classark logo are trademarks of Classark. The licence covers the "
    "program's code, not the right to use them.\n"
)
NOTICE = f"{PRODUCT}\n{COPYRIGHT}\n\n{NOTICE_BODY}"


def _bundled(name: str) -> Path | None:
    path = LEGAL_DIR / name
    return path if path.exists() else None


def version() -> str:
    path = _bundled("version.txt")
    if path:
        return path.read_text(encoding="utf-8").strip()
    try:
        return tomllib.loads((REPO / "engine" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, KeyError, ValueError):
        return "unknown"


def license_text() -> str:
    path = _bundled("LICENSE") or REPO / "LICENSE"
    return path.read_text(encoding="utf-8")


# -- the source --------------------------------------------------------------------------


def source_files(repo: Path = REPO) -> list[str]:
    """The repository's tracked files that make up the source, as posix paths."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True
    ).stdout.decode("utf-8")
    return sorted(
        f for f in listed.split("\0") if f and not f.startswith(_EXCLUDE_PREFIXES) and (repo / f).is_file()
    )


def make_source_zip(repo: Path = REPO) -> bytes:
    """The Corresponding Source as one zip, under a top folder named for the version."""
    top = f"ClassarkExamBuilder-{version()}-source"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in source_files(repo):
            zf.write(repo / name, f"{top}/{name}")
        zf.writestr(f"{top}/NOTICE.txt", NOTICE)
    return buffer.getvalue()


def source_zip() -> tuple[bytes, str]:
    """The source to hand out, with its file name: the build's own copy, or one made from
    the repository when running from source."""
    name = f"ClassarkExamBuilder-{version()}-source.zip"
    path = _bundled("source.zip")
    return (path.read_bytes() if path else make_source_zip()), name


# -- third-party notices -----------------------------------------------------------------

_LICENSE_FILE_WORDS = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")


def runtime_packages(repo: Path = REPO) -> list[str]:
    """The Python distributions the app ships with: the engine's dependencies, without the
    dev group (tests, PyInstaller), read from uv's lock."""
    out = subprocess.run(
        ["uv", "export", "--project", str(repo / "engine"), "--no-dev", "--no-hashes",
         "--no-emit-project", "--no-header", "--no-annotate", "--format", "requirements.txt"],
        capture_output=True, text=True, check=True,
    ).stdout
    names = []
    for line in out.splitlines():
        line = line.split(";")[0].strip()
        if line and not line.startswith(("#", "-")):
            names.append(line.split("==")[0].strip())
    return sorted(set(names), key=str.lower)


def _python_section(name: str) -> str | None:
    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None  # a platform-only dependency not installed here
    meta = dist.metadata
    licence = meta.get("License-Expression") or (meta.get("License") or "").split("\n")[0]
    texts = []
    for file in dist.files or []:
        # licence files live in the .dist-info folder (often under licenses/)
        if file.name.upper().startswith(_LICENSE_FILE_WORDS) and ".dist-info" in file.as_posix():
            try:
                texts.append(Path(dist.locate_file(file)).read_text(encoding="utf-8", errors="replace").strip())
            except OSError:
                pass
    body = "\n\n".join(dict.fromkeys(texts)) or f"(Licence: {licence or 'see the project'})"
    return f"{meta['Name']} {dist.version}" + (f" - {licence}" if licence else "") + "\n\n" + body


def _npm_section(node_modules: Path, name: str) -> str | None:
    folder = node_modules / name
    try:
        info = __import__("json").loads((folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    texts = [p.read_text(encoding="utf-8", errors="replace").strip() for p in sorted(folder.iterdir())
             if p.is_file() and p.name.upper().startswith(_LICENSE_FILE_WORDS)]
    return f"{name} {info.get('version', '')} - {info.get('license', '')}\n\n" + ("\n\n".join(texts) or "")


def make_third_party_text(repo: Path = REPO) -> str:
    sections = [
        "Third-party software in Classark Exam Builder, with each one's licence.\n"
        "PyMuPDF is licensed under the GNU AGPL-3.0, which is why this program is too.",
        "Rubik font - SIL Open Font License 1.1\n\n"
        + (repo / "ui" / "src" / "assets" / "fonts" / "OFL.txt").read_text(encoding="utf-8").strip(),
    ]
    node_modules = repo / "ui" / "node_modules"
    sections += [s for s in (_npm_section(node_modules, n) for n in UI_PACKAGES) if s]
    sections += [s for s in (_python_section(n) for n in runtime_packages(repo)) if s]
    sections.append("Python - Python Software Foundation License\nhttps://docs.python.org/3/license.html")
    return ("\n\n" + "=" * 78 + "\n\n").join(sections) + "\n"


def third_party_text() -> str:
    path = _bundled("THIRD_PARTY_LICENSES.txt")
    if path:
        return path.read_text(encoding="utf-8")
    try:
        return make_third_party_text()
    except (OSError, subprocess.CalledProcessError) as e:
        return f"The third-party licences couldn't be gathered here ({e}). They're in the source download."


def write_legal_folder(out: Path, repo: Path = REPO) -> Path:
    """For package.py: the version, licence, notices, third-party licences and source zip,
    in one folder bundled as qbx/legal."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "version.txt").write_text(version(), encoding="utf-8")
    (out / "LICENSE").write_text(license_text(), encoding="utf-8")
    (out / "NOTICE.txt").write_text(NOTICE, encoding="utf-8")
    (out / "THIRD_PARTY_LICENSES.txt").write_text(make_third_party_text(repo), encoding="utf-8")
    (out / "source.zip").write_bytes(make_source_zip(repo))
    return out
