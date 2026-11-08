"""Crop each question part's region to a vector asset (SVG, not a rasterised PNG - per
explicit product decision, overriding CLAUDE.md's original "cropped PNG" wording).

PyMuPDF's `get_svg_image()` has no `clip` argument, so cropping works by temporarily
setting the page's cropbox to the target region, exporting, then restoring the original
cropbox. A part's boundary can span a page break (see tree.py), in which case it's rendered
as one SVG per page segment rather than force-fit into a single 2D image.
"""

import re
from pathlib import Path

import pymupdf

from qbx.anchors import (
    HEADER_FOOTER_MARGIN,
    bottom_limit,
    is_horizontal_line,
    is_machine_readable_mark,
    is_page_furniture_text,
    is_page_number,
    top_margin,
)
from qbx.pages import PageType, classify_page

Position = tuple[int, float, float]  # (page, y, x) - see tree.ResolvedAnchor.position

# A drawn "DO NOT WRITE IN THIS MARGIN" sidebar is sometimes just a tinted fill rectangle
# with no accompanying rotated text at all (real-paper finding: 0620 w25 qp 41 has the fill
# but no label on several pages) - a narrow, near-full-page-height filled rect is decorative
# chrome regardless of whether there's a text label to catch it by.
_DECORATIVE_FILL_MAX_WIDTH = 25.0
_DECORATIVE_FILL_MIN_HEIGHT_FRACTION = 0.5

# Real-paper finding (0620 w25 qp 41, Q4/Q5): when a part's answer space runs past the
# bottom of a page, Cambridge prints a navigational line like "Question 5 starts on the
# next page." into what is otherwise blank workspace. Lifted out of its original page and
# cropped as if it were this part's own content, it reads as a stray, out-of-context
# sentence floating in empty space - not a genuine part of the answer. Treat it as noise,
# the same way header/footer text already is.
_BOILERPLATE_RE = re.compile(
    r"^(question\s+\d+\s+(starts|continues|is continued)\s+on\s+the\s+(next|following)\s+page\.?"
    # Edexcel's header over an answer space carried onto the next page (International A Level)
    r"|question\s+\d+\s+continued"
    r"|\(?turn over\)?\.?"
    r"|please turn over\.?"
    r"|end)$",
    re.IGNORECASE,
)

# The copyright block every paper ends with. Real-paper finding (0972/11): it doesn't
# always get a page to itself - that paper runs questions 37-40 down the top of its final
# page and tucks this underneath them, and the last question's region runs to the bottom
# margin, so it swallowed the whole notice.
#
# Matched by its opening sentence and used to cut *everything below it*, rather than
# filtering the block line by line: it's flowed prose whose continuation lines ("publisher
# will be pleased to make amends...") carry no distinctive phrase at all, and its last line
# is sliced into meaningless glyph fragments ("L", "l E", "ti") by the crop region's own
# bottom edge. One reliable marker for the top of the block beats eight fragile ones.
# Since 2021 the block opens on "To avoid the issue of disclosure of answer-related
# information to candidates, all copyright acknowledgements are reproduced online in our
# Copyright / Acknowledgements Booklet...", a paragraph above "Permission to reproduce" and
# wrapping the booklet's name across two lines - so neither marker was reached until it had
# been taken in (user-reported, 119 papers of 0478, 0610, 0625).
_ACKNOWLEDGEMENTS_RE = re.compile(
    r"Permission to reproduce items|Copyright Acknowledgements Booklet"
    r"|To avoid the issue of disclosure of answer.related information",
    re.IGNORECASE,
)

# A wide rule is usually drawn directly above the copyright block; it belongs to the block,
# not to the question above it.
_ACKNOWLEDGEMENTS_RULE_REACH = 25.0

# Small breathing room around the tightened content bbox so glyph anti-aliasing / descenders
# never get clipped flush against the crop edge.
_TIGHT_CROP_PADDING = 4.0
# The widest stroked line that counts as content: table and figure rules measured 0.5pt;
# page-corner registration marks are 2.8pt.
THIN_RULE_MAX_WIDTH = 1.5

# A candidate segment shorter than this (points) is pagination leftover, not real content -
# see the comment where it's used in page_segments.
_MIN_SEGMENT_HEIGHT = 10.0
_HEADER_GRACE = 0.5
_PAGE_NUMBER_REACH = 12.0


