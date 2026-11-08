"""Pearson Edexcel International GCSE papers: which board a document is, and which paper.

Edexcel prints no Cambridge-style footer code. The cover says the unit and paper ("Unit:
4CH1", "Paper: 1C"), the exam date and a Pearson reference ("P78950A", unique per paper and,
from 2022, printed on the mark scheme too), but **not the session**, and what it does print
can mislead: the autumn 2020 papers' covers carry the cancelled *May* 2020 date. So the
session and year come from the folder and file names, as the user files them (their
decision, measured on 188 files):

    January 2012\\Question-paper\\Questionpaper-Paper1C-January2012.pdf
    June 2013\\Mark-scheme\\4CH0_1C_msc_20130823.pdf
    November 2020\\Mark-scheme\\MarkScheme-Paper2CR-November2020.pdf

read in this order: the session folder's name, then the file name's month and year words,
then (question papers only) an 8-digit date in the file name. A mark scheme's 8-digit date
(`_rms_20120823`, `_msc_20130307`) is when it was *published*, months after the exam, and
would file the paper under the wrong sitting - so it is never read. The cover is the last
resort.

The paper code goes the other way: the cover wins over the file name, because names are
wrong sometimes - `June 2013\\…\\Markscheme-Paper1C-June2013.pdf` is the 1CR scheme by its
cover, and `Markscheme-PaperCR-January2020.pdf` drops the 2. Covers can say nothing at all
(the January 2023 question papers' cover text holds only the barcode), and then the name
gives it.

Sessions: JAN, JUN and NOV. A paper key reads 4CH1_1C_JUN_25 - unit, paper, variant ("C"
or "CR"), session, year - the same shape as a Cambridge key.
"""

import re
from pathlib import Path

from pydantic import BaseModel

BOARD = "Edexcel"
LEVEL = "International GCSE"  # when the cover doesn't name its qualification
# "Pearson Edexcel International Advanced Level", "Pearson Edexcel International GCSE in ..."
_LEVEL_RE = re.compile(
    r"\bEdexcel\s+(International\s+Advanced\s+Level|International\s+GCSE|"
    r"Level\s+1/Level\s+2\s+Certificate|Level\s+3\s+GCE|GCSE|GCE)\b"
)

# a Pearson cover or footer. "4CHO" (letter O) is the January 2015 scheme's own typo.
_EDEXCEL_RE = re.compile(r"\bPearson\b|\bEdexcel\b|\b4CH[01O]\b")
# A Pearson unit code by its shape, never a list of subjects, so a new subject pairs with
# nothing added here: International GCSE and UK codes are a digit, two letters and the
# spec's 0/1 (4CH1, 4MA0, 1MA1, 9MA0; K for the Level 1/2 certificates), International
# A Level units W, two or three letters and two digits (WMA11, WST01, WFM01, WDM11).
# The closing 0/1 keeps an equation's "6CO2" from reading as one.
_UNIT = r"(?:[0-9K][A-Z]{2}[01O]|W[A-Z]{2,3}\d{2})"
# ends at anything but a letter or digit: a publications code runs on with "_" (WME03_01_2306_MS)
_UNIT_RE = re.compile(rf"\b({_UNIT})(?![A-Za-z0-9])")
# Co-listed units are other qualifications sitting the same paper: KCH0 (the Level 1/2
# certificate) and 4SC0 (double award science) beside 4CH0; 4SD0 beside 4CH1.
_UNIT_ALIASES = {"4CHO": "4CH0", "KCH0": "4CH0", "4SC0": "4CH0", "4SD0": "4CH1"}
# "Paper: 1C", "PAPER: 2CR", "4CH1/1C", "(4CH1) Paper 2CR", "(4CH0 2C)", "Paper\n1C", and
# other subjects' tiers: "PAPER 1H", "4MA1/2F" (Mathematics A, Higher and Foundation), and
# International A Level's numbered papers: "WMA11/01", "WMA11/01R", "WMA11/01A" (June 2025),
# "(WST01) Paper 01", and the publications code a scheme may print instead, "WMA12_01_1906_MS"
_PAPER_CODE = r"(?:[1-4](?:C|H|F|P|B)R?|0[1-9][A-Z]?)"
_COVER_PAPER_RES = (
    re.compile(rf"\bpaper:?\s*({_PAPER_CODE})\b", re.IGNORECASE),
    re.compile(rf"\b{_UNIT}\)?\s*[/_]?\s*({_PAPER_CODE})(?![A-Za-z0-9])"),
)
# the subject as a mark scheme's cover names it: "In Mathematics A (4MA1) Paper 1H",
# "In Statistics S1 (WST01) Paper 01"
_SUBJECT_NAME_RE = re.compile(rf"\b(?:in|In|IN)\s+([A-Z][A-Za-z0-9 ]+?)\s*\(\s*{_UNIT}")
_PREF_RE = re.compile(r"\bP\d{5}[A-Z]{0,2}\b")
_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Summer|Autumn|Winter)\s+(20\d\d)\b",
    re.IGNORECASE,
)

