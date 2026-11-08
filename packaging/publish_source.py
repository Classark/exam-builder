"""Prepare the public source repository (https://github.com/Classark/exam-builder) for a release.

    uv run --project engine python packaging/publish_source.py [<folder>]

The public repo holds one commit per release: exactly the files of the in-app source
download (qbx/legal.source_files - what git tracks here, minus notes and IDE settings),
never this repository's history. This copies them into <folder> (default: a sibling folder
`exam-builder-public`), removing files that are gone, and commits them as "Classark Exam
Builder <version>" with a v<version> tag - authored by your own git identity, with no
co-author lines (user-decided). It never pushes: check the commit, then

    git -C <folder> push origin main --tags
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTE = "https://github.com/Classark/exam-builder.git"
sys.path.insert(0, str(ROOT / "engine"))

from qbx import legal  # noqa: E402


def git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True, text=True).stdout


def main() -> None:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT.parent / "exam-builder-public"
    if not (target / ".git").exists():
        target.mkdir(parents=True, exist_ok=True)
        git(target, "init", "-b", "main")
        git(target, "remote", "add", "origin", REMOTE)

    # untracked files aren't in the source, so only changes to tracked ones matter
    changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, capture_output=True, text=True)
    if changed.stdout.strip():
        print("warning: this repository has uncommitted changes - they're included as they stand")

    files = legal.source_files(ROOT)
    keep = set(files) | {"NOTICE.txt"}
    for path in sorted(target.rglob("*"), reverse=True):
        rel = path.relative_to(target).as_posix()
        if rel == ".git" or rel.startswith(".git/"):
            continue
        if path.is_file() and rel not in keep:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for name in files:
        dest = target / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, dest)
    (target / "NOTICE.txt").write_text(legal.NOTICE, encoding="utf-8")

    version = legal.version()
    git(target, "add", "-A")
    if not git(target, "status", "--porcelain").strip():
        print(f"nothing changed since the last snapshot ({version})")
        return
    git(target, "commit", "-m", f"Classark Exam Builder {version}")
    git(target, "tag", "-f", f"v{version}")
    print(f"committed {len(files) + 1} files as v{version} in {target}")
    print(f"check it, then: git -C \"{target}\" push origin main --tags")


if __name__ == "__main__":
    main()
