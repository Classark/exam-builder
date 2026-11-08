"""Edexcel International GCSE mark schemes - a different table from Cambridge's, so a parser
of its own (CLAUDE.md: never reuse the question parser, and the Cambridge MS parser's
column and label heuristics don't hold here either).

Measured on 92 Edexcel Chemistry schemes, Jan 2012 - June 2025:

- Four column sets, all ruled: Question number | Answer | Notes | Marks (most);
  … | Expected Answer | Accept | Reject | Marks (2012-15); … | Answer | Marks;
  … | Answer | Additional guidance | Marks. Some pages are rotated 90°, some aren't.
- **Columns come from the table's vertical rules**, not the header words: a header is
  centred over its column (2025: "Notes" at x 422, its text from x 374), and some pages
  print no header at all. The first column is the label, the last the marks, the second
  the answer; the rest are guidance, named by their header where there is one.
- A label is split over spans and lines - `4` `(a)` `(i)` side by side, or `2 (a)` over
  `(i)`, or `1 a` / `i` without brackets - and a row often prints only the part that
  changed (`(ii)` under `4 (a) (i)`). So labels are read as a running path: a number
  starts a question, a letter a part, a numeral a subpart.
- A heading line (`2 (a)` with its subparts below) has no answer and no marks: it only
  sets the path.
- "Total for Question 7 = 7 marks" rows end a question; their number isn't a row's mark.
- Marks are printed once per row (` 2 `), sometimes one per marking point; they're summed.
"""

import re
from pathlib import Path

import pymupdf

from qbx.markscheme import MSLayout, MSRowLayout, MSSpan, _rotated_spans
from qbx.schema import BBox, LabelRef, MarkSchemeEntry

# a column rule is a thin vertical line or rect at least this tall, upright
_RULE_MIN_HEIGHT = 10.0
_RULE_MAX_WIDTH = 3.0
# rules within this of each other are one rule (a cell border drawn twice, 710 and 711)
_RULE_MERGE = 4.0
# a label line: its spans' tops within this of each other
_LINE_TOLERANCE = 4.0
# how far above its label a centred mark can sit: 12pt measured (June 2025 1C 5(b)(iii),
# 6(b), 6(c)); the next row down is never closer than 20
_MARK_LEAD = 15.0
_PAGE_MARGIN = 20.0
_LEADING_SPACE = 8.0
_GUIDANCE_RE = re.compile(r"marking guidance|marking guidelines|using the mark scheme", re.IGNORECASE)
_MARK_CODE_RE = re.compile(r"^[MABC]\d$")
# "1." - Mathematics A prints its question numbers with a full stop
# "ai", "aii", "biv": a part letter run into its numeral, no brackets (4MA1 1FR January
# 2021, 4MA0 3H June 2016 - dropped as not a label, each question lost its (a)(i) row)
_LABEL_PIECE_RE = re.compile(r"^(?:\d{1,2}\.?|\(?[a-h]\)?|\(?[ivx]{1,4}\)?|[a-h](?:i{1,3}|iv|vi{0,3}|ix)|[MABC]\d)$")

_NOTES_FOR_QUESTION_RE = re.compile(r"^notes?\s+(?:on|for)\s+question\s+\d{1,2}\b", re.IGNORECASE)


def _notes_bar(line_text: str, column: int | None, xs: list[float]) -> bool:
    """A notes block's heading across the table: "Notes on Question 6", "Notes for
    Question 7", wherever it's set (WDM11 January 2023: centred, and a line "11x + 8y ..."
    under it read as Q11) - or a "Notes" bar alone, in any column but the last, where
    Mathematics A heads its Notes column (WMA14 June 2022: the notes' "(c)(i)" under it
    read as Q2's)."""
    if _NOTES_FOR_QUESTION_RE.match(line_text):
        return True
    return line_text in ("notes", "notes:") and column not in (None, len(xs) - 2)
# International A Level closes a question with a lone "(6 marks)" in its Marks column
_LONE_MARKS_TOTAL = r"^\s*\(\s*\d{1,2}\s+marks?\s*\)\s*$"
_QUESTION_TOTAL_LINE_RE = re.compile(
    rf"\btotal\b[^0-9]*\d+\s*marks?\b|\btotal\s+for\s+question\b|{_LONE_MARKS_TOTAL}", re.IGNORECASE
)
_STARRED_RE = re.compile(r"^(\s*\d{1,2}\.?)\*(\s*)$")
_ROMANS = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]
_LABEL_TOKEN_RE = re.compile(r"\d{1,2}|\(?[a-z]{1,5}\)?", re.IGNORECASE)
# "3", "(3)", and Mechanics' "[3]" (International A Level WME01)
_MARKS_RE = re.compile(r"^[(\[]?(\d{1,2})[)\]]?$")
# The tag 4BI1 schemes set under each mark (4BI1 2B November 2020: "1" over "comp", "3" over
# "exp", or "6 exp" in one span). Half the Mark column was words, so a page took the column
# the page before had used, and every mark on it was lost.
_MARK_TAG_RE = re.compile(r"\s*\b(?:exp|grad|comp|cler)$", re.IGNORECASE)
# "Total for Question 7 = 7 marks", "Total = 11", "Total 6 marks" - not an answer that
# opens with the word: "(total) vol(CO2) = 480 x 140" (4CH0 2C January 2019, 8(a)(i))
_TOTAL_RE = re.compile(rf"^\(?\s*total\s*(?:for\b|=|\d|marks?\b|$)|{_LONE_MARKS_TOTAL}", re.IGNORECASE)
# bracketed markers (and a question number) leading a span, then the answer's own words
# - and the answer can open with a bracket of its own: "(iii) (F/it is) insoluble" (June 2021)
# - or bare, without brackets: "ii B", "ii hydroxide (ion)" (4CH0 1C June 2015). One style
# or the other, never both: "(ii) a circle around s" is (ii) and an answer opening "a"
# (4CH0 1C January 2017).
_GLUED_LABEL_RE = re.compile(
    # or number and letter run together, "4a Correct method..." (International A Level, WME01)
    r"^(\d{1,2}[a-h](?:[ivx]{1,4})?\s+|(?:\d{1,2}\s+)?(?:(?:\((?:[a-z]|[ivx]{2,5})\)\s*)+|(?:\b(?:[a-h]|[ivx]{1,4})\s+)+))(\S.*)$"
)
# how far below a numeral its wrapped last letter can sit: "ii" over "i" for (iii) in a
# narrow label cell, 13pt apart (4CH0 1C June 2015)
_WRAP_GAP = 16.0
# ...and within one ruled cell, a label's pieces can sit further apart (WDM11: 22pt)
_CELL_GAP = 40.0
# a label line's height, for finding its middle
_LABEL_HEIGHT = 12.0
_STACK_GAP = 14.0
_MAX_QUESTION_STEP = 5
# a header row's letters: "questionnumberanswernotesmarks" and its longer variants
_HEADER_MAX_LETTERS = 60
_PUSHED_MARKER_RE = re.compile(r"^(\((?:[a-h]|[ivx]{1,4})\))(?:\s+(\S.*))?$")
_HEADER_WORDS = {
    "question", "number", "question number", "answer", "expected answer", "notes", "accept",
    "reject", "marks", "mark", "additional guidance", "guidance", "acceptable answers",
    # Mathematics A (4MA0/4MA1): Question | Working | Answer | Mark | Notes
    "working",
    # International A Level: Question Number | Scheme | Marks
    "scheme",
    # ...headed "Qu | Scheme | Marks" (WST01 June 2023): the page's questions 1-3 were lost
    "qu",
}
# what a header calls its answer column
_ANSWER_WORDS = ("answer", "scheme")


def _names_answer(text: str) -> bool:
    return any(word in text for word in _ANSWER_WORDS)


def _drawings(page: pymupdf.Page) -> list[dict]:
    """The page's line art, read once: column rules, table bands and row rules all look at
    it, and on a Mathematics A scheme reading it three times tripled the parse."""
    cached = getattr(page, "_qbx_drawings", None)
    if cached is None:
        cached = page.get_drawings()
        page._qbx_drawings = cached
    return cached


def _rules(page: pymupdf.Page) -> dict[float, list[tuple[float, float]]]:
    """The page's vertical rules, upright: x -> the (top, bottom) of each piece drawn there."""
    matrix = page.rotation_matrix
    pieces: dict[float, list[tuple[float, float]]] = {}
    for drawing in _drawings(page):
        for item in drawing["items"]:
            if item[0] == "re":
                rect = pymupdf.Rect(item[1])
            elif item[0] == "l":
                rect = pymupdf.Rect(item[1], item[2])
            else:
                continue
            rect = rect * matrix
            rect.normalize()
            if rect.height < _RULE_MIN_HEIGHT or rect.width > _RULE_MAX_WIDTH:
                continue
            x = next((k for k in pieces if abs(k - rect.x0) <= _RULE_MERGE), rect.x0)
            pieces.setdefault(x, []).append((rect.y0, rect.y1))
    # 4CH0 2C January 2014 draws its whole (dotted) grid as images: thin strips, one per
    # rule, rather than lines - they count the same
    for image in page.get_image_info():
        rect = pymupdf.Rect(image["bbox"]) * matrix
        rect.normalize()
        if rect.height < _RULE_MIN_HEIGHT or rect.width > _RULE_MAX_WIDTH:
            continue
        x = next((k for k in pieces if abs(k - rect.x0) <= _RULE_MERGE), rect.x0)
        pieces.setdefault(x, []).append((rect.y0, rect.y1))
    return pieces


# a row separator: a thin horizontal line or rect, upright
_HRULE_MAX_HEIGHT = 3.0
_HRULE_MIN_WIDTH = 10.0
# How far above its label a row's first line can start. A label isn't always on its row's
# first line: 4MA0 3H January 2017 prints 12(b)(i)'s "180 - 48", "132", "1", "B1" 12pt above
# "(b) (i)", just under the rule that opens the row - read from the label down, they went
# to (a)(ii), and (b)(i) came out with no answer and no mark.
_ROW_RULE_GAP = 20.0


def _row_rules(page: pymupdf.Page, label_column: tuple[float, float]) -> list[float]:
    """The y of each horizontal rule crossing the label column, upright, top to bottom -
    the rules that separate rows (a rule only across the answer columns divides marking
    points inside one row)."""
    matrix = page.rotation_matrix
    x0, x1 = label_column
    ys = []
    for drawing in _drawings(page):
        for item in drawing["items"]:
            if item[0] == "re":
                rect = pymupdf.Rect(item[1])
            elif item[0] == "l":
                rect = pymupdf.Rect(item[1], item[2])
            else:
                continue
            rect = rect * matrix
            rect.normalize()
            if rect.height > _HRULE_MAX_HEIGHT or rect.width < _HRULE_MIN_WIDTH:
                continue
            if rect.x0 < x1 - 2 and rect.x1 > x0 + 2:
                ys.append((rect.y0 + rect.y1) / 2)
    return sorted(ys)


