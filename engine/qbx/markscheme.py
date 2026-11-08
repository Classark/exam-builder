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

import pymupdf
from pydantic import BaseModel

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
_LENIENT_LABEL_RE = re.compile(
    r"^(?P<question>\d{1,2})\s*\(?\s*(?P<part>[a-z])\s*\)?"
    r"(?:\s*\(?\s*(?P<subpart>(?i:i|ii|iii|iv|v|vi|vii|viii))\s*\)?)?(?:(?<=\))[iI])?$"
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
    r"\((?:i|ii|iii|iv|v|vi|vii|viii)\)\s*(?:,|&|and|–|-)\s*\((?:i|ii|iii|iv|v|vi|vii|viii)\)$"
)


def match_label(text: str) -> re.Match | None:
    """A row label, strictly formed or with one of the malformations above."""
    return (
        ROW_LABEL_RE.match(text) or _LENIENT_LABEL_RE.match(text) or _QUESTIONLESS_LABEL_RE.match(text)
        or _COMBINED_LABEL_RE.match(text)
    )


ROW_LABEL_RE = re.compile(
    r"^(?P<question>\d{1,2})(\((?P<part>[a-z])\))?(\((?P<subpart>i|ii|iii|iv|v|vi|vii|viii)\))?$"
)
BARE_MARKS_RE = re.compile(r"^\d{1,2}$")
# A Marks-column value. Real-paper finding (0620 w18 MS 41): "any three from:" rows carry
# "max 3" in the Marks column instead of a bare digit, so requiring BARE_MARKS_RE there
# rejected 3(d)(ii) and 6(a)(iii) as labels and merged their answers into the row above.
MARKS_VALUE_RE = re.compile(r"^(?:max\s*)?(?P<marks>\d{1,2})$", re.IGNORECASE)

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
    r"^(?P<marks>\d{1,2})\s+(?P<code>(?:(?:SC|[MABC])\d+|FT"
    r"|" + _GUIDANCE_WORDS +
    # and the one-letter forms its abbreviations table defines - A allow, I ignore, R
    # reject - which 0620/42 MJ26 glues on too ("1 A 'sodium'"). Splitting one off is
    # harmless even where it'd be an answer letter: the two are separate columns anyway.
    r"|[AIR])\b.*"
    # or a note's heading: "4 Remember: Answers must be different" (0417/11 MJ26 Q1)
    r"|[A-Z][a-z]+:.*)$"
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
# Codes are left-aligned where numbers are right-aligned, so a code in the Marks column is
# placed by its right edge: measured 2pt from the numbers' right edge in 0580 m25 MS 22,
# against 27pt for the nearest Partial Marks code.
MARK_CODE_EDGE_TOLERANCE = 6.0


def _split_marks_and_code(span: dict) -> list[dict]:
    """Splits a "2 B1"-style span into its marks value and the code after it, positioning
    the code by the measured width of what precedes it."""
    text = span["text"].strip()
    match = _MARKS_THEN_CODE_RE.match(text) or _CODE_THEN_GUIDANCE_RE.match(text)
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


def _rotated_spans(doc: object) -> list[MSSpan]:
    spans = []
    for page_index, page in enumerate(doc):
        matrix = page.rotation_matrix
        for block in page_blocks(page):
            for line in block.get("lines", []):
                for span in (piece for raw in line["spans"] for piece in _split_marks_and_code(raw)):
                    text = span["text"].strip()
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


