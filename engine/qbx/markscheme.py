"""Parse Cambridge mark scheme PDFs (CLAUDE.md section 7). A different problem from the
question parser: mark schemes are laid out as a Question/Answer/Marks table.

Real-paper finding (against the real 0478 mark scheme): `find_tables()` detects a table
region but almost no actual cells - this table has no ruled borders, just whitespace-aligned
columns, which PyMuPDF's line-based table detector can't pick up. Text-pattern anchoring
(the same technique as anchors.py/tree.py) works far better here.

Real-paper finding #2: this mark scheme's pages have `page.rotation == 90`. PyMuPDF's raw
span bboxes are in the page's *unrotated* coordinate space, so a visual table row (constant
y, varying x, after rotation) shows up in raw coordinates as a run of spans sharing roughly
the same raw x0 with *varying* raw y0 - i.e. a raw "line" (grouped by raw y-proximity) does
NOT correspond to a visual row here. Every span must be rotated individually via
`page.rotation_matrix` before rows can be found by clustering on the rotated y.

Mark codes appearing in mark scheme text (not yet parsed out individually, just documented
for whoever tags marks by type later): M1 (method), A1 (accuracy), B1 (independent), C1
(compensatory), ecf (error carried forward), owtte, AVP.
"""

import re
from collections import defaultdict

import pymupdf
from pydantic import BaseModel

from qbx import markscheme_paper
from qbx.anchors import page_blocks
from qbx.schema import BBox, LabelRef, MarkSchemeEntry

MARK_CODES = {"M", "A", "B", "C"}
ANNOTATION_CODES = {"ecf", "owtte", "AVP"}

# Cambridge's own typos in the Question column, found calibrating ~200 papers: "1f" for
# 1(f) (0620/41 ON22), "3(b(i)" for 3(b)(i) (0620/42 MJ23). Each lost its row, leaving the
# part it marks unjoined. Tried only when a label doesn't match ROW_LABEL_RE, and requires
# a part letter - a bare number is already covered, and a lone "1" must stay ambiguous
# with a marks value exactly as before.
#
# More, from 0620 2019-2023: a capital in the numeral, "7(c)(Ii)" (0620/31 ON21); and a stray
# "i" after the closing bracket, "6(a)(i)i" and "4(f)(iii)i" (0620/32 ON19, 0620/42 MJ23) -
# the rows that follow, (ii) and (iv), say the numeral in brackets is the real one.
# And from 0625 2016-2018: a doubled bracket, "7(a)((i)" (0625/41 MJ16); and one row in
# numbered pieces, "1(a)(i)1" then "1(a)(i)2" (0625/41 ON18) - `piece`, see _entry_ids - or
# lettered items, "6(a)(i)(a)", "6(a)(i)(b)" (0580/33 MJ19) - `item`.
_LENIENT_LABEL_RE = re.compile(
    r"^(?P<question>\d{1,2})\s*\(?\s*(?P<part>[a-z])\s*\)?"
    r"(?:\s*\(*\s*(?P<subpart>(?i:i|ii|iii|iv|v|vi|vii|viii))\s*\)?(?:\((?P<item>[a-h])\))?)?"
    r"(?:(?<=\))[iI]|(?<=\))(?P<piece>[1-9]))?$"
)
# A label printed without its question number, "(a)(ii)" under "2(a)(i)" (0620/33 MJ25).
# `question` matches empty: _entry_ids gives it the question of the row above. A part letter
# is required - a bare "(ii)" is too easily an answer's own numbering.
_QUESTIONLESS_LABEL_RE = re.compile(
    r"^(?P<question>)\((?P<part>[a-z])\)(?:\((?P<subpart>i|ii|iii|iv|v|vi|vii|viii)\))?$"
)


# One row answering two subparts, "5(c)(i),(ii)" (0625/42 FM21): read as the part they share,
# which the join links to each of them (`subpart` matches empty). Unread, the row's marks
# went to the row above.
_COMBINED_LABEL_RE = re.compile(
    r"^(?P<question>\d{1,2})\((?P<part>[a-z])\)(?P<subpart>)"
    # or three: "2(b)(i),(ii),(iii)" (0625/61 ON17)
    r"\((?:i|ii|iii|iv|v|vi|vii|viii)\)(?:\s*(?:,|&|and|–|-)\s*\((?:i|ii|iii|iv|v|vi|vii|viii)\))+$"
)
# One row for two parts, "2(a),(b)" (0625/52 and 53 MJ16, a table filled in for both): read
# as the first, with the other in `also` - the entry's `also_paths`, which the join links too
_COMBINED_PARTS_RE = re.compile(
    r"^(?P<question>\d{1,2})\((?P<part>[a-z])\)(?P<subpart>)\s*(?:,|&|and)\s*\((?P<also>[a-z])\)$"
)


def match_label(text: str) -> re.Match | None:
    """A row label, strictly formed or with one of the malformations above."""
    return (
        ROW_LABEL_RE.match(text) or _LENIENT_LABEL_RE.match(text) or _QUESTIONLESS_LABEL_RE.match(text)
        or _COMBINED_LABEL_RE.match(text) or _COMBINED_PARTS_RE.match(text) or _TABLE_ROW_LABEL_RE.match(text)
    )


# A row of a table the question fills in, labelled by the table's own row - "11A", "11B",
# "11C" for sequences A, B and C (0580/43 ON20) - is a lettered item of the question's row:
# the paper marks question 11 once
_TABLE_ROW_LABEL_RE = re.compile(r"^(?P<question>\d{1,2})(?P<part>)(?P<subpart>)(?P<item>[A-H])$")


ROW_LABEL_RE = re.compile(
    r"^(?P<question>\d{1,2})(\((?P<part>[a-z])\))?(\((?P<subpart>i|ii|iii|iv|v|vi|vii|viii)\))?$"
)
BARE_MARKS_RE = re.compile(r"^\d{1,2}$")
# A Marks-column value. Real-paper finding (0620 w18 MS 41): "any three from:" rows carry
# "max 3" in the Marks column instead of a bare digit, so requiring BARE_MARKS_RE there
# rejected 3(d)(ii) and 6(a)(iii) as labels and merged their answers into the row above.
# Older maths schemes glue follow-through to it: "1FT" (0580 2016).
MARKS_VALUE_RE = re.compile(r"^(?:max\s*)?(?P<marks>\d{1,2})(?:\s*FT)?$", re.IGNORECASE)

# Page furniture, not answer content. The last row of a mark scheme has no following row
# label to stop at, so without this it swallows everything to the end of the document -
# real-paper finding (0478 MS): a composed paper showed "© UCLES 2020 Page 5 of 11 0478/11
# Cambridge IGCSE Mark Scheme May/June 2020 PUBLISHED Question Answer Marks" appended to
# the final answer. These strings repeat on every page, so dropping them by pattern is
# safer than guessing a y-band on pages that are rotated anyway.
BOILERPLATE_RE = re.compile(
    r"^(©.*"
    r"|Page\s+\d+\s+of\s+\d+"
    r"|PUBLISHED"
    r"|Question|Answer|Marks"
    r"|Mark\s+Scheme"
    r"|Cambridge\s+\w+.*"
    r"|\d{4}/\d{2}"
    r"|(May/June|October/November|February/March)\s*\d{2,4})$",
    re.IGNORECASE,
)

# Rows belonging to the same entry land within a few pt of each other on the rotated y-axis
# (see the module docstring); this is generous enough to swallow font-size jitter without
# merging two adjacent, genuinely different rows, which are never closer than ~24pt.
ROW_TOLERANCE = 4.0
# How far above its label a row's own text may print and still be on the label's line
_SAME_LINE = 2.0
# ...or how far above a label centred in a two-line cell its row's first line may be: 6pt
# measured (0580/11 MJ20, 0580/21 MJ21), rows ~12pt a line
_CENTRED_LABEL_LEAD = 8.0


class MSSpan(BaseModel):
    text: str
    page: int
    x: float
    y: float
    # unrounded, upright (post-rotation) geometry - what locating a row's region and
    # redrawing its label need; x/y above stay rounded for the row-matching logic
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    origin: tuple[float, float] = (0.0, 0.0)
    size: float = 10.0
    bold: bool = False
    font: str = ""
    # a label whose row's mark is printed at the row's foot, beside its last marking point,
    # rather than on its own line (older schemes with part cells - see _fill_part_cells)
    marks_below: bool = False

    @property
    def position(self) -> tuple[int, float, float]:
        return (self.page, self.y, self.x)


