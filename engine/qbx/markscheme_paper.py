"""Cambridge mark schemes printed like a question paper, not a table.

Real-paper finding (0478 Computer Science, 2016, user-reported - 11 papers to review, two
scheme rows read for the whole paper): there's no Question | Answer | Marks table at all.
The question number sits at the left margin, "(a)" and "(ii)" indented after it with the
answer run on, and each answer closes with its "[n]" at the right margin - the layout of
the question paper itself. The table parser had no column to find.

So it's read with the question paper's own parser (anchors, tree): every part it finds is a
scheme row, its "[n]" the row's marks. Used only for a scheme whose answers close with "[n]"
and that has no table header anywhere ("Mark" or "Marks" beside "Question" or "Answer"), so
every table scheme reads as before.

0625 Physics, February/March 2016 (papers 32, 42 - user-reported): the same layout with
the marks as codes down the right margin, "B1", "C1", "A1", and no "[n]" at all. A part's
marks are then its codes' (markscheme._row_marks: a C mark under an A worth 2+ is the A's
part credit).
"""

import re

import pymupdf

from qbx import tree
from qbx.anchors import HEADER_FOOTER_MARGIN, find_anchor_spans, marker_page_shifts, resolve_tiers
from qbx.pages import PageType, classify_document
from qbx.schema import BBox, MarkSchemeEntry

# how far apart in y two cells of one header row may be read
_ROW_TOLERANCE = 4.0


# a question paper's mark allocation, closing its line
_MARK_RE = re.compile(r"\[\d{1,2}\]\s*$")
# answers closed this way, at least, before the layout counts as a question paper's
_MIN_MARKS = 3


def is_paper_layout(spans: list, doc: pymupdf.Document) -> bool:
    """Answers closed by "[n]" as in a question paper, and no table header anywhere: no
    "Mark"/"Marks" cell with a "Question" or "Answer" cell on its row. A lone word won't do -
    "1 mark for each..." is an answer, and the running header's "Mark Scheme" isn't a cell
    (0478/12 MJ16)."""
    marks = [s for s in spans if s.text.strip().lower() in ("mark", "marks")]
    if any(
        other.page == mark.page
        and abs(other.y - mark.y) <= _ROW_TOLERANCE
        and other.text.strip().lower().split()[:1] in (["question"], ["answer"])
        for mark in marks
        for other in spans
    ):
        return False
    closed = sum(
        1
        for page in doc
        for line in page.get_text("text").splitlines()
        if _MARK_RE.search(line)
    )
    return closed >= _MIN_MARKS or len(_margin_codes(spans, doc)) >= _MIN_MARKS


def _margin_codes(spans: list, doc: pymupdf.Document) -> list:
    """Mark codes ("B1", "A2") in the right quarter of the page: the marks of a scheme that
    prints no "[n]"."""
    from qbx.markscheme import MARK_CODE_RE

    return [s for s in spans if MARK_CODE_RE.match(s.text) and s.x > doc[s.page].rect.width * 0.75]


def _upright(doc: pymupdf.Document) -> pymupdf.Document:
    """The document with its turned pages straightened. Some pages are printed sideways to
    fit a wide table, the petty cash book in 0452 2016 (rotated 90). The question paper's
    parser reads only horizontal text, so "1 (a)" and "(ii)" on such a page were lost:
    0452/22 FM16's whole Q1, and 1(b)(ii) in June 2016. Upright coordinates are what the
    table schemes use too (`markscheme._rotated_spans`), and composing straightens the
    source the same way."""
    if not any(page.rotation for page in doc):
        return doc
    copy = pymupdf.open("pdf", doc.tobytes())
    for page in copy:
        if page.rotation:
            page.remove_rotation()
    return copy


# How far left / right of its tier's column a scheme's bracketed marker may sit
_REACH_LEFT = 25.0
_REACH_RIGHT = 15.0


def _placed_markers(spans: list, turned: set[int]) -> list:
    """A scheme set out like its question paper (0452 2016) wanders more than a paper does.
    A page turned sideways has its own margins: 0452/22 FM16's "1 (a)" 12pt right of the
    upright pages', June 2016's "(ii)" 19pt right - off every cluster, and lost. Nothing on
    the page measures it, so a marker there is placed by its pattern, at its tier's usual x
    on the upright pages: a bracketed letter or numeral, and a question number only with a
    part marker after it on its line - a table's bare numbers fill these pages."""
    usual: dict[str, list[float]] = {}
    for span in spans:
        if span.page in turned:
            continue
        if len(span.pattern_tiers) == 1:
            usual.setdefault(span.pattern_tiers[0], []).append(span.x0)
        elif span.follows_marker and "subpart" in span.pattern_tiers:
            usual.setdefault("subpart", []).append(span.x0)  # the "(i)" in "(e) (i)"
    # the leftmost x that repeats: a table's column of numbers repeats its x more often
    # than the question numbers do, but always further right
    columns = {}
    for tier, xs in usual.items():
        rounded = [round(x) for x in xs]
        repeated = [x for x in set(rounded) if rounded.count(x) >= 2]
        if repeated:
            columns[tier] = float(min(repeated))
    out = []
    for span in spans:
        tier = span.pattern_tiers[0] if len(span.pattern_tiers) == 1 else None
        # a scheme's indents wander more than a paper's: "(e)" at the question number's indent
        # (0452/22 MJ16 3(e)), "(ii)" 9pt further in than the rest (0452/23 ON16 5(e)(ii)) -
        # a bracketed marker leading its line within reach of its column is placed there
        if (
            span.page not in turned
            and tier in ("part", "subpart")
            and tier in columns
            and not span.follows_marker
            and -_REACH_LEFT <= span.x0 - columns[tier] <= _REACH_RIGHT
        ):
            span.x0 = columns[tier]
        if span.page in turned:
            if tier == "question" and not any(
                other.page == span.page and abs(other.y0 - span.y0) <= 2 and "part" in other.pattern_tiers and 0 < other.x0 - span.x0 <= 40 for other in spans
            ):
                continue
            if tier in columns and not span.follows_marker:
                span.x0 = columns[tier]
        out.append(span)
    return out


