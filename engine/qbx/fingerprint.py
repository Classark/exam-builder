"""Derive paper metadata from the Cambridge footer code (CLAUDE.md section 5.4).

Footer format: SUBJECT/PAPER_VARIANT/SESSION/YEAR, e.g. 9702/42/M/J/23
  9702 -> subject (9xxx = A Level, 0xxx = IGCSE)
  4    -> paper number
  2    -> variant
  M/J  -> session (also O/N, F/M, and SP for a specimen paper)
  23   -> year

Newer papers use a second layout, 03_0620_22_2026_1.16, with no session in it - see
NEW_CODE_RE below; the session then comes from the cover.
"""

import re

from pydantic import BaseModel

_FOOTER_RE = re.compile(
    r"(?P<subject>\d{4})/(?P<paper>\d)(?P<variant>\d)/(?P<session>M/J|O/N|F/M|SP)/(?P<year>\d{2})"
)

# Specimen papers print "SP" where a live paper prints its sitting (0620/02/SP/23). None
# is in the corpus yet, so that form is taken from the syllabus documents' own footers.
_SESSION_NAMES = {"M/J": "May/June", "O/N": "Oct/Nov", "F/M": "Feb/March", "SP": "Specimen"}
# Edexcel's sittings (qbx/edexcel.py); their codes carry no slash
_SESSION_NAMES |= {"JAN": "January", "JUN": "May/June", "NOV": "Oct/Nov"}


def session_name(code: str) -> str:
    """A session code as people say it: "M/J" -> "May/June"."""
    return _SESSION_NAMES.get(code, code)


class Fingerprint(BaseModel):
    raw: str
    board: str
    level: str
    subject: str
    paper: str
    variant: str
    session: str
    year: int

    @property
    def paper_key(self) -> str:
        session_slug = self.session.replace("/", "")
        return f"{self.subject}_{self.paper}{self.variant}_{session_slug}_{self.year:02d}"


class FingerprintError(ValueError):
    pass


# Newer papers print their code in another layout, e.g. 03_0620_22_2026_1.16:
#   03    -> a document number     0620 -> subject     22 -> paper 2, variant 2
#   2026  -> year (four digits)    1.16 -> a version number
# It carries no session at all - that comes from the cover, which prints the sitting
# ("May/June 2026") or says it's a specimen. No such paper is in the corpus yet: the layout
# is taken from one footer the user showed, so treat this as a first reading of it.
NEW_CODE_RE = re.compile(
    r"\b\d{2}_(?P<subject>\d{4})_(?P<paper>\d)(?P<variant>\d)_(?P<year>\d{4})_\d+(?:\.\d+)*\b"
)
# The single-month forms too: the March 2021 schemes print "March 2021" where every other
# paper prints "February/March 2021" (found calibrating ~200 papers - all six went unpaired).
_SITTING_RE = re.compile(
    r"\b(?P<sitting>February/March|May/June|October/November|March|June|November)\s+(?P<year>\d{4})\b",
    re.IGNORECASE,
)
_SITTING_CODES = {
    "february/march": "F/M", "may/june": "M/J", "october/november": "O/N",
    "march": "F/M", "june": "M/J", "november": "O/N",
}
_SPECIMEN_RE = re.compile(r"\bspecimen\b", re.IGNORECASE)


def read_sitting(text: str) -> tuple[str, int] | None:
    """(session code, four-digit year) as a cover states it - "May/June 2026" gives
    ("M/J", 2026); a specimen gives ("SP", its year if one is printed, else 0)."""
    match = _SITTING_RE.search(text)
    if match:
        return _SITTING_CODES[match.group("sitting").lower()], int(match.group("year"))
    if _SPECIMEN_RE.search(text):
        year = re.search(r"\b(20\d{2})\b", text)
        return "SP", int(year.group(1)) if year else 0
    return None


def parse_footer(footer_text: str, cover_text: str = "") -> Fingerprint:
    """Reads the paper code from a page footer. `cover_text` is needed only for the newer
    code layout, which leaves the session to the cover."""
    match = _FOOTER_RE.search(footer_text)
    if match:
        raw = match.group(0)
        subject, paper, variant = match.group("subject"), match.group("paper"), match.group("variant")
        session = match.group("session")
        year = int(match.group("year"))
    else:
        new = NEW_CODE_RE.search(footer_text) or NEW_CODE_RE.search(cover_text)
        if not new:
            raise FingerprintError(f"no paper code found in footer: {footer_text!r}")
        raw = new.group(0)
        subject, paper, variant = new.group("subject"), new.group("paper"), new.group("variant")
        year = int(new.group("year")) % 100
        sitting = read_sitting(cover_text) or read_sitting(footer_text)
        if sitting is None:
            raise FingerprintError(
                f"paper code {raw} doesn't include the session, and the cover doesn't say which "
                "sitting it is (e.g. May/June 2026, or Specimen)"
            )
        session = sitting[0]

    if session not in _SESSION_NAMES:
        raise FingerprintError(f"unknown session code: {session!r}")

    # F/M (Feb/March, the India sitting) only exists as variant 2.
    if session == "F/M" and variant != "2":
        raise FingerprintError(
            f"F/M session only exists as variant 2, got variant {variant} in {footer_text!r}"
        )

    return Fingerprint(
        raw=raw,
        board="Cambridge",
        level="A Level" if subject.startswith("9") else "IGCSE",
        subject=subject,
        paper=paper,
        variant=variant,
        session=session,
        year=year,
    )


# The cover prints the subject's name on the line above the paper code and the paper's
# title on the line below - "CHEMISTRY" / "0620/41" / "Paper 4 Theory (Extended)" on every
# paper in the corpus - so both come from the document rather than a lookup table.
_CONTROL_RE = re.compile(r"[\x00-\x1f]")


def read_cover_titles(cover_text: str, subject: str, paper: str, variant: str) -> tuple[str, str]:
    """(subject name, paper title) from the cover, e.g. ("Chemistry", "Paper 4 Theory
    (Extended)"); an empty string for whichever can't be found."""
    lines = [_CONTROL_RE.sub("", line).strip() for line in cover_text.splitlines()]
    lines = [line for line in lines if line]
    code = f"{subject}/{paper}{variant}"
    for index, line in enumerate(lines):
        if line != code:
            continue
        above = lines[index - 1] if index > 0 else ""
        below = lines[index + 1] if index + 1 < len(lines) else ""
        name = above.title() if above.isupper() and re.fullmatch(r"[A-Z][A-Z &()\-–]*", above) else ""
        title = below if below.lower().startswith("paper") else ""
        return name, " ".join(title.split())
    return "", ""
