"""Anchor clustering: find question/part/subpart markers by x0 clustering (CLAUDE.md section 5.1).

Real-paper findings (engine/scratch_anchors.py against a real Cambridge 0478 paper):

1. Cover-page barcode digits and page-footer page numbers also match `^\\d{1,2}$`. Both are
   excluded upstream by the caller: skip non-QUESTION pages (see pages.py) and skip
   header/footer y-bands (HEADER_FOOTER_MARGIN).

2. Bare 0/1 digits inside an in-question binary truth table also match `^\\d{1,2}$`, and
   this noise survives page/margin filtering because it's genuine mid-page question content.
   Clustering all three patterns (question/part/subpart) together in one x0 pool - as a
   literal reading of "cluster their x0 values" suggests - lets this noise's own gaps
   (~28pt, between table columns) outweigh the real tier-boundary gaps (~20pt) and wrecks
   the split. Parenthesised part/subpart patterns never collide with bare digits, so there's
   no need to cluster them against the question-number pattern in the first place: cluster
   *within* each pattern tier separately instead. Real anchors for a tier sit at one x0;
   same-pattern noise (the binary digits, all matching the question-number pattern) shows up
   as its own separate, usually smaller, sub-cluster - keep the largest sub-cluster per tier
   and flag the rest.

3. `(i)` and `(v)` are genuinely ambiguous between PART_RE (a single lowercase letter, "i"
   or "v") and SUBPART_RE (the roman numerals one and five) - a real paper's ninth part
   would collide with its first roman-numeral subpart. Picking a fixed regex-precedence
   order silently misassigns every "(i)" subpart to the part tier. This is exactly the case
   the spec's cross-validation is for: resolve the ambiguity by x0 position (whichever of
   the part/subpart x0 centers - established from the *unambiguous* markers - the span sits
   closer to), not by regex order.
"""

import re

import pymupdf
from pydantic import BaseModel

# International A Level prints its question numbers with a full stop ("1.", "3." alone on
# its line, WMA11 June 2024): the number is the digits (group 1)
# - and twice by mistake, "3.." and "6.." (WMA13 November 2020: both questions lost)
QUESTION_NUMBER_RE = re.compile(r"^(\d{1,2})\.{0,2}$")
# Not anchored to end-of-span ($): a bold-run marker like "(c)" can get glued to the next
# word into one span ("(c) Four") when they share formatting - real-paper finding against
# the 0478 QP. The closing paren is already a safe boundary regardless of what follows in
# the same span, unlike the bare question-number pattern, which stays a strict full match
# since relaxing that would make it match any span merely starting with 1-2 digits.
PART_RE = re.compile(r"^\([a-z]\)")
# A question number glued to its question's first words in one span. Real-paper finding
# (0580 m25 qp 22): nine of its 23 question numbers came out as "11 A company sells...",
# "12 (a) Write as...", so the strict full-span match above never saw them and their parts
# merged into the previous question. Only trusted at a line's own left edge, and only when
# what follows starts the way a question does - a capital or a part marker - which is
# what separates it from every other digit-led line measured across the corpus: "15 cm",
# "43 g / cm3", "24 dm3 of...", "1 .........." answer-line numbering.
# a number with nothing after it on its line but an answer's dotted line
_NUMBERED_ANSWER_LINE_RE = re.compile(r"^\d{1,2}\s*[.…]{4,}\s*$")
# Or a maths question defining its variable first - "19 a = (vector)", "f(x) = ..."
# (4MA0 4H January 2015's Q19 was lost to it); a unit stays out ("15 cm": no "=" or "(").
# What separates markers on a line: spaces, and any control character - BEL after a
# marker (Cambridge), and \x03, a shifted font's own space ("8\x03 (a) Solve", 4MA0
# 3H June 2018 Q8: read as one token, every such question lost its (a)).
_GAP_CHARS = " \t\n\r\xa0" + "".join(chr(c) for c in range(32))
GLUED_QUESTION_NUMBER_RE = re.compile(r"^(\d{1,2})\.?[\s\x00-\x1f ]+(?=[A-Z(]|[a-z]\s*(?:=|\())")
SUBPART_RE = re.compile(r"^\((i|ii|iii|iv|v|vi|vii|viii)\)")

TIERS = ["question", "part", "subpart"]

# The band at each page edge holding the header (page number) and footer (paper code +
# copyright) rather than question content. Measured on every paper this has seen:
#
#   paper            header y    first body line y    footer y
#   0478 s20 qp 11       35.7                 61.0       796.7
#   0620 m22 qp 42       33.7                 58.3       799.5
#   0620 w25 qp 11       36.1                 62.5       796.6
#   0972 w24 qp 11       36.1                 62.5       796.6
#
# Headers end by 36 and content starts at 58, so the cut belongs in the middle of that
# gap. It sat at 60 and silently swallowed every question number on the 0620 m22 paper,
# whose questions start at 58.8 - the whole paper came out with one question, numbered 0.
# Anything from roughly 40 to 56 separates the two cleanly on all four; 48 is the midpoint.
HEADER_FOOTER_MARGIN = 48.0
# The top edge alone cuts higher. International A Level Decision papers from 2024 (WDM11
# January 2024 on) start each question at y 46, nothing above it - at 48 their numbers were
# all cut, and each question's parts went to the one before. Headers measured above end by
# 36.1; the footer, 796 on an 842 page, keeps the 48 below.
HEADER_MARGIN = 42.0


def top_margin(margin: float) -> float:
    return min(margin, HEADER_MARGIN)