def is_noise_line(text: str) -> bool:
    """Ink that is on the page but is not part of any question: navigational one-liners and
    per-page barcodes. Shared with quiz_export, which has to remove it from the embedded
    region as well as leave it out of the crop's bounds."""
    stripped = text.strip()
    return bool(stripped) and (
        bool(_BOILERPLATE_RE.match(stripped)) or is_machine_readable_mark(stripped)
        or is_page_furniture_text(stripped)
    )


# Edexcel draws every question page inside a grey rounded frame (2pt, x 35-560, y 36-794)
# and puts black tick bars on its left edge, outside the frame (x 0-27). Neither is part of
# a question - left in, every crop showed the frame's sides and corners - so a crop stays
# inside the frame and neither counts as ink. A frame is a stroked outline enclosing most
# of the page; a tick is a short filled bar touching the page's left edge.
_FRAME_MIN_FRACTION = 0.8
_EDGE_TICK_MAX_WIDTH = 60.0
_EDGE_TICK_MAX_HEIGHT = 6.0
_EDGE_BAR_MAX_WIDTH = 16.0


# Reading a page's text layer is the slow part of cropping - 0.2s an Edexcel page, whose
# rotated margin strips and dense vector text make it heavy - and every region asked for it
# four times over, twice per region: 4CH1 1C June 2025 spent 199 of its 212 seconds there.
# So each page is read once per document, and clipping is done here.
def _cached(page: pymupdf.Page, what: str):
    store = page.parent.__dict__.setdefault("_qbx_page_cache", {})
    entry = store.setdefault(page.number, {})
    if what not in entry:
        if what == "raw":
            entry[what] = [
                line for block in page.get_text("rawdict")["blocks"] for line in block.get("lines", [])
            ]
        elif what == "drawings":
            entry[what] = page.get_drawings()
        elif what == "images":
            entry[what] = page.get_image_info()
        elif what == "text":
            entry[what] = page.get_text("text")
    return entry[what]


def _line_text(line: dict) -> str:
    return "".join(c["c"] for span in line["spans"] for c in span["chars"])


def _page_lines(page: pymupdf.Page) -> list[tuple[dict, str, tuple]]:
    """(line, its text, its bbox) for every line on the page."""
    return [(line, _line_text(line), tuple(line["bbox"])) for line in _cached(page, "raw")]


def _clipped_lines(page: pymupdf.Page, rect: pymupdf.Rect) -> list[tuple[dict, str, tuple]]:
    """As `get_text("dict", clip=rect)` gives them: each line cut down to the characters
    whose centre lies inside `rect`, with the text and bbox of just those."""
    out = []
    for line in _cached(page, "raw"):
        lx0, ly0, lx1, ly1 = line["bbox"]
        if lx1 < rect.x0 or lx0 > rect.x1 or ly1 < rect.y0 or ly0 > rect.y1:
            continue
        chars = [
            c for span in line["spans"] for c in span["chars"]
            if rect.x0 <= (c["bbox"][0] + c["bbox"][2]) / 2 <= rect.x1
            and rect.y0 <= (c["bbox"][1] + c["bbox"][3]) / 2 <= rect.y1
        ]
        if not chars:
            continue
        bbox = (
            min(c["bbox"][0] for c in chars), min(c["bbox"][1] for c in chars),
            max(c["bbox"][2] for c in chars), max(c["bbox"][3] for c in chars),
        )
        out.append((line, "".join(c["c"] for c in chars), bbox))
    return out


def _page_frame(page: pymupdf.Page) -> tuple[pymupdf.Rect, float] | None:
    """The page's frame and its stroke width, if it draws one."""
    for drawing in _cached(page, "drawings"):
        r = drawing["rect"]
        if (
            drawing.get("fill") is None
            and r.width >= _FRAME_MIN_FRACTION * page.rect.width
            and r.height >= _FRAME_MIN_FRACTION * page.rect.height
        ):
            return pymupdf.Rect(r), drawing.get("width") or 1.0
    return None


def is_page_furniture_drawing(drawing: dict, page: pymupdf.Page) -> bool:
    r = drawing["rect"]
    if drawing.get("fill") is None:
        return r.width >= _FRAME_MIN_FRACTION * page.rect.width and r.height >= _FRAME_MIN_FRACTION * page.rect.height
    if r.x0 <= 1 and r.x1 <= _EDGE_TICK_MAX_WIDTH and r.height <= _EDGE_TICK_MAX_HEIGHT:
        return True
    # a dark bar set against either edge, as tall as it likes: older Decision papers print
    # one each side halfway down (WDM11 June 2019: x 0-11 and 584-595, 56pt tall), and the
    # crop reached out to both (user-reported)
    return r.width <= _EDGE_BAR_MAX_WIDTH and (r.x0 <= 1 or r.x1 >= page.rect.width - 1)