# A Marks-column value run together with the first partial-mark code beside it. Real-paper
# finding (0580 m25 MS 22): maths mark schemes add a fourth "Partial Marks" column, and a
# row's mark and its first code ("2" and "B1", both bold, one space apart) come out of the
# PDF as a single span "2 B1" - which no marks-value pattern matches, so the row's label
# lost its marks partner and the whole row merged into the one above.
#
# Real-paper finding #2 (0620 MJ26 schemes): these add a fourth "Guidance" column, and the
# mark runs into the guidance the same way - "1 ALLOW calcium carbonate -> ..." - so the
# guidance keywords split it too, including their one-letter forms (see below).
_GUIDANCE_WORDS = (
    # in either case: 0620/41 MJ26 writes "1 ALLOW", 0620/61 MJ26 "1 Allow"
    r"(?i:allow|accept|ignore|condone|reject|con|ora|owtte|not|max|ecf|do\s+not)"
)
_MARKS_THEN_CODE_RE = re.compile(
    # "max 6 Ignore additional steps" too: 0620/61 and /62 MJ26 Q4, the planning question
    # a code may carry its suffix, "4 B3FT for 7 or 8 correctly plotted points" (0580/31 MJ24)
    r"^(?P<marks>(?:max\s*)?\d{1,2})\s+(?P<code>(?:(?:SC|[MABC])\d+(?:FT|oe|dep)?|FT"
    r"|" + _GUIDANCE_WORDS +
    # and the one-letter forms its abbreviations table defines - A allow, I ignore, R
    # reject - which 0620/42 MJ26 glues on too ("1 A 'sodium'"). Splitting one off is
    # harmless even where it'd be an answer letter: the two are separate columns anyway.
    r"|[AIR])\b.*"
    # or a note's heading: "4 Remember: Answers must be different" (0417/11 MJ26 Q1)
    r"|[A-Z][a-z]+:.*"
    # or a guidance sentence: "6 Ticks must show where marks are awarded" (0620/62 MJ26 Q4)
    # - a whole sentence, five words or more, so a short answer ("4 Remember the units")
    # stays whole
    r"|[A-Z][a-z]+(?:\s+[a-z]+,?){4,}.*)$"
)
# The same with a code in the marks' place: 0625 MJ26 schemes mark in codes only, and the
# code runs into the guidance beside it ("B1 accept 4.4 (m/s)").
_CODE_THEN_GUIDANCE_RE = re.compile(
    # any guidance, not just its keywords: "B1 A" (allow), "B1 must be in words", "A2
    # answers must describe", "B2 For all bullet points" (0625/41 MJ26)
    r"^(?P<marks>(?:SC|[MABC])\d)\s+(?P<code>[A-Za-z].*)$"
)
# A mark code standing in for a number in the Marks column. Real-paper finding (0580 m25 MS
# 22, 18(a)): a row can list its marks as codes - "M1" above "A1" - with no number at all,
# worth the sum of their digits. Only trusted *in* the Marks column: the same codes fill the
# Answer and Partial Marks columns of every maths scheme.
MARK_CODE_RE = re.compile(r"^(?:SC|[MABC])(?P<marks>\d)$")
# A question number run into its first marking point, "4 MP1" (0625/53 MJ17's planning
# question): split like a mark and its code, so the number stands as the row's label
_NUMBER_THEN_POINT_RE = re.compile(r"^(?P<marks>\d{1,2})\s+(?P<code>MP\d{1,2}\b.*)$")
# Codes are left-aligned where numbers are right-aligned, so a code in the Marks column is
# placed by its right edge: measured 2pt from the numbers' right edge in 0580 m25 MS 22,
# against 27pt for the nearest Partial Marks code.
MARK_CODE_EDGE_TOLERANCE = 6.0


# Two marks in one cell, one per answer: "1, 1" (0580/32 MJ16 1(d), "145" and "155")
_MARKS_LIST_RE = re.compile(r"^\d(?:\s*,\s*\d)+$")


def _split_marks_and_code(span: dict) -> list[dict]:
    """Splits a "2 B1"-style span into its marks value and the code after it, positioning
    the code by the measured width of what precedes it; and "1, 1" into its marks."""
    text = span["text"].strip()
    if _MARKS_LIST_RE.match(text):
        x0, y0, x1, y1 = span["bbox"]
        return [
            {**span, "text": value, "bbox": (x0, y0, x1, y1)}
            for value in re.findall(r"\d", text)
        ]
    match = (
        _MARKS_THEN_CODE_RE.match(text) or _CODE_THEN_GUIDANCE_RE.match(text) or _NUMBER_THEN_POINT_RE.match(text)
    )
    if not match:
        return [span]
    x0, y0, x1, y1 = span["bbox"]
    size = span["size"]
    marks = match.group("marks")
    lead = text[: match.start("code")]
    marks_x1 = x0 + pymupdf.get_text_length(marks, fontsize=size)
    code_x0 = x0 + pymupdf.get_text_length(lead, fontsize=size)
    return [
        {**span, "text": marks, "bbox": (x0, y0, marks_x1, y1)},
        {**span, "text": match.group("code"), "bbox": (code_x0, y0, x1, y1), "origin": (code_x0, span["origin"][1])},
    ]


# Older schemes bracket the Marks column: "[3]", "[max 3]", "[max. 1]" (0610 2016, every
# theory and practical paper - none of their rows joined). Read as the bare "3" / "max 3"
# newer ones print; "[Total: 10]" stays as it is.
_BRACKETED_MARKS_RE = re.compile(r"^\[\s*(?P<max>max\.?\s*)?(?P<marks>\d{1,2})\s*\]$", re.IGNORECASE)


def _unbracketed(text: str) -> str:
    match = _BRACKETED_MARKS_RE.match(text)
    if not match:
        return text
    return f"max {match['marks']}" if match["max"] else match["marks"]


# Characters this close (pt) are drawn touching, in one run of text
_TOUCHING = 1.0
# Two tiles' copies of one font can differ in size by a rounding error: 0680/11 MJ16's
# Q3 page has 11.001 beside 10.995, which cut "3(a)(i)" into "3(" + "a)(i)" - Q3's rows
# went unread and onto 2(b). A superscript is half the size.
_SAME_SIZE = 0.25


def _tiled_blocks(page: pymupdf.Page) -> list[dict] | None:
    """A page drawn in overlapping tiles, its text rebuilt from characters - or None when
    the page isn't one.

    Real-paper finding (0625/42 MJ17, page 9 - question 10's only page): every word comes
    back cut in pieces at the tiles' edges, "1" + "0(a)", "10" + "0(c)(i)", "Que" + "estion",
    with a character on a seam drawn twice (the "0" of "10" again as the "0" of "0(c)(i)").
    No label read, and the question went unjoined. Told apart by that: the same character
    at the same place in two spans. Each such character is kept once, and touching
    characters on a row are joined into one span."""
    matrix = page.rotation_matrix
    chars, seen, doubled = [], set(), False
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    upright = pymupdf.Rect(char["bbox"]) * matrix
                    key = (char["c"], round(upright.x0, 1), round(upright.y0, 1))
                    if key in seen:
                        doubled = doubled or not char["c"].isspace()
                        continue
                    seen.add(key)
                    origin = pymupdf.Point(char["origin"]) * matrix
                    chars.append((origin.y, upright.x0, upright.x1, char, span))
    if not doubled:
        return None
    chars.sort(key=lambda c: (round(c[0], 1), c[1]))
    rows: list[list] = []
    for char in chars:
        if rows and abs(char[0] - rows[-1][0][0]) <= ROW_TOLERANCE / 4:
            rows[-1].append(char)
        else:
            rows.append([char])
    spans = []
    for row in rows:
        row.sort(key=lambda c: c[1])
        run = [row[0]]
        for char in row[1:] + [None]:
            if char is not None and char[3]["c"] == run[-1][3]["c"] and char[1] < run[-1][2] - _TOUCHING:
                continue  # a seam's character drawn again, a little off the first
            if (
                char is not None
                and char[1] - run[-1][2] <= _TOUCHING
                and abs(char[4]["size"] - run[-1][4]["size"]) <= _SAME_SIZE
            ):
                run.append(char)
                continue
            rects = [pymupdf.Rect(c[3]["bbox"]) for c in run]
            box = rects[0]
            for rect in rects[1:]:
                box |= rect
            first = run[0][4]
            spans.append({
                "text": "".join(c[3]["c"] for c in run),
                "bbox": tuple(box),
                "origin": run[0][3]["origin"],
                "size": first["size"],
                "flags": first["flags"],
                "font": first.get("font", ""),
            })
            run = [char]
    return [{"lines": [{"spans": [span]} for span in spans]}]


def _rotated_spans(doc: object, unbracket: bool = False) -> list[MSSpan]:
    """`unbracket` reads Cambridge's bracketed marks bare. Not for Edexcel: International A
    Level's "[3]" is a part's total (WME02 June 2022 read 7(a)'s marks twice)."""
    spans = []
    for page_index, page in enumerate(doc):
        matrix = page.rotation_matrix
        for block in _tiled_blocks(page) or page_blocks(page):
            for line in block.get("lines", []):
                for span in (piece for raw in line["spans"] for piece in _split_marks_and_code(raw)):
                    text = span["text"].strip()
                    text = _unbracketed(text) if unbracket else text
                    if not text:
                        continue
                    point = pymupdf.Point(span["bbox"][0], span["bbox"][1]) * matrix
                    rect = pymupdf.Rect(span["bbox"]) * matrix
                    origin = pymupdf.Point(span["origin"]) * matrix
                    # Rounded to damp sub-point rendering jitter: a label and its same-row
                    # marks value can differ by a fraction of a point (e.g. 261.9 vs
                    # 261.86), which is enough for a strict tuple comparison to wrongly
                    # exclude the marks span from its own row's window.
                    spans.append(
                        MSSpan(
                            text=text,
                            page=page_index,
                            x=round(point.x, 1),
                            y=round(point.y),
                            bbox=(rect.x0, rect.y0, rect.x1, rect.y1),
                            origin=(origin.x, origin.y),
                            size=span["size"],
                            bold=bool(span["flags"] & pymupdf.TEXT_FONT_BOLD),
                            font=span.get("font", ""),
                        )
                    )
    return spans


# How far a code may start from its column's "Mark"/"Marks" header: codes measured 4pt (0625
# MJ24) and 19pt (0625 MJ26) right of the header's start; the nearest Answer or Guidance
# text, 26pt left or 130pt right of it.
_CODE_HEADER_REACH = (-10.0, 40.0)


def _marks_headers(spans: list[MSSpan]) -> dict[int, float]:
    """Each page's "Mark"/"Marks" column header x, on a header row beside "Question"."""
    # "Question" alone, or run into the next header: "Question Answer" (0625/43 ON23)
    questions = {(s.page, s.y) for s in spans if s.text.split()[0] == "Question"}
    return {
        s.page: s.x
        for s in spans
        if s.text in ("Mark", "Marks")
        and any(page == s.page and abs(y - s.y) <= ROW_TOLERANCE for page, y in questions)
    }


def _under_header(span: MSSpan, headers: dict[int, float]) -> bool:
    low, high = _CODE_HEADER_REACH
    return span.page in headers and headers[span.page] + low <= span.x <= headers[span.page] + high