def _ruled_extent(page: pymupdf.Page, left_edges: list[float]) -> list[tuple[float, float]]:
    """Where the table's left edge is ruled, top to bottom: (top, bottom) of each stretch,
    pieces closer than _TABLE_GAP joined."""
    pieces = sorted(
        piece for x, spans in _rules(page).items() if any(abs(x - e) <= _SLIVER for e in left_edges) for piece in spans
    )
    extent: list[list[float]] = []
    for y0, y1 in pieces:
        if extent and y0 <= extent[-1][1] + _TABLE_GAP:
            extent[-1][1] = max(extent[-1][1], y1)
        else:
            extent.append([y0, y1])
    return [(y0, y1) for y0, y1 in extent]


def _ruled(extent: list[tuple[float, float]], y: float) -> bool:
    """Whether a line starting at y is inside the ruled table: below its top rule, and
    starting above its bottom one - notes can open right under it (WMA11 October 2024:
    "(a)" at 294.2 under a rule at 293.3)."""
    return any(y0 - 2 <= y < y1 for y0, y1 in extent)


def _boxed_tables(tables: dict, read: "_Read", spans: list[MSSpan]) -> tuple[dict, bool]:
    """Pages whose table is ruled down through its labelled rows, with where; and whether
    the scheme boxes its tables with text set outside them - a label at the label column
    outside the ruled table on such a page. Some schemes rule only their header (no label
    inside the ruling), and stay as they were."""
    extents, boxed = {}, False
    for page_index, (xs, _, _, _) in tables.items():
        extent = read.extents.get(page_index, [])
        labels = [
            s for s in spans
            if s.page == page_index and _column_of(s, xs) == 0 and _label_pieces(s.text.strip())
        ]
        if not any(_ruled(extent, s.bbox[1]) for s in labels):
            continue
        extents[page_index] = extent
        boxed = boxed or any(not _ruled(extent, s.bbox[1]) for s in labels)
    return (extents if boxed else {}), boxed


def _rule_above(rules: list[float], top: float, reach: float = _ROW_RULE_GAP) -> float | None:
    return max((y for y in rules if top - reach <= y <= top + 1), default=None)


# A table edge drawn as a double line (x 79 and 84, 4CH1 1CR June 2019, pages 17-19) is one
# boundary, not a 5pt column - which took the label column's place and lost question 7.
_SLIVER = 8.0


def _without_slivers(xs: list[float]) -> list[float]:
    kept: list[float] = []
    for x in xs:
        if not kept or x - kept[-1] >= _SLIVER:
            kept.append(x)
    return kept


def _covered(spans: list[tuple[float, float]]) -> float:
    covered, reach = 0.0, None
    for y0, y1 in sorted(spans):
        if reach is None or y0 > reach:
            covered += y1 - y0
            reach = y1
        elif y1 > reach:
            covered += y1 - reach
            reach = y1
    return covered


# tables on one page are this far apart at least (4MA0 3H January 2017 page 14: 26pt)
_TABLE_GAP = 8.0


def _table_bands(page: pymupdf.Page) -> list[tuple[float, float, list[float]]]:
    """Each separately ruled table on the page: its top, bottom and columns, from the
    rules running most of its own height. Mathematics A boxes every question in a table of
    its own, and sets their columns differently: 4MA0 3H January 2017 page 14 has 12's
    Answer | Mark | Notes at x 397 | 510 | 557 and 13's with an extra column. Measured
    over the page, neither table's rules ran far enough to count, and the page borrowed
    the last page's columns - which put every mark on it in the Notes."""
    pieces = [(y0, y1, x) for x, spans in _rules(page).items() for y0, y1 in spans]
    groups: list[list] = []
    for y0, y1, x in sorted(pieces):
        if groups and y0 <= groups[-1][1] + _TABLE_GAP:
            groups[-1][1] = max(groups[-1][1], y1)
            groups[-1][2].setdefault(x, []).append((y0, y1))
        else:
            groups.append([y0, y1, {x: [(y0, y1)]}])
    bands = []
    for top, bottom, by_x in groups:
        xs = _without_slivers(sorted(x for x, spans in by_x.items() if _covered(spans) >= 0.6 * (bottom - top)))
        if len(xs) >= 4:
            bands.append((top, bottom, xs))
    return bands


def _columns(page: pymupdf.Page, header: tuple[float, float] | None) -> list[float] | None:
    """The table's column boundaries, x upright, left to right; None for a page with no
    table (the cover, the marking guidance).

    Taken from the rules crossing the header row where there is one. Measured: some
    schemes rule only the header (January 2014 2C) - the body rows have no vertical rules
    at all - and others split the Question column into a cell per label piece (`2` | `(a)`
    | `(i)`, January 2016 2C), whose rules don't reach the header. Without a header, the
    rules running most of the table's height."""
    pieces = _rules(page)
    if not pieces:
        return None
    if header is not None:
        xs = sorted(
            x for x, spans in pieces.items()
            if any(y0 <= header[1] and y1 >= header[0] for y0, y1 in spans)
        )
        xs = _without_slivers(xs)
        return xs if len(xs) >= 4 else None  # label, answer, marks at least
    # how much of the page each rule covers - a union, since a border can be drawn twice
    # (the 2025 schemes' left edge, at x 75 and 76) or once per cell
    heights = {x: _covered(spans) for x, spans in pieces.items()}
    tallest = max(heights.values())
    xs = sorted(x for x, h in heights.items() if h >= 0.6 * tallest)
    xs = _without_slivers(xs)
    return xs if len(xs) >= 4 else None


def _text_columns(spans: list[MSSpan], header: list[MSSpan], width: float) -> list[float] | None:
    """Columns read from where the text lines up, for a table with no rules to read them
    from - 4CH0 2C January 2014 draws its dotted grid as images. Answer and Notes start at
    the nearest left edge many lines share at or before their header word (a header is
    centred over its column); the marks, centred too, sit right of their header's start."""
    from collections import Counter

    below = [s for s in spans if s.bbox[1] > max(h.bbox[3] for h in header)]
    words = {h.text.lower().strip(): h.bbox[0] for h in header}
    marks = next((x for w, x in words.items() if w.startswith("mark")), None)
    if marks is None:
        return None

    def shared(texts: list[MSSpan]) -> list[float]:
        edges = Counter(round(s.bbox[0] / 3) * 3 for s in texts)
        return sorted(x for x, n in edges.items() if n >= 3 and x < marks - 6)

    # the answer starts where text that isn't a label piece or a marking-point code lines up
    answers = shared([s for s in below if not _LABEL_PIECE_RE.match(s.text.strip())])
    if not answers:
        return None
    starts = [answers[0]]
    for word, word_x in sorted(words.items(), key=lambda item: item[1]):
        if _names_answer(word) or word.startswith("mark") or word in ("question", "number", "question number"):
            continue
        # a guidance column (Notes, Accept, Reject...) starts at the last edge before its word
        start = max((x for x in answers if x <= word_x + 2), default=None)
        if start is not None and start > starts[-1] + 40:
            starts.append(start)
    left = min(min(h.bbox[0] for h in header), min(s.bbox[0] for s in below)) - 8
    xs = [left, *(x - 2 for x in starts), marks - 6, width - _PAGE_MARGIN]
    return xs if len(xs) >= 4 else None


def _label_pieces(text: str, codes: bool = True) -> bool:
    """Every word of `text` a piece of a label: "3 (a)", "(i)", "1 a i" - not "graph".
    With `codes` off, a marking code isn't one: "ii M1" is a label and its answer's code.
    Brackets run together are pieces each: "(b)(i)" (4MA0 2F January 2018) was split as
    label "(b)" and answer "(i)", which lost every subpart of the question. So is a number
    run into its bracket: "3(a)", "4(c)(i)" (International A Level)."""
    words = re.sub(r"(\d)\(", r"\1 (", text).replace(")(", ") (").split()
    if not codes and any(w.rstrip(".").isdigit() for w in words[1:]):
        return False  # a question number leads a label: "(iii) 2" is (iii) and its answer, 2
    return bool(words) and all(
        _LABEL_PIECE_RE.match(w) and (codes or not _MARK_CODE_RE.match(w)) for w in words
    )


# a broken header's pieces are short: "An" "swer", "Acc" "cept", "Question number"
_HEADER_FRAGMENT = 16


def _is_header(line: list[MSSpan]) -> bool:
    words = [s.text.lower().strip() for s in line]
    if all(w in _HEADER_WORDS for w in words) and any(_names_answer(w) for w in words):
        return True
    # broken into fragments - "An" "swer" "Acc" "cept" "Reject" "Marks" (4CH0 2C June 2013,
    # page 13): its letters run together still say it
    # - but never a row: a header has no numbers or bracketed labels, and "(b) 14t - 6 1 B1
    # Mark response on answer line" (4MA0 4H January 2014 6(b)) says "mark" and "answer" too
    joined = "".join(words).replace(" ", "")
    if any(ch.isdigit() or ch == "(" for ch in joined):
        return False
    # and only fragments, never a sentence: "NB: All ranges for answers ... marks" above
    # 4MA0 4HR June 2014's table was taken for its header, and the page was lost with Q1
    if any(len(w) > _HEADER_FRAGMENT for w in words):
        return False
    # "answer" only: "scheme" counts in a line of nothing but header words (above), never
    # in prose - "mark scheme" in a note took 4MA0 3HR June 2017's page for a header
    return "answer" in joined and "mark" in joined and len(joined) <= _HEADER_MAX_LETTERS


def _column_of(span: MSSpan, xs: list[float]) -> int | None:
    # A few points in from its left edge: a span's box includes a leading space, so an
    # answer can start 6pt left of its own column's rule (4CH0 1C January 2015, x 95 against
    # 101) - but no further in than half a short span, so a "1" stays in the Marks column
    # and a label run into its answer stays in the label column.
    x = span.bbox[0] + min(_LEADING_SPACE, (span.bbox[2] - span.bbox[0]) / 2)
    for index in range(len(xs) - 1):
        if xs[index] - 2 <= x < xs[index + 1]:
            return index
    return None