SESSION_NAMES = {"JAN": "January", "JUN": "May/June", "NOV": "October/November"}
# month words as they appear in folder and file names ("January2012", "2020 oct", "May-Oct")
_MONTH_SESSIONS = {
    "jan": "JAN", "january": "JAN", "feb": "JAN", "february": "JAN", "winter": "JAN",
    "may": "JUN", "jun": "JUN", "june": "JUN", "summer": "JUN",
    "oct": "NOV", "october": "NOV", "nov": "NOV", "november": "NOV", "autumn": "NOV",
}
_MONTH_NUMBER_SESSIONS = {1: "JAN", 2: "JAN", 5: "JUN", 6: "JUN", 10: "NOV", 11: "NOV"}

_WORD_RE = re.compile(r"[A-Za-z]+|\d+")
# "Paper1CR", "Paper-2C", "PaperCR" (the digit dropped), "_1C_", "-2CR-", "-1C.pdf"
_NAME_PAPER_RES = (
    re.compile(r"paper[-_ ]?([1-4]?)((?:C|H|F|P|B)R?)(?![a-z])", re.IGNORECASE),
    re.compile(r"(?:^|[-_ .])([1-4])((?:C|H|F|P|B)R?)(?=$|[-_ .])", re.IGNORECASE),
)
_QP_NAME_RE = re.compile(r"question[-_ ]?papers?|_que_|_qp_", re.IGNORECASE)
_MS_NAME_RE = re.compile(r"mark[-_ ]?schemes?|_msc_|_rms_|_ms_", re.IGNORECASE)
_REPORT_NAME_RE = re.compile(r"examiner|_pef_|_er_", re.IGNORECASE)
_DATE8_RE = re.compile(r"(?<!\d)(20\d\d)(\d\d)(\d\d)(?!\d)")


def is_edexcel(text: str) -> bool:
    return bool(_EDEXCEL_RE.search(text))


def detect_board(text: str) -> str:
    return BOARD if is_edexcel(text) else "Cambridge"


class NameReading(BaseModel):
    """What a file's folder and name say about it."""
    kind: str | None = None  # "question paper", "mark scheme", "report"
    session: str | None = None
    year: int | None = None  # four digits
    paper: str | None = None  # "1C", "2CR", or "CR" with the digit missing
    source: str | None = None  # where the sitting came from: "folder" or "filename"


def _kind_of(name: str) -> str | None:
    if _REPORT_NAME_RE.search(name):
        return "report"
    if _MS_NAME_RE.search(name):
        return "mark scheme"
    if _QP_NAME_RE.search(name):
        return "question paper"
    return None


def _sitting_in_words(text: str) -> tuple[str, int] | None:
    words = _WORD_RE.findall(text)
    years = [int(w) for w in words if re.fullmatch(r"20\d\d", w)]
    sessions = {_MONTH_SESSIONS[w.lower()] for w in words if w.lower() in _MONTH_SESSIONS}
    if not years or not sessions:
        return None
    # "May-Oct 2020": the rescheduled 2020 series, sat in the autumn
    session = "NOV" if "NOV" in sessions else sessions.pop() if len(sessions) == 1 else None
    return (session, years[0]) if session else None