def _join_split_labels(spans: list[MSSpan]) -> list[MSSpan]:
    """A label set as two spans - its number, then "(a)" or "(b)(i)" - joined back into one.
    0625/61 MJ26 prints most of question 3 this way; each piece alone read as question 3's
    own row, three times over, and the parts went unjoined."""
    out = []
    used = set()
    for i, span in enumerate(spans):
        if i in used:
            continue
        if BARE_MARKS_RE.match(span.text):
            for j in range(i + 1, min(i + 4, len(spans))):
                piece = spans[j]
                if (
                    j not in used
                    and piece.page == span.page
                    and abs(piece.y - span.y) <= ROW_TOLERANCE
                    and 0 < piece.x - span.x <= _LABEL_PIECE_GAP
                    and piece.text.startswith("(")
                    and match_label(span.text + piece.text)
                ):
                    used.add(j)
                    span = span.model_copy(update={
                        "text": span.text + piece.text,
                        # the row starts at its higher piece, or text beside it sorts above
                        "y": min(span.y, piece.y),
                        "bbox": (
                            min(span.bbox[0], piece.bbox[0]), min(span.bbox[1], piece.bbox[1]),
                            max(span.bbox[2], piece.bbox[2]), max(span.bbox[3], piece.bbox[3]),
                        ),
                    })
                    break
        out.append(span)
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
    return any(
        s.page == candidate.page
        and abs(s.y - candidate.y) <= ROW_TOLERANCE
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
    ordered = sorted(candidates, key=lambda s: s.x)
    groups = [[ordered[0]]]
    for span in ordered[1:]:
        if span.x - groups[-1][-1].x <= tolerance:
            groups[-1].append(span)
        else:
            groups.append([span])
    return groups[0]


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


def _row_marks(column_spans: list[MSSpan]) -> int:
    """Real-paper finding (0620 w18 MS 41): some mark schemes print one value per marking
    point - M1..M5 each with its own "1" in the Marks column - rather than a single row
    total, so taking one value read a 5-mark row as 1 (MS total 65 vs the paper's 80).
    Sum every value in the column; a "max N" caps the row outright, since it sits beside a
    list of creditworthy points that is deliberately longer than N."""
    capped = [s for s in column_spans if s.text.lower().startswith("max")]
    if capped:
        return int(MARKS_VALUE_RE.match(capped[0].text)["marks"])
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
    return bool(MARK_CODE_RE.match(span.text)) and abs(span.bbox[2] - column_right) <= MARK_CODE_EDGE_TOLERANCE


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

    # A row has one label, its leftmost. Real-paper finding (0620/33 ON19): an answer that is
    # just a number - "5" beside 2(d)(ii), "36" beside 5(b)(iv) - starts the Answer column a
    # couple of dozen points right of the label, and labels' own x spreads enough (86-105pt
    # here) for the Question-column cluster to chain out to it. Each became a row "5" /
    # "36" of its own, splitting its real row and leaving an orphan.
    labels = [
        label
        for label in labels
        if not any(
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


def _entry_ids(labels: list[MSSpan]) -> list[tuple[str, int, str]]:
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
            ids and entry_id == ids[-1][0] and path and labels[index - 1].page == label.page
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
    return sorted(items.items()) or None


def parse_mark_scheme(pdf_path: object) -> list[MarkSchemeEntry]:
    doc = pymupdf.open(pdf_path)
    try:
        all_spans = _join_split_labels(_rotated_spans(doc))
        key = _answer_key(all_spans)
        if key is not None:
            return [
                MarkSchemeEntry(
                    id=f"{number}.stem", paper_key="", question_number=number,
                    part_path="", answer_text=letter, marks=ANSWER_KEY_MARKS,
                )
                for number, letter in key
            ]
        spans, labels = _find_labels(all_spans)
        codes = _codes_only(all_spans)
        # a column of codes has no numbers to place: any it'd find are answers' (cm², 14)
        marks_column_x = None if codes else _marks_column_x(labels, spans)
        column_right = _marks_column_right(labels, spans, codes)
        # codes under one header don't share an edge ("A1" 18pt left of the "M1" above it,
        # 0625/32 ON25 6(d)), so a column of codes is read by its header where there is one
        headers = _marks_headers(spans) if codes else {}
        # numbers too, by the header where they wander: 0610 schemes set a row's "1" at x 525,
        # 536 or 544 under a header at 521 - the median took 525, and 544 fell outside it
        number_headers = {} if codes else _marks_headers(spans)

        entries = []
        ids = _entry_ids(labels)
        for i, label in enumerate(labels):
            end = labels[i + 1].position if i + 1 < len(labels) else None
            row_spans = sorted(
                (
                    s
                    for s in spans
                    if s.position >= label.position and (end is None or s.position < end)
                ),
                key=lambda s: s.position,
            )

            column_spans = [
                s
                for s in row_spans
                if (
                    marks_column_x is not None
                    and MARKS_VALUE_RE.match(s.text)
                    and (
                        abs(s.x - marks_column_x) <= MARKS_COLUMN_TOLERANCE
                        or _under_header(s, number_headers)
                    )
                )
                or (_is_row_code(s, headers) if s.page in headers else _is_column_code(s, column_right))
            ]
            marks = _row_marks(column_spans)

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
                )
            )
        return entries
    finally:
        doc.close()


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
        all_spans = _join_split_labels(_rotated_spans(doc))
        if _answer_key(all_spans) is not None:
            # no rows to copy: a key's cells hold two items each, so composed schemes set
            # the letters from extracted text instead
            return MSLayout()
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