def _label_steps(text: str, path: list) -> list | None:
    """The running path [question, part, subpart] after a label line, or None if the line
    isn't a label (an answer that strayed into the column, "Total", "Q1")."""
    # Two numbers in the label column are working, not a label: "4" "3" in Q3's notes read as
    # question 43 (WFM03 January 2025), "10" "2" as 32 (WMA11 June 2019), and every question
    # after was lost - and kept as "4", it took the place of the real "4(a)" a page on. A
    # question number is never split in two, nor followed by another.
    if re.match(r"^\s*\d{1,2}\.?\s+\d", text):
        return None
    raw = text.replace(" ", "").replace(".", "")  # "1. (a)" in Mathematics A
    tokens = _LABEL_TOKEN_RE.findall(raw)
    if not tokens or "".join(tokens) != raw:
        return None
    # Pearson's labels are lowercase: a capital is a table's in an answer - a precedence
    # table's activities "D", "I" down the label column read as a question 5 (WDM11 January
    # 2023). Not "I" in a numeral run ("Ii", Cambridge's typo, never Pearson's alone).
    if any(t.strip("()").isalpha() and t.strip("()").isupper() for t in tokens):
        return None
    question, part, subpart = path
    changed = False
    numeral_here = False  # a numeral read in this label: a letter after it nests under it
    letter_here = False  # a part letter read in this label: "1(h)(i)" is h's subpart (i)
    for token in tokens:
        word = token.strip("()").lower()
        if word.isdigit():
            # Question numbers only go forward, a step or so at a time. Anything else is a
            # number from the next column - a stacked fraction's "3" over "85" in the
            # Working column (4MA0 1FR January 2017 read questions 3, 4, 9 and 80 after 2).
            if question is not None and not question <= int(word) <= question + _MAX_QUESTION_STEP:
                return None
            # "3 iv" under 3(c)(iii) (4CH0 2C June 2015): the question number reprinted
            # at the top of a page, not a new question - a letter after it resets the part
            if int(word) != question:
                part, subpart = None, None
            question, changed = int(word), True
            continue
        # "2 a l": a lowercase L typed for the numeral i (4CH1 1CR June 2019) - read as one
        # wherever a numeral can stand, never after (k), where (l) is the next part
        if word.endswith("l") and (len(word) > 1 or (part is not None and part != "k")):
            word = word[:-1] + "i"
        pieces = [word]
        # "ai" (1ai): a letter run into its numeral
        if word not in _ROMANS and len(word) > 1 and word[1:] in _ROMANS:
            pieces = [word[0], word[1:]]
        elif word not in _ROMANS and len(word) > 1:
            return None
        for piece in pieces:
            # Numeral parts with letters inside them, "7(i)(a)", "(b)", "7(ii)(a)" (Further
            # Pure, WFM01 January 2025): the numeral part sits in the subpart slot with no
            # letter part ([7, None, "i"]); a letter under it nests ([7, "i", "a"], 7.i.a);
            # the next letter moves on within it, the next numeral starts the next part.
            # A label can print its numeral again: "(i)(b)", "(ii)(a)" (WMA11 January 2025) -
            # the letter after a numeral in the same label nests under it, whichever letter.
            nested = part in _ROMANS and subpart is not None and subpart not in _ROMANS
            letter = len(piece) == 1 and piece.isalpha() and not (piece == "i" and part is None)
            if letter and part is None and subpart in _ROMANS and (piece == "a" or numeral_here):
                part, subpart, changed = subpart, piece, True
                continue
            if nested and letter and piece not in _ROMANS and 0 < ord(piece) - ord(subpart) <= 2:
                subpart, changed = piece, True
                continue
            if nested and piece in _ROMANS and (len(piece) > 1 or len(tokens) > 1):
                part, subpart, changed, numeral_here = None, piece, True, True
                continue
            roman = piece in _ROMANS and (
                len(piece) > 1
                # "7(i)": a lone "i" straight after the question number is the numeral - the
                # letter (i) never comes first
                or (piece == "i" and part is None and subpart is None)
                or (piece == "i" and part is not None and (part != "h" or letter_here))
                or (piece == "v" and subpart == "iv")
                or (piece == "x" and subpart == "ix")
            )
            # A question numbering its parts (i), (ii) with no letters: the numeral after a
            # numeral standing as the part is the next part, not its subpart - "4 (i)" then
            # "(ii)" read as 4.i.ii (Mathematics A 4MA0)
            numeral_part = (
                roman and subpart is None and part in _ROMANS and part != "h"
                and _ROMANS.index(piece) == _ROMANS.index(part) + 1
            )
            if numeral_part:
                part = piece
            elif roman:
                subpart, numeral_here = piece, True
            else:
                # A part letter moves on from (a) a step or two at a time: "x", "y", "n",
                # "p", "t" are an answer's algebra (4MA1: "x = 4" orphaned as 12.x)
                # - and where one follows a question number ("12 x"), the label stops there
                if (piece not in "ab") if (part is None or part in _ROMANS) else (
                    piece != "a" and not 0 <= ord(piece) - ord(part) <= 2
                ):
                    return [question, part, subpart] if changed and question is not None else None
                if piece == "a" and part is not None and part > "a" and question is not None and not any(
                    t.strip("()").isdigit() for t in tokens
                ):
                    # "(a)" after 1(d), its question number left off - 4CH1 2CR January 2023
                    # starts question 2 on a new table that way. Parts never go back to (a)
                    # within a question, so this is the next one.
                    question += 1
                part, subpart, letter_here = piece, None, True
            changed = True
    if not changed or question is None:
        return None
    # "c" printed under "6 c i" (4CH0 2C June 2015): a part can't follow its own subpart, so
    # it's the next one, (ii), with its numeral left off
    if [question, part] == path[:2] and subpart is None and path[2] is not None:
        index = _ROMANS.index(path[2]) + 1 if path[2] in _ROMANS else None
        subpart = _ROMANS[index] if index is not None and index < len(_ROMANS) else None
    return [question, part, subpart]


def _lines(spans: list[MSSpan]) -> list[list[MSSpan]]:
    lines: list[list[MSSpan]] = []
    for span in sorted(spans, key=lambda s: (s.page, s.bbox[1], s.bbox[0])):
        if lines and lines[-1][0].page == span.page and abs(lines[-1][0].bbox[1] - span.bbox[1]) <= _LINE_TOLERANCE:
            lines[-1].append(span)
        else:
            lines.append([span])
    return [sorted(line, key=lambda s: s.bbox[0]) for line in lines]


class _Row:
    def __init__(self, path: list, page: int, y: float, label: list[MSSpan]):
        self.path = path
        self.start = (page, y)
        self.label = label
        self.answer: list[str] = []
        self.guidance: dict[str, list[str]] = {}
        self.marks: list[int] = []
        self.mark_codes: list[str] = []  # codes printed in the Marks column itself ("M1", "B1, B1")
        self.closing: list[int] = []  # bracketed part totals, "(3)" - one per way of answering
        self.extent: dict[int, list[float]] = {}  # page -> [top, bottom] of its content
        self.heading = False  # set once the row after it is known
        self.rule: float | None = None  # the rule that opens it, on its first page

    def cover(self, page: int, spans: list[MSSpan]) -> None:
        if not spans:  # a line wholly outside the table's columns
            return
        top, bottom = min(s.bbox[1] for s in spans), max(s.bbox[3] for s in spans)
        old = self.extent.setdefault(page, [top, bottom])
        old[0], old[1] = min(old[0], top), max(old[1], bottom)

    @property
    def entry_id(self) -> str:
        question, part, subpart = self.path
        part_path = ".".join(p for p in (part, subpart) if p)
        return f"{question}.{part_path or 'stem'}"


class _Read:
    """What one pass over a scheme finds: its rows, and the geometry composing needs."""
    def __init__(self):
        self.rows: list[_Row] = []
        self.columns: dict[int, list[float]] = {}  # page -> column x
        self.headers: dict[int, tuple[float, float]] = {}  # page -> header row top, bottom
        self.row_rules: dict[int, list[float]] = {}  # page -> row separators' y
        self.extents: dict[int, list[tuple[float, float]]] = {}  # page -> where the table is ruled
        self.totals: dict[int, int] = {}  # question -> its "Total n marks"
        self.sizes: dict[int, tuple[float, float]] = {}  # page -> width, height