def read_name(path: Path | str) -> NameReading:
    path = Path(path)
    reading = NameReading()
    folders = [p.name for p in path.parents][:2]  # the kind folder, then the session folder
    stem = path.stem

    reading.kind = _kind_of(stem)
    for folder in folders[:1]:
        reading.kind = _kind_of(folder) or reading.kind

    # the sitting: the nearest folder naming one, then the file name
    for folder in folders:
        sitting = _sitting_in_words(folder)
        if sitting:
            reading.session, reading.year, reading.source = *sitting, "folder"
            break
    else:
        sitting = _sitting_in_words(stem)
        if sitting:
            reading.session, reading.year, reading.source = *sitting, "filename"
        elif reading.kind == "question paper" and (date := _DATE8_RE.search(stem)):
            session = _MONTH_NUMBER_SESSIONS.get(int(date.group(2)))
            if session:
                reading.session, reading.year, reading.source = session, int(date.group(1)), "filename"

    for pattern in _NAME_PAPER_RES:
        match = pattern.search(stem)
        if match:
            reading.paper = (match.group(1) + match.group(2)).upper()
            break
    return reading


class CoverReading(BaseModel):
    unit: str | None = None  # "4CH1"
    paper: str | None = None  # "1C"
    pref: str | None = None  # "P78950A"
    session: str | None = None
    year: int | None = None


def _session_of_month(word: str) -> str | None:
    word = word.lower()
    if word in ("january", "february", "winter"):
        return "JAN"
    if word in ("april", "may", "june", "july", "summer"):
        return "JUN"
    if word in ("september", "october", "november", "december", "autumn"):
        return "NOV"
    return None


def read_cover(cover_text: str, document_text: str = "") -> CoverReading:
    """The unit, paper, reference and stated sitting from a cover. `document_text` (the
    whole document) is searched for the unit when the cover doesn't print one."""
    reading = CoverReading()
    for text in (cover_text, document_text):
        found = _UNIT_RE.findall(text)
        units = [_UNIT_ALIASES.get(u, u) for u in found]
        # the paper's own unit where it's printed; a co-listed one (KCH0, 4SD0) says which
        own = [u for u in found if u not in _UNIT_ALIASES or u == "4CHO"]
        if own or units:
            reading.unit = _UNIT_ALIASES.get(own[0], own[0]) if own else units[0]
            break
    for pattern in _COVER_PAPER_RES:
        match = pattern.search(cover_text)
        if match:
            reading.paper = match.group(1).upper()
            break
    pref = _PREF_RE.search(cover_text)
    reading.pref = pref.group(0) if pref else None
    date = _DATE_RE.search(cover_text)
    if date:
        reading.session, reading.year = _session_of_month(date.group(1)), int(date.group(2))
    return reading


class EdexcelIdentity(BaseModel):
    unit: str | None
    paper: str | None  # "1C"
    session: str | None
    year: int | None
    pref: str | None
    source: str | None  # "folder", "filename" or "cover"
    problems: list[str] = []


def identify(path: Path | str, cover_text: str, document_text: str = "") -> EdexcelIdentity:
    """Names first for the sitting, cover first for the paper code (see the module notes)."""
    name, cover = read_name(path), read_cover(cover_text, document_text)
    problems = []

    session, year, source = name.session, name.year, name.source
    if session is None and cover.session:
        session, year, source = cover.session, cover.year, "cover"

    paper = cover.paper
    if name.paper and len(name.paper) >= 2 and name.paper[0].isdigit():
        if paper is None:
            paper = name.paper
        elif paper != name.paper:
            problems.append(
                f"the file name says paper {name.paper} but the cover says {paper}; the cover was used"
            )
    elif paper is None and name.paper:
        problems.append(f"the file name says paper {name.paper}, with no paper number")
    return EdexcelIdentity(
        unit=cover.unit, paper=paper, session=session, year=year, pref=cover.pref,
        source=source, problems=problems,
    )


