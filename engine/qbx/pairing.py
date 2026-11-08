"""Checks that a question paper and a mark scheme belong together, before extraction.

Real-paper finding: a mismatched pair doesn't fail - it extracts, and then every part
fails to join and every mark scheme row reads as orphaned, which looks like a parser bug
rather than the wrong file. Both documents say what they are and which paper they belong to
on their first pages, so the pairing can be checked outright:

- *what* each file is, from its cover: a question paper carries candidate INSTRUCTIONS; a
  mark scheme says "MARK SCHEME" and gives a "Maximum Mark"; an insert says INSERT.
- *which* paper, from the code ("0580/22") and sitting ("February/March 2025") both print.

That catches the ways it goes wrong: two question papers, two mark schemes, the two swapped
between the upload slots, or a pair from different papers.
"""

import re
from pathlib import Path

import pymupdf
from pydantic import BaseModel

from qbx import edexcel
from qbx.fingerprint import NEW_CODE_RE, read_sitting

_CODE_RE = re.compile(r"\b(\d{4})\s*/\s*(\d{2})\b")
# the footer form on question paper pages: 0580/22/F/M/25
_FOOTER_SITTING_RE = re.compile(r"\b\d{4}/\d{2}/([FMO])/([MJN])/(\d{2})\b")

# Cover wording, measured on every paper in the corpus. Each kind is identified by signals
# the others never print on their first pages.
_MARK_SCHEME_SIGNALS = (
    re.compile(r"\bMARK SCHEME\b"),
    re.compile(r"\bMaximum Mark\b", re.IGNORECASE),
    re.compile(r"\bGeneric Marking Principles\b", re.IGNORECASE),
    # Every mark scheme page's running header: "0620/12  Cambridge IGCSE – Mark Scheme
    # May/June 2021" over "PUBLISHED" - the code and sitting are read from it below, and
    # the title and PUBLISHED say what it is. A question paper prints neither.
    # whatever the qualification's name: "IGCSE", "IGCSE(9–1)" (0972), "O Level"...
    re.compile(r"\bCambridge\b[^\n]{0,40}?[–-]\s*Mark Scheme\b", re.IGNORECASE),
    re.compile(r"^\s*PUBLISHED\s*$", re.MULTILINE),
    # Edexcel's cover: "Mark Scheme (Results)" over "Summer 2025"
    re.compile(r"\bMark Scheme \(Results\)", re.IGNORECASE),
)
_QUESTION_PAPER_SIGNALS = (
    re.compile(r"\bINSTRUCTIONS\b"),
    re.compile(r"\bAnswer all questions\b", re.IGNORECASE),
    re.compile(r"\bcandidate number\b", re.IGNORECASE),
)
_INSERT_SIGNALS = (re.compile(r"\bINSERT\b"),)

# enough of the document to include its cover and running header, without reading it all
_PAGES_READ = 3


class PairingError(ValueError):
    """The two files aren't a question paper and its own mark scheme."""


class DocumentIdentity(BaseModel):
    kind: str  # "question paper", "mark scheme", "insert" or "unknown"
    code: str | None = None  # "0580/22"
    session: str | None = None  # "F/M"
    year: int | None = None  # 2025
    board: str = "Cambridge"
    pref: str | None = None  # Edexcel's Pearson reference, "P78950A"
    problems: list[str] = []

    def describe(self) -> str:
        sitting = f" {self.session} {self.year}" if self.session and self.year else ""
        return f"{self.code or 'unknown paper'}{sitting}"


def _count(signals: tuple[re.Pattern, ...], text: str) -> int:
    return sum(1 for pattern in signals if pattern.search(text))