def _read(pdf_path: Path | str) -> _Read:
    read = _Read()
    with pymupdf.open(pdf_path) as doc:
        # "4*": a question whose working must be shown, starred in its label (Mathematics A
        # from 2024; 4MA1 2H June 2025 lost 21 of its 26 questions' rows to it)
        spans = [
            s.model_copy(update={"text": _STARRED_RE.sub(r"\1\2", s.text)}) if _STARRED_RE.match(s.text) else s
            for s in _rotated_spans(doc)
        ]
        tables = {}
        # International A Level heads its Answer column "Scheme" - and is the layout that
        # sets notes after each question (the notes rules below)
        scheme_header = False
        previous = None
        for index, page in enumerate(doc):
            read.sizes[index] = (page.rect.width, page.rect.height)
            page_spans = [s for s in spans if s.page == index]
            # a line of nothing but header words first, then a broken one
            page_lines = _lines(page_spans)
            header = next(
                (line for line in page_lines if all(s.text.lower().strip() in _HEADER_WORDS for s in line) and _is_header(line)),
                None,
            ) or next((line for line in page_lines if _is_header(line)), None)
            band = (min(s.bbox[1] for s in header), max(s.bbox[3] for s in header)) if header else None
            scheme_header = scheme_header or (header is not None and any("scheme" in s.text.lower() for s in header))
            if header is None and previous is not None and previous[1] == page.rect.width:
                # A continuation page, without a header of its own: the last page's columns.
                # Not a guess from its own rules - 4CH0 2C June 2015 page 6 has only the
                # label column's inner cell rules (x 91, 115), which read as columns lost
                # every label on it. But its own rules where they draw the same table, moved:
                # 4CH1 2CR June 2025 page 8 sets its label column 13pt wider than page 7's,
                # and the borrowed columns put 4(b)(ii) in the Answer column.
                # And its own rules where they run the table's width, whatever their number:
                # the last page's table can read a column short (4MA0 2F January 2014 page 7
                # took page 6's merged Working | Answer, and its marks went to the answers)
                own = _columns(page, None)
                last = previous[0]
                full_width = own is not None and own[-1] - own[0] >= 0.8 * (last[-1] - last[0])
                xs = own if own is not None and (len(own) == len(last) or full_width) else last
            else:
                xs = _columns(page, band)
                if xs is None and header is not None:
                    xs = _text_columns(page_spans, header, page.rect.width)
            if xs is None:
                previous = None
                continue
            # Two tables ruled differently each read by their own columns (_table_bands);
            # a lone table as before. Header pages too: 4MA1 2H June 2025 page 5 boxes Q1
            # and Q2 each under a header of its own, and Q2's (a)/(b) sit where Q1's
            # Working starts - read by Q1's columns, (b)'s row merged into (a)'s.
            # A header page's own columns count as one of the tables: there, Q1's rules
            # are drawn in pieces and only Q2's table read as one.
            # And there only a table whose rules contradict the header's - one missing a
            # header column edge. One with extra inner rules (a label column split into
            # cells, a small table inside an answer: 4CH0 2C January 2016) is the header's
            # own table, and the header reads it right.
            bands = _table_bands(page)
            if header is not None:
                # a rule drawn a few points off is the same edge (4CH0 1C January 2012: 699
                # under a header ruled at 694)
                bands = [b for b in bands if any(min(abs(x - y) for y in b[2]) > _SLIVER for x in xs)]
            layouts = {tuple(round(x) for x in b[2]) for b in bands}
            if header is not None and bands:
                layouts.add(tuple(round(x) for x in xs))
            if len(layouts) < 2:
                bands = []
            if bands:
                bands = [(t, b, _label_cells_merged(xs_, [s for s in page_spans if t - 2 <= s.bbox[1] <= b])) for t, b, xs_ in bands]
                if header is None:
                    xs = bands[0][2]
            else:
                # header pages too: 4MA0 4HR June 2014 heads "Question" over a "16" cell and
                # an "(a)" cell, and its part letters went to the Working column
                xs = _label_cells_merged(xs, page_spans)
            # the next page carries on the page's last table, not its first
            previous = (bands[-1][2] if bands else xs, page.rect.width)
            # The whole page, not from the header down: a question can start a new table
            # mid-page, below the end of the last one (June 2025 1C: 5(d) above Q6's
            # header). Only the margins hold page furniture.
            tables[index] = (xs, _PAGE_MARGIN, page.rect.height - _PAGE_MARGIN, bands)
            read.columns[index] = xs
            read.row_rules[index] = _row_rules(page, (xs[0], xs[1]))
            read.extents[index] = _ruled_extent(page, [xs[0], *(b[2][0] for b in bands)])
            if band is not None:
                # the header as printed, for copying: "Question" over "number" sits a line
                # above and below the rest of it
                top, bottom = band
                for line in _lines(page_spans):
                    if all(s.text.lower().strip() in _HEADER_WORDS for s in line) and (
                        bottom - 30 < line[0].bbox[1] < bottom + 20
                    ):
                        top = min(top, min(s.bbox[1] for s in line))
                        bottom = max(bottom, max(s.bbox[3] for s in line))
                read.headers[index] = (top, bottom)

    _usual_label_column(tables, read, spans)
    boxed_extents, boxed = _boxed_tables(tables, read, spans)
    rows = read.rows
    pending: dict[tuple[int, float], list[int]] = {}
    path: list = [None, None, None]
    # a "Total n marks" row was the last thing read: the next label starts a new question
    after_total = False
    # under a question's "Notes" heading: nothing is a row until the next question's number
    in_notes = False
    notes_seen = False  # whether the scheme sets notes after its questions (see below)
    parts_read: dict[int, set[tuple]] = {}  # question -> the (part, subpart) labels read
    current: _Row | None = None
    names: dict[int, str] = {}
    # the last column the "Answer" header covers: some schemes give the M1/M2 codes a
    # column of their own (4CH0 2C June 2015, "Answer" centred over both)
    answer_last = 1
    # the column headed "Mark(s)" - the last but one in the sciences, but Mathematics A puts
    # its Notes after it
    header_marks: int | None = None
    # in a scheme that boxes its tables, only numbers inside them: a notes line opening with
    # a "2" of working outran the real question numbers (WMA12 October 2024 lost Q3)
    real_numbers, last_number, table_numbers = _question_number_run(
        [s for s in spans if not boxed or _ruled(boxed_extents.get(s.page, []), s.bbox[1])] if boxed else spans,
        tables,
    )
    for page_index in sorted(tables):
        page_xs, top, bottom, bands = tables[page_index]
        xs = page_xs
        marks_column = header_marks if header_marks is not None and header_marks < len(xs) - 1 else len(xs) - 2
        page_marks_column = marks_column
        page_spans = [
            s for s in spans
            if s.page == page_index and top - 2 <= s.bbox[1] and s.bbox[3] <= bottom + 2
        ]
        # Text outside the ruled table isn't a row. International A Level boxes each question
        # (Question Number | Scheme | Marks, closing "(6 marks)") and sets its examiner notes
        # below the box, headed "(a)", "(i)", "Alt (a)" at the margin - read as rows, every
        # question gained phantom parts and later ones (WMA11 October 2024: 11 questions
        # from 9). Only where the label column is ruled down through labelled rows: some
        # schemes rule just their header.
        # A scheme that boxes its tables this way also runs notes onto pages with no table at
        # all: there, nothing is a row.
        extent = boxed_extents.get(page_index, [(0.0, 0.0)] if boxed else [])
        lines = []  # (spans by column, the label path it starts, its top)
        last_label = None  # the last label line read: its entry, text, path before it, top
        fragment = ""  # a header word cut short at the end of the line above
        for line in _lines(page_spans):
            if extent and not _ruled(extent, line[0].bbox[1]) and not any(_TOTAL_RE.match(s.text) for s in line):
                continue
            xs = _xs_at(page_xs, bands, line[0].bbox[1])
            by_column: dict[int, list[MSSpan]] = {}
            for span in line:
                column = _column_of(span, xs)
                if column is not None:
                    by_column.setdefault(column, []).append(span)
            words = [s.text.lower().strip() for s in line]
            # A "Notes" heading alone in the label column opens a question's notes: labels
            # under it ("(a)", "(c)(i)", each with its M1/A1 notes) are the question's again,
            # never rows, until the next question's own number (WFM03 June 2025 boxes them
            # below each question's table, with no total line between). Maths A's Notes
            # column heading sits far right, never in the label column.
            line_text = " ".join(w for w in words if w)
            first = next((s for s in line if s.text.strip()), None)
            notes_heading = first is not None and (
                # alone, or opening a line of its own: "Notes Mark (b) and (c) together"
                # (WFM01 January 2024)
                (re.match(r"^notes?:?(?:\s|$)", line_text) is not None and _column_of(first, xs) == 0)
                or _notes_bar(line_text, _column_of(first, xs), xs)
            )
            if notes_heading:
                in_notes = notes_seen = True
                continue
            if in_notes:
                label0 = [s for s in line if _column_of(s, xs) == 0 and s.text.strip()]
                # the next question's number - never this one's again: a "2" opening a line
                # of working in Q2's notes (WFM01 January 2024) is still the notes
                number = real_numbers.get((page_index, round(line[0].bbox[1], 1)))
                # - or this one's with a part, a real start: a "3 x 4" of working in Q2's notes
                # had already moved the reading to 3, and Q3's own "3(a)" stayed in the notes
                # (WMA14 January 2024)
                label0_text = " ".join(s.text for s in label0).strip()
                fresh = number is not None and number == path[0] and _label_pieces(label0_text) and not re.fullmatch(r"\d{1,2}\.?", label0_text)
                if not (label0 and number is not None and (path[0] is None or number > path[0] or fresh)):
                    continue
                in_notes = False
            # "Questio" over "n", "numbe" over "r": a header word wrapped in a narrow cell
            # (4CH0 1C January 2012) - its tail read as part n and part r
            if fragment and len(words) == 1 and fragment + words[0] in _HEADER_WORDS:
                fragment = ""
                continue
            def is_fragment(w: str) -> bool:
                return len(w) >= 4 and w not in _HEADER_WORDS and any(h.startswith(w) for h in _HEADER_WORDS)

            # only a line that's all header: an answer can start a header word too ("A
            # addition" - 4CH1 1CR January 2022 6(d)(i) - is not "Additional guidance")
            header_like = all(w in _HEADER_WORDS or is_fragment(w) for w in words)
            fragment = next((w for w in words if is_fragment(w)), "") if header_like else ""
            if fragment:
                continue
            if all(w in _HEADER_WORDS for w in words) or _is_header(line):
                # the header row names the guidance columns, until a page heads them anew
                answer_last = next(
                    (c for c, cs in by_column.items() if c > 0 and any(_names_answer(s.text.lower()) for s in cs)), 1
                )
                header_marks = next(
                    (c for c, cs in by_column.items() if c > 0 and any(s.text.lower().strip().startswith("mark") for s in cs)),
                    None,
                )
                if header_marks is not None and header_marks < len(xs) - 1:
                    marks_column = page_marks_column = header_marks
                names = {
                    column: " ".join(s.text.strip() for s in column_spans).title()
                    for column, column_spans in by_column.items()
                    if column > answer_last and column != marks_column
                }
                continue
            # never a line that opens with a label: "11 | Total = 46 + ..." is Q11's first
            # line with "Total" in its working (4MA1 2FR June 2023), not a Total row
            if any(_TOTAL_RE.match(s.text) for s in line) and not any(
                _label_pieces(s.text) for s in by_column.get(0, [])
            ):
                lines.append(("total", None, line[0].bbox[1]))
                stated = _TOTAL_MARKS_RE.search(" ".join(s.text for s in line))
                if stated and path[0] is not None:
                    read.totals[path[0]] = int(stated.group(1))
                # only a question's own total ends it for the next label: the word alone
                # can head a column in an answer's table (4MA1 2FR January 2020 8(a))
                if _QUESTION_TOTAL_LINE_RE.search(" ".join(s.text for s in line)):
                    after_total = True
                continue
            # a marking point's code in an unheaded column of its own, read as part of the
            # label column ("2 c i" ... "M1"): it belongs with the answer
            codes = [s for s in by_column.get(0, []) if _MARK_CODE_RE.match(s.text.strip())]
            if codes:
                by_column[0] = [s for s in by_column[0] if s not in codes]
                by_column[1] = codes + by_column.get(1, [])
                if not by_column[0]:
                    del by_column[0]
            # A note squeezed into the label column beside the labels ("all / clip / with /
            # graph" by "8", "(b)", "(i)" - 4CH1 1CR January 2022): only label pieces count
            if by_column.get(0) and any(_label_pieces(s.text) for s in by_column[0]):
                # a label run into its answer stays too: it's split off below
                by_column[0] = [
                    s for s in by_column[0] if _label_pieces(s.text) or _GLUED_LABEL_RE.match(s.text.strip())
                ]
            # "3" here and "(c) M1 (R =) 9.7" as the answer (4CH1 1CR January 2022): the part
            # letter pushed into the answer column beside a bare question number
            # - or with the label column empty, when it's the very next part letter: 4MA0 2FR
            # January 2016 prints 5(c) in the Working column, straight after 5(b). Only the
            # next letter, so an "(a)" a line of working mentions is never taken.
            answer_spans = by_column.get(1, [])
            pushed = _PUSHED_MARKER_RE.match(answer_spans[0].text.strip()) if answer_spans else None
            next_letter = (
                not by_column.get(0)
                and pushed is not None
                and isinstance(path[1], str)
                and len(path[1]) == 1
                and pushed.group(1).strip("()") == chr(ord(path[1]) + 1)
            )
            if pushed and (
                next_letter or (by_column.get(0) and all(s.text.strip().isdigit() for s in by_column[0]))
            ):
                first = answer_spans[0]
                by_column[0] = by_column.get(0, []) + [first.model_copy(update={"text": pushed.group(1)})]
                rest = [first.model_copy(update={"text": pushed.group(2)})] if pushed.group(2) else []
                by_column[1] = [*rest, *answer_spans[1:]]
                if not by_column[1]:
                    del by_column[1]
            label = by_column.get(0)
            if label:
                # "(iii) An explanation that links..." - a label run into its answer in one
                # span (June 2024 1C, 4 rows): the markers stay, the rest is the answer's
                # never a span that is all label, "(a) (i)" - its (i) is no answer (4MA0 1F January 2012)
                glued = None if _label_pieces(label[-1].text, codes=False) else _GLUED_LABEL_RE.match(label[-1].text.strip())
                if glued and _label_steps(" ".join([*(s.text for s in label[:-1]), glued.group(1)]), path):
                    # each half keeps its own share of the span's box, so a composed scheme
                    # redraws the label without blanking the answer beside it
                    whole = label[-1]
                    split_x = whole.bbox[0] + pymupdf.get_text_length(glued.group(1), fontsize=whole.size)
                    rest = whole.model_copy(
                        update={"text": glued.group(2), "bbox": (split_x, *whole.bbox[1:])}
                    )
                    marker = whole.model_copy(
                        update={"text": glued.group(1).strip(), "bbox": (whole.bbox[0], whole.bbox[1], split_x, whole.bbox[3])}
                    )
                    label = [*label[:-1], marker]
                    by_column[0] = label
                    by_column.setdefault(1, []).insert(0, rest)
            if label and (page_index, round(line[0].bbox[1], 1)) in table_numbers:
                # a table's column of numbers (_in_number_column): the answer's, not a label
                by_column.setdefault(1, [])[:0] = by_column.pop(0)
                label = None
            label_text = " ".join(s.text for s in label).strip() if label else ""
            previous = next((entry for entry in reversed(lines) if entry[1] is not None), None)
            if (
                label_text == "i"
                and len(by_column) == 1
                and previous is not None
                and previous[1][2] in _ROMANS
                and previous[1][2] + "i" in _ROMANS
                and 0 < line[0].bbox[1] - previous[2] <= _WRAP_GAP
            ):
                # the last letter of the numeral above, wrapped: "ii" + "i" is (iii)
                previous[1][2] = previous[1][2] + "i"
                path = list(previous[1])
                continue
            before = list(path)
            steps = _label_steps(label_text, path) if label else None
            if steps is not None and _LEADING_NUMBER_RE.match(label_text) and (
                (page_index, round(line[0].bbox[1], 1)) not in real_numbers
            ):
                steps = None  # a number off the question sequence: an answer's, not a label
            here = (page_index, round(line[0].bbox[1], 1))
            # A question's notes: an unnumbered label, after the question's rows, naming one of
            # its parts again - read on, it starts the next question ("(a)" after (e)) or
            # follows a Total. International A Level sets them after each question's table
            # (WST01 October 2024: (a)...(e) again; WFM03 June 2025: under the table, after its
            # Total, and every question after slid one along). Notes only when the next question
            # prints its own number further on - a paper that leaves it off altogether is the
            # rules below - or, once the scheme has shown notes like this, when there is no next
            # question. Never a label naming a new part: 4MA0 1FR June 2017's Q25 "(a)" follows
            # a Q24 with no parts, its number unread, and it is Q25.
            if (
                label
                and path[0] is not None
                and not _LEADING_NUMBER_RE.match(label_text)
                and (
                    after_total
                    or (steps is not None and steps[0] == path[0] + 1)
                    # or going back to one of the question's own parts already read: WFM03
                    # January 2025's notes walk Q3's (i), (ii)(a) again under its rows
                    or (
                        scheme_header
                        and steps is not None
                        and steps[0] == path[0]
                        and steps != path
                        and tuple(steps[1:]) in parts_read.get(path[0], set())
                    )
                )
            ):
                again = _label_steps(label_text, [path[0], None, None])
                if again is not None and tuple(again[1:]) in parts_read.get(path[0], set()) and (
                    any(n == path[0] + 1 and at > here for at, n in real_numbers.items())
                    or (notes_seen and path[0] >= last_number)
                ):
                    notes_seen = in_notes = True
                    continue
            # ...and only while a later question's number is printed further on, so the next
            # question exists: lines left after the last Total (an alternative method, notes)
            # made a question 24 on a 23-question paper (4MA0 4HR June 2013)
            if after_total and label and path[0] is not None and path[0] + 1 < last_number and (
                steps is None or (steps[0] == path[0] and not _LEADING_NUMBER_RE.match(label_text))
            ):
                # The question before ended with its Total row, so this label is the next
                # question's, whatever its number reads as - missing ("(a)" alone) or off the
                # sequence: 4MA1 2HR June 2023 prints Q24's number so it reads "4", and Q24's
                # answer ran on into Q23's row (worth 10 of its 5). A number read on the
                # sequence ("23" again for an ALT method, a later one) stays as printed.
                rest = re.sub(r"^\s*\d{1,2}\.?\s*", "", label_text, count=1) if _LEADING_NUMBER_RE.match(label_text) else label_text
                # only a label's pieces after the number - never a sentence or a figure that
                # strayed into the column ("340]", "6, ... is fine", WDM11 January 2023)
                if not rest or _label_pieces(rest):
                    steps = _label_steps(f"{path[0] + 1} {rest}".strip(), [None, None, None])
            if (
                steps is not None
                and steps[0] is not None
                and steps[0] > last_number
                and not _LEADING_NUMBER_RE.match(label_text)
            ):
                # A question inferred past the last number the scheme prints: the last
                # question's notes walking its parts again (WMA13 November 2023's "(b)", "(d)",
                # "(d)(i)" read as a question 11 on a 10-question paper). A question that
                # exists prints its number somewhere.
                in_notes = notes_seen = True
                continue
            if steps is not None:
                after_total = False
            # A label wrapped down a narrow Question cell, a piece a line: "20." / "(a)" /
            # "(ii)" (4MA0 3H January 2013). Read a line at a time, the bare "(a)" became
            # (a)'s next subpart and each piece a row of its own, every answer one row off.
            # One label when each piece goes a level deeper, a line below the last with no
            # row rule between: "(a)" then "(b)" never does.
            # Where rows are ruled, the cell decides, not the gap: "2" over "(a)" 22pt
            # apart with the answer's first line and M1 between them, one ruled cell (WDM11
            # June 2025) - "2" became a row of its own, matching no part.
            row_rules = read.row_rules.get(page_index, [])
            since = (
                next((i for i in range(len(lines) - 1, -1, -1) if lines[i] is last_label["entry"]), None)
                if last_label is not None
                else None
            )
            gap = line[0].bbox[1] - last_label["top"] if last_label is not None else 0.0
            in_between = lines[since + 1 :] if since is not None else None
            if (
                label
                and in_between is not None
                and not any(last_label["top"] + 1 < rule < line[0].bbox[1] - 1 for rule in row_rules)
                and (
                    (not in_between and 0 < gap <= _WRAP_GAP)
                    or (
                        row_rules
                        and 0 < gap <= _CELL_GAP
                        and all(steps_ is None and by_ != "total" for by_, steps_, _ in in_between)
                    )
                )
            ):
                question = [last_label["before"][0], None, None]
                joined_text = f"{last_label['text']} {label_text}"
                own = _label_steps(last_label["text"], question)
                joined = _label_steps(joined_text, question)
                # and deeper where the first piece really stands, too: "(b)" after (i)(a) is
                # (i)(b), and the "(ii)" below it the next part, not (b)'s subpart (WST02
                # January 2025 - (i)(b)'s mark went to (ii), and (i)(a) had none)
                here_own = _label_steps(last_label["text"], last_label["before"])
                here_joined = _label_steps(joined_text, last_label["before"])
                in_place = here_own is None or (here_joined is not None and _deeper(here_joined, here_own))
                if own is not None and joined is not None and _deeper(joined, own) and in_place:
                    steps = _label_steps(joined_text, last_label["before"]) or joined
                    last_label["entry"][1][:] = steps
                    parts_read.setdefault(steps[0], set()).add(tuple(steps[1:]))
                    last_label["text"], last_label["top"] = joined_text, line[0].bbox[1]
                    path = steps
                    del by_column[0]
                    lines.append((by_column, None, line[0].bbox[1]))
                    continue
            if (
                steps is not None
                and previous is not None
                and previous[1] is not None
                and _within(steps, previous[1])
                and steps[2] is not None
                and previous[1][2] is None
                and 0 < line[0].bbox[1] - previous[2] <= _STACK_GAP
            ):
                # "(b)" over "(i)", a line apart: one label stacked, not two rows - the row
                # above is (b)(i), and this line carries on its answer
                previous[1][:] = steps
                parts_read.setdefault(steps[0], set()).add(tuple(steps[1:]))
                path = steps
                del by_column[0]
                lines.append((by_column, None, line[0].bbox[1]))
                continue
            if steps is not None:
                path = steps
                if _LEADING_NUMBER_RE.match(label_text) and steps[1] in (None, "a", "i") and steps[2] in (None, "a", "i"):
                    # the question printing its own number at its first part starts it afresh:
                    # rows read under it before were something else's (WST02 January 2024:
                    # Q2's notes read as Q3(a)(i), (b), (c), and then Q3's own (b)-(e) looked
                    # like a walk back). Not "2(b)": WME01 prints the number on every label.
                    parts_read[steps[0]] = set()
                parts_read.setdefault(steps[0], set()).add(tuple(steps[1:]))
            lines.append((by_column, steps, line[0].bbox[1]))
            if steps is not None:
                last_label = {"entry": lines[-1], "text": label_text, "before": before, "top": line[0].bbox[1]}

        # A row starts at the rule above its label, not at the label: lines between the two
        # are the row's own first lines, read after its label rather than into the row above
        rules = read.row_rules.get(page_index, [])
        rule_of: dict[int, float] = {}
        if rules:
            keys = [top for _, _, top in lines]
            label_at = [i for i, (_, steps, _) in enumerate(lines) if steps is not None]
            # Where every row is ruled, its label can sit anywhere in its cell: WDM11 January
            # 2024 centres "(ii)" 60pt below the rule that opens its cell, and its first lines
            # and dM1 went to (d)(i). There the row starts at the nearest rule above, however
            # far, back to the label before. Elsewhere only a rule just above counts.
            # Only a label centred in its cell, though: Mathematics A rules between one row's
            # marking points too, and 4MA0 3H June 2012's "(ii)", sitting on the rule below it,
            # took the A1 line above it from 10(c)(i).
            fully_ruled = len(rules) >= len(label_at)
            for n, i in enumerate(label_at):
                top = lines[i][2]
                earlier = lines[label_at[n - 1]][2] if n else None
                rule = _rule_above(rules, top)
                if rule is None and fully_ruled:
                    far = _rule_above(rules, top, top - (earlier if earlier is not None else 0.0))
                    below = min((y for y in rules if y > top + 1), default=None)
                    if far is not None and below is not None:
                        middle = (far + below) / 2
                        if abs((top + _LABEL_HEIGHT / 2) - middle) <= (below - far) / 4:
                            rule = far
                if rule is None or (earlier is not None and earlier >= rule - 1):
                    continue
                rule_of[i] = rule
                for j in range(i - 1, -1, -1):
                    by_column, steps, line_top = lines[j]
                    if steps is not None or by_column == "total" or line_top < rule - 1:
                        break
                    keys[j] = top + 0.001 * (1 + line_top - rule)
            order = sorted(range(len(lines)), key=lambda k: keys[k])
            rule_of = {order.index(i): r for i, r in rule_of.items()}
            lines = [lines[k] for k in order]
        label_tops = [top for _, steps, top in lines if steps is not None]
        # the rule that opens each labelled row, by the label's top
        # - only one just above it: a rule found further up (a label centred in a tall cell)
        # moves the row's lines, but a mark centred across merged cells beneath it still
        # goes by the lead (4MA0 3H June 2012 10(c): one "5" for (i) and (ii))
        rule_at_top = {
            lines[position][2]: rule
            for position, rule in rule_of.items()
            if lines[position][2] - rule <= _ROW_RULE_GAP
        }
        # each table's own Mark column, by what's in it: a table can merge two columns the
        # header page keeps apart, and the header's column number then lands on the Notes
        # only the lines read by the page's own columns: a table with columns of its own
        # numbers them differently, and pooled together they picked Notes as the Mark
        # column (4MA0 4H January 2014 page 9 - every mark of Q15 lost)
        page_marks_column = _numeric_column(
            [
                bc for bc, _, t in lines
                if bc != "total" and not any(top_ - _TABLE_GAP <= t <= bottom_ for top_, bottom_, _ in bands)
            ],
            page_marks_column,
        )
        band_marks = [
            _numeric_column(
                [bc for bc, _, t in lines if bc != "total" and top_ - _TABLE_GAP <= t <= bottom_], marks_column
            )
            for top_, bottom_, _ in bands
        ]
        for position, (by_column, steps, line_top) in enumerate(lines):
            marks_column = next(
                (mc for (top_, bottom_, _), mc in zip(bands, band_marks) if top_ - _TABLE_GAP <= line_top <= bottom_),
                page_marks_column,
            )
            if by_column == "total":
                current = None  # "Total for Question 7 = 7 marks": the question is over
                continue
            if steps is not None:
                current = _Row(list(steps), page_index, line_top, by_column.get(0, []))
                current.rule = rule_of.get(position)
                rows.append(current)
            if current is not None:
                current.cover(page_index, [s for column_spans in by_column.values() for s in column_spans])
            for column, column_spans in sorted(by_column.items()):
                text = " ".join(s.text for s in column_spans).strip()
                if column == 0 or not text:
                    continue
                if column == marks_column:
                    for span in column_spans:
                        if not _MARK_TAG_RE.sub("", span.text.strip()):
                            continue
                        match = _MARKS_RE.match(_MARK_TAG_RE.sub("", span.text.strip()))
                        if not match:
                            # codes with the part's mark run on: "B1ft    (1)" (WST01)
                            coded = _CODES_THEN_MARK_RE.match(span.text.strip())
                            if current is not None and span.text.strip():
                                current.mark_codes.append(coded.group(1).strip() if coded else span.text.strip())
                            if not coded:
                                continue
                            match = _MARKS_RE.match(f"({coded.group(2)})")
                        # Marks are centred in their cell, labels set at its top: a mark
                        # printed just above the next label is that label's
                        # - unless it's on its own row's label line: rows can be 14pt apart
                        # (4CH0 2C June 2015, 4(a)(i) over (ii))
                        own_line = (
                            current is not None
                            and current.start[0] == page_index
                            and abs(current.start[1] - span.bbox[1]) <= _LINE_TOLERANCE
                        )
                        # - but a bracketed "(3)" closes the part above it: International A
                        # Level prints each part's total at the foot of its cell, 14pt over the
                        # next label (WMA11 October 2024: Q7(a)'s 3 went to (b), (b)'s to (c))
                        closing = span.text.strip().endswith((")", "]"))
                        ahead = None if own_line or closing else next(
                            (t for t in label_tops if 0 < t - span.bbox[1] <= _MARK_LEAD), None
                        )
                        # where rows are ruled, the rule decides: a mark above the line that
                        # opens the next row is in this row's cell, centred there. 4MA0 1FR
                        # January 2015 Q7: (b)'s "2" sits 15pt above (c)'s label, inside (b)'s
                        # cell - every mark in the question slid one row down.
                        if ahead is not None and rule_at_top.get(ahead) is not None and (
                            span.bbox[3] <= rule_at_top[ahead] + 1
                        ):
                            ahead = None
                        if ahead is not None:
                            pending.setdefault((page_index, ahead), []).append(int(match.group(1)))
                        elif current is not None and closing:
                            current.closing.append(int(match.group(1)))
                        elif current is not None:
                            current.marks.append(int(match.group(1)))
                elif current is None:
                    continue
                # the answer is always left of the marks: a header further back can put "the
                # last answer column" past this table's Mark column (4MA0 4HR June 2013 p13),
                # and its Notes - with every marking code - read as answer
                elif column <= min(answer_last, marks_column - 1):
                    current.answer.append(text)
                else:
                    current.guidance.setdefault(names.get(column, "Notes"), []).append(text)

    for row in rows:
        row.marks += pending.get(row.start, [])
        if row.closing:
            # Each "(3)" closes one way of answering the part: "Way 1", "Way 2", "Way 3"
            # (WMA11 January 2023 2(a)) are alternatives, each worth the part - the row's
            # mark is theirs, never their sum (it read 9 for a 3-mark part)
            row.marks.append(max(row.closing))
    _split_shared_marks(rows)
    _marks_from_codes(rows, read.totals)
    coded_scheme = bool(rows) and 2 * sum(1 for row in rows if row.mark_codes) > len(rows)
    for row, following in zip(rows, [*rows[1:], None]):
        # Empty, or a question's own row holding only what the question gives - International
        # A Level opens a question with its number and the matrix or function it sets ("1 |
        # M = ...", WFM01 June 2025), no mark and no code, before its (a)/(b) rows
        stem_only = row.path[1] is None and not row.mark_codes and not _row_codes(row)
        row.heading = (
            (not row.answer or stem_only)
            and not row.marks
            and following is not None
            and _within(following.path, row.path)
        )
        # A question-number row worth nothing - no mark, no code - in a question the scheme
        # answers part by part, wherever it sits: working from the notes before that opens
        # with the question's number ("5", "6" over WMA11 June 2023's Q5 and Q6), or a
        # question's own given information. Nothing a student scores; never an answer row.
        # In a scheme that codes every mark in its Marks column, a bare number there with no
        # code is the notes' too (WMA13 June 2024's "20", WMA14 November 2023's "5" and "2").
        if (
            stem_only
            and (not row.marks or coded_scheme)
            and not row.closing
            and any(other.path[0] == row.path[0] and other.path[1] is not None for other in rows)
        ):
            row.heading = True
    return read