# How far up from the page's foot its footer can sit
_FOOTER_BAND = 60.0
_FOOTER_NUMBER_RE = re.compile(r"^\s*\d{1,3}\s*$")


def bottom_limit(lines, page_height: float, margin: float) -> float:
    """Where a page's content ends: the bottom margin, or lower, just above the page's own
    footer ("© UCLES 2024", "0610/32/M/J/24", "[Turn over") when that sits lower. 0610/32
    MJ24 is set 10pt down the page - footer at y 806, page number at 48 - and the "[2]"
    closing 2(b)(i), at 794, fell in the 48pt margin: the part read no mark and its crop
    stopped above it (user-reported). `lines` is (text, bbox) pairs."""
    default = page_height - margin
    footer = [
        bbox[1] for text, bbox in lines
        if bbox[1] > page_height - _FOOTER_BAND
        # the page number beside it too: International A Level prints it 1pt above the
        # barcode's text, and every page's number read as a question (WMA12 June 2022)
        and (is_page_furniture_text(text) or _FOOTER_NUMBER_RE.match(text))
    ]
    return max(default, min(footer) - 1.0) if footer else default


def _dict_lines(blocks: list[dict]):
    for block in blocks:
        for line in block.get("lines", []):
            yield "".join(s.get("text") or "".join(c["c"] for c in s.get("chars", [])) for s in line["spans"]), line["bbox"]

# Within-tier spread (e.g. subpart markers of slightly different width) is a few pt;
# between-tier indentation steps are typically ~15-25pt.
X0_TOLERANCE = 5.0
# the share of a tier's markers that must be bold before a plain one is taken for text (0610/33
# ON22: 30 bold to the key's 8 plain, 79%)
_ALL_BOLD = 0.67
_BOLD_TIERS = ("question", "part", "subpart")

# Two runs printed on the same line can be reported as separate lines whose bbox tops
# differ by a fraction of a point, when they're set in different weights or sizes. Real
# lines are ~11pt apart, so anything closer than this belongs to the same visual row.
LINE_Y_TOLERANCE = 4.0


# Some 4MA0 papers set a line in a font whose text layer is shifted 29 code points down:
# "(a) Solve the equation" reads "\x0bD\x0c\x036ROYH\x03WKH\x03HTXDWLRQ" (4MA0 1F January
# 2018 Q21; all of 4MA0 2F June 2017). A space comes out as \x03 and no lowercase letter
# survives the shift (z lands on "]"), which is how a shifted span is told apart from
# ordinary prose with a stray control character in it (Cambridge's BEL after a marker).
_SHIFT = 29


def _is_shifted(text: str) -> bool:
    if "\x03" not in text or any("a" <= c <= "z" for c in text):
        return False
    decoded = _unshift(text)
    if sum(c.isalpha() for c in decoded) >= 2:
        return True
    # a part marker on its own - " (b) " is \x03\x0bE\x0c\x03, one letter (4MA0 2F January
    # 2016 Q7, lost its (b)) - or a span with no plain ASCII letter or digit at all, the
    # whole of it shifted ("3 -6 7 -10 -4", the numbers to order in the same question)
    if re.fullmatch(r"\s*\((?:[a-z]|[ivx]{1,4})\)\s*", decoded):
        return True
    return not any(c.isascii() and c.isalnum() for c in text) and any(c.isdigit() for c in decoded)


def _unshift(text: str) -> str:
    return "".join(chr(ord(c) + _SHIFT) if 0x03 <= ord(c) <= 0x5D and c != " " else c for c in text)


def decode_shifted_text(blocks: list[dict]) -> list[dict]:
    """A page's get_text("dict"/"rawdict") blocks with shifted spans decoded, in place."""
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if "chars" in span:
                    raw = "".join(c["c"] for c in span["chars"])
                    if _is_shifted(raw):
                        for char in span["chars"]:
                            char["c"] = _unshift(char["c"])
                elif _is_shifted(span.get("text", "")):
                    span["text"] = _unshift(span["text"])
    return blocks


def page_blocks(page: object, kind: str = "dict") -> list[dict]:
    """`page.get_text(kind)["blocks"]` with shifted-font text decoded."""
    return decode_shifted_text(page.get_text(kind)["blocks"])


def line_top_map(page: object) -> dict[float, float]:
    """Maps every line's raw bbox top on this page to one shared value per visual row.

    Real-paper finding (0620 s24): a question that opens "5  (a)  Lead(II) bromide..."
    puts its number and its first part on one printed line, but they come back as two
    lines 0.87pt apart. Everything downstream orders anchors and text by (page, y, x), so
    that sliver sorted the "(a)" *above* its own question number: the part was attributed
    to the previous question, and this question's (i)/(ii)/(iii) subparts, left with no
    container, were promoted to top-level parts. Snapping both anchors and text to a
    shared row value makes the x-coordinate the tie-breaker it's meant to be, so the
    outer marker leads.
    """
    tops = sorted(
        {
            line["bbox"][1]
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
        }
    )
    mapping: dict[float, float] = {}
    row_top = None
    for top in tops:
        if row_top is None or top - row_top > LINE_Y_TOLERANCE:
            row_top = top
        mapping[top] = row_top
    return mapping


# What may sit between markers sharing a line: ordinary spaces, and the BEL/tab characters
# these papers use as the gap between a marker and what follows.
_MARKER_RUN_RE = re.compile(r"^(?:(?:\d{1,2}\.?|\([a-z]{1,4}\))[\s\x00-\x1f ]*)+$")