# How far in from either edge a margin strip's text may sit, as a fraction of the page's
# width: measured at x 7-15 and 574-595 on Cambridge pages; Edexcel's 0-27.
_MARGIN_STRIP_REACH = 0.15


def _margin_strip_clip(page: pymupdf.Page) -> tuple[float, float]:
    """Returns (x0, x1) to clip crops to, carving out a "DO NOT WRITE IN THIS MARGIN"
    vertical sidebar (left or right edge, board-dependent) if one is present - anchors.py
    already excludes its *text* from extraction, but a page-width crop would still show
    its visual strip. Deliberately narrow: only the detected vertical strip's own x-range
    is excluded, not a text-derived bounding box, so a wide diagram or table that
    legitimately extends near the page edge is never clipped.
    """
    page_width = page.rect.width
    page_height = page.rect.height
    x0, x1 = 0.0, page_width
    for line, _, bbox in _page_lines(page):
            if is_horizontal_line(line):
                continue
            # The strip is set vertically at the page's edge. Text at a slant inside a figure
            # isn't it: Fig. 2.2's "CAR" in 0625/43 ON23 (x 352, at 27 degrees) clipped
            # every region on its page at x 352 (user-reported).
            dx, _ = line.get("dir", (0.0, 1.0))
            line_x0, line_x1 = bbox[0], bbox[2]
            if abs(dx) > 0.1 or (line_x1 > _MARGIN_STRIP_REACH * page_width and line_x0 < (1 - _MARGIN_STRIP_REACH) * page_width):
                continue
            if line_x1 < page_width / 2:
                x0 = max(x0, line_x1)
            elif line_x0 > page_width / 2:
                x1 = min(x1, line_x0)
    for drawing in _cached(page, "drawings"):
        if drawing.get("fill") is None:
            continue
        r = drawing["rect"]
        if r.width <= 0 or r.width > _DECORATIVE_FILL_MAX_WIDTH:
            continue
        if r.height < _DECORATIVE_FILL_MIN_HEIGHT_FRACTION * page_height:
            continue
        if r.x1 <= page_width / 2:
            x0 = max(x0, r.x1)
        elif r.x0 >= page_width / 2:
            x1 = min(x1, r.x0)
    frame = _page_frame(page)
    if frame is not None:
        rect, stroke = frame
        x0, x1 = max(x0, rect.x0 + stroke), min(x1, rect.x1 - stroke)
    return x0, x1


def _content_limit(page: pymupdf.Page) -> float:
    """The y below which nothing on this page is question content - the top of the closing
    copyright block, including any rule drawn just above it. The page's own height when the
    block isn't there, which is every page but the last."""
    limit = page.rect.height
    for _, text, bbox in _page_lines(page):
            if text.strip() and _ACKNOWLEDGEMENTS_RE.search(text):
                limit = min(limit, bbox[1])
    if limit >= page.rect.height:
        return limit
    for drawing in _cached(page, "drawings"):
        r = drawing["rect"]
        if limit - _ACKNOWLEDGEMENTS_RULE_REACH <= r.y1 <= limit:
            limit = min(limit, r.y0)
    return limit


# Edexcel closes every question with "(Total for Question 2 = 8 marks)" over a rule across
# the page. Both belong to the whole source question - in a composed paper the total is
# wrong besides - so a region stops above the line rather than taking it and the rule into
# its last part's crop (user-reported, 4CH1 1C June 2025 Q2(d)).
# "(Total for Question 3 = 8 marks)", the older "(Total 18 marks)" (WDM11 June 2019), and
# the paper's own "TOTAL FOR PAPER: 75 MARKS" - none of them part of the last part. And
# Cambridge's "[Total: 7]" on its own line under the last mark (user-reported): the source
# question's total, wrong in a composed paper that sets only some of its parts.
_QUESTION_TOTAL_RE = re.compile(
    r"^(?:\(?\s*Total\s+(?:for\s*Question\s+\d+|\d{1,3}\s+marks?\b|for\s+paper\b)|\[Total:\s*\d+\]$)",
    re.IGNORECASE,
)
_QUESTION_TOTAL_GAP = 2.0