# A marking code at the start of a line, worth its digit: "B1 Allow 0.33", "M2 for ...",
# "A1 cao", "SC1". One inside brackets - "(B1 for any three correct)" - is partial credit
# within the row's own, not more of it. A suffix can run on: "B1oe", "B1ft", "M1dep"
# (4MA0 4H June 2017 14(b)(ii)).
_LEADING_CODE_RE = re.compile(r"^\s*(?:SC|[MABP])(\d)(?:oe|ft|cao|dep|isw)?\b")


# "Total 6 marks", "Total for Question 7 = 7 marks": the number of marks
_TOTAL_MARKS_RE = re.compile(r"(\d{1,2})\s*marks?\b", re.IGNORECASE)


def _marks_from_codes(rows: list[_Row], totals: dict[int, int]) -> None:
    """A question's own "Total n marks" checks the marks read for its rows. Where they
    don't add up to it and the rows' marking codes do, the codes are right and a mark was
    misread - slid a row down from a centred cell, or a stray number taken for one (4MA0
    4H June 2012 Q13, "9" for 6(c)(ii) in 1F June 2016, "3" over three rows in 2F January
    2013). Only when every row with content has codes."""
    by_question: dict[int, list[_Row]] = {}
    for row in rows:
        if row.path[0] is not None and (row.answer or row.guidance or row.marks):
            by_question.setdefault(row.path[0], []).append(row)
    for question, group in by_question.items():
        total = totals.get(question)
        if total is None or sum(sum(row.marks) for row in group) == total:
            continue
        codes = [_row_codes(row) for row in group]
        if all(codes) and sum(codes) == total:
            for row, code in zip(group, codes):
                row.marks = [code]