def _is_row_code(span: MSSpan, headers: dict[int, float]) -> bool:
    """A mark code under its page's Mark column header."""
    low, high = _CODE_HEADER_REACH
    return (
        bool(MARK_CODE_RE.match(span.text))
        and span.page in headers
        and headers[span.page] + low <= span.x <= headers[span.page] + high
    )


def _codes_only(spans: list[MSSpan]) -> bool:
    """Whether the Mark column holds codes rather than numbers. Real-paper finding (0625
    Physics, papers 3 and 4): the column reads "B1", "A3", "B3" with no number anywhere, so
    no label had a marks partner and a 70-row scheme came out as 3 rows - the 3 a bare
    number in the answer happened to partner. Decided per document: labels partnered by a
    code at the end of their row far outnumber those partnered by a number."""
    # only labels with a part letter: a bare "10" in an answer (10⁶) is shaped like question
    # 10 and has its neighbours' numbers for partners (0625/41 MJ22)
    shaped = [s for s in spans if (match := match_label(s.text)) and match.group("part")]
    numbered = sum(_has_marks_partner(s, spans) for s in shaped)
    coded = sum(_has_marks_partner(s, spans, codes=True) for s in shaped)
    return coded > 2 * numbered


# How far right of a bare question number a bracketed piece may start and still be the
# rest of its label: "3" then "(a)" 5.6pt on, "3" then "(b)(i)" 5.5pt (0625/61 MJ26); the
# Answer column starts ~40pt further on
_LABEL_PIECE_GAP = 12.0
# ...or further, when the piece is nothing but part markers: older schemes give the number
# and the part cells of their own - "4" at x 81, "(a) (i)" at x 129 (0610 2016, 48pt) -
# and an answer is never just "(a) (i)"
_LABEL_CELL_GAP = 70.0
_ONLY_MARKERS_RE = re.compile(r"^(?:\(\s*(?:[a-z]|[ivx]+)\s*\)\s*){1,3}$")


def _join_split_labels(spans: list[MSSpan]) -> list[MSSpan]:
    """A label set as two spans - its number, then "(a)" or "(b)(i)" - joined back into one.
    0625/61 MJ26 prints most of question 3 this way; each piece alone read as question 3's
    own row, three times over, and the parts went unjoined.

    The pieces are looked for along the whole printed row, not just the spans next in the
    file: 0610/42 MJ18's text layer stores "3" and "(a)(i)" - and "2(" and "(b)(ii)", the
    bracket split off - far apart, and a stray "3" left alone became the Question column."""
    spans = _glued_brackets(spans)
    by_row: dict[tuple[int, float], list[int]] = {}
    for index, span in enumerate(spans):
        by_row.setdefault((span.page, span.y), []).append(index)

    def row_of(span: MSSpan) -> list[int]:
        return [
            j for dy in range(-int(ROW_TOLERANCE), int(ROW_TOLERANCE) + 1)
            for j in by_row.get((span.page, span.y + dy), [])
        ]

    # a part cell is inside the Question column: up to its heading's right edge (0610 2016,
    # parts at x 93-130 under "Question" ending ~150); 0625/31 MJ26's "(a)" 170pt out is an
    # answer's
    heading_right: dict[int, float] = {}
    for span in spans:
        if span.text.split() and span.text.split()[0] == "Question":
            heading_right[span.page] = max(heading_right.get(span.page, 0.0), span.bbox[2])
    joined: dict[int, MSSpan] = {}
    used: set[int] = set()
    part_cells: list[float] = []  # x of part cells joined across the wide gap
    for i, span in enumerate(spans):
        number = _SPLIT_NUMBER_RE.match(span.text)
        if i in used or not number:
            continue
        opened = span.text.endswith("(")
        for j in sorted(row_of(span), key=lambda k: spans[k].x):
            piece = spans[j]
            gap = piece.x - span.x
            if (
                j != i
                and j not in used
                and 0 < gap <= (
                    _LABEL_CELL_GAP
                    if opened or (
                        _ONLY_MARKERS_RE.match(piece.text)
                        # a page without the heading (0610/52 MJ16 pp.2-3) uses the others'
                        and piece.x <= heading_right.get(piece.page, max(heading_right.values(), default=0.0)) + 10
                    )
                    else _LABEL_PIECE_GAP
                )
                and piece.text.startswith("(")
                and match_label(number["number"] + piece.text)
            ):
                used.add(j)
                if gap > _LABEL_PIECE_GAP and not opened:
                    part_cells.append(piece.x)
                joined[i] = span.model_copy(update={
                    "text": number["number"] + piece.text,
                    # the row starts at its higher piece, or text beside it sorts above
                    "y": min(span.y, piece.y),
                    "bbox": (
                        min(span.bbox[0], piece.bbox[0]), min(span.bbox[1], piece.bbox[1]),
                        max(span.bbox[2], piece.bbox[2]), max(span.bbox[3], piece.bbox[3]),
                    ),
                })
                break
    out = [joined.get(i, span) for i, span in enumerate(spans) if i not in used or i in joined]
    # only a scheme laid out that way - most of its joined labels across the gap, as every
    # 0610 2016 scheme is; 0625 MJ26 joins "3" "(a)" 5pt apart, with a couple further
    wide = len(part_cells) >= 2 and len(part_cells) >= len(joined) / 2
    column_right = max(heading_right.values(), default=0.0)
    if wide:
        return _fill_part_cells(out, part_cells, column_right)
    # Or with the number and its first part in one cell, "14 (a)", "15 (a) (i)", and the
    # rows after printing only "(b)" or "(ii)", set in from the number but still under the
    # "Question" heading (0580 2016 schemes: "(b)" at x 58, "(ii)" at 77, numbers at 37, the
    # heading ending at 93 and answers at 108). Unread, every such row merged into the one
    # above and its part went unjoined.
    numbers_x: dict[int, float] = {}
    for span in out:
        if (label := match_label(span.text)) and label.group("question") and span.page in heading_right:
            numbers_x[span.page] = min(numbers_x.get(span.page, span.x), span.x)
    lone = [
        span.x for span in out
        if span.page in numbers_x
        and _ONLY_MARKERS_RE.match(span.text)
        and numbers_x[span.page] + _LONE_CELL_INSET <= span.x <= heading_right[span.page] + 10
    ]
    if len(lone) >= _LONE_CELLS:
        return _fill_part_cells(out, lone, column_right)
    return out


# A lone part cell is set this far in from the question numbers, at least ("(b)" 21pt in);
# and a scheme needs this many to be laid out so
_LONE_CELL_INSET = 10.0
_LONE_CELLS = 3


def _glued_brackets(spans: list[MSSpan]) -> list[MSSpan]:
    """A part marker whose bracket is a span of its own, "(" then "b)", touching (0610/31
    MJ16 6(b)) - one span again. Matched by position: the file can store them apart."""
    gone: set[int] = set()
    out = list(spans)
    for i, bracket in enumerate(spans):
        if bracket.text != "(":
            continue
        for j, span in enumerate(spans):
            if (
                j != i and j not in gone
                and span.page == bracket.page
                and abs(span.y - bracket.y) <= ROW_TOLERANCE
                and -2 <= span.bbox[0] - bracket.bbox[2] <= 2
                and _ONLY_MARKERS_RE.match("(" + span.text)
            ):
                out[i] = span.model_copy(update={
                    "text": "(" + span.text,
                    "bbox": (bracket.bbox[0], min(bracket.bbox[1], span.bbox[1]), span.bbox[2], max(bracket.bbox[3], span.bbox[3])),
                })
                gone.add(j)
                break
    return [span for k, span in enumerate(out) if k not in gone]


# a question number on its own, or with its bracket split from the part ("2(" + "(b)(ii)")
_SPLIT_NUMBER_RE = re.compile(r"^(?P<number>\d{1,2})\(?$")


_ROMAN_MARKERS = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii")


def _fill_part_cells(spans: list[MSSpan], part_cells: list[float], column_right: float = 0.0) -> list[MSSpan]:
    """In a scheme whose part cells stand apart from the question number (see
    _LABEL_CELL_GAP), a row further down the same question prints only its part cell -
    "(ii)", "(b)", "(c) (i)" - under "4 (a) (i)". Each is written out in full from the label
    above it ("4(a)(ii)"), but only at the part cells' own x, so an answer's "(ii)" is never
    taken for one - between the question number and the part cell only."""
    # the furthest: a lone "(a)" sits at x 111, "(b) (i)" at 130 (0610/33 MJ16)
    cell_x = max(part_cells)
    # out to the Question column's edge where the heading gives it: "(c) (i)" at x 119
    # when every joined part cell was a lone letter at 100 (0610/53 MJ16)
    reach = max(cell_x + 15, column_right + 10)
    order = sorted(range(len(spans)), key=lambda k: (spans[k].page, spans[k].y, spans[k].x))
    question = part = sub = None
    number_page = -1
    number_x = 0.0
    out = list(spans)
    for k in order:
        span = spans[k]
        full = ROW_LABEL_RE.match(span.text.replace(" ", "")) or _LENIENT_LABEL_RE.match(span.text)
        # only one in the Question column, left of the part cells: Guidance can cite another
        # part ("1(f)" at x 607, 0610/62 FM16), and that moved the count on
        if full and full.group("question") and span.x < cell_x - 15:
            question, part, sub = full.group("question"), full.group("part"), full.group("subpart")
            number_x, number_page = span.x, span.page
            out[k] = span.model_copy(update={"marks_below": True})
            continue
        # anywhere from just past the number to the part cell: a lone "(c)" sits 19pt left of
        # "(a) (i)" (centred in its cell); the Answer column starts ~50pt right of it
        # ...or at the number's own indent on a page with no number to set it in from: "(d) (i)"
        # at x 48, under "10 (a)" at 48 the page before (0580/43 MJ16)
        start = number_x - LABEL_COLUMN_SLACK if span.page != number_page else number_x
        if question is None or not (start < span.x <= reach) or not _ONLY_MARKERS_RE.match(span.text):
            continue
        tokens = re.findall(r"\(\s*([a-z]+)\s*\)", span.text)
        new_part, new_sub = part, None
        for n, token in enumerate(tokens):
            # a numeral is a subpart while there's a part letter to hang it on - unless it's
            # the letter after (h), part (i), first in its cell
            roman = token in _ROMAN_MARKERS and part is not None and not (n == 0 and token == "i" and part == "h")
            if n == 0 and not roman:
                new_part = token
            else:
                new_sub = token
        if new_part is None:
            continue
        part, sub = new_part, new_sub
        text = f"{question}({part})" + (f"({sub})" if sub else "")
        # set where the question's number is, as the label it stands for
        shift = span.x - number_x
        out[k] = span.model_copy(update={
            "text": text, "x": number_x, "marks_below": True,
            "bbox": (span.bbox[0] - shift, span.bbox[1], span.bbox[2], span.bbox[3]),
        })
    return out


