"""Checks on extracted papers that need no person and no Claude.

The six validators (validate.py) check a paper against its own numbers: marks add up,
every part joins. They can't see a parse whose numbers are right and whose regions are
wrong - a part's crop that took in the next part's lines, text the tree dropped
altogether, two parts claiming the same lines. This checks the output against the PDF
it came from, with what can be proved from the page:

- **Coverage.** Every line of question text on a question page lies inside some region
  (a stem, a part, a container heading). A line inside none was lost: it prints in no
  composed paper.
- **Overlap.** No two parts claim the same lines.
- **Order.** Parts' regions run down the paper in the order of their labels.
- **Marks in region.** The mark allocations printed inside a part's region add up to the
  marks the part carries (what the paper prints, before any scheme sharing). A crop cut
  short, or run on into the next part, shows here.
- **Own label.** A part's region holds its own label's line.
- **Shape.** Within one board, subject and component, papers from different sittings
  look alike: a paper whose total marks or question count is off from the others is
  worth a look even when it checks out against itself.

And `snapshot`/`diff` freeze each paper's structure (questions, parts, marks, links,
regions to the point) so that a parser change shows exactly which papers it changed -
the regression net for papers already known to be right.

Every finding names a page and a place, so a person looks only where something is wrong.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from qbx.anchors import bottom_limit, furniture_rows, is_page_number, top_margin, HEADER_FOOTER_MARGIN
from qbx.crops import _ACKNOWLEDGEMENTS_RE, _line_text, is_noise_line
from qbx.pages import PageType, classify_document
from qbx.schema import BBox, Paper
from qbx.tree import DOTTED_LINE_RE

# A line's centre may sit this far outside a region and still count as inside: regions are
# cut tight to ink, and a line box is a little taller than its glyphs.
_SLACK = 3.0
# Two parts' regions overlapping by less than this (points, vertically) is a shared edge.
_OVERLAP = 4.0

# Lines that are ink but not question content.
_FURNITURE_RE = re.compile(
    r"^(?:question \d+ continued|do not write in this area|blank page|turn over|"
    r"\(total for question.*|total for paper.*|end of (?:question )?paper|end|"
    r"\[total:\s*\d+\]|answer\s*\.*|working space|do not write in this space\.?|"
    # instructions to the candidate, not question content (first run over the release
    # library: 0452, 0625 Paper 5 - found in no region, rightly)
    r"(?:please turn over )?(?:for )?question \d+ (?:is|starts|begins|continues) on the \w+ page\.?|"
    r"please turn over for question \d+\.?|the .{3,40} is printed on the next page\.?|"
    r"question \d+ (?:continues|starts|begins|is printed|is) on (?:page \d+|the \w+ page)\.?|"
    r"tie your ray[- ]trace sheet.*|write your name, centre number and candidate number.*)$",
    re.IGNORECASE,
)
# the board's notice where a question it withdrew was ("Due to an issue with question 20,
# the question has been removed...")
# reference pages printed with the paper, used by no one question: 0478's copy of the
# pre-release material, 0620 practicals' "Notes for use in qualitative analysis"
_PRE_RELEASE_RE = re.compile(r"copy of the pre-release material|notes for use in qualitative analysis", re.IGNORECASE)
_WITHDRAWN_RE = re.compile(r"due to an issue with (?:question|q)", re.IGNORECASE)
# the margin strip says so; other sideways text is content (a table printed sideways)
_MARGIN_STRIP_RE = re.compile(r"\bWRITE\b", re.IGNORECASE)


@dataclass
class Finding:
    paper_key: str
    check: str
    detail: str
    page: int | None = None  # 1-based, as printed in a PDF viewer
    y: float | None = None

    def line(self) -> str:
        where = f" p.{self.page}" + (f" y{self.y:.0f}" if self.y is not None else "") if self.page else ""
        # ASCII: the Windows console can't print every character a paper's text holds
        detail = self.detail.encode("ascii", "replace").decode()
        return f"{self.paper_key:<22} {self.check:<18}{where:<12} {detail}"


@dataclass
class PaperAudit:
    paper_key: str
    findings: list[Finding] = field(default_factory=list)
    error: str = ""


def _inside(cx: float, cy: float, box: BBox, slack: float = _SLACK) -> bool:
    return box.x0 - slack <= cx <= box.x1 + slack and box.y0 - slack <= cy <= box.y1 + slack


def _regions(paper: Paper) -> list[tuple[str, BBox]]:
    """(owner, segment) for every region the paper claims. Owner is "Q3" for a stem,
    "Q3 b.i" for a part, "Q3 b" for a container heading."""
    out = []
    for q in paper.questions:
        for seg in q.stem_segments:
            out.append((f"Q{q.question_number}", seg))
        for h in q.container_headings:
            for seg in h.segments:
                out.append((f"Q{q.question_number} {h.path}", seg))
        for p in q.parts:
            for seg in p.segments + p.body_segments:
                out.append((f"Q{q.question_number} {p.path or '-'}", seg))
    return out


def _content_lines(page: pymupdf.Page) -> list[tuple[str, tuple]]:
    """(text, bbox) of each line on the page that is question content: not a header,
    footer, page number, barcode, margin strip, answer line or other furniture."""
    raw = [line for block in page.get_text("rawdict")["blocks"] for line in block.get("lines", [])]
    lines = [(_line_text(line), tuple(line["bbox"]), line) for line in raw]
    pairs = [(t, b) for t, b, _ in lines]
    furniture = furniture_rows(pairs)
    w, h = page.rect.width, page.rect.height
    top = top_margin(HEADER_FOOTER_MARGIN)
    bottom = bottom_limit(pairs, h, HEADER_FOOTER_MARGIN)
    ack = [b[1] for t, b in pairs if t.strip() and _ACKNOWLEDGEMENTS_RE.search(t)]
    if ack:
        bottom = min(bottom, min(ack) - 1)
    out = []
    for text, bbox, line in lines:
        stripped = text.strip()
        if not stripped or not any(c.isalnum() for c in stripped):
            continue
        if bbox in furniture or is_noise_line(stripped) or _FURNITURE_RE.match(stripped):
            continue
        if DOTTED_LINE_RE.match(stripped.replace(" ", "")) or set(stripped) <= set("._ "):
            continue
        dx, dy = line.get("dir", (1, 0))
        if (abs(dy) > 0.1 or dx < 0) and _MARGIN_STRIP_RE.search(stripped):
            continue  # the margin strip; any other sideways text is content
        cy = (bbox[1] + bbox[3]) / 2
        if cy < top or cy > bottom or is_page_number(stripped, bbox, w, h):
            continue
        # white text (padding, hidden letters) prints nothing
        if all(span.get("color", 0) == 0xFFFFFF for span in line["spans"]):
            continue
        out.append((stripped, bbox))
    return out


def audit_paper(paper: Paper) -> PaperAudit:
    result = PaperAudit(paper.paper_key)
    add = lambda check, detail, page=None, y=None: result.findings.append(
        Finding(paper.paper_key, check, detail, page, y)
    )
    regions = _regions(paper)
    by_page: dict[int, list[tuple[str, BBox]]] = defaultdict(list)
    for owner, seg in regions:
        by_page[seg.page].append((owner, seg))

    qp = Path(paper.source_qp_path)
    if not qp.is_file():
        result.error = f"question paper not found: {qp}"
        return result

    with pymupdf.open(qp) as doc:
        types = classify_document(doc)
        claimed_pages = set(by_page)
        page_lines: dict[int, list[tuple[str, tuple]]] = {}
        for index, page in enumerate(doc):
            # Pages the paper's regions are on, and question pages between its first and
            # last region: a question page with no region at all is the biggest loss.
            # Every question page, not only those between the first and last region: a
            # paper whose last questions were lost entirely (0972/11's Q37-40 on the
            # copyright page) has no region after them to bracket their page.
            if index in claimed_pages or types[index] == PageType.QUESTION:
                page_lines[index] = _content_lines(page)
        marker_lines = {index: _marker_lines(doc[index]) for index in claimed_pages if index < doc.page_count}

    first = min(((seg.page, seg.y0) for _, seg in regions), default=None)
    if first and first[0] in page_lines:
        page_lines[first[0]] = [l for l in page_lines[first[0]] if l[1][3] > first[1]]

    # Coverage: content lines inside no region, grouped into runs - lost lines with no
    # claimed line between them - so one dropped part (text, figure labels) is one finding.
    withdrawn_markers = {re.findall(r"\([a-z]+\)|^\d+$", w)[-1] for w in paper.withdrawn if re.findall(r"\([a-z]+\)|^\d+$", w)}
    for index, lines in sorted(page_lines.items()):
        segs = [seg for _, seg in by_page.get(index, [])]
        if not segs and lines:
            # Cambridge 0478's copy of the pre-release material, before Section A: Q1 relies
            # on it but no question prints it - known, not a parse fault
            if not any(_WITHDRAWN_RE.search(t) or _PRE_RELEASE_RE.search(t) for t, _ in lines):
                add("page_unclaimed", f"{len(lines)} line(s), none in any region: {lines[0][0][:60]!r}", index + 1)
            continue
        runs: list[list[tuple[str, tuple]]] = []
        claimed_since = True
        for text, bbox in sorted(lines, key=lambda l: (l[1][1], l[1][0])):
            if any(_inside((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2, s) for s in segs):
                claimed_since = True
                continue
            if claimed_since:
                runs.append([])
                claimed_since = False
            runs[-1].append((text, bbox))
        for run in runs:
            words = sum(len(t.split()) for t, _ in run)
            if words < 2 and len(run) == 1 and len(run[0][0]) < 4:
                continue  # a stray label or unit outside a figure's ink box
            first = run[0][0]
            # what the board withdrew stays printed, and is dropped on purpose
            if any(_WITHDRAWN_RE.search(t) for t, _ in run) or any(
                first.startswith(m) for m in withdrawn_markers
            ):
                continue
            add("text_unclaimed", f"{len(run)} line(s): {first[:60]!r}", index + 1, run[0][1][1])

    for q in paper.questions:
        parts = [p for p in q.parts if not p.removed]
        # Overlap between different parts (a stem and its one synthetic part share a box).
        for i, a in enumerate(parts):
            for b in parts[i + 1:]:
                for sa in a.segments:
                    for sb in b.segments:
                        if sa.page != sb.page:
                            continue
                        dy = min(sa.y1, sb.y1) - max(sa.y0, sb.y0)
                        dx = min(sa.x1, sb.x1) - max(sa.x0, sb.x0)
                        if dy > _OVERLAP and dx > _OVERLAP:
                            add("overlap", f"Q{q.question_number} {a.path} and {b.path} share {dy:.0f}pt",
                                sa.page + 1, max(sa.y0, sb.y0))
        # Order: each part's first segment at or below the one before.
        starts = [(p, min(((s.page, s.y0) for s in p.segments), default=None)) for p in parts]
        previous = None
        for p, start in starts:
            if start is None:
                continue
            if previous and start < (previous[1][0], previous[1][1] - _OVERLAP):
                add("order", f"Q{q.question_number} {p.path} starts above {previous[0].path}",
                    start[0] + 1, start[1])
            previous = (p, start)
        # Own label inside own region.
        for p in parts:
            lb = p.label_bbox
            if lb is None or not p.segments:
                continue
            cx, cy = (lb.x0 + lb.x1) / 2, (lb.y0 + lb.y1) / 2
            if not any(s.page == lb.page and _inside(cx, cy, s) for s in p.segments + p.body_segments):
                add("label_outside", f"Q{q.question_number} {p.path}'s label is outside its region",
                    lb.page + 1, lb.y0)

    _check_marks_in_regions(paper, add, page_lines)
    _check_markers_used(paper, add, marker_lines)
    _check_scheme(paper, add)
    return result


_MARK_AT_END_RE = re.compile(r"\[(\d{1,2})\]\s*$")
# A mark allocation ends at the paper's mark margin; "[n]" ending further left is an array
# index or a table heading (0478 pseudocode "Names[50]", a trace table's "[1]" ... "[8]").
_MARGIN_REACH = 30.0


def _check_marks_in_regions(paper: Paper, add, page_lines: dict | None = None) -> None:
    """The mark allocations printed in each part's region add up to the marks the part
    carries as printed. Skipped on a paper that prints none in any region (multiple
    choice). Cambridge only: Edexcel marks are read differently (edexcel.normalise_marks)."""
    if not paper.board.lower().startswith("cambridge"):
        return
    with pymupdf.open(Path(paper.source_qp_path)) as doc:
        marks: dict[int, list[tuple[int, float, float, float]]] = {}  # page -> (n, x1, cx, cy)
        for seg_page in {s.page for q in paper.questions for p in q.parts for s in p.segments}:
            if seg_page >= doc.page_count:
                continue
            found = []
            for block in doc[seg_page].get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    # by line: a mark can be split over spans, ".......... [" + "4]"
                    # (0452/22 FM20 3(d)); its place is the last span's
                    spans = [s for s in line["spans"] if s["text"].strip()]
                    text = "".join(s["text"] for s in spans)
                    m = _MARK_AT_END_RE.search(text)
                    # a trace table's heading row, "[4] [5] ... [16]" to the margin (0478/23 ON19)
                    if m and len(re.findall(r"\[\d+\]", text)) == 1:
                        # a sideways mark (a table printed sideways) is at its own margin
                        x1 = spans[-1]["bbox"][2] if line.get("dir", (1, 0))[0] > 0.9 else float("inf")
                        y0, y1 = line["bbox"][1], line["bbox"][3]
                        cx = spans[-1]["bbox"][2] - 3 if x1 == float("inf") else x1 - 3
                        found.append((int(m.group(1)), x1, cx, (y0 + y1) / 2))
            marks[seg_page] = found
        edges = sorted(x1 for found in marks.values() for _, x1, _, _ in found if x1 != float("inf"))
        if not edges:
            return
        margin = edges[len(edges) // 2]

        def printed_in(seg: BBox) -> int:
            return sum(
                n for n, x1, cx, cy in marks.get(seg.page, [])
                if x1 > margin - _MARGIN_REACH and _inside(cx, cy, seg, 1.0)
            )

        for q in paper.questions:
            parts = [p for p in q.parts if not p.removed and p.segments]
            rows = []
            for p in parts:
                printed = sum(printed_in(seg) for seg in p.segments)
                expected = p.marks_printed if p.marks_printed is not None else p.marks
                # an unlettered question that prints only "[Total: 5]", cut from its crop on
                # purpose (0620/41 MJ21 Q2): the parser read no mark of the part's own
                if p.marks_printed is None and len(parts) == 1 and printed == 0:
                    continue
                rows.append((p, printed, expected))
            if page_lines is not None:
                _check_text_after_mark(q, parts, marks, margin, page_lines, add)
            # A group mark printed once needs no allowance: the parser records what each
            # part printed (marks_printed, 0 before the group's last part). A pair like
            # "(c) prints 0 of 6, (d) prints 8 of 2" is (c)'s mark inside (d)'s region.
            _flag_marks(q, rows, add)
            # And the question as a whole prints its total: a mark the parser never read
            # (0452/21 MJ26 1(a)'s sideways "[11]") leaves every part agreeing with its
            # region - nothing printed, nothing expected - but the question short.
            if rows and not paper.withdrawn and not any(p.removed for p in q.parts):
                shown = sum(printed_in(seg) for p in parts for seg in p.segments)
                shown += sum(printed_in(seg) for seg in q.stem_segments)
                if 0 < shown < q.total_marks:
                    first = parts[0].segments[0]
                    add("marks_question", f"Q{q.question_number}: regions print {shown} of its {q.total_marks}",
                        first.page + 1, first.y0)


# how far in from the page's left edge a line of a part's wording starts (x 72-118)
_TEXT_COLUMN_REACH = 150.0
# A line of wording: three words of two letters or more (as tree._reads_as_prose)
_PROSE_RE = re.compile(r"[A-Za-z]{2,}")


def _check_text_after_mark(q, parts, marks: dict, margin: float, page_lines: dict, add) -> None:
    """Wording inside a part's region after the part's own last printed mark: a part ends
    at its mark, and what follows leads the next part in. 0452/21 MJ26 1(a)'s region ran on
    past its "[11]" over the paragraph that opens (b) - every line of it inside some region,
    so coverage saw nothing wrong. Only parts that print a mark of their own."""
    regions = [(p, seg) for p in parts for seg in p.segments]
    # by the marks printed in the region, not those the parser read: 0452/21 MJ26 1(a)'s
    # "[11]" was never read
    for p in parts:
        found = [
            (seg.page, cy)
            for seg in p.segments
            for n, x1, cx, cy in marks.get(seg.page, [])
            if x1 > margin - _MARGIN_REACH and _inside(cx, cy, seg, 1.0)
        ]
        if not found:
            continue
        last = max(found)
        # a sideways mark (a table printed sideways) says nothing of order on its page:
        # only later pages are after it
        sideways = any(
            x1 == float("inf") for seg in p.segments for n, x1, cx, cy in marks.get(seg.page, [])
            if (seg.page, cy) == last
        )
        after = []
        for seg in p.segments:
            for text, bbox in page_lines.get(seg.page, []):
                cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
                if (seg.page, bbox[1]) <= last or not _inside(cx, cy, seg):
                    continue
                if sideways and seg.page <= last[0]:
                    continue
                # wording, from the text column: a figure's labels after the mark ("wire
                # carrying current", 0625/32 FM16 10(c)) sit inside the figure
                if len(_PROSE_RE.findall(text)) < 3 or _MARK_AT_END_RE.search(text):
                    continue
                if bbox[0] > _TEXT_COLUMN_REACH or bbox[3] - bbox[1] > bbox[2] - bbox[0]:
                    continue  # set sideways: a table's title ("Saleh Petty Cash Book")
                # shared with another part's region: an overlap, reported as one
                if any(o is not p and o_seg.page == seg.page and _inside(cx, cy, o_seg) for o, o_seg in regions):
                    continue
                after.append((seg.page, bbox[1], text))
        if after:
            page, y, text = min(after)
            add("text_after_mark", f"Q{q.question_number} {p.path or '-'}: after its mark, {text[:50]!r}",
                page + 1, y)


# a line opening with a part letter or subpart numeral - not one named in a sentence that
# wrapped ("(a)(iii)." / "(b),")
_MARKER_LINE_RE = re.compile(r"^\(([a-z]|i{1,3}|iv|vi{0,3}|ix|x)\)(?![.,;:)])")


def _marker_lines(page: pymupdf.Page) -> list[tuple[str, tuple, bool, int]]:
    """(marker, its box, bold, how many markers its row lists) for every line on the page
    that opens with a part marker."""
    out = []
    rows: dict[int, int] = defaultdict(int)
    found = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if line.get("dir", (1, 0))[0] < 0.9:
                continue
            spans = [s for s in line["spans"] if s["text"].strip() and s.get("color", 0) != 0xFFFFFF]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            m = _MARKER_LINE_RE.match(text)
            row = round(line["bbox"][1] / 4)
            rows[row] += len(re.findall(r"\((?:[a-z]|i{1,3}|iv|vi{0,3}|ix|x)\)", text))
            if m:
                first = spans[0]
                bold = bool(first["flags"] & 16) or "bold" in first.get("font", "").lower()
                x0 = first["bbox"][0] + (len(first["text"]) - len(first["text"].lstrip())) * first["size"] * 0.28
                found.append((m.group(0), (x0, line["bbox"][1], line["bbox"][2], line["bbox"][3]), bold, row))
    for marker, bbox, bold, row in found:
        out.append((marker, bbox, bold, rows[row]))
    return out


# how far a marker may sit from its tier's column (0625/32 FM16's "(b)" is 10pt off)
_COLUMN_REACH = 12.0


def _check_markers_used(paper: Paper, add, marker_lines: dict) -> None:
    """A line opening with a part marker at the paper's label columns that no part's label
    is: a part the parser ran into its neighbour (0625/32 FM16's "(b)(i)", read as a second
    (a)(i)). Cambridge sets its markers in bold; where most labels are, a plain one is a
    figure's (a dichotomous key's "(a)"), not a part."""
    if not paper.board.lower().startswith("cambridge"):
        return
    labels = []
    for q in paper.questions:
        if q.number_bbox is not None:
            labels.append(q.number_bbox)
        for h in q.container_headings:
            if h.label_bbox is not None:
                labels.append(h.label_bbox)
        for p in q.parts:
            labels += [b for b in (p.label_bbox, p.body_label_bbox) if b is not None]
    marked = [b for q in paper.questions for p in q.parts for b in (p.label_bbox, p.body_label_bbox)
              if b is not None and b.x0 > (q.number_bbox.x0 + 8 if q.number_bbox else 0)]
    if len(marked) < 2:
        return
    columns = sorted({round(b.x0) for b in marked})
    mostly_bold = sum(b.bold for b in marked) >= 0.67 * len(marked)
    withdrawn = bool(paper.withdrawn)
    for page, lines in marker_lines.items():
        for marker, bbox, bold, listed in lines:
            if listed > 2 or (mostly_bold and not bold):
                continue  # items listed along a row, "(i) .... (ii) ...."; or a figure's
            if not any(abs(bbox[0] - c) <= _COLUMN_REACH for c in columns):
                continue
            # on the same row: a fraction or tall glyph can lift a line's top 7pt above its label
            if any(l.page == page and l.y0 < bbox[3] and l.y1 > bbox[1] and abs(l.x0 - bbox[0]) <= _COLUMN_REACH for l in labels):
                continue
            if withdrawn:
                continue  # a withdrawn part's marker is printed and dropped on purpose
            add("marker_unused", f"{marker} at x {bbox[0]:.0f} is no part's label", page + 1, bbox[1])


# ---- the mark scheme ------------------------------------------------------------------

# the table's own header row, repeated on every page
_MS_HEADER_RE = re.compile(
    r"^(?:question|answer|answers|expected answer|marks?|guidance|partial marks|notes|additional guidance|"
    r"\s|number|key)+$", re.IGNORECASE)


def _check_scheme(paper: Paper, add) -> None:
    """The mark scheme against its PDF and the paper, Cambridge table schemes only:

    - ms_marks: parts and scheme rows joined together carry the same marks - a row read
      short (a marking point lost to the next row) or a part's mark misread shows here.
    - ms_row_missing / ms_row_unread: every row read is one composing can copy, and the
      other way round - a row with no box prints as re-typed text, without its layout.
    - ms_text_unclaimed: inside the table, every line of the scheme is in some row's box:
      a line in none is a row lost or cut short."""
    if not paper.board.lower().startswith("cambridge") or not paper.mark_scheme:
        return
    entries = {e.id: e for e in paper.mark_scheme}
    _check_scheme_marks(paper, entries, add)

    ms = Path(paper.source_ms_path or "")
    if not ms.is_file():
        return
    from qbx.markscheme import parse_mark_scheme_layout

    layout = parse_mark_scheme_layout(ms)
    if not layout.rows:
        return  # an answer key: no rows to copy
    detected = {k for k, e in entries.items() if not e.manual}
    for key in sorted(detected - set(layout.rows)):
        add("ms_row_missing", f"scheme row {key} has no box to copy")
    withdrawn = {re.sub(r"[()]+", ".", w).strip(".") for w in paper.withdrawn}
    for key in sorted(set(layout.rows) - set(entries)):
        # a question's heading-only row, dropped on purpose (pipeline); a withdrawn part's
        if key.endswith(".stem") or any(key == w or key.startswith(w + ".") for w in withdrawn):
            continue
        seg = layout.rows[key].segments[0]
        add("ms_row_unread", f"a box labelled {key} that no scheme row reads", seg.page + 1, seg.y0)

    segs: dict[int, list[BBox]] = defaultdict(list)
    for row in layout.rows.values():
        for seg in row.segments:
            segs[seg.page].append(seg)
    for e in entries.values():  # rows linked by hand carry their own place
        for seg in e.segments:
            segs[seg.page].append(seg)
    with pymupdf.open(ms) as doc:
        for index, page_segs in sorted(segs.items()):
            if index >= doc.page_count:
                continue
            page = doc[index]
            page.remove_rotation()  # the layout is measured on upright pages
            top, bottom = min(s.y0 for s in page_segs), max(s.y1 for s in page_segs)
            left, right = min(s.x0 for s in page_segs), max(s.x1 for s in page_segs)
            lost = []
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if not any(c.isalnum() for c in text) or _MS_HEADER_RE.match(text):
                        continue
                    if re.match(r"^\[?total[:\s]", text, re.IGNORECASE):
                        continue  # a question's total, under its last row
                    x0, y0, x1, y1 = line["bbox"]
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    if not (top < cy < bottom and left < cx < right):
                        continue  # running header, footer, outside the table
                    if layout.header and layout.header.page == index and _inside(cx, cy, layout.header):
                        continue
                    if not any(_inside(cx, cy, s, 1.0) for s in page_segs):
                        lost.append((y0, text))
            # a header word in pieces ("Ma" | "rks"), a stray digit: not a lost row
            if lost and any(len(text) >= 4 for _, text in lost):
                lost.sort()
                add("ms_text_unclaimed", f"{len(lost)} line(s) in no row: {lost[0][1][:50]!r}", index + 1, lost[0][0])


def _check_scheme_marks(paper: Paper, entries: dict, add) -> None:
    parts = {p.id: (q, p) for q in paper.questions for p in q.parts if not p.removed}
    linked: dict[str, set[str]] = defaultdict(set)
    for j in paper.joins:
        if j.part_id in parts and j.mark_scheme_entry_id in entries:
            linked["p:" + j.part_id].add("e:" + j.mark_scheme_entry_id)
            linked["e:" + j.mark_scheme_entry_id].add("p:" + j.part_id)
    seen: set[str] = set()
    for start in sorted(linked):
        if start in seen:
            continue
        group, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node in group:
                continue
            group.add(node)
            stack.extend(linked[node] - group)
        seen |= group
        group_parts = [parts[n[2:]] for n in sorted(group) if n.startswith("p:")]
        paper_marks = sum(p.marks for _, p in group_parts)
        scheme_marks = sum(entries[n[2:]].marks for n in group if n.startswith("e:"))
        if paper_marks != scheme_marks:
            q = group_parts[0][0]
            names = ", ".join(p.path or "-" for _, p in group_parts)
            add("ms_marks", f"Q{q.question_number} {names}: paper {paper_marks} marks, scheme {scheme_marks}")


def _flag_marks(q, run: list, add) -> None:
    for p, printed, expected in run:
        if printed != expected:
            first = p.segments[0]
            add("marks_in_region",
                f"Q{q.question_number} {p.path or '-'}: region prints {printed}, part has {expected}",
                first.page + 1, first.y0)


def shape_findings(papers: list[Paper]) -> list[Finding]:
    """Papers whose total marks or question count differ from most papers of the same
    board, subject and component across sittings."""
    groups: dict[tuple, list[Paper]] = defaultdict(list)
    for paper in papers:
        component = (paper.paper or "")[:1] if paper.board.lower().startswith("cambridge") else paper.paper
        groups[(paper.board, paper.subject, component)].append(paper)
    out = []
    for (_, subject, component), members in groups.items():
        if len(members) < 4:
            continue
        totals = Counter(sum(q.total_marks for q in p.questions) for p in members)
        usual_total, n_total = totals.most_common(1)[0]
        if n_total / len(members) < 0.6:
            continue  # no settled shape for this component
        counts = Counter(len(p.questions) for p in members)
        for p in members:
            total = sum(q.total_marks for q in p.questions)
            cover = p.cover_total_marks
            if total != usual_total and cover != total and not p.withdrawn:
                out.append(Finding(p.paper_key, "shape_marks",
                                   f"{total} marks; {n_total} of {len(members)} {subject}/{component} papers have {usual_total}"))
            n = len(p.questions)
            usual_n, n_n = counts.most_common(1)[0]
            if n_n / len(members) >= 0.6 and abs(n - usual_n) > max(2, usual_n * 0.25):
                out.append(Finding(p.paper_key, "shape_questions",
                                   f"{n} questions; {n_n} of {len(members)} {subject}/{component} papers have {usual_n}"))
    return out


# ---- snapshots ------------------------------------------------------------------------

def snapshot(paper: Paper) -> dict:
    """A paper's structure, to the point - no text, so a baseline holds nothing from the
    paper itself and can be kept anywhere."""
    links: dict[str, list[str]] = defaultdict(list)
    entries = {e.id: e for e in paper.mark_scheme}
    for j in paper.joins:
        if j.mark_scheme_entry_id in entries:
            e = entries[j.mark_scheme_entry_id]
            links[j.part_id].append(f"{e.question_number}.{e.part_path}:{e.marks}")
    r = lambda seg: [seg.page, round(seg.x0), round(seg.y0), round(seg.x1), round(seg.y1)]
    return {
        "cover": paper.cover_total_marks,
        "questions": {
            str(q.question_number): {
                "total": q.total_marks,
                "stem": [r(s) for s in q.stem_segments],
                "parts": {
                    (p.path or "-"): {"marks": p.marks, "ms": sorted(links.get(p.id, [])),
                                      "seg": [r(s) for s in p.segments]}
                    for p in q.parts
                },
            }
            for q in paper.questions
        },
        "ms_entries": len(paper.mark_scheme),
    }


def diff_snapshots(old: dict, new: dict) -> list[str]:
    """What changed in one paper, one line each."""
    out = []
    if old.get("cover") != new.get("cover"):
        out.append(f"cover total {old.get('cover')} -> {new.get('cover')}")
    oq, nq = old["questions"], new["questions"]
    for n in sorted(set(oq) | set(nq), key=int):
        if n not in nq:
            out.append(f"Q{n} gone")
            continue
        if n not in oq:
            out.append(f"Q{n} new")
            continue
        a, b = oq[n], nq[n]
        if a["total"] != b["total"]:
            out.append(f"Q{n} total {a['total']} -> {b['total']}")
        if a["stem"] != b["stem"]:
            out.append(f"Q{n} stem region moved")
        for path in sorted(set(a["parts"]) | set(b["parts"])):
            pa, pb = a["parts"].get(path), b["parts"].get(path)
            if pa is None or pb is None:
                out.append(f"Q{n} {path} {'new' if pa is None else 'gone'}")
                continue
            if pa["marks"] != pb["marks"]:
                out.append(f"Q{n} {path} marks {pa['marks']} -> {pb['marks']}")
            if pa["ms"] != pb["ms"]:
                out.append(f"Q{n} {path} scheme {pa['ms']} -> {pb['ms']}")
            if pa["seg"] != pb["seg"]:
                moved = _moved(pa["seg"], pb["seg"])
                if moved:
                    out.append(f"Q{n} {path} region {moved}")
    if old.get("ms_entries") != new.get("ms_entries"):
        out.append(f"scheme rows {old.get('ms_entries')} -> {new.get('ms_entries')}")
    return out


def _moved(a: list, b: list, tolerance: int = 2) -> str:
    if len(a) != len(b):
        return f"{len(a)} -> {len(b)} segment(s)"
    worst = max((max(abs(x - y) for x, y in zip(sa[1:], sb[1:])) if sa[0] == sb[0] else 999) for sa, sb in zip(a, b))
    return "" if worst <= tolerance else f"moved up to {worst}pt" if worst < 999 else "changed page"


def load_papers(out_dir: Path, only: str = "", board: str = "cambridge") -> list[Paper]:
    return [Paper.model_validate_json(p.read_text(encoding="utf-8")) for p in paper_paths(out_dir, only, board)]


def _audit_one(path: str) -> PaperAudit:
    paper = Paper.model_validate_json(Path(path).read_text(encoding="utf-8"))
    try:
        return audit_paper(paper)
    except Exception as e:  # one broken file mustn't end a run over a thousand
        return PaperAudit(paper.paper_key, error=f"{type(e).__name__}: {e}")


def paper_paths(out_dir: Path, only: str = "", board: str = "cambridge") -> list[Path]:
    """paper.json files under `out_dir`, those whose key starts with `only`, of `board`
    ("" for every board). The board is read from the file's head, not the whole model."""
    out = []
    for path in sorted(out_dir.glob("*/paper.json")):
        if only and not path.parent.name.startswith(only):
            continue
        if board:
            head = path.read_text(encoding="utf-8")[:400].lower()
            if f'"board":"{board}' not in head.replace(" ", ""):
                continue
        out.append(path)
    return out


def audit_all(out_dir: Path, only: str = "", workers: int = 1, board: str = "cambridge") -> list[PaperAudit]:
    paths = [str(p) for p in paper_paths(out_dir, only, board)]
    if workers <= 1:
        return [_audit_one(p) for p in paths]
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(workers) as pool:
        return list(pool.map(_audit_one, paths, chunksize=4))


def save_baseline(papers: list[Paper], path: Path) -> None:
    path.write_text(json.dumps({p.paper_key: snapshot(p) for p in papers}, separators=(",", ":")), encoding="utf-8")