def _row_codes(row: _Row) -> int:
    """What a row's marking codes add up to: B1, M1, A1, P1 each worth their digit. Not a
    code that only finishes the line before's sentence - "... and dep on" / "M1" (4MA0
    4HR June 2013 20(d)(ii)) refers back to a mark, it isn't one."""
    total = 0
    for column in [row.answer, *row.guidance.values()]:
        for before, line in zip(["", *column], column):
            m = _LEADING_CODE_RE.match(line)
            if m and not _UNFINISHED_RE.search(before):
                total += int(m.group(1))
    # International A Level prints them in the Marks column, where a cell holds only codes:
    # every one counts ("B1, B1, B1", "M1A1", "dM1", "A1cso")
    for cell in row.mark_codes:
        total += sum(int(m.group(1)) for m in _MARK_COLUMN_CODE_RE.finditer(cell))
    return total


_CODES_THEN_MARK_RE = re.compile(r"^((?:d?(?:SC|[MABP])\d\S*[\s,]*)+?)\s*[(\[](\d{1,2})[)\]]$")
# a question's total in the Marks column: "17 marks", "(5 marks)"
_MARKS_TOTAL_CELL_RE = re.compile(r"^\(?\d{1,3}\s+marks?\)?$", re.IGNORECASE)
# a cell of nothing but codes: "M1", "dM1 A1", "B1, B1, B1", "A1cso"
_ONLY_CODES_RE = re.compile(r"^(?:d?(?:SC|[MABP])\d[a-z*]*[\s,]*)+$")
_MARK_COLUMN_CODE_RE = re.compile(r"(?<![A-Za-z])d?(?:SC|[MABP])(\d)(?!\d)")


# a line ending mid-sentence: what follows on the next line carries it on
_UNFINISHED_RE = re.compile(r"\b(?:on|of|and|or|for|with|after|from|by|the|in|to|than|a|an|award|give|gets?)\s*$", re.IGNORECASE)