def _has_marks_partner(candidate: MSSpan, spans: list[MSSpan], codes: bool = False) -> bool:
    """Real-paper finding (0620 MS): a numbered preamble heading ("6 Calculation specific
    guidance") also matches ROW_LABEL_RE (bare digit) and can sit at the same x as the real
    Question column, so the x-cluster filter alone doesn't reject it. Every genuine MS row
    - checked against both real papers this pipeline has seen - carries a mark value on the
    same row as its label; a preamble heading never does. Require that pairing instead of
    just pattern + position.

    "Same row" is a tolerance, not an equality. Real-paper finding (0620 s19 MS): the first
    row of every page has its label at a rotated y of 80.4 and its mark value at 80.6, which
    round to 80 and 81 - so exact comparison dropped the opening row of all six table pages
    (1(a), 2(d), 3(e)(i), 4(c), 5(a), 5(g)(iii)) and left their QP parts unjoined. Rounding
    to whole points already damps this jitter; it can't help when the jitter straddles a
    rounding boundary, and no amount of extra precision fixes that. Genuine rows are never
    closer than ~24pt, so ROW_TOLERANCE separates them with room to spare.
    """
    headers = _marks_headers(spans) if codes else {}
    if candidate.marks_below:
        # its mark is anywhere down its row: before the next label on the page
        below = [
            s.y for s in spans
            if s.page == candidate.page and s.marks_below and s.y > candidate.y + ROW_TOLERANCE
        ]
        end = min(below, default=float("inf"))
        return any(
            s.page == candidate.page
            and candidate.y - ROW_TOLERANCE <= s.y < end
            and s.x > candidate.x
            and MARKS_VALUE_RE.match(s.text)
            for s in spans
        )
    return any(
        s.page == candidate.page
        # or on the line above, the label centred in a two-line cell (see parse_mark_scheme)
        and candidate.y - _CENTRED_LABEL_LEAD <= s.y <= candidate.y + ROW_TOLERANCE
        and s.x > candidate.x
        and (_is_row_code(s, headers) if codes else MARKS_VALUE_RE.match(s.text))
        for s in spans
    )


def _row_label_pages(spans: list[MSSpan], codes: bool = False) -> set[int]:
    """A page counts as table content if at least one genuine row-label anchor sits on it -
    this skips the cover page and the "Generic Marking Principles" preamble pages without
    needing MS-specific boilerplate text heuristics."""
    return {
        s.page
        for s in spans
        if match_label(s.text) and _has_marks_partner(s, spans, codes)
    }