def _question_total_top(page: pymupdf.Page, top: float, bottom: float) -> float:
    """The top of the first question-total line between `top` and `bottom`, or `bottom`."""
    limit = bottom
    for _, text, bbox in _clipped_lines(page, pymupdf.Rect(0, top, page.rect.width, bottom)):
            if _QUESTION_TOTAL_RE.match(text.strip()) and top < bbox[1] < limit:
                limit = bbox[1] - _QUESTION_TOTAL_GAP
    return limit


_UNDERSCORE_LINE_RE = re.compile(r"^_{8,}$")


def _content_bbox(
    page: pymupdf.Page, rect: pymupdf.Rect, header_top: bool = False
) -> pymupdf.Rect | None:
    """The real ink inside `rect` - text (minus boilerplate noise), drawings and images -
    as a tight bounding box. None means the region has no real content at all (e.g. a
    page-break continuation that's just blank workspace plus a stray navigational line),
    which tells the caller to drop the segment entirely rather than emit an empty page.

    `header_top`: `rect` starts at the page's top margin (a region carried over a page
    break), so ink that starts above it - in the header band - is page furniture, whatever
    it is. Cambridge's page number (y 38-49) and filled corner mark (y 30-45) reach below
    HEADER_MARGIN's 42, and every continuation page came out with a 7pt sliver of them.

    Note this is the outer bound of *all* the ink, so blank space *between* two pieces of
    content is kept - a part whose answer space sits between its prompt and its trailing
    "[4]" keeps that room to write in. Only the empty run before the first mark and after
    the last one is trimmed away.
    """
    x0 = y0 = x1 = y1 = None

    def _union(bx0: float, by0: float, bx1: float, by1: float) -> None:
        nonlocal x0, y0, x1, y1
        if bx1 <= bx0 or by1 <= by0:
            return
        x0 = bx0 if x0 is None else min(x0, bx0)
        y0 = by0 if y0 is None else min(y0, by0)
        x1 = bx1 if x1 is None else max(x1, bx1)
        y1 = by1 if y1 is None else max(y1, by1)

    for line, text, bbox in _clipped_lines(page, rect):
            if not is_horizontal_line(line):
                continue
            text = text.strip()
            if not text or is_noise_line(text):
                continue
            if header_top and bbox[1] < rect.y0 - _HEADER_GRACE:
                continue
            # and the page number just under it, on a page set lower (y 48, 0610/32 MJ24):
            # only there, at the very top of a carried-over region - lone digits further down
            # are maths
            if header_top and bbox[1] < rect.y0 + _PAGE_NUMBER_REACH and is_page_number(
                text, bbox, page.rect.width, page.rect.height
            ):
                continue
            # International A Level's answer space: pages of underscore lines after a
            # question's last part, shared by all its parts (students write "(a)" themselves).
            # They're room to write, not the part's content - counted, the last part's region
            # ran on over every "Question n continued" page.
            if _UNDERSCORE_LINE_RE.match(text):
                continue
            bx0, by0, bx1, by1 = bbox
            _union(
                max(bx0, rect.x0), max(by0, rect.y0), min(bx1, rect.x1), min(by1, rect.y1)
            )

    for drawing in _cached(page, "drawings"):
        if is_page_furniture_drawing(drawing, page):
            continue
        r = pymupdf.Rect(drawing["rect"])
        if header_top and r.y0 < rect.y0 - _HEADER_GRACE:
            continue
        if r.width <= 0 or r.height <= 0:
            # A stroked line's bbox has no area - a horizontal rule is y0 == y1. Real-paper
            # finding (0620 s25 qp 61, Table 1.1): that table was drawn in stroked lines,
            # not the thin filled rectangles every earlier paper used, so its bottom rule
            # didn't count and the crop ended at the last row's text, cutting the border
            # off. A thin rule now counts, at its stroke's width.
            #
            # Only a thin one, though, and only one inside the region rather than grazing
            # its edge. Every page of 0580/22, 0580/42, 0620/41 w25 and 0620/61 carries
            # corner registration marks - 2.8pt strokes at x 37-58, y 791-811 - and counting
            # those stretched every crop near a page corner out to them.
            stroke = drawing.get("width") or 1.0
            if stroke > THIN_RULE_MAX_WIDTH:
                continue
            grace = 0.5  # the margin strip starts exactly where the clip ends
            if (
                r.x0 >= rect.x1 - grace
                or r.x1 <= rect.x0 + grace
                or r.y0 >= rect.y1 - grace
                or r.y1 <= rect.y0 + grace
            ):
                continue
            r = pymupdf.Rect(r.x0 - stroke / 2, r.y0 - stroke / 2, r.x1 + stroke / 2, r.y1 + stroke / 2)
        _union(max(r.x0, rect.x0), max(r.y0, rect.y0), min(r.x1, rect.x1), min(r.y1, rect.y1))

    for img in _cached(page, "images"):
        r = pymupdf.Rect(img["bbox"])
        if header_top and r.y0 < rect.y0 - _HEADER_GRACE:
            continue
        _union(max(r.x0, rect.x0), max(r.y0, rect.y0), min(r.x1, rect.x1), min(r.y1, rect.y1))

    return None if x0 is None else pymupdf.Rect(x0, y0, x1, y1)