def _split_shared_marks(rows: list[_Row]) -> None:
    """A Mark value printed once for several rows - "2" on 9(a)(i), nothing on (a)(ii) -
    split by the rows' own marking codes: B1 on each, so 1 and 1 (user-reported, Mathematics
    A 4MA0). Only where every row has codes and they add up to the mark printed.

    The mark can sit on any row of the ones it covers (centred in a merged cell): 4MA0 4H June
    2017 prints 14(a)'s "2" on (a)(ii), below a blank (a)(i). And it can cover a part or the
    whole question: 4MA1 2H January 2021 prints one "3" for 13(a)(i), (a)(ii) and (b). So an
    exact split is tried within each part, then across the question, before anything looser:
    run forward from the mark alone, 14(a)'s "2" once took in (b)(i), another part.

    Last, within a part, a row printing the mark can also list an alternative method, whose
    codes count twice ("With replacement method: M1 M1" under 18(b)(ii)'s M1 M1 A1, 4MA0 3HR
    January 2016): the other rows take their own codes, and it keeps the rest."""

    def groups(key) -> list[list[_Row]]:
        found: list[list[_Row]] = []
        for row in rows:
            if found and key(found[-1][0]) == key(row):
                found[-1].append(row)
            else:
                found.append([row])
        return found

    def exact(group: list[_Row]) -> None:
        marked = [row for row in group if row.marks]
        if len(group) < 2 or len(marked) != 1:
            return
        codes = [_row_codes(row) for row in group]
        if all(codes) and sum(codes) == sum(marked[0].marks):
            for row, code in zip(group, codes):
                row.marks = [code]

    def rest(group: list[_Row]) -> None:
        marked = [row for row in group if row.marks]
        if len(group) < 2 or len(marked) != 1:
            return
        total = sum(marked[0].marks)
        others = [(row, _row_codes(row)) for row in group if row is not marked[0]]
        given = sum(code for _, code in others)
        if all(code for _, code in others) and 0 < given < total:
            for row, code in others:
                row.marks = [code]
            marked[0].marks = [total - given]

    for group in groups(lambda row: tuple(row.path[:2])):
        exact(group)
    for group in groups(lambda row: row.path[0]):
        # only the rows still sharing a mark: those a part split has settled are left
        exact([row for row in group if row.answer or row.guidance or row.marks])
    # several marks in one question: each over the blank rows after it, in the question
    i = 0
    while i < len(rows):
        run = [rows[i]]
        while (
            i + len(run) < len(rows)
            and not rows[i + len(run)].marks
            and rows[i + len(run)].path[0] == rows[i].path[0]
            and (rows[i + len(run)].answer or rows[i + len(run)].guidance)
        ):
            run.append(rows[i + len(run)])
        if run[0].marks:
            exact(run)
        i += len(run)
    for group in groups(lambda row: tuple(row.path[:2])):
        rest(group)