def _only_markers(text: str) -> bool:
    """Whether everything before a span on its line is just other anchor markers.

    Real-paper finding (0620 s24, Q7): a part and its first subpart can share a line -
    "(a)  (i) Organic compound A has..." - so requiring an anchor to start its own line
    drops the "(i)" and orphans the subpart. But that requirement is what stops chemistry
    state symbols ("CO2(g)", "(s)", "(l)") being read as part labels, so it can't simply
    go: a marker preceded only by other markers still leads its line, whereas one preceded
    by a formula or prose does not.
    """
    return bool(text.strip()) and bool(_MARKER_RUN_RE.match(text.strip()))


_ROMAN_SEQUENCE = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]
_LINE_MARKER_RE = re.compile(r"\(([a-z]{1,4})\)")


def _follows(earlier: str, later: str) -> bool:
    """`later` is the marker straight after `earlier`: (i) then (ii), (a) then (b)."""
    if earlier in _ROMAN_SEQUENCE and later in _ROMAN_SEQUENCE:
        if _ROMAN_SEQUENCE.index(later) == _ROMAN_SEQUENCE.index(earlier) + 1:
            return True
    return len(earlier) == 1 and len(later) == 1 and ord(later) == ord(earlier) + 1


def _listed_markers(line_text: str) -> set[str]:
    """Markers listed side by side on one line - "(i) (ii) (iii)" under three shapes, or
    an answer line "(i) ...... (ii) ...... (iii) ......" - are items within one part, not
    parts. Each real part starts a line of its own; two markers of one sequence on one line
    never do. Read as subparts (4MA0 1FR January 2016 Q1(a), two such lines), they gave two
    parts both called (a)(i), and (a)'s own wording dropped into the stem. "(a) (i) Find"
    is untouched: (i) doesn't follow (a). Nor is "(h) (i) Suggest..." at a line's start: h
    then i read as a sequence, but there it's part (h) and its first subpart (4BI1 2B
    November 2020 lost both)."""
    found = list(_LINE_MARKER_RE.finditer(line_text))
    listed = set()
    for earlier, later in zip(found, found[1:]):
        if (earlier.group(1), later.group(1)) == ("h", "i") and not line_text[: earlier.start()].strip(
            _GAP_CHARS
        ) and not line_text[earlier.end() : later.start()].strip(_GAP_CHARS):
            continue
        if _follows(earlier.group(1), later.group(1)):
            listed.update((earlier.group(1), later.group(1)))
    return listed


_SPLIT_MARKER_HEAD_RE = re.compile(r"^\s*\([a-z]{1,4}$")


def _join_split_markers(spans: list[dict]) -> list[dict]:
    """Rejoins a marker the PDF broke across two spans. Real-paper finding (0620/52 MJ21,
    1(a)): the label came out as "(a" and ") Complete the table." - a font change on the
    closing bracket - so neither span matched as a part, and the question lost its (a)
    along with its marks. `rawdict` spans: the join is of their characters."""
    joined: list[dict] = []
    for span in spans:
        if joined and span["chars"] and span["chars"][0]["c"] == ")":
            head = "".join(c["c"] for c in joined[-1]["chars"])
            if _SPLIT_MARKER_HEAD_RE.match(head):
                prev = joined[-1]
                joined[-1] = {
                    **prev,
                    "chars": prev["chars"] + span["chars"],
                    "bbox": tuple(pymupdf.Rect(prev["bbox"]) | pymupdf.Rect(span["bbox"])),
                }
                continue
        joined.append(span)
    return joined


def _match_anchor(text: str) -> tuple[str, list[str]] | None:
    """Returns (clean_marker, tiers). clean_marker is the matched substring, not the raw
    span text, so a glued-on trailing word ("(c) Four") doesn't leak into the anchor's
    label."""
    tiers = []
    marker = None
    for tier, pattern in (
        ("question", QUESTION_NUMBER_RE),
        ("part", PART_RE),
        ("subpart", SUBPART_RE),
    ):
        match = pattern.match(text)
        if match:
            tiers.append(tier)
            marker = match.group(1) if tier == "question" else match.group(0)  # "1." -> "1"
    return (marker, tiers) if tiers else None


class AnchorSpan(BaseModel):
    text: str
    x0: float
    y0: float
    page: int
    pattern_tiers: list[str]  # >1 entry only for the "(i)"/"(v)" part-vs-subpart ambiguity
    # The marker glyph run's own bbox (span-level, not the line's) - used by quiz_export.py
    # to delete and redraw just the label itself (e.g. "(c)" -> "(a)") when composing a
    # custom paper, without touching the rest of the line's content.
    bbox: tuple[float, float, float, float]
    # The marker's own type style, so a redrawn label matches the source instead of being
    # set in a guessed font size/weight.
    size: float = 11.0
    bold: bool = True
    # The first glyph's baseline origin. A bbox alone can't place replacement text
    # accurately (its top/bottom include ascender/descender padding that varies by font),
    # whereas the origin is exactly where the original text sat.
    origin: tuple[float, float] = (0.0, 0.0)
    font: str = ""  # the source font's name, for redrawing in the same family
    # read off a line after another marker ("5 (a) Lead...", "(b) (i) Find...") rather than
    # starting it - its x says where the marker before it ended, not its tier's indent
    follows_marker: bool = False


class TierResult(BaseModel):
    tier: str
    x0_center: float | None
    spans: list[AnchorSpan]  # accepted as real anchors for this tier
    rejected: list[AnchorSpan]  # same-pattern spans that clustered away - likely noise
    resolved_by_position: list[AnchorSpan]  # ambiguous "(i)"/"(v)" spans, settled by x0