# How far above its marker a region's first line can reach: a 2x2 matrix of fractions beside
# "7. (i)" stands 22pt above it (WFM01 January 2025); a 3x3 one, half again
_OVERHANG_REACH = 80.0
# ink stacked into one tall object - a big bracket's glyph pieces, a fraction's bar - touches
# the piece next to it; separate lines of prose leave a gap of 2pt or more
_STACK_TOUCH = 1.0


def _overhang_top(page: pymupdf.Page, top: float, left: float, right: float) -> float:
    """Where a region starting at `top` (its marker line's top) really starts: the top of
    whatever is printed across that line and stacked against it. A matrix or a tall fraction
    beside a question number is centred on its line and stands above it - cut at the marker,
    the region lost the matrix's top rows (user-reported, International A Level). Only ink
    that crosses the line, and what touches it going up, so the line of prose above, a gap
    away, stays with the region before."""
    items = [
        tuple(line["bbox"])
        for line in _cached(page, "raw")
        if is_horizontal_line(line) and _line_text(line).strip()
    ] + [tuple(d["rect"]) for d in _cached(page, "drawings") if d["rect"].height < _OVERHANG_REACH]
    items = [b for b in items if b[2] > left and b[0] < right and b[3] > top - _OVERHANG_REACH]
    stack = [b for b in items if b[1] < top - 0.5 and b[3] > top + 1]
    reach = min((b[1] for b in stack), default=top)
    grown = True
    while grown:
        grown = False
        for b in items:
            if b in stack or b[1] >= reach or b[3] < reach - _STACK_TOUCH:
                continue
            if any(b[0] < s[2] + 2 and b[2] > s[0] - 2 for s in stack):
                stack.append(b)
                reach, grown = min(reach, b[1]), True
    return max(reach, top - _OVERHANG_REACH)


def _page_bottom(page: pymupdf.Page, margin: float) -> float:
    return bottom_limit(((text, bbox) for _, text, bbox in _page_lines(page)), page.rect.height, margin)


# Cambridge's "[n]" only: Edexcel's last page can be a spare copy of a diagram ("if you need to
# redraw") over its "(Total for Question" line (WST01 June 2025 Q7), and its regions end at that
# total anyway
_MARK_ON_PAGE_RE = re.compile(r"\[\d+\]|\[Total:\s*\d+\]")


def _last_mark_page(doc: pymupdf.Document, start_page: int) -> int:
    """Where a region with no marker after it ends: the last page, from its own on, that
    prints a mark - the paper's last part runs over its answer pages to its mark there.
    Ended on its own page, 0478/21 MJ26 Q12 lost two pages of answer lines and its "[15]"
    (user-reported). No further: spare lined pages at the back carry no mark."""
    last = start_page
    for page_index in range(start_page + 1, doc.page_count):
        page = doc[page_index]
        if classify_page(_cached(page, "text"), page_index + 1, doc.page_count) != PageType.QUESTION:
            continue
        if _MARK_ON_PAGE_RE.search(_cached(page, "text")):
            last = page_index
    return last