def fingerprint(qp_path: Path | str, cover_text: str, document_text: str, ms_identity=None):
    """The paper's Fingerprint, from the question paper's names and cover. What the question
    paper leaves out comes from its mark scheme's identity (`pairing.DocumentIdentity`, code
    "4CH1/1C")."""
    from qbx.fingerprint import Fingerprint, FingerprintError

    qp = identify(qp_path, cover_text, document_text)
    ms_unit, ms_paper = (ms_identity.code.split("/") if ms_identity and ms_identity.code else (None, None))
    unit = qp.unit or ms_unit
    paper = qp.paper or ms_paper
    session, year = qp.session, qp.year
    if session is None and ms_identity and ms_identity.session:
        session, year = ms_identity.session, ms_identity.year
    missing = [what for what, value in (("unit", unit), ("paper", paper), ("session", session)) if not value]
    if missing:
        raise FingerprintError(
            f"couldn't tell this Edexcel paper's {', '.join(missing)}: put it in a folder named for "
            "its sitting (e.g. \"June 2025\") and name the file with its paper (e.g. "
            "\"Questionpaper-Paper1C-June2025.pdf\")"
        )
    level = _LEVEL_RE.search(" ".join(cover_text.split()))
    number, variant = re.fullmatch(r"(\d*)(.*)", paper).groups()  # "1CR" -> 1, CR; "01R" -> 01, R
    return Fingerprint(
        raw=qp.pref or f"{unit}/{paper}",
        board=BOARD,
        level=" ".join(level.group(1).split()) if level else LEVEL,
        subject=unit,
        paper=number,
        variant=variant,
        session=session,
        year=year % 100,
    )


# ---------------------------------------------------------------------------------------
# Question papers: marks
#
# Edexcel prints a part's marks as "(2)" on a line of its own at the right margin (x 496
# on every 4CH0/4CH1 paper measured), and closes each question with "(Total for Question 2
# = 11 marks)". The tree reads Cambridge's "[2]" and "[Total: 11]", so these are rewritten
# into that form before it sees them. The margin is found per document - the x most "(n)"
# lines start at - never assumed; a "(2)" elsewhere (a list in a question) is left alone.
# ---------------------------------------------------------------------------------------

_PAREN_MARKS_RE = re.compile(r"^\((\d{1,2})\)$")
_DOTTED_MARKS_RE = re.compile(r"^[.…]{2,}\s*\((\d{1,2})\)$")
# "= 8 marks" in the sciences, "is 6 marks" in Mathematics A (4MA1)
_QUESTION_TOTAL_RE = re.compile(
    # "for" and "Question" can come as two spans with the space between lost: "(Total
    # forQuestion 1 is 6 marks)" (WFM01 June 2024, its total never read)
    r"\(\s*Total\s+for\s*Question\s+\d+\s*(?:=|is)\s*(\d+)\s*marks?\s*\)", re.IGNORECASE
)
# how far a marks line can stray from the margin: measured within 1pt on every paper
MARKS_MARGIN_TOLERANCE = 8.0
MAX_PAGE_SHIFT = 20.0


def _marks_margin(text_spans: list) -> float | None:
    from collections import Counter

    candidates = [s for s in text_spans if _PAREN_MARKS_RE.match(s.text.strip())]
    return Counter(round(s.x0) for s in candidates).most_common(1)[0][0] if candidates else None