def identify(path: Path | str) -> DocumentIdentity:
    with pymupdf.open(path) as doc:
        text = "\n".join(doc[i].get_text("text") for i in range(min(_PAGES_READ, doc.page_count)))

    scores = {
        "mark scheme": _count(_MARK_SCHEME_SIGNALS, text),
        "question paper": _count(_QUESTION_PAPER_SIGNALS, text),
        "insert": _count(_INSERT_SIGNALS, text),
    }
    best = max(scores, key=scores.get)
    kind = best if scores[best] > 0 else "unknown"
    # an insert's cover also has candidate instructions; its own INSERT label decides it
    if scores["insert"] and kind == "question paper" and scores["mark scheme"] == 0:
        kind = "insert"

    if edexcel.is_edexcel(text):
        return _identify_edexcel(path, kind, text)

    code_match = _CODE_RE.search(text)
    code = f"{code_match.group(1)}/{code_match.group(2)}" if code_match else None
    new_code = NEW_CODE_RE.search(text)  # 03_0620_22_2026_1.16 - see fingerprint.py
    if code is None and new_code:
        code = f"{new_code.group('subject')}/{new_code.group('paper')}{new_code.group('variant')}"

    session, year = None, None
    sitting = read_sitting(text)
    if sitting:
        session, year = sitting
        if not year and new_code:  # a specimen that prints its year only in the code
            year = int(new_code.group("year"))
    else:
        footer = _FOOTER_SITTING_RE.search(text)
        if footer:
            session = f"{footer.group(1)}/{footer.group(2)}"
            year = 2000 + int(footer.group(3))
    return DocumentIdentity(kind=kind, code=code, session=session, year=year)


def check_pair(qp_path: Path | str, ms_path: Path | str) -> tuple[DocumentIdentity, DocumentIdentity]:
    """Raises PairingError, with a message saying what's wrong and how to fix it, unless
    `qp_path` is a question paper and `ms_path` is that same paper's mark scheme. Returns
    what each file was identified as."""
    if Path(qp_path).resolve() == Path(ms_path).resolve() or (
        Path(qp_path).read_bytes() == Path(ms_path).read_bytes()
    ):
        raise PairingError("The same file was given as both the question paper and the mark scheme.")

    qp, ms = identify(qp_path), identify(ms_path)
    qp_name, ms_name = Path(qp_path).name, Path(ms_path).name

    if qp.kind == "mark scheme" and ms.kind == "question paper":
        raise PairingError(
            f"The files are the wrong way round: {qp_name} is a mark scheme and {ms_name} is a "
            "question paper. Swap them."
        )
    # Only a file positively identified as the wrong kind is refused. One this can't identify
    # at all ("unknown") is let through: other boards word their covers differently
    # (CLAUDE.md: nothing may hardcode Cambridge in a way that breaks them), and a paper
    # that can't be checked is no worse off than before the check existed.
    if qp.kind not in ("question paper", "unknown"):
        raise PairingError(f"{qp_name} was given as the question paper, but it looks like a {qp.kind}.")
    if ms.kind not in ("mark scheme", "unknown"):
        raise PairingError(f"{ms_name} was given as the mark scheme, but it looks like a {ms.kind}.")

    if qp.board != ms.board and "unknown" not in (qp.kind, ms.kind):
        raise PairingError(
            f"These are from different exam boards: {qp_name} is {qp.board} and {ms_name} is {ms.board}."
        )
    # Edexcel's Pearson reference names one paper; from 2022 both covers print it
    if qp.pref and ms.pref and qp.pref.rstrip("A") != ms.pref.rstrip("A"):
        raise PairingError(
            f"These aren't the same paper: the question paper is {qp.pref} but the mark scheme is {ms.pref}."
        )
    mismatched = [
        what
        for what, a, b in (("paper code", qp.code, ms.code), ("session", qp.session, ms.session), ("year", qp.year, ms.year))
        if a is not None and b is not None and a != b
    ]
    if mismatched:
        raise PairingError(
            f"These aren't the same paper: the question paper is {qp.describe()} but the mark "
            f"scheme is {ms.describe()} (different {', '.join(mismatched)})."
        )
    return qp, ms


def _identify_edexcel(path: Path | str, kind: str, text: str) -> DocumentIdentity:
    """Edexcel's sitting comes from the folder and file names, its paper from the cover
    (qbx/edexcel.py)."""
    found = edexcel.identify(path, text)
    if found.unit is None:  # not on the cover (January 2023): somewhere inside, then
        with pymupdf.open(path) as doc:
            found = edexcel.identify(path, text, "\n".join(page.get_text("text") for page in doc))
    if kind == "unknown":
        kind = edexcel.read_name(path).kind or "unknown"
    return DocumentIdentity(
        kind=kind,
        code=f"{found.unit}/{found.paper}" if found.unit and found.paper else None,
        session=found.session,
        year=found.year,
        board=edexcel.BOARD,
        pref=found.pref,
        problems=found.problems,
    )
