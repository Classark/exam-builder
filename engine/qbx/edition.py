"""Which build this is: "debug" or "release" (user-asked).

- **debug** - what development and `package.py exe` give: Claude Code on this computer and
  the Settings screen, for the user and friends testing.
- **release** - `package.py <target> --release`, for teachers: no Claude Code and no Settings.
  Teachers will reach Claude through their Classark account (not built yet), so until then
  a release build has no Claude at all, and the UI hides what needs it (`/api/state`'s `ai`).

`package.py` writes the edition into `edition.txt` beside this file in the build. Without the
file - running from source - it's debug. `QBX_EDITION` overrides either, for tests and checks.
"""

import os
from pathlib import Path

EDITION_FILE = Path(__file__).with_name("edition.txt")
EDITIONS = ("debug", "release")


def edition() -> str:
    value = os.environ.get("QBX_EDITION", "").strip().lower()
    if not value:
        try:
            value = EDITION_FILE.read_text(encoding="utf-8").strip().lower()
        except OSError:
            value = ""
    return value if value in EDITIONS else "debug"


def is_release() -> bool:
    return edition() == "release"


def claude_available() -> bool:
    """Whether this build can reach Claude at all. Release builds will once Classark
    accounts exist; until then only debug builds can, through Claude Code."""
    return not is_release()