def _leftmost_cluster(candidates: list[MSSpan], tolerance: float = 15.0) -> list[MSSpan]:
    """Real row labels are ambiguous with a bare marks value: "1" alone matches
    ROW_LABEL_RE just as validly as "8(a)" does (the part/subpart groups are optional), so
    a Marks-column "1" would otherwise be mistaken for the next row's label and truncate
    the current row before its own answer text. Same fix as anchors.py: cluster by x and
    keep the leftmost group - the Question column is, by construction, the leftmost of the
    three columns, whether or not a given row's label has a part suffix.
    """
    if not candidates:
        return []
    # page by page: 0610/42 MJ18 sets one page 25pt left of the rest, and its labels alone
    # made "the leftmost column" - every other page's rows were dropped. But only near the
    # column the pages share: a page with no label at all has answer numbers leftmost
    # ("11", "12" at x 216 against the labels' 98, 0478/22 ON21)
    firsts = []
    for page in sorted({s.page for s in candidates}):
        ordered = sorted((s for s in candidates if s.page == page), key=lambda s: s.x)
        groups = [[ordered[0]]]
        for span in ordered[1:]:
            if span.x - groups[-1][-1].x <= tolerance:
                groups[-1].append(span)
            else:
                groups.append([span])
        firsts.append(groups[0])
    xs = sorted(group[0].x for group in firsts)
    usual = xs[len(xs) // 2]

    def lettered(group: list[MSSpan]) -> bool:
        return any((match := match_label(span.text)) and match.group("part") for span in group)

    # within the usual column's own spread, as before; a page set further off must show a
    # real label - a trace table's bare "0"s, 27pt right of the labels, don't (0478/23 MJ24)
    return [
        span
        for group in firsts
        if abs(group[0].x - usual) <= tolerance
        or (abs(group[0].x - usual) <= _PAGE_COLUMN_SHIFT and lettered(group))
        for span in group
    ]


# How far one page's Question column may sit from the others': 25pt (0610/42 MJ18 p.6)
_PAGE_COLUMN_SHIFT = 40.0


# Values in the Marks column are right-aligned, so "max 2" starts a few pt left of a bare
# "1" (775.5 vs 778.6 in 0620 w18 MS 41); the Answer column never comes within ~100pt.
MARKS_COLUMN_TOLERANCE = 15.0


def _marks_column_x(labels: list[MSSpan], spans: list[MSSpan]) -> float:
    """The Marks column's x, derived per document (never hardcoded): the median x of the
    rightmost marks value sharing a row with each label."""
    rightmost = (
        max(
            (
                s.x
                for s in spans
                if s.page == label.page
                and abs(s.y - label.y) <= ROW_TOLERANCE
                and s.x > label.x
                and MARKS_VALUE_RE.match(s.text)
            ),
            default=None,
        )
        for label in labels
    )
    # a row whose marks are codes only ("M1", "A1") has no number to measure
    xs = sorted(x for x in rightmost if x is not None)
    return xs[len(xs) // 2] if xs else 0.0


def _has_partial_marks_column(spans: list[MSSpan]) -> bool:
    """A "Partial Marks" column beside Marks (Mathematics): "Partial" and "Marks" on one line."""
    lines: dict[tuple, list[str]] = defaultdict(list)
    for s in spans:
        lines[(s.page, round(s.y))].append(s.text.strip().lower())
    return any("partial marks" in " ".join(words) for words in lines.values())


# Labels a page needs, each with a mark beside it, to set its own Marks column; how closely
# most of their marks must line up; and how far from the document's column it may be - a
# Partial Marks column's numbers start 50pt+ further right
_PAGE_COLUMN_LABELS = 3
_PAGE_COLUMN_AGREE = 3.0
_PAGE_COLUMN_REACH = 30.0


def _page_marks_columns(labels: list[MSSpan], spans: list[MSSpan], document_x: float) -> dict[int, float]:
    """The Marks column per page, where a page has enough marked rows to say: one scheme
    can set it in two places - 0580/33 MJ20 at x 315 on pages 3-4 and 298 on 5-6, so every
    mark on the later pages fell outside the document's column and read 0 (found by
    `qbx audit`)."""
    out = {}
    for page in {label.page for label in labels}:
        mine = [label for label in labels if label.page == page]
        marked = [
            label for label in mine
            if any(
                s.page == page and abs(s.y - label.y) <= ROW_TOLERANCE and s.x > label.x
                and MARKS_VALUE_RE.match(s.text)
                for s in spans
            )
        ]
        if len(marked) < _PAGE_COLUMN_LABELS:
            continue
        x = _marks_column_x(marked, spans)
        rightmost = [
            max(s.x for s in spans
                if s.page == page and abs(s.y - label.y) <= ROW_TOLERANCE and s.x > label.x
                and MARKS_VALUE_RE.match(s.text))
            for label in marked
        ]
        agree = sum(abs(r - x) <= _PAGE_COLUMN_AGREE for r in rightmost)
        if agree * 2 > len(rightmost) and abs(x - document_x) <= _PAGE_COLUMN_REACH:
            out[page] = x
    return out


def _row_marks(column_spans: list[MSSpan], partial_marks: bool = False) -> int:
    """Real-paper finding (0620 w18 MS 41): some mark schemes print one value per marking
    point - M1..M5 each with its own "1" in the Marks column - rather than a single row
    total, so taking one value read a 5-mark row as 1 (MS total 65 vs the paper's 80).
    Sum every value in the column; a "max N" caps the row outright, since it sits beside a
    list of creditworthy points that is deliberately longer than N."""
    capped = [s for s in column_spans if s.text.lower().startswith("max")]
    if capped:
        return int(MARKS_VALUE_RE.match(capped[0].text)["marks"])
    # A row whose Marks cell holds a number is worth the number: a code beside it is part
    # credit or its breakdown, never more marks. Mathematics prints "3" | "B2 for 4x + 6 = 30"
    # in its Partial Marks column, the code's left edge 10pt from the number's right, and
    # every such row read 5 (0580/41 MJ22, found by `qbx audit`: 62 rows on Paper 4 alone).
    numbers = [s for s in column_spans if not MARK_CODE_RE.match(s.text) and MARKS_VALUE_RE.match(s.text)]
    # Marks are set in bold; a plain number beside them is answer content - the Partial
    # Marks column's fractions ("2" over "12", in Times, 0580/41 MJ22 7(d) read 18 for 4;
    # 0580/43 MJ20 11(e) 16 for 2) reach into the Marks column. Several bold numbers are
    # several marks (0580/42 ON20 1(b)(ii): "1" and "2" for two answer lines). Only beside
    # a Partial Marks column: elsewhere a plain "1" in line with the rest is a mark the
    # board set plain (0620/53 MJ20 2(f)).
    if partial_marks and any(s.bold for s in numbers) and not all(s.bold for s in numbers):
        numbers = [s for s in numbers if s.bold]
        column_spans = [s for s in column_spans if s.bold or MARK_CODE_RE.match(s.text)]
    if numbers and len(numbers) < len(column_spans):
        column_spans = numbers
    # A C (compensatory) mark under an A mark worth the whole calculation ("A2", "A3") is part
    # credit *instead of* it, never on top: "A2" over "C1" is worth 2. The bracketed "(C1)" of
    # newer schemes isn't read as a code at all; 0625/41 MJ21 prints it bare, and every such
    # row read high. Under an A1 they are steps of their own - "C1", "C1", "A1" is worth 3
    # (0625/31 MJ21).
    if any(s.text.startswith("A") and (m := MARK_CODE_RE.match(s.text)) and int(m["marks"]) > 1 for s in column_spans):
        column_spans = [s for s in column_spans if not (s.text.startswith("C") and MARK_CODE_RE.match(s.text))]
    return sum(
        int((MARKS_VALUE_RE.match(s.text) or MARK_CODE_RE.match(s.text))["marks"])
        for s in _without_breakdown(column_spans)
    )


def _code_totals(spans: list[MSSpan]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for s in spans:
        if match := MARK_CODE_RE.match(s.text):
            letter = s.text[: match.start("marks")]
            totals[letter] = totals.get(letter, 0) + int(match["marks"])
    return totals


# Codes this far apart in x are in different sub-columns of the Mark column
_SUB_COLUMN_GAP = 10.0


def _without_breakdown(column_spans: list[MSSpan]) -> list[MSSpan]:
    """A row's codes printed twice over: its total at the column's right ("B2") and the same
    marks broken down at its left ("B1", "B1"), 0625/41 ON22 - read as 4. When two
    sub-columns carry the same codes to the same totals, they're one set of marks. Different
    codes side by side ("M1" beside "A1", 0625/32 ON25 6(d)) are marks of their own."""
    codes = [s for s in column_spans if MARK_CODE_RE.match(s.text)]
    if len(codes) < 2 or len(codes) != len(column_spans):
        return column_spans
    right = max(s.x for s in codes)
    totals_right = _code_totals([s for s in codes if s.x > right - _SUB_COLUMN_GAP])
    left = [s for s in codes if s.x <= right - _SUB_COLUMN_GAP]
    if left and _code_totals(left) == totals_right:
        return left
    return column_spans


def _marks_column_right(labels: list[MSSpan], spans: list[MSSpan], codes: bool = False) -> float:
    """The right edge of the Marks column, where its right-aligned numbers end - or, in a
    column of codes only, where its codes do."""
    headers = _marks_headers(spans) if codes else {}
    edges = sorted(
        s.bbox[2]
        for label in labels
        for s in spans
        if s.page == label.page
        and abs(s.y - label.y) <= ROW_TOLERANCE
        and s.x > label.x
        and (_is_row_code(s, headers) if codes else MARKS_VALUE_RE.match(s.text))
    )
    return edges[len(edges) // 2] if edges else 0.0


# How far outside the found labels' x-range a mark-less label may sit and still be in the
# Question column: labels are centred there and measured within ~10pt of each other, while
# the Answer column's text starts ~20pt further right.
LABEL_COLUMN_SLACK = 6.0


def _is_column_code(span: MSSpan, column_right: float) -> bool:
    # Its right edge measured from the text: a span's box can run on over spaces stripped
    # from its text - "M1" 29pt wide, its ink ending 13pt sooner (0580/11 MJ24 Q22, a Marks
    # cell of "M1", "A1", "A1" and no number)
    # - or over spaces before it, "M1" boxed from 316 to 351 (0580/11 MJ25 Q17): either edge
    measured = min(span.bbox[2], span.x + pymupdf.get_text_length(span.text, fontsize=span.size))
    return bool(MARK_CODE_RE.match(span.text)) and any(
        abs(right - column_right) <= MARK_CODE_EDGE_TOLERANCE for right in (span.bbox[2], measured)
    )


# How far below a question number its row's first mark may start: 0625 Paper 5/6's planning
# question prints "4" alone, then "MP1 ... | 1" 25pt lower
_FIRST_MARK_BELOW = 40.0
# How far left of the "Question" header, or right of its start, the column's labels sit:
# "4" at x 92 and "3(a)" at 101 under "Question" at 115
_QUESTION_HEADER_REACH = (-40.0, 10.0)


def _numbers_marked_below(spans: list[MSSpan]) -> list[MSSpan]:
    """Labels heading a row whose marks start on a line further down - the planning question
    of 0625 Papers 5 and 6 (2016-2018): "4" alone, then marking points "MP1", "MP2"... each
    with its mark; and "2(a),(b)" over a table, its first mark a line lower (0625/52 MJ16). With no mark on its own line it had no partner, its
    page held no other label, and the question went unjoined. Only under a page's
    "Question" | "Marks" header, in the Question column, with a mark under the Marks header
    within _FIRST_MARK_BELOW and no other label in between."""
    headers = _marks_headers(spans)
    question_x = {
        s.page: s.x for s in spans
        if s.page in headers and s.text.split()[:1] == ["Question"]
    }
    low, high = _QUESTION_HEADER_REACH
    out = []
    for span in spans:
        if (
            span.page not in question_x
            or not (label := match_label(span.text))
            or label.group("question") in ("", "0")
            or not question_x[span.page] + low <= span.x <= question_x[span.page] + high
        ):
            continue
        below = sorted(
            (s for s in spans if s.page == span.page and span.y + ROW_TOLERANCE < s.y <= span.y + _FIRST_MARK_BELOW),
            key=lambda s: s.y,
        )
        first_mark = next(
            (s for s in below if (MARKS_VALUE_RE.match(s.text) or MARK_CODE_RE.match(s.text)) and _under_header(s, headers)),
            None,
        )
        if first_mark is None:
            continue
        if any(
            match_label(s.text) and s.y < first_mark.y - ROW_TOLERANCE and s.x <= question_x[span.page] + high
            for s in below
        ):
            continue
        out.append(span)
    return out


def _find_labels(all_spans: list[MSSpan]) -> tuple[list[MSSpan], list[MSSpan]]:
    """(spans on table pages, genuine row labels in reading order).

    Two passes: labels with a number beside them locate the Marks column, and then a label
    whose row carries only mark codes *in that column* counts too."""
    codes = _codes_only(all_spans)
    content_pages = _row_label_pages(all_spans, codes)
    # A page whose only row has its first mark far below the label - 0625/61 MJ26 1(a), a
    # table and a graph before "1" in the Marks column - is a scheme page too: under the
    # table header, with a label that has a part letter
    headed = {s.page for s in all_spans if s.text.split()[0] == "Question"}
    content_pages |= {
        s.page
        for s in all_spans
        if s.page in headed and (match := match_label(s.text)) and match.group("question") and match.group("part")
    }
    numbered = _numbers_marked_below(all_spans)
    content_pages |= {s.page for s in numbered}
    spans = [s for s in all_spans if s.page in content_pages]
    shaped = [s for s in spans if match_label(s.text)]
    labels = _leftmost_cluster([s for s in shaped if _has_marks_partner(s, spans, codes)])
    column_right = _marks_column_right(labels, spans, codes)
    if column_right:
        coded = [
            s
            for s in shaped
            if not _has_marks_partner(s, spans, codes)
            and any(
                c.page == s.page and abs(c.y - s.y) <= ROW_TOLERANCE and _is_column_code(c, column_right)
                for c in spans
            )
        ]
        labels = _leftmost_cluster(labels + coded)

    # A third pass for a row whose Marks cell Cambridge left blank. Real-paper finding
    # (0620/32 FM21, 7(e)(ii)): a properly ruled row with its answer, but nothing in the
    # Marks column, so it had no partner and was dropped. Taken only when it's unmistakably
    # a label: printed in the Question column itself (within a few points of the labels
    # already found), with a part letter - a bare "1" still needs its mark, as it's exactly
    # what a stray marks value looks like - and for a question that has other rows.
    if labels:
        column_x0 = min(label.x for label in labels) - LABEL_COLUMN_SLACK
        column_x1 = max(label.x for label in labels) + LABEL_COLUMN_SLACK
        questions = {match_label(label.text).group("question") for label in labels}
        found = {label.position for label in labels}
        # A row carried over a page break has its label printed again at the top of the new
        # page, with no mark there (0620 MJ26 schemes) - that's the same row continuing, not
        # a new one. Without this, the repeat was bumped to the next label in sequence and
        # invented rows the paper doesn't have (4(c)(iii) where it has only (i) and (ii)).
        found_texts = {label.text for label in labels}
        blank_marks = [
            s
            for s in shaped
            if s.position not in found
            and s.text not in found_texts
            and column_x0 <= s.x <= column_x1
            and match_label(s.text).group("part")
            and match_label(s.text).group("question") in questions
        ]
        labels = labels + blank_marks
    found = {label.position for label in labels}
    labels = labels + [s for s in numbered if s.position not in found]

    # A row has one label, its leftmost. Real-paper finding (0620/33 ON19): an answer that is
    # just a number - "5" beside 2(d)(ii), "36" beside 5(b)(iv) - starts the Answer column a
    # couple of dozen points right of the label, and labels' own x spreads enough (86-105pt
    # here) for the Question-column cluster to chain out to it. Each became a row "5" /
    # "36" of its own, splitting its real row and leaving an orphan.
    # No question is numbered 0: a running header broken into pieces, "0" then "478/12" at the
    # top of 0478/12 FM17's page 2, read as question 0 with the "12" for its mark
    labels = [
        label
        for label in labels
        if match_label(label.text).group("question") != "0"
        and not any(
            other.page == label.page and abs(other.y - label.y) <= ROW_TOLERANCE and other.x < label.x
            for other in labels
        )
    ]
    return spans, sorted(labels, key=lambda s: s.position)


def _entry_id(label_text: str, question_above: int = 0) -> tuple[str, int, str]:
    """`question_above` stands in for a label printed without its question number."""
    match = match_label(label_text)
    question_number = int(match.group("question") or question_above)
    # dotted to match tree.py's QuestionPart.path format ("d.i", not "d(i)")
    part_path = ".".join(g.lower() for g in (match.group("part"), match.group("subpart")) if g)
    return f"{question_number}.{part_path or 'stem'}", question_number, part_path


_ROMANS = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii"]


def _step(last: str, by: int) -> str | None:
    """The label component `by` places after `last` in its sequence: roman or letter."""
    if last in _ROMANS:
        index = _ROMANS.index(last) + by
        return _ROMANS[index] if 0 <= index < len(_ROMANS) else None
    if len(last) == 1 and last.isalpha():
        stepped = chr(ord(last) + by)
        return stepped if "a" <= stepped <= "z" else None
    return None


def _entry_ids(labels: list[MSSpan], fresh: set[int] | None = None) -> list[tuple[str, int, str]]:
    """_entry_id for every label in reading order, correcting the typos where one label is
    printed twice in a row:

    - On the *next page*, a repeat is the same row carried over the page break, its label
      printed again (0620 MJ26 schemes): it keeps the same id, and the rows are merged.
    - On the *same page*, two consecutive rows can't genuinely share a label. If the row
      before the pair leaves a gap the first of them would fill - (i), (iii), (iii) - the
      first is the typo, for (ii) (0620/63 ON23). Otherwise the second is the typo, for the
      next in sequence: "3(d)(i)" twice meaning (i) then (ii) (0620/32 ON24).
    """
    ids = []
    for index, label in enumerate(labels):
        entry_id, question, path = _entry_id(label.text, ids[-1][1] if ids else 0)
        # a row's later numbered piece, "1(a)(i)2" after "1(a)(i)1": the same row
        # Likewise a lettered item inside a subpart, "6(a)(i)(a)" then "6(a)(i)(b)" (0580
        # Paper 3/4: the question paper marks 6(a)(i) once, its (a) and (b) inside it).
        groups = match_label(label.text).groupdict()
        piece, item = groups.get("piece"), groups.get("item")
        if ((piece and int(piece) > 1) or (item and item.lower() > "a")) and ids and ids[-1][0] == entry_id:
            ids.append((entry_id, question, path))
            continue
        # Real-paper finding #2 (0620/42 FM21): after 1(a)(v), rows labelled "1(v)(i)",
        # "1(v)(ii)", "1(v)(iii)" - the numerals vi, vii, viii split in two. When the two
        # pieces joined make the numeral after the previous row's, that's what it is.
        if ids and ids[-1][1] == question and path.count(".") == 1:
            prev_head, _, prev_last = ids[-1][2].rpartition(".")
            joined = path.replace(".", "")
            if (
                prev_head
                # a different letter in front: 1(i)(ii) after 1(i)(i) is simply the next
                # subpart of part (i), never "iii" (0620/52 MJ22)
                and path.split(".")[0] != prev_head
                and prev_last in _ROMANS
                and joined in _ROMANS
                and _ROMANS.index(joined) >= _ROMANS.index(prev_last) + 1
            ):
                path = f"{prev_head}.{joined}"
                entry_id = f"{question}.{path}"
        same_page_repeat = (
            ids and entry_id == ids[-1][0] and path
            and (labels[index - 1].page == label.page or index in (fresh or ()))
        )
        if same_page_repeat:
            *head, last = path.split(".")
            gap_filler = _step(last, -1)
            before = ids[-2][2] if len(ids) >= 2 and ids[-2][1] == question else None
            if gap_filler and before is not None and before.split(".")[:-1] == head and before.split(".")[-1] == _step(gap_filler, -1):
                # (i), (iii), (iii): the first "(iii)" was the typo - it's (ii)
                fixed = ".".join([*head, gap_filler])
                ids[-1] = (f"{question}.{fixed}", question, fixed)
            else:
                bumped = _step(last, 1)
                if bumped:
                    path = ".".join([*head, bumped])
                    entry_id = f"{question}.{path}"
        elif path and ids and ids[-1][1] == question and any(entry_id == seen for seen, _, _ in ids[:-1]):
            # A label repeating an earlier, non-adjacent row: "2(b)" again after 2(e)
            # (0620/33 MJ24), meaning 2(f). It took the earlier row's id, so two entries
            # collided. Only when the lengths agree - (b) after (e), (a)(i) after (c)(ii).
            prev_path = ids[-1][2]
            if prev_path.count(".") == path.count("."):
                *head, last = prev_path.split(".")
                bumped = _step(last, 1)
                if bumped:
                    path = ".".join([*head, bumped])
                    entry_id = f"{question}.{path}"
        ids.append((entry_id, question, path))
    return ids


# Real-paper finding (0620/11-13 2016 multiple-choice schemes): older MCQ schemes aren't a
# Question/Answer/Marks table but an answer key printed two-up - "Question Number | Key |
# Question Number | Key", rows reading "1 D 21 A" - with no Marks column at all. The table
# parser took each right-hand question number for the left-hand row's marks (question 1
# worth 21) and never saw 21-40, so half the paper went unjoined and every part was left
# with no marks. Every item on such a key is worth one mark.
_KEY_ANSWER_RE = re.compile(r"^[A-E]$")
_KEY_REMOVED_RE = re.compile(r"^question\s+(?:removed|discounted)$", re.IGNORECASE)
_KEY_CELL_REACH = 9.0
_KEY_CELL_WIDTH = 150.0  # number to its key: 54pt on 0455/12 ON16; the next column is 180pt on
# what a withdrawn question's key row reads as: validate.is_removed_notice knows it
REMOVED_KEY = "Question removed"
ANSWER_KEY_MARKS = 1


def _answer_key(spans: list[MSSpan]) -> list[tuple[int, str]] | None:
    """(question number, key letter) for each item of an answer-key scheme, or None when
    the document isn't one. Recognised by a page headed "Key" with no "Marks" header, and
    read as each bare number paired with the letter immediately to its right."""
    key_pages = {s.page for s in spans if s.text == "Key"} - {
        s.page for s in spans if s.text.lower() == "marks"
    }
    if not key_pages:
        return None
    items: dict[int, str] = {}
    for span in spans:
        if span.page not in key_pages or not BARE_MARKS_RE.match(span.text):
            continue
        right = [
            s for s in spans
            if s.page == span.page and abs(s.y - span.y) <= ROW_TOLERANCE and s.x > span.x
        ]
        nearest = min(right, key=lambda s: s.x, default=None)
        if nearest is not None and _KEY_ANSWER_RE.match(nearest.text):
            items.setdefault(int(span.text), nearest.text)
            continue
        # a withdrawn question: "Question" over "Removed" in its Key cell, the two lines
        # centred on the number's row (0455/12 ON16 Q15; user-reported)
        cell = [
            s for s in spans
            if s.page == span.page and abs(s.y - span.y) <= _KEY_CELL_REACH
            and span.x < s.x < span.x + _KEY_CELL_WIDTH and not BARE_MARKS_RE.match(s.text)
        ]
        if _KEY_REMOVED_RE.match(" ".join(s.text.strip() for s in sorted(cell, key=lambda s: s.y))):
            items.setdefault(int(span.text), REMOVED_KEY)
    return sorted(items.items()) or None


_OVERTYPED_RE = re.compile(r"^\d{3}$")
_OVERTYPED_REACH = 6.0  # the column's numbers are centred: 1-2 digits sit within 3pt of "113"'s centre


def _overtyped_numbers(spans: list[MSSpan]) -> list[MSSpan]:
    """A question number misprinted with a digit too many: "113" between "12" and "14" in
    the Question column (0455/12 FM20; user-reported) is 13. Read as no label at all, its
    row merged into 12's ("C 113 D", 12 worth 2) and 13 joined nothing. Taken only when
    dropping one digit gives the number its column's neighbours leave out - k-1 the label
    above, k+1 the one below, both centred with it - and k isn't printed there already."""
    def centre(s: MSSpan) -> float:
        return (s.bbox[0] + s.bbox[2]) / 2 if s.bbox[2] > s.bbox[0] else s.x

    fixed = []
    for span in spans:
        text = span.text.strip()
        if _OVERTYPED_RE.match(text):
            column = [
                s for s in spans
                if s.page == span.page and s is not span and BARE_MARKS_RE.match(s.text.strip())
                and abs(centre(s) - centre(span)) <= _OVERTYPED_REACH
            ]
            above = max((s for s in column if s.y < span.y), key=lambda s: s.y, default=None)
            below = min((s for s in column if s.y > span.y), key=lambda s: s.y, default=None)
            printed = {s.text.strip() for s in column}
            for k in {text[1:], text[:2], text[0] + text[2]}:
                if (
                    not k.startswith("0") and k not in printed
                    and above is not None and above.text.strip() == str(int(k) - 1)
                    and below is not None and below.text.strip() == str(int(k) + 1)
                ):
                    span = span.model_copy(update={"text": k})
                    break
        fixed.append(span)
    return fixed


def parse_mark_scheme(pdf_path: object) -> list[MarkSchemeEntry]:
    doc = pymupdf.open(pdf_path)
    try:
        all_spans = _overtyped_numbers(_join_split_labels(_rotated_spans(doc, unbracket=True)))
        key = _answer_key(all_spans)
        if key is not None:
            return [
                MarkSchemeEntry(
                    id=f"{number}.stem", paper_key="", question_number=number,
                    part_path="", answer_text=letter, marks=ANSWER_KEY_MARKS,
                )
                for number, letter in key
            ]
        if markscheme_paper.is_paper_layout(all_spans, doc):
            return markscheme_paper.entries(doc, all_spans)
        spans, labels = _find_labels(all_spans)
        partial_marks = _has_partial_marks_column(all_spans)
        codes = _codes_only(all_spans)
        # a column of codes has no numbers to place: any it'd find are answers' (cm², 14)
        marks_column_x = None if codes else _marks_column_x(labels, spans)
        page_columns = {} if codes else _page_marks_columns(labels, spans, marks_column_x)

        def column_x(page: int) -> float | None:
            return None if marks_column_x is None else page_columns.get(page, marks_column_x)
        column_right = _marks_column_right(labels, spans, codes)
        # codes under one header don't share an edge ("A1" 18pt left of the "M1" above it,
        # 0625/32 ON25 6(d)), so a column of codes is read by its header where there is one
        headers = _marks_headers(spans) if codes else {}
        # numbers too, by the header where they wander: 0610 schemes set a row's "1" at x 525,
        # 536 or 544 under a header at 521 - the median took 525, and 544 fell outside it
        number_headers = {} if codes else _marks_headers(spans)

        entries = []
        # a label reprinted at the top of the next page is its row carried over - with no mark
        # beside it. With one, it's the next row misprinted: "2(c)(i)" again on 0610/62
        # MJ17's last page, for 2(c)(ii)
        fresh = {
            i for i in range(1, len(labels))
            if labels[i].text == labels[i - 1].text
            and labels[i].page != labels[i - 1].page
            and _has_marks_partner(labels[i].model_copy(update={"marks_below": False}), spans, codes)
        }
        ids = _entry_ids(labels, fresh)
        # a row starts on its label's printed line, which can sit a point higher than the
        # label: 0580/12 MJ16's "(0, –3)" at y 271 beside "16 (a)" at 272 went to 15(b)
        # And a label centred in its cell starts its row at the first line's mark, up to
        # _CENTRED_LABEL_LEAD higher: "19" at y 441, its "2" and "B1 for each" at 435 (0580/11
        # MJ20) - that line went to Q18 and Q19 read no mark.
        def line_of(label: MSSpan) -> tuple[int, float]:
            lead = [
                s.y for s in spans
                if s.page == label.page
                and label.y - _CENTRED_LABEL_LEAD <= s.y < label.y
                and s.x > label.x
                and (
                    (column_x(s.page) is not None and MARKS_VALUE_RE.match(s.text)
                     and abs(s.x - column_x(s.page)) <= MARKS_COLUMN_TOLERANCE)
                    or _is_column_code(s, column_right)
                )
            ]
            return (label.page, min([label.y, *lead]) - _SAME_LINE)

        for i, label in enumerate(labels):
            end = line_of(labels[i + 1]) if i + 1 < len(labels) else None
            row_spans = sorted(
                (
                    s
                    for s in spans
                    if (s.page, s.y) >= line_of(label) and (end is None or (s.page, s.y) < end)
                ),
                # a span just above the label's line is on it - a superscript, "K⁺" - and reads
                # in its place, not first: "+ M2 potassium" hid 0620/52 MJ26's marking point
                key=lambda s: (
                    s.page,
                    label.y if s.page == label.page and label.y - _SAME_LINE <= s.y < label.y else s.y,
                    s.x,
                ),
            )

            column_spans = [
                s
                for s in row_spans
                if (
                    column_x(s.page) is not None
                    and MARKS_VALUE_RE.match(s.text)
                    and (
                        abs(s.x - column_x(s.page)) <= MARKS_COLUMN_TOLERANCE
                        or _under_header(s, number_headers)
                    )
                )
                or (_is_row_code(s, headers) if s.page in headers else _is_column_code(s, column_right))
            ]
            # "Total: 21" set as two spans: the question's total, not this row's mark (0610/62
            # MJ17 2(c)(iii) read 26)
            totals = [t for t in row_spans if t.text.rstrip().lower().rstrip(":") == "total"]
            column_spans = [
                c for c in column_spans
                if not any(t.page == c.page and abs(t.y - c.y) <= ROW_TOLERANCE and t.x < c.x for t in totals)
            ]
            marks = _row_marks(column_spans, partial_marks=partial_marks)

            excluded = {label.position} | {s.position for s in column_spans}
            answer_text = " ".join(
                s.text
                for s in row_spans
                if s.position not in excluded and not BOILERPLATE_RE.match(s.text)
            ).strip()

            entry_id, question_number, part_path = ids[i]

            if entries and entries[-1].id == entry_id:
                # the same row carried over a page break (see _entry_ids): one entry
                entries[-1].marks += marks
                entries[-1].answer_text = f"{entries[-1].answer_text} {answer_text}".strip()
                continue
            printed = match_label(label.text)
            printed_path = ".".join(g.lower() for g in (printed.group("part"), printed.group("subpart")) if g)
            entries.append(
                MarkSchemeEntry(
                    id=entry_id,
                    paper_key="",
                    question_number=question_number,
                    part_path=part_path,
                    answer_text=answer_text,
                    marks=marks,
                    printed_path=printed_path if printed_path != part_path else None,
                    also_paths=[combined["also"]] if (combined := _COMBINED_PARTS_RE.match(label.text)) else [],
                )
            )
        _split_shared_marks(entries)
        return entries
    finally:
        doc.close()


_ROW_CODE_RE = re.compile(r"\b(?:M|A|B|C)\d\b")


def _split_shared_marks(entries: list[MarkSchemeEntry]) -> None:
    """One Marks cell merged over several subparts' rows, its number printed beside the
    first: 0620/42 ON18 2(b)(i) "M1 Same number of outer electrons" | 2, then 2(b)(ii) "M2
    (Sr has) outer electrons are in the 5th shell" with a blank cell (user-reported - (i)
    read 2, (ii) 0, against the QP's one [2] for both). Split by the rows' own marking
    codes, one mark each, only where every row in the run has codes, the run shares a
    parent part, and the codes add up to the mark printed."""
    i = 0
    while i < len(entries):
        first = entries[i]
        parent = first.part_path.rsplit(".", 1)[0] if "." in first.part_path else None
        run = [first]
        while (
            parent is not None
            and i + len(run) < len(entries)
            and (nxt := entries[i + len(run)]).marks == 0
            and nxt.question_number == first.question_number
            and nxt.part_path.rsplit(".", 1)[0] == parent
            and "." in nxt.part_path
            and nxt.answer_text
        ):
            run.append(nxt)
        if len(run) > 1 and first.marks > 0:
            codes = [len(_ROW_CODE_RE.findall(e.answer_text)) for e in run]
            if all(codes) and sum(codes) == first.marks:
                for entry, n in zip(run, codes):
                    entry.marks = n
        i += len(run)


# ---------------------------------------------------------------------------------------
# Layout: where each row sits in the source, so quiz_export.py can copy it as-is.
#
# Coordinates here are *upright* - after the page's rotation, the way a reader sees it -
# which is also the raw coordinate space of the page once `pymupdf.Page.remove_rotation()`
# has been applied. quiz_export.py copies from such a de-rotated handle, so these boxes
# can be used as clip rectangles directly.
# ---------------------------------------------------------------------------------------

# A table rule is a hairline: every rule measured across the corpus is a filled rectangle
# 0.5pt tall. Anything taller is a cell's fill (the grey header) or a drawing.
RULE_MAX_THICKNESS = 1.5
# A row separator runs the table's full width; a rule inside an answer (a table or graph
# the answer contains) doesn't. Cambridge draws each separator as one rectangle per cell,
# so coverage is measured as the union of every hairline at that height.
SEPARATOR_MIN_COVERAGE = 0.9


class MSRowLayout(BaseModel):
    # one region per page the row occupies, each including the rules above and below it
    segments: list[BBox]
    label: LabelRef


class MSLayout(BaseModel):
    rows: dict[str, MSRowLayout] = {}
    # the "Question | Answer | Marks" header row, copied above each question's table
    header: BBox | None = None
    # x of the table's vertical rules, left to right: its left edge, Question|Answer,
    # Answer|Marks, its right edge - measured on the header row. A maths scheme has a
    # fifth, for its "Partial Marks" column.
    columns: list[float] = []
    # which column (index into the gaps between `columns`) is headed "Marks"
    marks_column: int | None = None
    # the table pages' orientation - multiple-choice and maths schemes are portrait
    landscape: bool = True
    # the marking-principles pages ahead of the table, with the board's running header
    # and footer cut away
    preamble: list[BBox] = []
    # each preamble page's first line ("Generic Marking Principles", "MARK SCHEME NOTES",
    # "GENERIC MARKING PRINCIPLE 5:") - what a composed scheme dedupes them by
    preamble_titles: list[str] = []
    # Where a scheme sets its table differently page to page (Edexcel's do, by up to 20pt),
    # each page's own column rules - and `regrid` asks a composed scheme to set every row
    # in one grid, copying cell by cell, rather than pasting rows that don't line up.
    page_columns: dict[int, list[float]] = {}
    regrid: bool = False
    # the header's words in each column ("Question number", "Answer", "Notes", "Marks"),
    # for drawing it in the grid rather than copying the source's
    column_names: list[str] = []


class _Rule(BaseModel):
    y0: float
    y1: float
    x0: float
    x1: float


def _hairlines(page: pymupdf.Page) -> list[pymupdf.Rect]:
    matrix = page.rotation_matrix
    rects = []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "re":
                rect = pymupdf.Rect(item[1]) * matrix
            elif item[0] == "l":
                rect = pymupdf.Rect(item[1] * matrix, item[2] * matrix)
            else:
                continue
            rect.normalize()
            if rect.height <= RULE_MAX_THICKNESS and rect.width > 0:
                rects.append(rect)
            elif item[0] == "re" and "s" in (drawing.get("type") or ""):
                # a stroked (not filled) cell border contributes its top and bottom edges
                rects.append(pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0))
                rects.append(pymupdf.Rect(rect.x0, rect.y1, rect.x1, rect.y1))
    return rects


def _column_rules(page: pymupdf.Page, band: BBox) -> list[float]:
    """x centres of the vertical rules crossing `band`, merged within a point."""
    matrix = page.rotation_matrix
    xs = []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "re":
                rect = pymupdf.Rect(item[1]) * matrix
            elif item[0] == "l":
                rect = pymupdf.Rect(item[1] * matrix, item[2] * matrix)
            else:
                continue
            rect.normalize()
            if rect.width <= RULE_MAX_THICKNESS and rect.height > 5 and rect.y0 < band.y1 and rect.y1 > band.y0:
                xs.append((rect.x0 + rect.x1) / 2)
    merged: list[float] = []
    for x in sorted(xs):
        if not merged or x - merged[-1] > 1.0:
            merged.append(x)
    return merged


def _separators(page: pymupdf.Page) -> list[_Rule]:
    """Full-width horizontal rules on a page, top to bottom."""
    lines = sorted(_hairlines(page), key=lambda r: r.y0)
    if not lines:
        return []
    table_x0 = min(r.x0 for r in lines)
    width = max(r.x1 for r in lines) - table_x0

    groups: list[list[pymupdf.Rect]] = []
    for rect in lines:
        if groups and rect.y0 - groups[-1][0].y0 <= 1.0:
            groups[-1].append(rect)
        else:
            groups.append([rect])

    rules = []
    for group in groups:
        covered, reach = 0.0, None
        for rect in sorted(group, key=lambda r: r.x0):
            start = rect.x0 if reach is None else max(rect.x0, reach)
            covered += max(rect.x1 - start, 0.0)
            reach = rect.x1 if reach is None else max(reach, rect.x1)
        # A separator between rows crosses the Question column too. Real-paper finding (0620
        # s25 MS 61): a rule between one row's marking points ("M1 methyl orange" / "M2
        # yellow to orange") spans Answer and Marks under a Question cell merged across
        # both - 91% of the width, enough to pass as a row break, so every multi-point row
        # was cut after its first point, with its bottom border broken under the label.
        crosses_question_column = min(r.x0 for r in group) <= table_x0 + 2
        if width > 0 and covered >= SEPARATOR_MIN_COVERAGE * width and crosses_question_column:
            rules.append(
                _Rule(
                    y0=min(r.y0 for r in group),
                    y1=max(r.y1 for r in group),
                    x0=min(r.x0 for r in group),
                    x1=max(r.x1 for r in group),
                )
            )
    return rules


def _rule_at_or_above(rules: list[_Rule], y: float) -> _Rule | None:
    above = [r for r in rules if r.y0 <= y + 0.5]
    return above[-1] if above else None


def _rule_below(rules: list[_Rule], y: float) -> _Rule | None:
    return next((r for r in rules if r.y0 >= y - 0.5), None)


def _band(page_index: int, top: _Rule, bottom: _Rule) -> BBox:
    return BBox(
        page=page_index,
        x0=min(top.x0, bottom.x0) - 1,
        y0=top.y0 - 0.25,
        x1=max(top.x1, bottom.x1) + 1,
        y1=bottom.y1 + 0.25,
    )


def _row_segments(
    label: MSSpan,
    labels: list[MSSpan],
    rules_by_page: dict[int, list[_Rule]],
    headers_by_page: dict[int, list[MSSpan]],
    last_page: int,
) -> list[BBox]:
    """The label's own cell band, plus the band at the top of each following page for as
    long as the row runs on - a long answer carries over a page break with no label of its
    own on the new page, beneath a repeated header (or none)."""
    rules = rules_by_page.get(label.page, [])
    top = _rule_at_or_above(rules, label.bbox[1])
    bottom = _rule_below(rules, label.bbox[3])
    if top is None or bottom is None:
        return []
    segments = [_band(label.page, top, bottom)]

    page = label.page
    while bottom is rules[-1] and page < last_page:
        page += 1
        rules = rules_by_page.get(page, [])
        if len(rules) < 2:
            break
        top = rules[0]
        headers = headers_by_page.get(page, [])
        if headers and headers[0].bbox[1] < rules[1].y0:
            # the page opens on a repeated header row: the carried-over cell sits below it
            below_header = _rule_below(rules, headers[0].bbox[3])
            if below_header is None:
                break
            top = below_header
        following = [r for r in rules if r.y0 > top.y1]
        if not following:
            break
        bottom = following[0]
        if any(other.page == page and top.y1 <= other.bbox[1] < bottom.y0 for other in labels):
            break  # the next row starts here - nothing carried over
        segments.append(_band(page, top, bottom))
    return segments


def _body_band(spans: list[MSSpan], page_index: int, height: float) -> tuple[float, float]:
    """(top, bottom) of a page's body: below its running header, above its footer."""
    furniture = [s for s in spans if s.page == page_index and BOILERPLATE_RE.match(s.text)]
    top = max((s.bbox[3] for s in furniture if s.bbox[3] < height / 4), default=0.0)
    bottom = min((s.bbox[1] for s in furniture if s.bbox[1] > height * 3 / 4), default=height)
    return top, bottom


def _label_ref(span: MSSpan) -> LabelRef:
    return LabelRef(
        page=span.page,
        x0=span.bbox[0],
        y0=span.bbox[1],
        x1=span.bbox[2],
        y1=span.bbox[3],
        size=span.size,
        bold=span.bold,
        font=span.font,
        origin_x=span.origin[0],
        origin_y=span.origin[1],
    )


def parse_mark_scheme_layout(pdf_path: object) -> MSLayout:
    """Where every row, the table header and the marking-principles pages sit in the
    source mark scheme - so a composed mark scheme can copy them rather than re-typeset
    extracted text, which can't carry subscripts, bold mark codes or sketched answers."""
    doc = pymupdf.open(pdf_path)
    try:
        all_spans = _join_split_labels(_rotated_spans(doc, unbracket=True))
        if _answer_key(all_spans) is not None:
            # no rows to copy: a key's cells hold two items each, so composed schemes set
            # the letters from extracted text instead
            return MSLayout()
        if markscheme_paper.is_paper_layout(all_spans, doc):
            # no table: rows copied without a header or columns, portrait as printed
            return MSLayout(rows=markscheme_paper.rows(doc), landscape=doc[0].rect.width > doc[0].rect.height)
        spans, labels = _find_labels(all_spans)
        if not labels:
            return MSLayout()
        table_pages = sorted({s.page for s in spans})
        rules_by_page = {p: _separators(doc[p]) for p in table_pages}
        headers_by_page: dict[int, list[MSSpan]] = {}
        for span in spans:
            if span.text.lower() == "question":
                headers_by_page.setdefault(span.page, []).append(span)
        for headers in headers_by_page.values():
            headers.sort(key=lambda s: s.bbox[1])

        rows: dict[str, MSRowLayout] = {}
        for label, (entry_id, _, _) in zip(labels, _entry_ids(labels)):
            segments = _row_segments(label, labels, rules_by_page, headers_by_page, table_pages[-1])
            if segments and entry_id in rows:
                # a row carried over a page break with its label repeated: one row, both parts
                rows[entry_id].segments.extend(segments)
            elif segments:
                rows[entry_id] = MSRowLayout(segments=segments, label=_label_ref(label))

        header = None
        for page in table_pages:
            if headers_by_page.get(page):
                first = headers_by_page[page][0]
                top = _rule_at_or_above(rules_by_page[page], first.bbox[1])
                bottom = _rule_below(rules_by_page[page], first.bbox[3])
                if top is not None and bottom is not None:
                    header = _band(page, top, bottom)
                    break
        columns = _column_rules(doc[header.page], header) if header is not None else []
        marks_column = None
        if header is not None:
            for span in spans:
                if span.page == header.page and span.text.lower() == "marks" and header.y0 <= span.bbox[1] <= header.y1:
                    centre = (span.bbox[0] + span.bbox[2]) / 2
                    marks_column = next(
                        (i for i in range(len(columns) - 1) if columns[i] <= centre < columns[i + 1]), None
                    )
                    break
        table_page = doc[header.page if header is not None else labels[0].page].rect
        landscape = table_page.width > table_page.height

        # Everything between the cover and the first table: the generic and subject-
        # specific marking principles, plus any instructions set above the first table.
        preamble, titles = [], []
        first_table_page = labels[0].page
        for page in range(1, first_table_page + 1):
            width, height = doc[page].rect.width, doc[page].rect.height
            top, bottom = _body_band(all_spans, page, height)
            if page == first_table_page:
                table_top = _rule_at_or_above(rules_by_page.get(page, []), labels[0].bbox[1])
                if header is not None and header.page == page and header.y1 <= labels[0].bbox[1]:
                    bottom = header.y0
                elif table_top is not None:
                    bottom = table_top.y0
            body = sorted(
                (
                    s
                    for s in all_spans
                    if s.page == page and s.bbox[1] >= top and s.bbox[3] <= bottom
                    and not BOILERPLATE_RE.match(s.text)
                ),
                key=lambda s: (round(s.bbox[1]), s.bbox[0]),
            )
            if body:
                preamble.append(BBox(page=page, x0=0, y0=top + 1, x1=width, y1=bottom - 1))
                first_line = round(body[0].bbox[1])
                titles.append(" ".join(s.text for s in body if round(s.bbox[1]) == first_line))

        return MSLayout(
            rows=rows,
            header=header,
            columns=columns,
            marks_column=marks_column,
            landscape=landscape,
            preamble=preamble,
            preamble_titles=titles,
        )
    finally:
        doc.close()