def page_shifts(text_spans: list) -> dict[int, float]:
    """How far each page's content sits from where the rest of the paper's does, measured
    by its marks column. Real-paper finding (4CH0 2C June 2015): page 15 is set 11.5pt left
    of every other page - its marks at x 484, not 496 - so its part letters and numerals
    fell outside their tiers' clusters and its marks outside the margin. Only pages whose
    marks all agree on one offset are shifted."""
    from statistics import median

    margin = _marks_margin(text_spans)
    if margin is None:
        return {}
    by_page: dict[int, list[float]] = {}
    for span in text_spans:
        if _PAREN_MARKS_RE.match(span.text.strip()):
            by_page.setdefault(span.page, []).append(span.x0 - margin)
    # Where the page's lines start, against where the paper's do. The marks column alone
    # can move without the page: the "DO NOT WRITE IN THIS AREA" strip changes sides between
    # left and right pages, so the marks sit ~5pt further right on one side while question
    # numbers stay at x 42.5 on every page (4MA1 1FR June 2019 Q14 was "shifted" out of the
    # question column and lost). A page is moved only when its left edge moved too.
    starts: dict[tuple[int, float], float] = {}
    for span in text_spans:
        if span.text.strip():
            key = (span.page, round(span.y0))
            starts[key] = min(starts.get(key, span.x0), span.x0)
    edges = {page: [] for page in by_page}
    for (page, _), x in starts.items():
        if page in edges:
            edges[page].append(round(x))
    every = [round(x) for x in starts.values()]
    paper_edge = max(set(every), key=every.count) if every else None

    shifts = {}
    for page, offsets in by_page.items():
        shift = median(offsets)
        # A page offset is a few points - 11.5 measured. Anything bigger is a "(1)" somewhere
        # else on a page (a table cell), not the marks column moved: 4CH0 1C June 2017 had
        # its pages "shifted" 230pt by one, dragging a scale's "40" into the question column.
        if not (2 < abs(shift) <= MAX_PAGE_SHIFT and max(offsets) - min(offsets) <= 2):
            continue
        own = edges.get(page) or []
        if paper_edge is not None and own:
            page_edge = max(set(own), key=own.count)
            if abs((page_edge - paper_edge) - shift) > 2:
                continue  # its marks moved, its content didn't
        shifts[page] = shift

    # A page with no marks of its own to measure (its question's marks overleaf) by its
    # question number: leftmost on the page, a few points off where every other page puts
    # its question numbers. 4MA1 1F November 2020 page 24 sets "21" at x 51.9 against 42.5,
    # and Q21 fell out of the question column.
    leftmost: dict[int, object] = {}
    for span in text_spans:
        if span.text.strip() and (span.page not in leftmost or span.x0 < leftmost[span.page].x0):
            leftmost[span.page] = span
    numbers = {page: span.x0 for page, span in leftmost.items() if _QUESTION_NUMBER_RE.match(span.text.strip())}
    if len(numbers) >= 3:
        column = median(numbers.values())
        for page, x in numbers.items():
            if page not in by_page and 2 < abs(x - column) <= MAX_PAGE_SHIFT:
                shifts[page] = x - column
    return shifts


_QUESTION_NUMBER_RE = re.compile(r"^\d{1,2}\.{0,2}$")  # "3.." too, as anchors.QUESTION_NUMBER_RE


def normalise_marks(text_spans: list) -> list:
    """`tree.TextSpan`s with Edexcel's marks rewritten as Cambridge's (see above)."""
    margin = _marks_margin(text_spans)
    shifts = page_shifts(text_spans)

    lines: dict[tuple[int, float], list] = {}
    for span in text_spans:
        lines.setdefault((span.page, span.y0), []).append(span)
    out = []
    for span in text_spans:
        line = lines[(span.page, span.y0)]
        joined = "".join(s.text for s in line)
        total = _QUESTION_TOTAL_RE.search(joined)
        if total:
            if span is line[0]:
                out.append(span.model_copy(update={"text": f" [Total: {total.group(1)}] "}))
            continue
        match = _PAREN_MARKS_RE.match(span.text.strip())
        x0 = span.x0 - shifts.get(span.page, 0.0)
        # alone on its line - or with nothing else there but punctuation: a stray full stop
        # at the margin shares 1(i)(b)'s "(2)" line (WFM01 June 2024, the mark lost)
        alone = all(s is span or not any(c.isalnum() for c in s.text) for s in line)
        if match and margin is not None and abs(x0 - margin) <= MARKS_MARGIN_TOLERANCE and alone:
            out.append(span.model_copy(update={"text": f" [{match.group(1)}] "}))
            continue
        # run onto the end of an answer line's dots, "..(2)" (4CH1 2CR June 2022 4(b)(iv)):
        # only a mark ends a dotted line that way
        dotted = _DOTTED_MARKS_RE.match(span.text.strip())
        if dotted:
            out.append(span.model_copy(update={"text": f" [{dotted.group(1)}] "}))
            continue
        out.append(span)
    return out


def subject_name(*cover_texts: str) -> str:
    """The subject as a cover names it: "Mathematics A" from "In Mathematics A (4MA1)
    Paper 1H" on a mark scheme's cover."""
    for text in cover_texts:
        match = _SUBJECT_NAME_RE.search(" ".join(text.split()))
        if match:
            return match.group(1).strip()
    return ""
