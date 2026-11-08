"""Snapping a region drawn in the Reviewer to the question it's meant to hold.

A teacher drags a rough box; this moves each edge to where the question's own content
starts and stops, the same way a crop is found (crops.py):

- top: to the nearest line that opens with a question, part or subpart marker - "3", "(b)",
  "(ii)" - or else to the nearest line of content;
- bottom: to the nearest mark allocation ("(2)", "[3]") ending the part, or else the nearest
  bottom of content - never through the next marker below the box, and never into
  Edexcel's "(Total for Question n = m marks)" line;
- sides: to all the ink across the band, kept clear of the page's frame and margin strip.

Each edge only moves within `REACH` of where it was drawn, so a box drawn on purpose round
part of a question (one figure) stays roughly where it was put. Page furniture (frame,
margin strip, barcode, page number) is never snapped to.
"""

import re

import pymupdf

from qbx.anchors import GLUED_QUESTION_NUMBER_RE, PART_RE, QUESTION_NUMBER_RE, SUBPART_RE, is_horizontal_line
from qbx.crops import (
    _QUESTION_TOTAL_RE,

    _content_bbox,
    _content_limit,
    _margin_strip_clip,
    _page_lines,
    is_noise_line,
)
from qbx.schema import BBox

REACH = 36.0  # pt an edge may move: about three lines of text
PAD = 3.0
_MARK_RE = re.compile(r"(?:\(\s*\d{1,2}\s*\)|\[\s*\d{1,2}\s*\])\s*$")

def _starts_with_marker(text: str) -> bool:
    text = text.strip()
    first = text.split()[0] if text.split() else ""
    return bool(
        QUESTION_NUMBER_RE.match(first)
        or GLUED_QUESTION_NUMBER_RE.match(text)
        or PART_RE.match(text)
        or SUBPART_RE.match(text)
    )

def _content_lines(page: pymupdf.Page, x0: float, x1: float) -> list[tuple[str, tuple]]:
    lines = []
    limit = _content_limit(page)
    for line, text, bbox in _page_lines(page):
        if not is_horizontal_line(line):
            continue
        text = text.strip()
        if not text or is_noise_line(text) or bbox[2] < x0 or bbox[0] > x1 or bbox[1] >= limit:
            continue
        if re.fullmatch(r"\.{4,}|\d{1,3}", text):
            continue  # answer dots alone, or a page number
        lines.append((text, bbox))
    return lines

def snap_box(page: pymupdf.Page, box: BBox) -> BBox:
    """`box` (one page's region) with its edges moved to the content they were drawn near."""
    clip_x0, clip_x1 = _margin_strip_clip(page)
    lines = _content_lines(page, clip_x0, clip_x1)
    top, bottom = box.y0, box.y1

    # top: a marker line near the drawn edge, else any content line
    near_top = [(t, b) for t, b in lines if abs(b[1] - top) <= REACH]
    markers = [(t, b) for t, b in near_top if _starts_with_marker(t)]
    pool = markers or near_top
    if pool:
        top = min(pool, key=lambda tb: abs(tb[1][1] - box.y0))[1][1] - PAD

    # bottom: never into the next marker below the box - markers well inside it were
    # drawn round on purpose (a whole question takes its parts)
    next_marker = min(
        (b[1] for t, b in lines if _starts_with_marker(t) and b[1] > max(top + PAD + 4, box.y1 - 10)),
        default=page.rect.height,
    )
    totals = [b[1] for t, b in lines if _QUESTION_TOTAL_RE.match(t) and b[1] > top + PAD]
    ceiling = min([next_marker, *totals]) - 1.0
    near_bottom = [(t, b) for t, b in lines if abs(b[3] - box.y1) <= REACH and b[3] <= ceiling and b[1] > top]
    marks = [(t, b) for t, b in near_bottom if _MARK_RE.search(t)]
    pool = marks or near_bottom
    if pool:
        bottom = min(pool, key=lambda tb: abs(tb[1][3] - box.y1))[1][3] + PAD
    bottom = min(bottom, ceiling + 1.0) if box.y1 > ceiling else bottom
    if bottom - top < 8:
        return box

    # sides: the ink in the band, near where the box was drawn
    band = pymupdf.Rect(clip_x0, top, clip_x1, bottom)
    ink = _content_bbox(page, band)
    x0, x1 = box.x0, box.x1
    if ink is not None:
        x0 = max(clip_x0, ink.x0 - PAD)
        x1 = min(clip_x1, ink.x1 + PAD)
    return BBox(page=box.page, x0=round(x0, 1), y0=round(top, 1), x1=round(x1, 1), y1=round(bottom, 1))

def snap_segments(pdf_path: str, segments: list[BBox]) -> list[BBox]:
    with pymupdf.open(pdf_path) as doc:
        out = []
        for box in segments:
            if not 0 <= box.page < doc.page_count:
                out.append(box)
                continue
            out.append(snap_box(doc[box.page], box))
        return out