# "3", "3.", "3(a)", and "3a" / "3aii" with the part run on (WMA11 November 2023)
_LEADING_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\.?(?:\s|\(|$|[a-h](?:[ivx]{1,4})?(?![A-Za-z]))")


# a label piece's own cell: 46pt on 4MA1 1H June 2019
_LABEL_CELL_MAX = 60.0


def _label_cells_merged(xs: list[float], spans: list[MSSpan]) -> list[float]:
    """The label column whole again where a table splits it into a cell per label piece:
    4MA1 1H June 2019 rules "3" | "(a)" | ... with the (a)-(d) cell running the table's
    height, so it read as a column and every part letter went to the Answer. Only a narrow
    cell holding part letters or numerals: a Working column (240pt) can hold nothing but
    bare numbers on a page (4MA0 2F June 2016), and merging it lost every label after."""
    while len(xs) > 4 and xs[2] - xs[1] <= _LABEL_CELL_MAX:
        # an empty one too: 4CH0 2C January 2016 page 11 has only "(iii)", in the third
        second = [s for s in spans if _column_of(s, xs) == 1 and s.text.strip()]
        # a label glued to its working counts too - "(b) 3 x 100 (= 3..." in one span
        # overflowing the cell (4MA1 2FR June 2025: its (b)s ran into each (a))
        if second and not all(_label_pieces(s.text) or _GLUED_LABEL_RE.match(s.text.strip()) for s in second):
            break
        if second and not any(re.search(r"[a-z]", s.text) for s in second):
            break
        xs = [xs[0], *xs[2:]]
    return xs


def _numeric_column(lines: list[dict], guess: int) -> int:
    """A table's Mark column: the rightmost column holding nothing but marks, the column
    the header names when that one does. 4MA0 4H June 2015 Q3 splits Working from Answer at
    a different x on each row, so its table reads one column short, and the header's fourth
    column - the Mark - was its Notes: every mark in it lost."""
    numeric: dict[int, int] = {}
    total: dict[int, int] = {}
    for by_column in lines:
        for column, spans in by_column.items():
            texts = [t for s in spans if (t := _MARK_TAG_RE.sub("", s.text.strip()))]
            if column and texts:
                total[column] = total.get(column, 0) + len(texts)
                # a marking code is a mark too: International A Level's Marks column holds
                # mostly "M1", "dM1 A1", "B1ft (1)" (WDM11 January 2024 page 17 lost it to
                # the Scheme column's "12560")
                numeric[column] = numeric.get(column, 0) + sum(
                    1 for t in texts
                    if _MARKS_RE.match(t) or _CODES_THEN_MARK_RE.match(t) or _ONLY_CODES_RE.match(t)
                    or _MARKS_TOTAL_CELL_RE.match(t)
                )
    marks = [c for c in total if numeric.get(c) == total[c]]
    # The header's column while most of what it holds is marks ("max 3" beside them is
    # fine). Not merely any number in it: 4MA0 3HR January 2016's Notes held one stray "0"
    # from "or 0.009" and kept the mark from (a)'s "2".
    if guess in total and 2 * numeric.get(guess, 0) > total[guess]:
        return guess
    if not marks:
        # nor only a column of nothing but marks: one mostly marks beats a guess that holds
        # hardly any - WDM11 January 2024 page 17 came in guessing its Scheme column, and its
        # Marks column's "17 marks" kept it from counting as all marks
        mostly = [c for c in total if 4 * numeric.get(c, 0) >= 3 * total[c] and c != guess]
        return max(mostly) if mostly else guess
    return max(marks)


# a Question column this much wider than the scheme's usual one has lost its rule
_WIDE_LABEL = 2.0  # 211pt against ~80 when the rule is lost; 55-92pt is ordinary variation


def _usual_label_column(tables: dict, read: "_Read", spans: list[MSSpan]) -> None:
    """One Question column width for the whole scheme. A table whose Question | Working rule
    doesn't run its height reads the label column ~210pt wide instead of ~80 (4MA1 2HR
    January 2022 page 20, Q18): the working joins the label, "18 3y2 + 7y + 16 = ..." isn't
    one, and the question loses its row. Such a table gets the scheme's usual label edge -
    replacing its first rule, or added before it, whichever leaves the column count the
    scheme's pages most often have."""
    from collections import Counter

    layouts = [xs for xs, _, _, _ in tables.values()] + [b[2] for *_, bands in tables.values() for b in bands]
    if len(layouts) < 3:
        return
    widths = sorted(xs[1] - xs[0] for xs in layouts)
    usual = widths[len(widths) // 2]
    count = Counter(len(xs) for xs in layouts).most_common(1)[0][0]

    def fixed(xs: list[float], page: int) -> list[float]:
        if xs[1] - xs[0] <= _WIDE_LABEL * usual:
            return xs
        edge = xs[0] + usual
        # never through a label: a scheme that centres its numbers puts "3" across the
        # usual edge (4MA1 2H January 2023 Q3, x 106-112 against an edge at 109) - and
        # the label went to the Working column, the question with it
        for span in spans:
            if (
                span.page == page
                and span.bbox[0] < edge < span.bbox[2]
                and _label_pieces(span.text.strip().rstrip("*"))
            ):
                edge = max(edge, span.bbox[2] + 2)
        replaced, inserted = [xs[0], edge, *xs[2:]], [xs[0], edge, *xs[1:]]
        if xs[2] - edge < _SLIVER:
            return [xs[0], edge, *xs[3:]] if len(xs) > 4 else xs
        return inserted if len(inserted) == count and len(replaced) != count else replaced

    for index, (xs, top, bottom, bands) in list(tables.items()):
        new_xs = fixed(xs, index)
        new_bands = [(t, b, fixed(b_xs, index)) for t, b, b_xs in bands]
        tables[index] = (new_xs, top, bottom, new_bands)
        read.columns[index] = new_xs


def _xs_at(page_xs: list[float], bands: list, y: float) -> list[float]:
    """The columns of the table a line at `y` sits in."""
    for top, bottom, xs in bands:
        if top - _TABLE_GAP <= y <= bottom:
            return xs
    return page_xs


# A table's column of numbers in the label column's reach: WMA12 January 2024's notes list
# "Values of n up to 50 for which..." - 6, 11, 13, 16 ... 28, one line (14-15pt) apart -
# which outran questions 9 and 10 as the run of question numbers, and their rows went to
# "question 28". Three or more different numbers stacked a line apart in one column are a
# table. One question's number printed on each of its one-line rows - 9, 9, 9, 9, 13pt
# apart (4MA0 1F January 2013) - is not.
_NUMBER_COLUMN_GAP = 16.0
# the most a run of question numbers may jump at its end - a missed number or two, never 30
_LAST_NUMBER_LEAP = 5
_NUMBER_COLUMN_MIN = 3


def _in_number_column(item: tuple, found: list[tuple]) -> bool:
    page, top, _, x = item
    column = sorted(
        (f[1], f[2]) for f in found if f[0] == page and abs(f[3] - x) <= 4.0 and abs(f[1] - top) <= 200
    )
    i = [t for t, _ in column].index(top)
    lo = hi = i
    while lo > 0 and column[lo][0] - column[lo - 1][0] <= _NUMBER_COLUMN_GAP:
        lo -= 1
    while hi + 1 < len(column) and column[hi + 1][0] - column[hi][0] <= _NUMBER_COLUMN_GAP:
        hi += 1
    return len({n for _, n in column[lo : hi + 1]}) >= _NUMBER_COLUMN_MIN


def _coefficient(number: MSSpan, line: list[MSSpan]) -> bool:
    """A number with a variable set against it and an operator after - "11" "x" "+" of
    "11x + 8y - 220" in a note (WDM11 January 2023): algebra, not a question number. A
    label's part letter can sit as close ("2" "a", Mathematics A), but answer text follows it."""
    after = sorted((s for s in line if s is not number and s.bbox[0] >= number.bbox[2] - 0.5), key=lambda s: s.bbox[0])
    return (
        len(after) >= 2
        and after[0].bbox[0] - number.bbox[2] < 2.0
        and re.fullmatch(r"[a-z]", after[0].text.strip()) is not None
        and after[1].text.strip()[:1] in ("+", "-", "−", "=", "×", "÷")
    )


def _question_number_run(
    spans: list[MSSpan], tables: dict
) -> tuple[dict[tuple[int, float], int], int, set[tuple[int, float]]]:
    """The lines whose leading number is a real question number: the longest run of them,
    in reading order, that never goes back. A stray number in the label column - a stacked
    fraction's "22" from question 19's working, a "3" after question 20 (Mathematics A) -
    falls off the run, and a question whose own number didn't read doesn't stop the rest,
    as a fixed limit on the step did. With the run, its last (highest) number, and the lines
    whose number is a table's (`_in_number_column`)."""
    found = []  # (page, top, number, x)
    coefficients = set()
    for page_index in sorted(tables):
        page_xs, _, _, bands = tables[page_index]
        page_spans = [s for s in spans if s.page == page_index]
        for line in _lines(page_spans):
            xs = _xs_at(page_xs, bands, line[0].bbox[1])
            label = [s for s in line if _column_of(s, xs) == 0]
            if not label:
                continue
            match = _LEADING_NUMBER_RE.match(label[0].text)
            if match:
                found.append((page_index, round(line[0].bbox[1], 1), int(match.group(1)), label[0].bbox[0]))
                if _coefficient(label[0], line):
                    coefficients.add(found[-1][:2])
    table_numbers = coefficients | {f[:2] for f in found if _in_number_column(f, found)}
    found = [f[:3] for f in found if f[:2] not in table_numbers]
    if not found:
        return {}, 0, table_numbers
    # longest non-decreasing subsequence of the numbers (a question's number can be printed
    # again at the top of a page), preferring the earliest at each length
    best = [1] * len(found)
    back = [-1] * len(found)
    for i in range(len(found)):
        for j in range(i):
            if found[j][2] <= found[i][2] and best[j] + 1 > best[i]:
                best[i], back[i] = best[j] + 1, j
    i = max(range(len(found)), key=lambda k: (best[k], -k))
    # Nor does it end on a leap: "40" in working after question 10 (WMA12 June 2019), "24"
    # (WMA13 November 2023). The run is the longest, so it took them - and with "the last
    # question is 40", the last question's notes were read as a question 11 to come.
    while back[i] != -1 and found[i][2] - found[back[i]][2] > _LAST_NUMBER_LEAP:
        i = back[i]
    last = found[i][2]
    kept = {}  # (page, top) -> the question number printed there
    while i != -1:
        kept[found[i][:2]] = found[i][2]
        i = back[i]
    return kept, last, table_numbers


def _deeper(path: list, than: list) -> bool:
    """`path` keeps every level `than` has and adds one: [20, "a", None] past [20, None, None]."""
    kept = all(b is None or a == b for a, b in zip(path, than))
    return kept and sum(x is not None for x in path) > sum(x is not None for x in than)


def _within(path: list, parent: list) -> bool:
    """`path` is one of `parent`'s own parts or subparts."""
    question, part, subpart = parent
    if path[0] != question or subpart is not None:
        return False
    return path[1] is not None if part is None else path[1] == part and path[2] is not None


def _is_heading(row: _Row) -> bool:
    """A heading line - "2 (a)" over its subparts: no answer, no marks, and its subparts
    next. A row with neither and no subparts is still a row: its answer can be all drawing
    (4CH0 1C January 2017, 10(b)(i), a graph to plot)."""
    return row.heading


def parse_mark_scheme(pdf_path: Path | str) -> list[MarkSchemeEntry]:
    entries: list[MarkSchemeEntry] = []
    for row in _read(pdf_path).rows:
        question, part, subpart = row.path
        part_path = ".".join(p for p in (part, subpart) if p)
        entry_id = row.entry_id
        answer = " ".join(row.answer)
        guidance = "\n".join(f"{name}: {' '.join(texts)}" for name, texts in row.guidance.items())
        marks = sum(row.marks)
        if _is_heading(row):
            continue
        same = next((e for e in entries if e.id == entry_id), None)
        if same is not None:  # a row continued on the next page under its label again
            same.marks += marks
            same.answer_text = f"{same.answer_text} {answer}".strip()
            same.guidance_text = "\n".join(t for t in (same.guidance_text, guidance) if t)
            continue
        entries.append(
            MarkSchemeEntry(
                id=entry_id, paper_key="", question_number=question, part_path=part_path,
                answer_text=answer, marks=marks, guidance_text=guidance,
            )
        )
    return entries


# How far a copied row reaches beyond its own text, above and below: Edexcel rules few of
# its rows (only where a question or part starts), so a row's region is its content's, and
# this keeps descenders and a row's top rule in without reaching the next row's text.
_ROW_PAD = 4.0


def parse_mark_scheme_layout(pdf_path: Path | str) -> MSLayout:
    """Where each row and the header sit on the source pages - the same MSLayout the
    Cambridge parser gives, so composed mark schemes copy Edexcel rows as they're printed.
    A row's region on each page runs the table's width, from its first line to its last -
    or, where the table rules its rows, from the rule above it to the rule below."""
    read = _read(pdf_path)
    rows: dict[str, MSRowLayout] = {}
    starts = sorted(
        (row.start[0], row.rule if row.rule is not None else row.start[1])
        for row in read.rows if not _is_heading(row) or row.extent
    )
    for row in read.rows:
        if _is_heading(row) or not row.label:
            continue
        segments = []
        for page, (top, bottom) in sorted(row.extent.items()):
            xs = read.columns.get(page)
            if not xs:
                continue
            # never into the next row: its own first line bounds this one
            following = [y for p, y in starts if p == page and y > top + _LINE_TOLERANCE]
            limit = min(following) - 1 if following else bottom + _ROW_PAD
            y0, y1 = top - _ROW_PAD, min(bottom + _ROW_PAD, limit)
            if row.rule is not None and page == row.start[0]:
                y0 = row.rule
            rules = read.row_rules.get(page, [])
            below = [y for y in rules if bottom - 1 <= y <= bottom + _ROW_RULE_GAP]
            if below and (not following or min(below) <= min(following) + 1):
                y1 = min(below)
            segments.append(BBox(page=page, x0=xs[0], y0=y0, x1=xs[-1], y1=y1))
        if not segments:
            continue
        label = row.label
        ref = LabelRef(
            page=label[0].page,
            x0=min(s.bbox[0] for s in label), y0=min(s.bbox[1] for s in label),
            x1=max(s.bbox[2] for s in label), y1=max(s.bbox[3] for s in label),
            size=label[0].size, bold=label[0].bold, font=label[0].font,
            origin_x=label[0].origin[0], origin_y=label[0].origin[1],
        )
        if row.entry_id in rows:  # its label printed again over a page break
            rows[row.entry_id].segments.extend(segments)
        else:
            rows[row.entry_id] = MSRowLayout(segments=segments, label=ref)

    header, columns, landscape = None, [], True
    if read.headers:
        page = min(read.headers)
        top, bottom = read.headers[page]
        columns = read.columns[page]
        header = BBox(page=page, x0=columns[0], y0=top - _ROW_PAD, x1=columns[-1], y1=bottom + _ROW_PAD)
    table_pages = sorted(read.columns)
    if table_pages:
        width, height = read.sizes[header.page if header else table_pages[0]]
        landscape = width > height
        columns = columns or read.columns[table_pages[0]]
    # The marking guidance ahead of the table, which a composed scheme carries the way it
    # carries Cambridge's marking principles: every page before the first table that
    # gives guidance - not the cover, nor the page about the publisher.
    preamble, titles = [], []
    first_table = min((row.start[0] for row in read.rows), default=None)
    if first_table is not None:
        with pymupdf.open(pdf_path) as doc:
            for index in range(1, first_table):
                page = doc[index]
                text = page.get_text("text")
                if not _GUIDANCE_RE.search(text):
                    continue
                width, height = page.rect.width, page.rect.height
                preamble.append(BBox(page=index, x0=0, y0=_PAGE_MARGIN, x1=width, y1=height - _PAGE_MARGIN))
                titles.append(next((line.strip() for line in text.splitlines() if line.strip()), "Marking guidance"))
    # The grid a composed scheme sets every row in: each column as wide as it gets on any
    # page with the same set of columns, so no page's cell is cut short.
    if columns:
        widths = [columns[i + 1] - columns[i] for i in range(len(columns) - 1)]
        for xs in read.columns.values():
            if len(xs) == len(columns):
                widths = [max(w, xs[i + 1] - xs[i]) for i, w in enumerate(widths)]
        grid = [columns[0]]
        for width in widths:
            grid.append(grid[-1] + width)
        columns = grid
    column_names = _column_names(pdf_path, read)
    return MSLayout(
        rows=rows,
        header=header,
        columns=columns,
        marks_column=_marks_column(column_names, columns),
        landscape=landscape,
        preamble=preamble,
        preamble_titles=titles,
        page_columns=read.columns,
        regrid=True,
        column_names=column_names,
    )


def _column_names(pdf_path: Path | str, read: "_Read") -> list[str]:
    """The header's words in each of its columns, on the first page with a header."""
    if not read.headers:
        return []
    page = min(read.headers)
    top, bottom = read.headers[page]
    xs = read.columns[page]
    with pymupdf.open(pdf_path) as doc:
        spans = [s for s in _rotated_spans(doc) if s.page == page and top - 2 <= s.bbox[1] <= bottom + 2]
    names = [[] for _ in range(len(xs) - 1)]
    for span in sorted(spans, key=lambda s: (round(s.bbox[1]), s.bbox[0])):
        column = _column_of(span, xs)
        # header words only - the band can reach the first row's label and answer
        if column is not None and span.text.lower().strip() in _HEADER_WORDS:
            names[column].append(span.text.strip())
    names = [" ".join(" ".join(words).split()) for words in names]
    # where a header word was split or missing, the standard names
    defaults = ["Question number", "Answer"] + ["Notes"] * (len(names) - 3) + ["Marks"]
    return [name or default for name, default in zip(names, defaults)]


def _marks_column(names: list[str], columns: list[float]) -> int | None:
    """The column headed "Mark(s)", else the last but one."""
    found = next((i for i, name in enumerate(names) if name.lower().startswith("mark")), None)
    if found is not None:
        return found
    return len(columns) - 2 if len(columns) >= 2 else None


def scheme_layout(pdf_path: Path | str, board: str) -> MSLayout:
    """The layout reader for the board the scheme belongs to."""
    from qbx import markscheme

    if board == "Edexcel":
        return parse_mark_scheme_layout(pdf_path)
    return markscheme.parse_mark_scheme_layout(pdf_path)