def page_segments(
    doc: pymupdf.Document, start: Position, end: Position | None, margin: float
) -> list[tuple[int, pymupdf.Rect]]:
    """Public so cli.py can reuse the exact same geometry to populate QuestionPart.segments
    (schema.BBox) for the reviewer UI's overlay, not just for rendering the SVG crop.

    Each candidate segment is tightened to its real content (see `_content_bbox`) and
    dropped if it has none - typically a page-break continuation that turned out to be
    nothing but blank workspace, which would otherwise surface as a near-empty filler page.
    If filtering would drop every candidate, the untightened list is returned instead so
    callers can always rely on at least one segment.

    The vertical trim matters as much as the horizontal one: a part's boundary runs to the
    *next* anchor, so when that anchor is over a page break the part inherits all the empty
    space to the bottom of its page. Left in, a composed paper shows a short question
    followed by a large, unexplained gap (real-paper finding: 0478 Q1(c)(ii) carried ~100pt
    of dead space below its last answer line).
    """
    start_page, start_y, _ = start
    if end is None:
        end_page = _last_mark_page(doc, start_page)
        end_y = _page_bottom(doc[end_page], margin)
    else:
        end_page, end_y, _ = end

    candidates = []
    for page_index in range(start_page, end_page + 1):
        page = doc[page_index]
        # A region runs on across page breaks, but never onto a page that holds no
        # questions. Real-paper finding (0620 s25 qp 61): Q1 ends on page 3 and Q2 starts
        # on page 5, with a BLANK PAGE between - so Q1(f), running up to Q2's number, took
        # the blank page as its continuation, and "BLANK PAGE" turned up in its crop and in
        # every composed paper using it. Anchor and text collection already skip such pages.
        if page_index != start_page and (
            classify_page(_cached(page, "text"), page_index + 1, doc.page_count) != PageType.QUESTION
        ):
            continue
        page_height = page.rect.height
        left, right = _margin_strip_clip(page)
        # from the top of what's printed on the marker's line, to the top of the next one's
        top = _overhang_top(page, start_y, left, right) if page_index == start_page else top_margin(margin)
        bottom = (
            _overhang_top(page, end_y, left, right)
            if page_index == end_page and end is not None
            else end_y if page_index == end_page else _page_bottom(page, margin)
        )
        bottom = min(bottom, _content_limit(page))
        bottom = min(bottom, _question_total_top(page, top, bottom))
        frame = _page_frame(page)
        if frame is not None:
            top, bottom = max(top, frame[0].y0 + frame[1]), min(bottom, frame[0].y1 - frame[1])
        if bottom > top:
            candidates.append((page_index, pymupdf.Rect(left, top, right, bottom)))

    segments = []
    for page_index, rect in candidates:
        # A sliver a few points tall - the leftover gap between a margin and the very next
        # anchor - can't hold meaningful content on its own; a stray descender or a
        # registration mark clipping into it would otherwise pass the content check below
        # and still produce a near-blank filler page (real-paper finding: 0620 w25 qp 41's
        # Q4(b) page-break continuation was an 8px-tall sliver, not truly content-free).
        if rect.height < _MIN_SEGMENT_HEIGHT:
            continue
        page = doc[page_index]
        content = _content_bbox(page, rect, header_top=page_index != start_page)
        if content is None:
            continue
        segments.append((
            page_index,
            pymupdf.Rect(
                max(rect.x0, content.x0 - _TIGHT_CROP_PADDING),
                max(rect.y0, content.y0 - _TIGHT_CROP_PADDING),
                min(rect.x1, content.x1 + _TIGHT_CROP_PADDING),
                min(rect.y1, content.y1 + _TIGHT_CROP_PADDING),
            ),
        ))

    return segments if segments else candidates


def crop_to_svg(
    doc: pymupdf.Document,
    start: Position,
    end: Position | None,
    out_path: Path,
    margin: float = HEADER_FOOTER_MARGIN,
) -> list[Path]:
    """Writes one SVG per page the region spans. Returns the paths written, in order -
    out_path itself for a single-page region, or out_path with a "_pN" suffix per page
    segment when the region crosses a page break.
    """
    segments = page_segments(doc, start, end, margin)
    written = []
    for i, (page_index, rect) in enumerate(segments):
        page = doc[page_index]
        # The page's own CropBox entry, put back exactly as the file had it (or removed, if it
        # had none). Not via set_cropbox(page.cropbox): `cropbox` is in PDF coordinates and
        # set_cropbox takes page coordinates, so the round trip flips the box - harmless
        # when it's centred on the MediaBox, but the 4CH0 June 2016 downloads' is 0.1pt off
        # centre, and the flipped box fell outside the page ("CropBox not in MediaBox").
        kind, original_cropbox = doc.xref_get_key(page.xref, "CropBox")
        page.set_cropbox(rect)
        svg = page.get_svg_image()
        doc.xref_set_key(page.xref, "CropBox", original_cropbox if kind != "null" else "null")

        path = out_path if len(segments) == 1 else out_path.with_name(
            f"{out_path.stem}_p{i + 1}{out_path.suffix}"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8")
        written.append(path)
    return written