def is_horizontal_line(line: dict) -> bool:
    """Real-paper finding (0620 QP): a "DO NOT WRITE IN THIS MARGIN" sidebar, repeated
    top-to-bottom in a narrow strip along the page edge (left or right, board-dependent),
    is set as a single vertically-oriented text line (PyMuPDF's line["dir"] is (0,-1) or
    similar, not (1,0)). It happened to be excluded on one page only because its bbox
    top coincided with the header margin - not reliably, since its bbox can start anywhere
    down the page depending on how tall the strip is rendered. Real exam content is always
    horizontal, so checking line direction directly is a robust, position-independent way
    to drop this (and any other rotated marginalia) rather than relying on y-band luck.
    """
    dx, dy = line.get("dir", (1.0, 0.0))
    return abs(dy) < 0.1 and abs(dx) > 0.1


# Edexcel's page furniture as text: the barcode's human-readable line ("*P60252A0324*",
# the paper reference plus page numbers) and "Turn over" - user-reported in 4CH1 1CR June
# 2019, where both ran into a part's text ("3*P60252A0324*Turn over (b) Describe...").
#
# Cambridge's footer too, by what it says: the June 2026 template moved it up from y 796-807
# to 782-793, inside the bottom margin, and every region running to a page's foot took in
# "© Cambridge University Press & Assessment 2026", "0625/32/M/J/26" and "[Turn over"
# (user-reported, 0625/32 June 2026 Q1(b) cropped to the whole page).
#
# And the print job code beside it, "03_0452_12_2026_1.21" (page, syllabus, component,
# year, version): 0452/12 FM26 Q4, the last question on its page, cropped down to it at the
# foot (user-reported); 64 papers in the bank had it in their part text.
_PAGE_FURNITURE_RE = re.compile(
    r"^(?:\*?P\d{5}[A-Z]{0,2}\d{4,}\*?|\[?turn over\s*[►▶]?|©.*|\d{4}/\d{2}/(?:[A-Z]/)+\d{2}"
    r"|\d{2}_\d{4}_\d{2}_\d{4}_\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)


def is_page_furniture_text(text: str) -> bool:
    return bool(_PAGE_FURNITURE_RE.match(text.strip()))


# A lone number in the middle third of the page, near its top or foot, is a page number:
# question numbers sit at the left margin. Edexcel prints them inside the page's frame (y 59
# on 4CH1 1CR June 2019), where they ran into the next part's text ("3(b) Describe..."), and
# Cambridge's (y 38-49) sits inside HEADER_MARGIN's top edge.
_PAGE_NUMBER_BAND = 80.0


def is_page_number(text: str, bbox: tuple, page_width: float, page_height: float) -> bool:
    text = text.strip()
    x0, y0, x1, _ = bbox
    if not (text.isdigit() and len(text) <= 2):
        return False
    centred = x0 > page_width / 3 and x1 < 2 * page_width / 3
    # At the foot, in a corner too: a download larger than A4 (652 x 898pt, 4CH1 1CR June
    # 2019) puts the footer's page number (x 564, y 827) inside the margin the header/footer
    # trim keeps - right on odd pages, left on even ones. A question number never stands
    # alone on the page's last line: its question would start below the foot.
    in_foot = y0 > page_height - _PAGE_NUMBER_BAND
    return (centred and y0 < _PAGE_NUMBER_BAND) or in_foot


def is_machine_readable_mark(text: str) -> bool:
    """Whether a line is a barcode strip rather than something a reader can read.

    Real-paper finding (0620 w25 qp 41): every page carries a barcode just beneath the
    header, at y=54-59. That's *below* HEADER_FOOTER_MARGIN and squarely inside the band
    where body text starts (58-62), so neither the header rule nor the tight crop can
    exclude it by position - it came out as a stray barcode floating above a question, and
    on a page-break continuation as a segment containing nothing else at all.

    It's identified by what it is, not where it sits: a run set in a barcode font, whose
    glyphs are control codes, so the line has no letters or digits anywhere. Papers do use
    control characters inside ordinary prose (BEL as the gap after a marker, backspace
    before a mark allocation), but always alongside real words, and a dotted answer line
    has no control characters at all - so requiring both conditions separates them.
    """
    stripped = text.strip()
    # The 2D barcode beside the footer: rows of accented Latin glyphs, "ĬÍĊ®Ġ³íÅù..." -
    # nothing plain ASCII, no spaces, and none of the symbol fonts' private-use characters
    # that maths is set in (0610/32 MJ24, where a lower footer brought it into a crop).
    # Mixed glyphs: a run of one ("­­­­", "¯¯¯¯") is a drawn answer line.
    if len(stripped) >= 8 and len(set(stripped)) >= 4 and all(0x80 <= ord(c) <= 0x24F for c in stripped):
        return True
    control = sum(1 for c in stripped if ord(c) < 0x20 or ord(c) == 0x7F)
    if control < 3 or control * 2 < len(stripped):
        return False
    return not any(c.isalnum() for c in stripped)


def find_anchor_spans(
    doc: object,
    content_pages: set[int] | None = None,
    margin: float = HEADER_FOOTER_MARGIN,
) -> list[AnchorSpan]:
    """doc is a pymupdf.Document. `content_pages`, if given, is the set of 0-indexed page
    numbers to scan (typically the pages pages.classify_document flagged as QUESTION) -
    everything else (cover, blank, insert, acknowledgements) is skipped.

    Real-paper finding (0620 QP): an anchor's y0 must come from its *line*, not its own
    span. A bold marker glued to the next word into one span (e.g. "(e) Calculate the
    volume...", same mechanism as the "(c) Four" finding above) can have a span-level y0
    a point or so off from its line's y0, if the line's bbox aggregates spans of slightly
    different heights. tree.py's collect_text_spans keys all content by line-level y - a
    mismatched anchor y0 here means the boundary comparison silently misattributes that
    whole line's content to the previous part instead of starting the new one.

    Real-paper finding #2 (0620 multiple-choice QP): chemistry state symbols - "(g)" gas,
    "(s)" solid, "(l)" liquid - are single letters in parens, indistinguishable from a real
    part label by pattern alone, and appear constantly in equations ("CO2(g)"). On a paper
    with no genuine lettered parts anywhere, the x0-clustering cross-validation has nothing
    real to converge on and can promote this noise to "the" part tier. The structural
    invariant that actually holds: a real anchor always starts its own line (that's the
    point of an outline-style list); inline notation like a state symbol never does. Require
    the marker's span to sit at the line's own left edge, not embedded mid-line.
    """
    spans = []
    width_votes: list[tuple[bool | None, bool | None]] = []  # (as Arial, as Times)
    family_votes: list[bool | None] = []
    for page_index, page in enumerate(doc):
        if content_pages is not None and page_index not in content_pages:
            continue
        page_height = page.rect.height
        row_tops = line_top_map(page)
        # every printed row's text, left to right: a list's markers can each be a "line"
        # of their own in the text layer, far apart on one row
        rows: dict[float, list[tuple[float, str]]] = {}
        blocks = page_blocks(page, "rawdict")
        for block in blocks:
            for line in block.get("lines", []):
                if is_horizontal_line(line):
                    top = row_tops.get(line["bbox"][1], line["bbox"][1])
                    text = "".join(c["c"] for span in line["spans"] for c in span["chars"])
                    rows.setdefault(round(top, 1), []).append((line["bbox"][0], text))
        listed_by_row = {top: _listed_markers(" ".join(t for _, t in sorted(parts))) for top, parts in rows.items()}
        # "rawdict" (not "dict") for per-character bboxes - a bold marker glued to the next
        # word into one span ("(b) The equation...", see the docstring above) means the
        # span's own bbox spans the whole line, not just the marker glyphs. quiz_export.py
        # needs to mask/redraw only the marker itself when relabeling a composed paper, so
        # the anchor's `bbox` must be measured from just the marker's own characters.
        page_bottom = bottom_limit(_dict_lines(blocks), page_height, margin)
        for block in blocks:
            for line in block.get("lines", []):
                if not is_horizontal_line(line):
                    continue
                y0 = row_tops.get(line["bbox"][1], line["bbox"][1])
                if y0 < top_margin(margin) or y0 > page_bottom:
                    continue
                # measured from ink, not span boxes: a line can open with a span of spaces
                # (4MA0 1F January 2018 Q17, "  " then " (a) Work out..."), which put its
                # (a) 18pt from the line's edge and lost the part
                line_x0 = min(
                    (x for x in (_ink_x0(span) for span in line["spans"] if not _invisible(span)) if x is not None),
                    default=line["bbox"][0],
                )
                preceding = ""
                line_text = "".join(c["c"] for span in line["spans"] for c in span["chars"]).strip()
                line_start = len(spans)
                listed = listed_by_row.get(round(y0, 1), set())
                for span in _join_split_markers(line["spans"]):
                    raw_text = "".join(c["c"] for c in span["chars"])
                    text = raw_text.strip()
                    if not text or _invisible(span):
                        continue
                    at_line_start = abs(_ink_x0(span) - line_x0) <= 3.0
                    leads_the_line = at_line_start or _only_markers(preceding)
                    preceding += raw_text
                    if not leads_the_line:
                        continue  # inline notation mid-sentence, not a real anchor
                    # A span can hold several markers in a row before its text starts -
                    # "12 (a) Write as...", "(b) (i) Find..." (real-paper finding: 0580 m25
                    # qp 22, where every such first part was lost and its marks with it) -
                    # so markers are read off the front one after another, each located by
                    # its own characters.
                    pos = len(raw_text) - len(raw_text.lstrip())
                    first = True
                    while pos < len(raw_text):
                        rest = raw_text[pos:].strip()
                        matched = _match_anchor(rest)
                        if not matched and first and at_line_start:
                            glued = GLUED_QUESTION_NUMBER_RE.match(rest)
                            if glued:
                                matched = (glued.group(1), ["question"])
                        if not matched or (not first and matched[1] == ["question"]):
                            break  # past the markers - a number here is the text's own
                        if matched[1] == ["question"] and _NUMBERED_ANSWER_LINE_RE.match(line_text):
                            # "2......" - the second of two numbered answer lines ("Give two
                            # reasons"), a bare "2" at the question indent (Edexcel 4CH1 1C
                            # Nov 2021 read it as question 2, a page early)
                            break
                        marker, tiers = matched
                        marker_chars = span["chars"][pos : pos + len(marker)]
                        # a span that opens with a space (" (a) Work out...", 4MA0 1F January
                        # 2018 Q17) has its bbox start 12pt left of the marker, at the
                        # question tier - so measure from the marker's own first character
                        leading_space = first and raw_text[:1].isspace()
                        anchor_x0 = (
                            span["bbox"][0] if first and not leading_space else marker_chars[0]["bbox"][0]
                        )
                        follows = not first or not at_line_start
                        pos += len(marker)
                        pos += len(raw_text[pos:]) - len(raw_text[pos:].lstrip(_GAP_CHARS))
                        first = False
                        marker_bbox = (
                            marker_chars[0]["bbox"][0],
                            min(c["bbox"][1] for c in marker_chars),
                            marker_chars[-1]["bbox"][2],
                            max(c["bbox"][3] for c in marker_chars),
                        )
                        spans.append(
                            AnchorSpan(
                                text=marker,
                                x0=anchor_x0,
                                y0=y0,
                                page=page_index,
                                pattern_tiers=tiers,
                                bbox=marker_bbox,
                                origin=tuple(
                                    marker_chars[0].get("origin", marker_bbox[:2])
                                ),
                                size=span.get("size", 11.0),
                                bold=_declares_bold(span),
                                font=span.get("font", ""),
                                follows_marker=follows,
                            )
                        )
                        width_votes.append(
                            (
                                _width_suggests_bold(marker, marker_bbox, span),
                                _width_suggests_bold(marker, marker_bbox, span, times=True),
                            )
                        )
                        family_votes.append(_family_vote(marker, marker_bbox, span))
                if listed:
                    # a list across the line, not parts: drop its markers (and their votes)
                    keep = [
                        i for i in range(line_start, len(spans)) if spans[i].text.strip("()") not in listed
                    ]
                    spans[line_start:] = [spans[i] for i in keep]
                    width_votes[line_start:] = [width_votes[i] for i in keep]
                    family_votes[line_start:] = [family_votes[i] for i in keep]
    # the family first: whether a label is bold can only be read off its width once it's
    # known which family's widths to compare it against
    _resolve_family(spans, family_votes)
    _resolve_bold(
        spans,
        [times if "times" in span.font.lower() else sans for span, (sans, times) in zip(spans, width_votes)],
    )
    return spans


# White text: on the page, but not seen. 0625/32 ON25 prints a white "i" just before
# "(i) for the core of a transformer", so the (i) wasn't first on its line and part
# 8(b)(i) was lost. Only kept out of where a line starts - white text on a dark cell is
# real, and stays text.
_WHITE = 0xFFFFFF


def _invisible(span: dict) -> bool:
    return span.get("color") == _WHITE


def _ink_x0(span: dict) -> float | None:
    """Where a rawdict span's first visible character starts (None if it's all spaces)."""
    for char in span["chars"]:
        if not char["c"].isspace():
            return char["bbox"][0]
    return None


def _declares_bold(span: dict) -> bool:
    """Bit 4 of a span's flags is PyMuPDF's bold flag, and a subset font often just names
    itself "...-Bold" instead. Either counts as the document saying so outright."""
    return bool(span.get("flags", 0) & 2**4) or "bold" in str(span.get("font", "")).lower()


_NAMED_FAMILY_RE = re.compile(r"arial|helvetica|times", re.IGNORECASE)


# the smallest width difference between the two families' renderings of a marker that
# counts as evidence either way - "(i)" is the same width in both and proves nothing
_FAMILY_DECISIVE_PT = 0.3


def _family_vote(marker: str, marker_bbox: tuple, span: dict) -> bool | None:
    """Whether the marker's drawn width matches Times rather than Arial - None when the
    font's own name already says which (nothing to measure), or when the two families would
    draw this marker too alike to tell.

    Real-paper finding (0580 m25 qp 22): every glyph on the page comes from one merged
    font named "AllAndNone", Times question numbers and Arial part letters alike, and its
    flags claim serif for both. The widths still tell them apart: a bold Times digit is
    0.50em wide against Arial's 0.556em."""
    if _NAMED_FAMILY_RE.search(span.get("font", "")):
        return None
    size = span.get("size", 11.0)
    measured = marker_bbox[2] - marker_bbox[0]

    def distance(regular: str, bold: str) -> float:
        return min(
            abs(measured - pymupdf.get_text_length(marker, fontsize=size, fontname=f))
            for f in (regular, bold)
        )

    times, sans = distance("tiro", "tibo"), distance("helv", "hebo")
    if abs(times - sans) < _FAMILY_DECISIVE_PT:
        return None
    return times < sans


def _resolve_family(spans: list["AnchorSpan"], votes: list[bool | None]) -> None:
    """Settle the family once per document for each kind of marker - numbers, and bracketed
    letters - by majority of the decisive votes, as a paper sets each kind consistently:
    0580 m25 qp 22 numbers its questions in Times and letters its parts in Arial."""
    for is_number in (True, False):
        kind = [(s, v) for s, v in zip(spans, votes) if s.text.isdigit() == is_number]
        decisive = [v for _, v in kind if v is not None]
        if decisive and sum(decisive) > len(decisive) / 2:
            for span, _ in kind:
                if not _NAMED_FAMILY_RE.search(span.font):
                    span.font = f"{span.font} (measured: Times)"


def _width_suggests_bold(marker: str, marker_bbox: tuple, span: dict, times: bool = False) -> bool | None:
    """Whether the marker's drawn width matches bold rather than regular metrics. None
    when the two weights would render this marker at the same width, so it proves nothing.

    Helvetica's metrics stand in for Arial's here - the two are metrically compatible, and
    Arial is what most papers are set in; Times-Roman's for Times New Roman's when `times`.
    Measuring a Times label against Arial's widths judged 0580's bold Times labels regular.
    """
    size = span.get("size", 11.0)
    regular_font, bold_font = ("tiro", "tibo") if times else ("helv", "hebo")
    regular = pymupdf.get_text_length(marker, fontsize=size, fontname=regular_font)
    bold = pymupdf.get_text_length(marker, fontsize=size, fontname=bold_font)
    if abs(bold - regular) < 0.05:
        return None
    measured = marker_bbox[2] - marker_bbox[0]
    return abs(measured - bold) < abs(measured - regular)


def _resolve_bold(spans: list[AnchorSpan], width_votes: list[bool | None]) -> None:
    """Settle the labels' weight once for the whole document, then apply it to every one.

    Real-paper finding: one paper (0478) names its label font "Arial-BoldMT" with the bold
    flag set, so the document says so outright. Another (0620) merges the entire page into
    a single font resource called "AllAndNone" carrying no weight information at all - its
    labels are visibly bold but every style flag says regular, so relabelling redrew them
    in regular weight. Glyph widths are then the only evidence, and no *single* label is
    decisive: "(a)" happens to be exactly as wide in Helvetica bold as regular. A paper
    styles all its labels alike, so the votes are pooled and decided together.
    """
    if any(span.bold for span in spans):
        return  # the document already said so
    votes = [vote for vote in width_votes if vote is not None]
    if votes and sum(votes) * 2 > len(votes):
        for span in spans:
            span.bold = True


def _chain_cluster(spans: list[AnchorSpan], tolerance: float) -> list[list[AnchorSpan]]:
    """Single-linkage 1D clustering: start a new group whenever the gap to the previous
    (sorted) x0 exceeds `tolerance`."""
    ordered = sorted(spans, key=lambda s: s.x0)
    groups = [[ordered[0]]]
    for span in ordered[1:]:
        if span.x0 - groups[-1][-1].x0 <= tolerance:
            groups[-1].append(span)
        else:
            groups.append([span])
    return groups


def _cluster_unambiguous(tier_spans: list[AnchorSpan], tolerance: float) -> tuple[list[AnchorSpan], list[AnchorSpan]]:
    """Returns (accepted, rejected). `_chain_cluster` walks x0 in ascending order, so its
    groups come out leftmost-first already; take that leftmost sub-cluster as the real
    anchors. Leftmost, not largest: a question number is the outermost structural level, so
    it should sit at the smallest indent of any same-pattern candidate - unlike a count-based
    pick, this doesn't flip if noise (e.g. a wide binary truth table) happens to outnumber
    the real anchors.
    """
    if not tier_spans:
        return [], []
    groups = _chain_cluster(tier_spans, tolerance)
    # Real-paper finding (Edexcel 4CH0 2C June 2015): one page set a "(b)" at x 49 and two
    # numerals at x 68, left of the real (b)s at 61 and numerals at 79 - and as the leftmost
    # groups they took both tiers from every real anchor in the paper. A real tier runs
    # through the paper; a page's own indentation doesn't. So the leftmost group seen on
    # more than one page wins, when there is one.
    spread = [g for g in groups if len({s.page for s in g}) > 1]
    accepted = spread[0] if spread else groups[0]
    rejected = [s for group in groups if group is not accepted for s in group]
    return accepted, rejected


# how far from its tier's column an ambiguous marker can sit and still be in it: a page set
# off from the rest moves it up to 20pt (edexcel.MAX_PAGE_SHIFT)
_AMBIGUOUS_REACH = 20.0


def resolve_tiers(spans: list[AnchorSpan], tolerance: float = X0_TOLERANCE) -> list[TierResult]:
    """For each pattern tier, cluster its *unambiguous* spans by x0 and keep the largest
    sub-cluster as that tier's real anchors; same-pattern spans in smaller sub-clusters are
    noise (e.g. a binary truth table's bare digits matching the question-number pattern) and
    are returned as `rejected` rather than guessed away silently.

    Spans matching more than one pattern (only "(i)"/"(v)", part-letter vs subpart-numeral)
    are held back from that clustering step and then assigned to whichever tier's x0 center
    they sit closest to.
    """
    # Real-paper finding (Edexcel 4CH0 1CR June 2013): a marker after another on its line
    # ("5 (a) The diagram...", one space apart) sits wherever the first one ended - x 49,
    # against 61 for every other (a) - and as the leftmost group it took the part tier from
    # all forty real ones. Such markers don't shape the clusters; a part letter or numeral
    # after its parent marker is that tier by its pattern alone.
    # Never a question number: none follows another marker, and a scale's "70 80" would
    # (4CH0 1C June 2017 read a measuring cylinder's 80 and 40 as questions).
    def is_follower(s: AnchorSpan) -> bool:
        return s.follows_marker and s.pattern_tiers in (["part"], ["subpart"])

    followers = [s for s in spans if is_follower(s)]
    spans = [s for s in spans if not is_follower(s)]
    unambiguous = [s for s in spans if len(s.pattern_tiers) == 1]
    ambiguous = [s for s in spans if len(s.pattern_tiers) > 1]

    accepted_by_tier: dict[str, list[AnchorSpan]] = {}
    rejected_by_tier: dict[str, list[AnchorSpan]] = {}
    numeral_parts: list[AnchorSpan] = []
    for tier in TIERS:
        tier_spans = [s for s in unambiguous if s.pattern_tiers == [tier]]
        if tier == "subpart" and accepted_by_tier.get("part"):
            # Numerals set where the letters go are parts (a question numbering its parts
            # (i), (ii) - older Mathematics A papers), and kept out of the subpart cluster:
            # seen on several pages, they won it from the real subparts at x 76 (4MA0 2F
            # June 2012).
            part_x = sum(s.x0 for s in accepted_by_tier["part"]) / len(accepted_by_tier["part"])
            numeral_parts = [s for s in tier_spans if abs(s.x0 - part_x) <= tolerance]
            tier_spans = [s for s in tier_spans if s not in numeral_parts]
        accepted, rejected = _cluster_unambiguous(tier_spans, tolerance)
        accepted_by_tier[tier] = accepted
        rejected_by_tier[tier] = rejected
    accepted_by_tier["part"] = accepted_by_tier["part"] + numeral_parts

    centers = {
        tier: sum(s.x0 for s in accepted_by_tier[tier]) / len(accepted_by_tier[tier])
        for tier in TIERS
        if accepted_by_tier[tier]
    }

    resolved_by_tier: dict[str, list[AnchorSpan]] = {tier: [] for tier in TIERS}
    for span in ambiguous:
        candidates = {t: centers[t] for t in span.pattern_tiers if t in centers}
        if not candidates:
            continue
        chosen = min(candidates, key=lambda t: abs(span.x0 - candidates[t]))
        # only a tier it actually sits in: "(i) a ____" by an answer line at x 393 joined the
        # subparts at x 76, the nearest (4MA0 2F June 2011 Q1 became one part "(i)", worth 0)
        if abs(span.x0 - candidates[chosen]) > _AMBIGUOUS_REACH:
            continue
        resolved_by_tier[chosen].append(span)

    for span in followers:
        if span.pattern_tiers[0] in centers:
            resolved_by_tier[span.pattern_tiers[0]].append(span)

    # Parts numbered (i), (ii), each with its own (a), (b) inside, set an indent further in
    # than the paper's other part letters: International A Level Further Pure ("7. (i)
    # A = ..." / "(a) Describe..." / "(ii) M = ..." / "(a) determine k", WFM01 January 2025,
    # user-reported). As a second column of letters they lost the cluster to the real part
    # letters and were dropped; each (i)'s (a), (b) ran into it. A letter set in further than
    # the part letters, under a numeral part, is that part's subpart.
    if "part" in centers:
        def position(s: AnchorSpan) -> tuple:
            return (s.page, s.y0, s.x0)

        letter_x = [s.x0 for s in accepted_by_tier["part"] if s not in numeral_parts and PART_RE.match(s.text)]
        part_x = sum(letter_x) / len(letter_x) if letter_x else centers["part"]
        questions = sorted(position(s) for s in accepted_by_tier["question"])
        parts = sorted(accepted_by_tier["part"] + resolved_by_tier["part"], key=position)
        for span in list(rejected_by_tier["part"]):
            if span.x0 <= part_x + tolerance:
                continue
            here = position(span)
            question_at = max((q for q in questions if q < here), default=None)
            parent = next((p for p in reversed(parts) if position(p) < here), None)
            if (
                parent is not None
                and (question_at is None or position(parent) > question_at)
                and SUBPART_RE.match(parent.text)
            ):
                rejected_by_tier["part"].remove(span)
                resolved_by_tier["subpart"].append(span)


    # A paper that sets a tier's markers in bold sets every one of them so: a plain "(b)"
    # 4pt off the part column is a figure's - 0610/33 ON22 Q1(a)'s dichotomous key, "1 (a)
    # ... (b)", "2 (a) ... (b)" in ArialMT, whose last "(b)" took Q1's (a)(ii) for (b)(ii).
    # Question numbers too: a spreadsheet's row numbers 1-19 down column A, at the question
    # indent (0417/11 ON21 Q10(b)), outran the real 11-13 as questions 11-19. A table packs
    # its numbers onto a page or two - there were more plain ones than bold - so question
    # numbers are judged by the pages they're on: bold on 12 pages, plain on 2.
    for tier in _BOLD_TIERS:
        tier_spans = accepted_by_tier[tier] + resolved_by_tier[tier]
        if tier == "question":
            bold_pages = {s.page for s in tier_spans if s.bold}
            mostly_bold = len(bold_pages) >= 5 and len(bold_pages) >= _ALL_BOLD * len({s.page for s in tier_spans})
        else:
            mostly_bold = len(tier_spans) >= 5 and sum(s.bold for s in tier_spans) >= _ALL_BOLD * len(tier_spans)
        if mostly_bold:
            plain = [s for s in tier_spans if not s.bold]
            accepted_by_tier[tier] = [s for s in accepted_by_tier[tier] if s.bold]
            resolved_by_tier[tier] = [s for s in resolved_by_tier[tier] if s.bold]
            rejected_by_tier[tier].extend(plain)

    results = []
    for tier in TIERS:
        all_accepted = sorted(
            accepted_by_tier[tier] + resolved_by_tier[tier], key=lambda s: (s.page, s.y0, s.x0)
        )
        x0_center = centers.get(tier)
        results.append(
            TierResult(
                tier=tier,
                x0_center=x0_center,
                spans=all_accepted,
                rejected=rejected_by_tier[tier],
                resolved_by_position=resolved_by_tier[tier],
            )
        )
    return results


def check_tier_ordering(results: list[TierResult]) -> bool:
    """Sanity check: question indentation < part indentation < subpart indentation.
    A failure here means something structural is wrong and the paper should be flagged
    rather than parsed on a guess.
    """
    centers = [r.x0_center for r in results if r.x0_center is not None]
    return centers == sorted(centers)