def _parse(doc: pymupdf.Document) -> tuple[list, dict]:
    turned = {page.number for page in doc if page.rotation}
    doc = _upright(doc)
    page_types = classify_document(doc)
    content_pages = {i for i, kind in enumerate(page_types) if kind == PageType.QUESTION}
    spans = _placed_markers(find_anchor_spans(doc, content_pages=content_pages), turned)
    shifts = marker_page_shifts(spans)
    for span in spans:
        span.x0 -= shifts.get(span.page, 0.0)
    text_spans = tree.collect_text_spans(doc, content_pages=content_pages)
    boundaries: dict = {}
    questions = tree.build_tree(resolve_tiers(spans), text_spans, boundaries_out=boundaries)
    return questions, boundaries


# "10 × (1) mark" under a run of one-letter answers: the run's mark, printed in place of
# "[10]" (0452/11 ON16, /13 ON16 Q1)
_TIMES_MARKS_RE = re.compile(r"\b(?P<count>\d{1,2})\s*[×x]\s*\(\s*(?P<each>\d)\s*\)\s*marks?\b")


# "[max 3]" / "[Max 4]" after a list of "(1)" points: the part's mark, which the question
# paper's parser (reading "[n]") leaves at 0 (0452/12 MJ16 5(a), 5(g))
_MAX_MARKS_RE = re.compile(r"\[\s*max\.?\s*(?P<marks>\d{1,2})\s*\]", re.IGNORECASE)


# a line's top (the tree's positions) and its spans' tops differ by a point or so
_LINE_SLACK = 2.0


def entries(doc: pymupdf.Document, spans: list) -> list[MarkSchemeEntry]:
    from qbx.markscheme import _row_marks

    questions, boundaries = _parse(doc)
    codes = _margin_codes(spans, doc)
    out = []
    for question in questions:
        entry_marks: list[int] = []
        first = len(out)
        for part in question.parts:
            marks = part.marks
            start, end = boundaries.get(part.id, (None, None))
            if not marks and codes and start is not None:
                inside = [
                    c for c in codes
                    if (c.page, c.y) >= (start[0], start[1] - _LINE_SLACK)
                    and (end is None or (c.page, c.y) < (end[0], end[1] - _LINE_SLACK))
                ]
                marks = _row_marks(inside) if inside else 0
            if not marks and (times := _TIMES_MARKS_RE.search(part.text)):
                marks = int(times["count"]) * int(times["each"])
            if not marks and (capped := _MAX_MARKS_RE.search(part.text)):
                marks = int(capped["marks"])
            entry_marks.append(marks)
            out.append(
                MarkSchemeEntry(
                    id=f"{question.question_number}.{part.path or 'stem'}",
                    paper_key="",
                    question_number=question.question_number,
                    part_path=part.path,
                    answer_text=part.text,
                    marks=marks,
                )
            )
        _share_group_marks(out[first:], [p.path for p in question.parts])
    return out


def _share_group_marks(entries: list[MarkSchemeEntry], paths: list[str]) -> None:
    """A group of rows marked once, on its last: 0452/13 MJ16 Q1 answers (a) to (j) a
    letter each, closed by one "[10]" - (j) read 10 and the rest 0 against the paper's 1
    each (found by `qbx audit`). Siblings with no mark, closed by a sibling whose mark
    divides evenly among them all, share it - as the question paper's own (tree)."""
    def parent(path: str) -> str:
        return path.rsplit(".", 1)[0] if "." in path else ""

    run: list[int] = []
    for i, entry in enumerate(entries):
        if run and parent(paths[i]) != parent(paths[run[0]]):
            run = []
        if not entry.marks:
            run.append(i)
            continue
        group = run + [i]
        if run and entry.marks % len(group) == 0:
            for k in group:
                entries[k].marks = entry.marks // len(group)
        run = []


def rows(doc: pymupdf.Document) -> dict:
    """{entry id: MSRowLayout} - each row's region, from its marker to the next, for
    copying into a composed mark scheme."""
    from qbx.crops import page_segments
    from qbx.markscheme import MSRowLayout

    doc = _upright(doc)
    questions, boundaries = _parse(doc)
    out = {}
    for question in questions:
        for part in question.parts:
            start, end = boundaries.get(part.id, (None, None))
            label = part.label_bbox or question.number_bbox
            if start is None or label is None:
                continue
            segments = [
                BBox(page=page, x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1)
                for page, rect in page_segments(doc, start, end, HEADER_FOOTER_MARGIN)
            ]
            if segments:
                out[f"{question.question_number}.{part.path or 'stem'}"] = MSRowLayout(segments=segments, label=label)
    return out
