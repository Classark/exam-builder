"""Composes a custom practice paper from selected question parts into two separate PDFs -
a Question Paper and a Mark Scheme - matching the structural conventions of a real paper
(sequential question numbers, lettered parts, preserved indentation, page numbering)
without reproducing any board's actual branding/letterhead (CLAUDE.md: board names and
positioning are metadata-only).

Selection granularity is per-question: a `QuizGroup` is one source question plus the subset
of its parts to include, in the order they should appear. The output relabels them from
scratch, and does so *hierarchically*:

    picking 3(d) and 3(c)(ii) and 3(c)(i)  ->  (a), then (b)(i) and (b)(ii)

i.e. subparts of the same original container collapse under one new letter and keep roman
numerals beneath it, rather than each being promoted to its own top-level letter. Whenever
any subpart is selected, its container's heading ("(c) All smartphones have a MAC address.")
is rendered above it - lifted out on its own, a subpart loses the context it depends on.

Two properties this module has to guarantee, both learned from real output:

1. Nothing invisible comes along for the ride. Source regions are copied as vector content
   (`show_pdf_page`, never rasterised), but a clip rectangle only bounds what is *drawn* -
   text outside it can survive in the copied content stream and resurface under Ctrl+A or
   any text extraction. So every region is copied from a throwaway, single-use reopen of
   the source whose out-of-region content has been genuinely redacted away first.

2. Relabelling is a real edit, not an overlay. The original glyphs are deleted (same
   redaction pass) and the new label is drawn at the original's own baseline, in its own
   font size and weight - not a floating "Question N" title bar bolted above the content,
   which is what produced mismatched, duplicated numbering before.
"""

import html
import re
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pymupdf

from qbx.crops import is_noise_line
from qbx.fingerprint import session_name
from qbx.markscheme import MSLayout, MSRowLayout
from qbx.markscheme_edexcel import scheme_layout
from qbx.pages import formula_sheet_region, periodic_table_region
from qbx.schema import BBox, ContainerHeading, LabelRef, Paper, Question, QuestionPart

PAGE_WIDTH, PAGE_HEIGHT = pymupdf.paper_size("a4")
# The periodic table every composed paper prints, whichever source paper its questions came
# from - one clean copy rather than whichever source page happened to be first, which
# carried that paper's own footer and copyright line.
PERIODIC_TABLE_PDF = Path(__file__).parent / "assets" / "periodic_table.pdf"
MARGIN = 50
ITEM_GAP = 14  # between the blocks of one question (stem, heading, parts)
QUESTION_GAP = 26  # between questions, which a real paper separates more clearly
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN
# room reserved at the top for the page number and at the foot for the paper's name, the
# way a real paper carries them clear of the question content
CONTENT_TOP = MARGIN + 22
CONTENT_BOTTOM = PAGE_HEIGHT - MARGIN - 20

# Cambridge sets its papers in Arial. Helvetica (PyMuPDF's built-in "helv"/"hebo") is
# metrically compatible and is the fallback, but the real font file is used when present so
# a redrawn label is indistinguishable from the glyphs it replaced.
_ARIAL_FILES = {
    True: Path("C:/Windows/Fonts/arialbd.ttf"),
    False: Path("C:/Windows/Fonts/arial.ttf"),
}
_FALLBACK_FONTS = {True: "hebo", False: "helv"}
# Maths papers and schemes (0580) are set in Times New Roman instead - a label redrawn in
# Arial stood out beside the Times text around it. Times-Roman is the base-14 stand-in.
_TIMES_FILES = {
    True: Path("C:/Windows/Fonts/timesbd.ttf"),
    False: Path("C:/Windows/Fonts/times.ttf"),
}
_TIMES_FALLBACK_FONTS = {True: "tibo", False: "tiro"}

# the four option letters of a multiple-choice item, in order, each starting a word - they
# come out of extraction glued to their option text ("Aa liquid being frozen Ba solid...")
_MCQ_OPTIONS_RE = re.compile(r"\sA.+\sB.+\sC.+\sD", re.S)


@dataclass
class QuizGroup:
    paper_key: str
    question_number: int
    part_paths: list[str]  # selected leaf parts, in the order they should appear
    # other papers printing the same question (a classified booklet's reprints), as the
    # sources list names them - "0620/42 May/June 2023"
    also_in: list[str] = field(default_factory=list)


@dataclass
class _Block:
    """One contiguous region to render, plus the label edit to apply to it (if any)."""

    segments: list[BBox]
    relabel: tuple[LabelRef, str] | None = None
    # a question stem or container heading - it introduces what follows, so it must never
    # be left at the foot of a page with that content overleaf
    introduces: bool = False
    # The question's number, rewritten on this block's first segment - for a question with
    # no stem of its own (see _place_question_number), where no stem block carries it.
    number: tuple[LabelRef, str] | None = None
    # A4 points per source point - 1.0 for an A4 source. Its segments are in the source's
    # own coordinates, so a paper printed on A3 has to be brought down to A4 before it can
    # share a layout with the rest (see _source_unit).
    unit: float = 1.0
    # A part's own mark, to print at the margin where the source didn't - its mark was the
    # group's or the question's (older Edexcel papers) - as "(2)" or "[2]" by board; any
    # mark the source printed there (the group's) is blanked first.
    marks: int | None = None
    mark_style: str = "[{}]"


@lru_cache(maxsize=512)
def _source_unit(source_path: str) -> float:
    """How many A4 points one of this source's points is worth, so every source is laid
    out as if it had been printed on A4.

    Real-paper finding (0620 s17 qp 11, 31 and 62): three downloads are A3 (842 x 1191),
    everything else A4. The paper's single layout took its scale from the widest crop, so
    the A3 paper's crops (out to x=780) shrank every A4 question to 74% - and then the A3
    question itself, already 1.41x larger, came out oversized beside them."""
    try:
        with pymupdf.open(source_path) as doc:
            width = doc[0].rect.width if doc.page_count else PAGE_WIDTH
    except (OSError, RuntimeError, ValueError):
        return 1.0
    return PAGE_WIDTH / width if width > 0 else 1.0


@dataclass
class _ResolvedGroup:
    paper: Paper
    question: Question
    blocks: list[_Block] = field(default_factory=list)
    marks_total: int = 0
    # (heading text for the mark scheme, marks, part id) per selected leaf, in output order
    ms_entries: list[tuple[str, int, str]] = field(default_factory=list)
    missing_paths: list[str] = field(default_factory=list)
    has_multiple_choice: bool = False
    # the source parts the teacher picked, for the sources list
    part_paths: list[str] = field(default_factory=list)
    also_in: list[str] = field(default_factory=list)


def _letters(n: int) -> list[str]:
    alphabet = string.ascii_lowercase
    return [alphabet[i] if i < len(alphabet) else f"z{i}" for i in range(n)]


def _roman(n: int) -> str:
    """1 -> "i", 4 -> "iv". Subpart counts are small, but this stays correct past viii."""
    numerals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = []
    for value, symbol in numerals:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def _group_selection(part_paths: list[str]) -> list[tuple[str | None, list[str]]]:
    """Splits the selected paths into one entry per *new top-level letter*, in the order
    those letters should be assigned. Subparts sharing a container ("c.i", "c.ii") collapse
    into a single entry keyed by that container; every top-level part stands alone.
    """
    groups: list[tuple[str | None, list[str]]] = []
    index_by_container: dict[str, int] = {}
    for path in part_paths:
        if "." in path:
            container = path.rsplit(".", 1)[0]
            if container not in index_by_container:
                index_by_container[container] = len(groups)
                groups.append((container, []))
            groups[index_by_container[container]][1].append(path)
        else:
            groups.append((None, [path]))
    return groups


_MCQ_ANSWER_RE = re.compile(r"^\s*[A-D]\s*$")


def is_multiple_choice(part: QuestionPart, paper: Paper | None = None) -> bool:
    """A multiple-choice item extracts as a single unlettered part worth one mark - it
    prints no "[n]" of its own, so pipeline.py backfills the mark from its mark scheme.
    That shape alone would also match a one-mark unlettered written answer, so one of
    these has to agree:

    - the paper's cover calls it a multiple-choice paper ("Paper 1 Multiple Choice (Core)")
    - its mark scheme answer is a single letter A-D
    - its options are lettered A to D inside its own text

    The text alone missed 5 of 40 items on 0620 s17 qp 11 - options drawn as labels on a
    diagram, or glued together in extraction ("ACl2BCH4CH2O DHCl") - which left them off
    the answer sheet."""
    if part.path or part.marks != 1:
        return False
    if paper is not None:
        if "multiple choice" in paper.paper_title.lower().replace("-", " "):
            return True
        answers = {e.id: e.answer_text for e in paper.mark_scheme}
        joined = [answers.get(j.mark_scheme_entry_id, "") for j in paper.joins if j.part_id == part.id]
        if joined and all(_MCQ_ANSWER_RE.match(answer) for answer in joined):
            return True
    return bool(_MCQ_OPTIONS_RE.search(part.text))


def _leaf_region(part: QuestionPart) -> tuple[list[BBox], LabelRef | None]:
    """A leaf on its own: its own marker and content, never its container's absorbed intro
    (which the container heading renders instead - see schema.QuestionPart.body_segments)."""
    if part.body_segments:
        return part.body_segments, part.body_label_bbox
    return part.segments, part.label_bbox


def _resolve_group(
    group: QuizGroup, out_dir: Path, papers: dict[str, Paper | None], quiz_number: int
) -> _ResolvedGroup | None:
    if group.paper_key not in papers:
        paper_json = out_dir / group.paper_key / "paper.json"
        papers[group.paper_key] = (
            Paper.model_validate_json(paper_json.read_text(encoding="utf-8"))
            if paper_json.exists()
            else None
        )
    paper = papers[group.paper_key]
    if paper is None:
        return None
    question = next((q for q in paper.questions if q.question_number == group.question_number), None)
    if question is None:
        return None

    parts_by_path = {p.path: p for p in question.parts}
    headings_by_path: dict[str, ContainerHeading] = {h.path: h for h in question.container_headings}
    resolved = _ResolvedGroup(
        paper=paper, question=question, part_paths=list(group.part_paths), also_in=list(group.also_in)
    )

    selection = _group_selection(group.part_paths)
    letters = _letters(len(selection))
    lettered = any(path for _, paths in selection for path in paths)

    if lettered and question.stem_segments:
        relabel = (
            (question.number_bbox, str(quiz_number)) if question.number_bbox is not None else None
        )
        resolved.blocks.append(_Block(segments=question.stem_segments, relabel=relabel, introduces=True))

    for letter, (container, paths) in zip(letters, selection):
        if container is not None:
            heading = headings_by_path.get(container)
            if heading is not None and heading.segments:
                heading_relabel = (
                    (heading.label_bbox, f"({letter})") if heading.label_bbox is not None else None
                )
                resolved.blocks.append(
                    _Block(segments=heading.segments, relabel=heading_relabel, introduces=True)
                )

        for index, path in enumerate(paths):
            part = parts_by_path.get(path)
            if part is None:
                resolved.missing_paths.append(path)
                continue
            segments, label = _leaf_region(part)
            if container is None:
                new_label = f"({letter})" if path else str(quiz_number)
                # an unlettered question has no part marker of its own - its only label is
                # the question number, which the stem block above didn't render for it.
                if not path:
                    label = question.number_bbox
            else:
                new_label = f"({_roman(index + 1)})"

            relabel = (label, new_label) if label is not None else None
            if segments:
                own = part.marks if part.marks_printed is not None and part.marks_printed != part.marks else None
                resolved.blocks.append(_Block(
                    segments=segments, relabel=relabel, marks=own,
                    mark_style="({})" if paper.board == "Edexcel" else "[{}]",
                ))
            resolved.marks_total += part.marks
            resolved.has_multiple_choice |= is_multiple_choice(part, paper)
            ms_label = f"({letter})" if path else ""
            if container is not None:
                ms_label += f"({_roman(index + 1)})"
            resolved.ms_entries.append((ms_label, part.marks, part.id))

    if lettered and not question.stem_segments:
        _place_question_number(resolved.blocks, question.number_bbox, quiz_number)
    unit = _source_unit(paper.source_qp_path)
    for block in resolved.blocks:
        block.unit = unit
    return resolved


def _place_question_number(blocks: list[_Block], number: LabelRef | None, quiz_number: int) -> None:
    """A question whose number shares a line with its first part ("12 (a) Write as...") has
    no stem, so no stem block carries its number. Real-paper finding (0580 m25 qp 22): the
    composed paper kept the source's "12" beside the new "(a)", and picking only 12(b) lost
    the number altogether, since the line holding it wasn't copied.

    So the first block carries it instead: rewritten in place when the number sits inside
    that block's first segment, and otherwise set in the question-number column on the
    block's own first line - where the source would have put it."""
    if number is None or not blocks:
        return
    first = blocks[0]
    segment = first.segments[0]
    inside = segment.page == number.page and segment.y0 - 1 <= number.y0 <= segment.y1
    if inside:
        first.number = (number, str(quiz_number))
    elif first.relabel is not None:
        line = first.relabel[0]
        first.number = (
            number.model_copy(
                update={
                    "page": line.page,
                    "y0": line.y0,
                    "y1": line.y1,
                    "origin_y": line.origin_y,
                }
            ),
            str(quiz_number),
        )


@dataclass
class _Layout:
    """Shared placement for every block of one question: a single scale and a single
    left origin, so the source's own indentation (part letters outdented from body text,
    subparts indented further) survives, and the question as a whole sits centred on the
    page the way it does in a real paper - rather than each block being flushed left."""

    origin_x: float
    min_x0: float
    scale: float

    @classmethod
    def for_blocks(cls, blocks: list[_Block]) -> "_Layout":
        # in A4 points, so sources printed at another size share one layout (_source_unit)
        spans = [(s.x0 * block.unit, s.x1 * block.unit) for block in blocks for s in block.segments]
        if not spans:
            return cls(origin_x=MARGIN, min_x0=0.0, scale=1.0)
        min_x0 = min(x0 for x0, _ in spans)
        max_x1 = max(x1 for _, x1 in spans)
        width = max(max_x1 - min_x0, 1.0)
        scale = min(CONTENT_WIDTH / width, 1.0)
        return cls(origin_x=(PAGE_WIDTH - width * scale) / 2, min_x0=min_x0, scale=scale)


def _block_height(block: _Block, layout: _Layout) -> float:
    if not block.segments:
        return 0.0
    heights = [(s.y1 - s.y0) * layout.scale * block.unit for s in block.segments]
    return sum(heights) + ITEM_GAP * (len(heights) - 1)


# International A Level rules its answer space 20pt apart (WMA11 June 2024: y 171.9, 191.9...)
_ANSWER_LINE_GAP = 20.0
# what a question's "(Total for Question n = m marks)" line takes at the foot of its space
_TOTAL_ROOM = 34.0
_UNDERSCORE_LINE_RE = re.compile(r"^\s*_{8,}\s*$", re.MULTILINE)
_CONTINUED_RE = re.compile(r"^\s*Question\s+(\d{1,2})\s+continued\s*$", re.IGNORECASE | re.MULTILINE)


@lru_cache(maxsize=64)
def _answer_pages_by_question(source_path: str, mtime_ns: int) -> dict[int, int] | None:
    """How many "Question n continued" pages of ruled lines the paper gives each question,
    or None for a paper that doesn't set its answer space that way (Cambridge's dotted lines
    under each part, Edexcel International GCSE's)."""
    pages: dict[int, int] = {}
    lined = False
    with pymupdf.open(source_path) as doc:
        for page in doc:
            text = page.get_text("text")
            if len(_UNDERSCORE_LINE_RE.findall(text)) >= 5:
                lined = True
            for match in _CONTINUED_RE.finditer(text):
                pages[int(match.group(1))] = pages.get(int(match.group(1)), 0) + 1
                break
    return pages if lined else None


def _source_answer_pages(source_path: str, question_number: int) -> int | None:
    try:
        by_question = _answer_pages_by_question(source_path, Path(source_path).stat().st_mtime_ns)
    except (OSError, RuntimeError, ValueError):
        return None
    return None if by_question is None else by_question.get(question_number, 0)


class _PageFlow:
    """Packs content blocks onto A4 pages by their real rendered height - never one block
    per page, which would waste most of a page on a one-line part."""

    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        self.page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        self.cursor_y = CONTENT_TOP
        # the pages holding questions, as opposed to answer sheets and data pages - what
        # a board's page style (the Edexcel frame) is drawn on
        self.question_pages = [self.page.number]

    def _new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        self.cursor_y = CONTENT_TOP
        self.question_pages.append(self.page.number)

    def place_total(self, text: str, size: float = 10.5) -> None:
        """Edexcel's close to a question - "(Total for Question 3 = 8 marks)", right-aligned
        in bold over a rule across the page - or to the paper."""
        self.reserve(30)
        right = PAGE_WIDTH - MARGIN
        _insert_right(self.page, right, self.cursor_y + 12, text, fontsize=size, fontname="hebo")
        self.page.draw_line((MARGIN - 8, self.cursor_y + 20), (right + 8, self.cursor_y + 20), color=(0.4, 0.4, 0.4), width=0.6)
        self.cursor_y += 30 + ITEM_GAP

    def place_answer_space(self, question_number: int, extra_pages: int) -> None:
        """International A Level's answer space, drawn fresh: ruled lines down the rest of
        the question's page, then `extra_pages` more pages of them headed "Question n
        continued", leaving room at the foot of the last for the question's total. The
        source's own lines are never copied - they're cut from every crop (crops.py) since
        they sit after the last part, shared by them all."""
        foot = CONTENT_BOTTOM - _TOTAL_ROOM

        def rule_lines(top: float) -> None:
            y = top + _ANSWER_LINE_GAP
            while y <= foot:
                self.page.draw_line((MARGIN, y), (PAGE_WIDTH - MARGIN, y), color=(0.55, 0.55, 0.55), width=0.5)
                y += _ANSWER_LINE_GAP
            self.cursor_y = foot

        if foot - self.cursor_y >= 2 * _ANSWER_LINE_GAP:
            rule_lines(self.cursor_y)
        for _ in range(extra_pages):
            self._new_page()
            self.page.insert_text((MARGIN, self.cursor_y + 10), f"Question {question_number} continued", fontsize=9, fontname="hebo")
            rule_lines(self.cursor_y + 16)

    def start_new_page(self) -> None:
        """A fresh page, unless the current one is still empty."""
        if self.cursor_y > CONTENT_TOP:
            self._new_page()

    def place_heading(self, text: str) -> None:
        self.page.insert_text((MARGIN, self.cursor_y + 12), text, fontsize=13, fontname="hebo")
        self.cursor_y += 12 + QUESTION_GAP

    def insert_pages(self, draw) -> None:
        """Pages that `draw(doc)` adds at this point - the answer sheet - with the flow
        carrying on on a fresh page after them. The flow's own page is always the
        document's last, so an empty one is simply dropped rather than left blank."""
        if self.cursor_y <= CONTENT_TOP:
            self.doc.delete_page(-1)
            self.question_pages.pop()
        draw(self.doc)
        self._new_page()

    def reserve(self, height: float) -> None:
        if self.cursor_y + height > CONTENT_BOTTOM and self.cursor_y > CONTENT_TOP:
            self._new_page()

    def place_block(
        self, source_path: str, block: _Block, layout: _Layout, keep_with_next: float = 0.0
    ) -> None:
        """`keep_with_next` is height that must also fit below this block before it's worth
        starting here - it stops a question stem or a container heading from being stranded
        alone at the foot of a page with the part it introduces overleaf, the way a real
        paper never splits them."""
        relabel_page = block.relabel[0].page if block.relabel is not None else None
        for index, segment in enumerate(block.segments):
            relabels = []
            if block.relabel is not None and segment.page == relabel_page:
                relabels.append(block.relabel)
            if block.number is not None and index == 0:
                relabels.append(block.number)
            last = index == len(block.segments) - 1
            mark = block.mark_style.format(block.marks) if block.marks is not None and block.marks > 0 and last else None
            self._place_segment(source_path, segment, relabels, layout, keep_with_next, block.unit, mark)
            relabel_page = None  # only the first (labelled) segment carries the marker
            keep_with_next = 0.0  # only the block's first segment has to keep the company

    def _place_segment(
        self,
        source_path: str,
        segment: BBox,
        relabels: list[tuple[LabelRef, str]],
        layout: _Layout,
        keep_with_next: float = 0.0,
        unit: float = 1.0,
        mark: str | None = None,
    ) -> None:
        clip = pymupdf.Rect(segment.x0, segment.y0, segment.x1, segment.y1)
        scale = layout.scale * unit
        rendered_w, rendered_h = clip.width * scale, clip.height * scale

        available_on_fresh_page = CONTENT_BOTTOM - CONTENT_TOP
        if rendered_h > available_on_fresh_page:
            # taller than a whole page even alone - shrink this one block further so it
            # still renders completely rather than running off the page.
            scale = min(scale, available_on_fresh_page / clip.height)
            rendered_w, rendered_h = clip.width * scale, clip.height * scale

        self.reserve(rendered_h + keep_with_next)
        left = layout.origin_x + (segment.x0 * unit - layout.min_x0) * layout.scale
        left = max(MARGIN, min(left, PAGE_WIDTH - MARGIN - rendered_w))
        target = pymupdf.Rect(left, self.cursor_y, left + rendered_w, self.cursor_y + rendered_h)

        blanked = _printed_marks(source_path, segment.page, clip) if mark else []
        trimmed = _trimmed_source(source_path, segment.page, clip, [label for label, _ in relabels] + blanked)
        try:
            self.page.show_pdf_page(target, trimmed, segment.page, clip=clip)
        finally:
            trimmed.close()

        for label, new_text in relabels:
            self._draw_label(clip, target, scale, label, new_text)
        if mark:
            # where the source printed the group's, else at the margin by the part's foot
            spot = blanked[-1] if blanked else None
            size = 10.0
            width = pymupdf.get_text_length(mark, fontsize=size, fontname="hebo")
            if spot is not None:
                baseline = target.y0 + (spot.y1 - clip.y0) * scale - 2
            else:
                baseline = target.y1 - 3
            self.page.insert_text(
                (PAGE_WIDTH - MARGIN - width, baseline), mark, fontsize=size, fontname="hebo",
                color=(0.35, 0.35, 0.35),
            )

        self.cursor_y += rendered_h + ITEM_GAP

    def _draw_label(
        self, clip: pymupdf.Rect, target: pymupdf.Rect, scale: float, label: LabelRef, new_text: str
    ) -> None:
        # show_pdf_page maps `clip` onto `target` with a uniform scale + translate (target
        # was built from clip's own aspect ratio), so the same transform places the label.
        origin_x = label.origin_x or label.x0
        origin_y = label.origin_y or label.y1
        x = target.x0 + (origin_x - clip.x0) * scale
        baseline_y = target.y0 + (origin_y - clip.y0) * scale
        font, fontfile = _label_font(label.bold, label.font)
        self.page.insert_text(
            (x, baseline_y), new_text, fontsize=label.size * scale, fontname=font, fontfile=fontfile
        )

    def place_placeholder(self, message: str) -> None:
        self.reserve(20)
        self.page.insert_text(
            (MARGIN, self.cursor_y + 10), message, fontsize=10, fontname="helv",
            color=(0.6, 0.2, 0.2),
        )
        self.cursor_y += 20 + ITEM_GAP


def _label_font(bold: bool, source_font: str = "") -> tuple[str, str | None]:
    """(fontname, fontfile) for redrawing a label in the source's own family."""
    if "times" in source_font.lower():
        path = _TIMES_FILES[bold]
        if path.exists():
            return ("timesbd" if bold else "times"), str(path)
        return _TIMES_FALLBACK_FONTS[bold], None
    path = _ARIAL_FILES[bold]
    if path.exists():
        return ("arialbd" if bold else "arial"), str(path)
    return _FALLBACK_FONTS[bold], None


def _noise_rects(page: pymupdf.Page, clip: pymupdf.Rect) -> list[pymupdf.Rect]:
    """Page furniture that falls *inside* the region and so can't be cut away by tightening
    its bounds - a per-page barcode, a "Question 5 starts on the next page." line.

    crops.py already keeps these out of the bounds it measures, which drops a segment that
    holds nothing else; this is the other half, for when one overlaps the band where real
    content starts (real-paper finding: 0620 w25 qp 41's barcode sits at y=54-59 and the
    first body line at 58.3, so no horizontal or vertical trim can separate them).
    """
    rects = []
    for block in page.get_text("dict", clip=clip)["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            if is_noise_line(text):
                rects.append(pymupdf.Rect(line["bbox"]))
    return rects


_PRINTED_MARK_RE = re.compile(r"^(?:\(\d{1,2}\)|\[\d{1,2}\])$")


def _printed_marks(source_path: str, page_index: int, clip: pymupdf.Rect) -> list[LabelRef]:
    """Mark allocations printed in the right-hand part of a region - "(2)", "[2]" - to
    blank where a part's own mark replaces the group's (see _Block.marks)."""
    found = []
    with pymupdf.open(source_path) as doc:
        page = doc[page_index]
        for block in page.get_text("dict", clip=clip)["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    x0, y0, x1, y1 = span["bbox"]
                    if _PRINTED_MARK_RE.match(span["text"].strip()) and x0 > clip.x0 + 0.6 * clip.width:
                        found.append(LabelRef(page=page_index, x0=x0, y0=y0, x1=x1, y1=y1))
    return found


def _mtime_ns(path: str) -> int:
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


@lru_cache(maxsize=16)  # a few MB each; a miss is only a re-read
def _source_bytes(source_path: str, mtime_ns: int) -> bytes:
    """A source PDF's bytes, read once - every throwaway copy below opens from memory.

    Real finding (a classified booklet of 237 questions, 0610 Enzymes): opened from the
    file, each copy kept its file handle open after close() - the composed document's graft
    map holds on to every source it copied from until the document itself closes - and a
    mark scheme of ~1,000 cells ran out of file handles ("Too many open files"). From
    memory there's no handle to run out of, and the file is read once, not once a cell."""
    del mtime_ns  # part of the cache key only: a replaced file is read again
    return Path(source_path).read_bytes()


def _trimmed_source(
    source_path: str,
    page_index: int,
    clip: pymupdf.Rect,
    label: LabelRef | list[LabelRef] | None,
    upright: bool = False,
    strip_rules: bool = False,
) -> pymupdf.Document:
    """A throwaway copy of the source whose page holds *only* the clipped region - every
    page bit outside it (headers, footers, neighbouring questions) is genuinely redacted
    away, and so is the original label when one is being replaced.

    Clipping at draw time isn't enough on its own: text outside the clip can survive in the
    copied content stream and show up under Ctrl+A. Redacting first is what makes the copy
    contain nothing but what the reader can see.

    It's also why this is a fresh Document per region rather than one shared handle:
    PyMuPDF caches a graft map the first time `show_pdf_page` copies between a given
    (src, dst) pair, and mutating the source afterwards invalidates it - a second copy from
    the same mutated handle raises "source object number out of range".

    `upright` is for mark schemes, whose pages are rotated 90 degrees: clip rectangles and
    redactions both work in the page's unrotated space, and `show_pdf_page` copies a
    rotated source sideways - so the page is straightened first, which makes the upright
    coordinates markscheme.py measured valid for all three.
    """
    doc = pymupdf.open(stream=_source_bytes(source_path, _mtime_ns(source_path)), filetype="pdf")
    page = doc[page_index]
    if upright and page.rotation:
        page.remove_rotation()
    bounds = page.rect
    outside = [
        pymupdf.Rect(bounds.x0, bounds.y0, bounds.x1, clip.y0),  # above
        pymupdf.Rect(bounds.x0, clip.y1, bounds.x1, bounds.y1),  # below
        pymupdf.Rect(bounds.x0, clip.y0, clip.x0, clip.y1),  # left
        pymupdf.Rect(clip.x1, clip.y0, bounds.x1, clip.y1),  # right
    ]
    labels = label if isinstance(label, list) else [label] if label is not None else []
    for each in labels:
        if each.page == page_index:
            outside.append(pymupdf.Rect(each.x0, each.y0, each.x1, each.y1))
    outside.extend(_noise_rects(page, clip))
    for rect in outside:
        if rect.width > 0 and rect.height > 0:
            page.add_redact_annot(rect, fill=None)
    page.apply_redactions()
    if strip_rules:
        # A cell copied into a grid of our own: the source's table rules and cell shading
        # would show as stray pieces inside it. Only lines running most of the cell's
        # width or height, and fills covering most of it - a fraction bar stays.
        stripped = False
        for drawing in page.get_drawings():
            r = pymupdf.Rect(drawing["rect"])
            rule = (r.height <= 4 and r.width >= 0.6 * clip.width) or (r.width <= 4 and r.height >= 0.6 * clip.height)
            shade = drawing.get("fill") is not None and r.width >= 0.6 * clip.width and r.height >= 0.6 * clip.height
            # a cell drawn as an outlined box: only its edge shows inside the clip
            border = drawing.get("fill") is None and (r.width >= 0.6 * clip.width or r.height >= 0.6 * clip.height)
            # a neighbouring cell's shading reaching over the edge - the grey header row
            # showed as a strip along the top of the first row's cells (4CH1 1C June 2025)
            overlap = r & clip
            spill = drawing.get("fill") is not None and not overlap.is_empty and overlap.height < 0.5 * r.height
            # a row's border drawn in short pieces, along the cell's top or foot - where
            # nothing of the answer's own sits (a fraction bar is well inside)
            edge = r.height <= 1.5 and (r.y1 <= clip.y0 + _CELL_EDGE or r.y0 >= clip.y1 - _CELL_EDGE)
            if rule or shade or border or spill or edge:
                page.add_redact_annot(r + (-1, -1, 1, 1), fill=None)
                stripped = True
        if stripped:
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_NONE,
            )
        # And whatever line art touches the cell's outer band: a grid drawn as one path
        # across the whole table (4CH1 1CR January 2022) matches none of the shapes above,
        # and only the source's own table lines reach the band - the grid is ours.
        for band in (
            pymupdf.Rect(clip.x0, clip.y0, clip.x1, clip.y0 + _CELL_EDGE),
            pymupdf.Rect(clip.x0, clip.y1 - _CELL_EDGE, clip.x1, clip.y1),
            pymupdf.Rect(clip.x0, clip.y0, clip.x0 + _CELL_EDGE, clip.y1),
            pymupdf.Rect(clip.x1 - _CELL_EDGE, clip.y0, clip.x1, clip.y1),
        ):
            page.add_redact_annot(band, fill=None)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            text=pymupdf.PDF_REDACT_TEXT_NONE,
        )
    return doc


def _section_heading(page: pymupdf.Page, y: float, text: str) -> float:
    page.insert_text((MARGIN, y), text, fontsize=12, fontname="hebo")
    return y + 18


def _draw_edexcel_cover(
    doc: pymupdf.Document,
    title: str,
    n_questions: int,
    total_marks: int,
    is_mark_scheme: bool,
    duration_minutes: int | None = None,
    has_periodic_table: bool = False,
    subject_name: str = "",
) -> float:
    """An Edexcel exam's front page, set out the way the board's own latest papers are
    (4CH1, June 2025): a rounded panel holding the candidate's surname, other names, centre
    and candidate number boxes, the paper's title and time, what to bring and a Total Marks
    box; then Instructions, Information and Advice. Structure and wording only - never the
    board's name, logo, barcode or paper reference (CLAUDE.md)."""
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    right = PAGE_WIDTH - MARGIN
    grey = (0.55, 0.55, 0.55)
    y = MARGIN
    panel_top = y

    if not is_mark_scheme:
        page.draw_rect(pymupdf.Rect(MARGIN + 10, y + 10, right - 10, y + 36), color=grey, width=0.8, radius=0.2)
        page.insert_text((MARGIN + 14, y + 19), "Candidate name", fontsize=7)
        y += 46
        for label, x, cells in (("Centre Number", MARGIN + 10, 5), ("Candidate Number", MARGIN + 140, 4)):
            page.insert_text((x, y), label, fontsize=8)
            for i in range(cells):
                page.draw_rect(pymupdf.Rect(x + i * 20, y + 4, x + (i + 1) * 20, y + 26), color=grey, width=0.8)
        y += 40

    if is_mark_scheme:
        y += 10  # no candidate boxes above it: the panel's own inset, as the boxes have
    page.draw_rect(pymupdf.Rect(MARGIN + 10, y, right - 10, y + 30), color=grey, width=1.2, radius=0.2)
    page.insert_text((MARGIN + 18, y + 21), title, fontsize=17, fontname="hebo")
    y += 44
    when = _format_duration(duration_minutes) if duration_minutes else ""
    page.insert_text((MARGIN + 14, y), f"(Time: {when})" if when else "", fontsize=10)
    _insert_right(page, right - 14, y, "Mark Scheme" if is_mark_scheme else "Question Paper", fontsize=11, fontname="hebo")
    y += 14

    subject_top = y
    page.insert_text((MARGIN + 18, y + 22), subject_name or "Practice paper", fontsize=20, fontname="hebo")
    page.insert_text(
        (MARGIN + 18, y + 40), f"{n_questions} question{'s' if n_questions != 1 else ''}", fontsize=12, fontname="hebo"
    )
    y += 52
    page.draw_rect(pymupdf.Rect(MARGIN + 10, subject_top, right - 10, y), color=grey, width=1.2, radius=0.08)
    y += 8

    if not is_mark_scheme:
        page.draw_rect(pymupdf.Rect(MARGIN + 10, y, right - 70, y + 40), color=grey, width=1.2, radius=0.15)
        page.insert_text((MARGIN + 16, y + 13), "You must have:", fontsize=9, fontname="hebo")
        page.insert_text((MARGIN + 16, y + 26), "Calculator, ruler", fontsize=9)
        page.draw_rect(pymupdf.Rect(right - 64, y, right - 10, y + 40), color=grey, width=0.8, radius=0.15)
        page.insert_text((right - 60, y + 11), "Total Marks", fontsize=7)
        y += 48
    page.draw_rect(pymupdf.Rect(MARGIN, panel_top, right, y + 2), color=grey, width=1.6, radius=0.04)
    y += 34

    if is_mark_scheme:
        y = _section_heading(page, y, "Information")
        y = _bullet(page, MARGIN, y, "This mark scheme accompanies the question paper of the same name.")
        y = _bullet(page, MARGIN, y, f"The total mark for this paper is {total_marks}.")
        y = _bullet(
            page, MARGIN, y,
            "Answers are grouped by question and part, numbered as in the question paper rather "
            "than as in the papers they were taken from.",
        )
        return y

    y = _section_heading(page, y, "Instructions")
    for line in (
        "Use black ink or ball-point pen.",
        "If pencil is used for diagrams/sketches/graphs it must be dark (HB or B).",
        "Fill in the boxes at the top of this page with your name, centre number and candidate number.",
        "Answer all questions.",
        "Answer the questions in the spaces provided - there may be more space than you need.",
        "Show all the steps in any calculations and state the units.",
    ):
        y = _bullet(page, MARGIN, y, line)
    y += 8
    y = _section_heading(page, y, "Information")
    y = _bullet(page, MARGIN, y, f"The total mark for this paper is {total_marks}.")
    y = _bullet(
        page, MARGIN, y,
        "The marks for each question are shown in brackets - use this as a guide as to how much "
        "time to spend on each question.",
    )
    if has_periodic_table:
        y = _bullet(page, MARGIN, y, "The Periodic Table is printed at the end of this paper.")
    y += 8
    y = _section_heading(page, y, "Advice")
    for line in (
        "Read each question carefully before you start to answer it.",
        "Write your answers neatly and in good English.",
        "Try to answer every question.",
        "Check your answers if you have time at the end.",
    ):
        y = _bullet(page, MARGIN, y, line)
    return y


def _draw_booklet_cover(
    doc: pymupdf.Document,
    title: str,
    n_questions: int,
    total_marks: int,
    is_mark_scheme: bool,
    has_multiple_choice: bool = False,
    has_periodic_table: bool = False,
    board: str = "Cambridge",
    subject_name: str = "",
) -> float:
    """A classified (topical) booklet's front page: the topic, the subject, how many
    questions and marks. It's practice material gathered by topic, not an exam - so no
    candidate boxes and no time allowed. In the board's look (Edexcel's grey rounded panel,
    Cambridge's ruled heading), with nothing of either board's letterhead (CLAUDE.md)."""
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    right = PAGE_WIDTH - MARGIN
    grey = (0.55, 0.55, 0.55)
    kind = "Mark Scheme" if is_mark_scheme else "Question Booklet"
    y = MARGIN + 20
    top = y - 20
    page.insert_text((MARGIN + 10, y + 4), "Topic questions", fontsize=11, fontname="hebo", color=grey)
    _insert_right(page, right - 10, y + 4, kind, fontsize=11, fontname="hebo")
    y += 20
    # a long topic name wraps, up to three lines
    box_width = right - MARGIN - 20
    lines = min(3, max(1, -(-int(pymupdf.get_text_length(title, fontsize=22, fontname="hebo")) // int(box_width - 20))))
    page.insert_textbox(
        pymupdf.Rect(MARGIN + 10, y, right - 10, y + lines * 27 + 6), title, fontsize=22, fontname="hebo", lineheight=1.15
    )
    y += lines * 27 + 18
    if subject_name:
        page.insert_text((MARGIN + 10, y), subject_name, fontsize=13)
        y += 22
    marks_text = f"{total_marks} mark{'' if total_marks == 1 else 's'}"
    page.insert_text(
        (MARGIN + 10, y), f"{n_questions} question{'' if n_questions == 1 else 's'}", fontsize=12, fontname="hebo"
    )
    _insert_right(page, right - 10, y, marks_text, fontsize=12, fontname="hebo")
    y += 18
    if board == "Edexcel":
        page.draw_rect(pymupdf.Rect(MARGIN, top, right, y), color=grey, width=1.6, radius=0.06)
    else:
        page.draw_line((MARGIN, top), (right, top), width=1.2)
        page.draw_line((MARGIN, y), (right, y), width=1.2)
    y += 34

    page.insert_text((MARGIN, y), "INFORMATION", fontsize=11, fontname="hebo")
    y += 16
    if is_mark_scheme:
        y = _bullet(page, MARGIN, y, "This mark scheme accompanies the question booklet of the same name.")
        y = _bullet(
            page, MARGIN, y,
            "Answers are numbered as in the question booklet rather than as in the papers they were taken from.",
        )
        return y
    y = _bullet(
        page, MARGIN, y,
        "Every past-paper question on this topic in the bank, newest sitting first. Where a "
        "question also covers other topics, only the parts on this topic (and the parts they "
        "need) are included.",
    )
    y = _bullet(
        page, MARGIN, y,
        "Questions are renumbered. The list at the end gives the paper, sitting and question "
        "each one was taken from.",
    )
    brackets = "( )" if board == "Edexcel" else "[ ]"
    y = _bullet(page, MARGIN, y, f"The number of marks for each question or part question is shown in brackets {brackets}.")
    if has_multiple_choice:
        y = _bullet(page, MARGIN, y, "The multiple-choice questions come last, as a section of their own.")
    if has_periodic_table:
        y = _bullet(page, MARGIN, y, "The Periodic Table is printed at the end of the questions.")
    return y


def _draw_cover(
    doc: pymupdf.Document,
    title: str,
    n_questions: int,
    total_marks: int,
    is_mark_scheme: bool,
    duration_minutes: int | None = None,
    has_multiple_choice: bool = False,
    has_periodic_table: bool = False,
    board: str = "Cambridge",
    subject_name: str = "",
    booklet: bool = False,
) -> float:
    if booklet:
        return _draw_booklet_cover(
            doc, title, n_questions, total_marks, is_mark_scheme, has_multiple_choice, has_periodic_table,
            board, subject_name,
        )
    if board == "Edexcel":
        return _draw_edexcel_cover(
            doc, title, n_questions, total_marks, is_mark_scheme, duration_minutes, has_periodic_table, subject_name
        )
    """The front page of a real paper: a name box, what the paper is, how long it runs,
    then INSTRUCTIONS and INFORMATION blocks. Structure only - the wording and title are
    this tool's own, never a board's letterhead (CLAUDE.md).

    `duration_minutes` is the time allowance the teacher set for this paper; it's simply
    omitted when they didn't set one, since a made-up allowance is worse than none.

    Returns the y to write the final page-count line at, which can only be filled in once
    the rest of the paper has been laid out. Only the y, not the page: adding further pages
    invalidates an existing Page handle, so the caller re-fetches it.
    """
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    right = PAGE_WIDTH - MARGIN
    y = MARGIN + 20

    page.insert_text((MARGIN, y), title, fontsize=18, fontname="hebo")
    y += 34

    if not is_mark_scheme:
        page.insert_text((MARGIN, y + 10), "CANDIDATE", fontsize=9)
        page.insert_text((MARGIN, y + 22), "NAME", fontsize=9)
        page.draw_rect(pymupdf.Rect(MARGIN + 70, y - 2, right, y + 30), width=0.8)
        y += 46

    page.draw_line((MARGIN, y), (right, y), width=1.2)
    y += 20

    marks_text = f"{total_marks} mark{'' if total_marks == 1 else 's'}"
    page.insert_text(
        (MARGIN, y), "Mark Scheme" if is_mark_scheme else "Question Paper",
        fontsize=12, fontname="hebo",
    )
    _insert_right(page, right, y, marks_text, fontsize=12, fontname="hebo")
    y += 16
    page.insert_text(
        (MARGIN, y), f"{n_questions} question{'' if n_questions == 1 else 's'}", fontsize=11
    )
    y += 6
    if not is_mark_scheme and duration_minutes:
        # the allowance sits on its own line, a little clear of the mark total above it,
        # the way a real paper's front page sets them apart
        y += 0
        _insert_right(
            page, right, y, _format_duration(duration_minutes), fontsize=11, fontname="hebo"
        )
        y += 16
    y += 14

    if is_mark_scheme:
        page.insert_text(
            (MARGIN, y), "This mark scheme accompanies the question paper of the same name.",
            fontsize=11,
        )
        y += 32
        page.insert_text((MARGIN, y), "INFORMATION", fontsize=11, fontname="hebo")
        y += 16
        y = _bullet(page, MARGIN, y, f"The total mark for this paper is {total_marks}.")
        y = _bullet(
            page, MARGIN, y,
            "Answers are grouped by question and part, matching the question paper's own "
            "numbering rather than the numbering of the papers they were taken from.",
        )
        return y

    page.insert_text((MARGIN, y), "You must answer on the question paper.", fontsize=11)
    y += 24
    page.insert_text((MARGIN, y), "No additional materials are needed.", fontsize=11)
    y += 10
    page.draw_line((MARGIN, y), (right, y), width=1.2)
    y += 20

    page.insert_text((MARGIN, y), "INSTRUCTIONS", fontsize=11, fontname="hebo")
    y += 16
    for line in (
        "Answer all questions.",
        "Use a black or dark blue pen. You may use an HB pencil for any diagrams or graphs.",
        "Write your name in the box at the top of the page.",
        "Do not use an erasable pen or correction fluid.",
        "Write your answer to each question in the space provided.",
    ):
        y = _bullet(page, MARGIN, y, line)
    y += 18

    page.insert_text((MARGIN, y), "INFORMATION", fontsize=11, fontname="hebo")
    y += 16
    y = _bullet(page, MARGIN, y, f"The total mark for this paper is {total_marks}.")
    y = _bullet(
        page, MARGIN, y,
        "The number of marks for each question or part question is shown in brackets [ ].",
    )
    if has_multiple_choice:
        y = _bullet(page, MARGIN, y, "Each multiple-choice question is worth 1 mark.")
        y = _bullet(
            page, MARGIN, y,
            "Answer the multiple-choice questions on the answer sheet printed just before them.",
        )
    if has_periodic_table:
        y = _bullet(page, MARGIN, y, "The Periodic Table is printed in the question paper.")
    return y


# ---------------------------------------------------------------------------------------
# Multiple-choice answer sheet
#
# Laid out like a board's supplementary answer sheet - instructions, a black title bar,
# a grid of lozenges - but this tool's own, with no letterhead
# (CLAUDE.md). Options are A-D only: the boards' printed sheet runs to H, but every
# multiple-choice item these papers set has four options.
# ---------------------------------------------------------------------------------------

MCQ_OPTIONS = "ABCD"
_SHEET_ROWS_PER_COLUMN = 20
_SHEET_ROW_HEIGHT = 26
_SHEET_NUMBER_WIDTH = 24
_SHEET_OPTION_PITCH = 28
_SHEET_COLUMN_GAP = 18
_SHEET_COLUMN_WIDTH = _SHEET_NUMBER_WIDTH + _SHEET_OPTION_PITCH * len(MCQ_OPTIONS) + 6
_SHEET_ROW_SHADE = (0.99, 0.96, 0.92)
_SHEET_LOZENGE_W, _SHEET_LOZENGE_H = 15.0, 4.5
_SHEET_MAX_COLUMNS = 3  # as many as fit across the page at full width
_SHEET_INSTRUCTIONS_HEIGHT = 84


def _mcq_sheet_columns(numbers: list[int]) -> list[list[list[int]]]:
    """Pages of columns of question numbers, sized to the paper's multiple-choice section
    rather than a fixed form: one column up to 20 questions, then as many balanced columns
    as it takes (up to three across the page), never more than 20 rows in a column."""
    per_page = _SHEET_MAX_COLUMNS * _SHEET_ROWS_PER_COLUMN
    pages = []
    for start in range(0, len(numbers), per_page):
        chunk = numbers[start:start + per_page]
        n_columns = -(-len(chunk) // _SHEET_ROWS_PER_COLUMN)
        per_column = -(-len(chunk) // n_columns)
        pages.append([chunk[i:i + per_column] for i in range(0, len(chunk), per_column)])
    return pages


def _draw_lozenge_row(page: pymupdf.Page, x: float, y: float, filled: str = "") -> None:
    """A-D, each letter over its lozenge; `filled` shades one, for the worked example."""
    for i, letter in enumerate(MCQ_OPTIONS):
        cx = x + _SHEET_OPTION_PITCH * (i + 0.5)
        width = pymupdf.get_text_length(letter, fontsize=10, fontname="hebo")
        page.insert_text((cx - width / 2, y + 12), letter, fontsize=10, fontname="hebo")
        lozenge = pymupdf.Rect(
            cx - _SHEET_LOZENGE_W / 2, y + 15, cx + _SHEET_LOZENGE_W / 2, y + 15 + _SHEET_LOZENGE_H
        )
        page.draw_rect(
            lozenge, color=(0, 0, 0), fill=(0.2, 0.2, 0.2) if letter == filled else None,
            width=0.6, radius=0.5,
        )


def _draw_answer_column(page: pymupdf.Page, x: float, y: float, numbers: list[int]) -> None:
    right = x + _SHEET_COLUMN_WIDTH
    for row, number in enumerate(numbers):
        top = y + row * _SHEET_ROW_HEIGHT
        if row % 2 == 0:
            page.draw_rect(
                pymupdf.Rect(x + _SHEET_NUMBER_WIDTH, top, right, top + _SHEET_ROW_HEIGHT),
                color=None, fill=_SHEET_ROW_SHADE,
            )
        label = str(number)
        label_w = pymupdf.get_text_length(label, fontsize=11, fontname="hebo")
        page.insert_text(
            (x + (_SHEET_NUMBER_WIDTH - label_w) / 2, top + 17), label, fontsize=11, fontname="hebo"
        )
        _draw_lozenge_row(page, x + _SHEET_NUMBER_WIDTH + 3, top + 1)
        page.draw_line((x, top), (right, top), width=0.5)
    bottom = y + len(numbers) * _SHEET_ROW_HEIGHT
    page.draw_line((x, bottom), (right, bottom), width=0.5)
    for edge in (x, x + _SHEET_NUMBER_WIDTH, right):
        page.draw_line((edge, y), (edge, bottom), width=0.5)


def _draw_instructions_band(page: pymupdf.Page, rect: pymupdf.Rect) -> None:
    """The instructions as one row across the top of the sheet, in three cells: what to
    write with, how to answer, and a worked example."""
    page.draw_rect(rect, color=(0, 0, 0), width=1.2)
    widths = (140, 205)
    edges = [rect.x0, rect.x0 + widths[0], rect.x0 + widths[0] + widths[1], rect.x1]
    for x in edges[1:-1]:
        page.draw_line((x, rect.y0), (x, rect.y1), width=0.6)
    pad = 10
    top = rect.y0 + pad

    # what to write with
    x0, x1 = edges[0] + pad, edges[1] - pad
    page.insert_text((x0, top + 14), "Instructions", fontsize=14, fontname="hebo")
    page.insert_text((x0, top + 36), "Use a soft pencil", fontsize=11, fontname="hebo")
    page.insert_text((x0, top + 50), "(B or HB).", fontsize=10)

    # how to answer
    x0, x1 = edges[1] + pad, edges[2] - pad
    y = top + 4
    for text in (
        "Shade one lozenge for each question.",
        "Make sure your answer is in line with the correct question number.",
        "Rub out any answer you wish to change.",
    ):
        page.draw_circle((x0 + 2.5, y + 5), 1.6, color=None, fill=(0, 0, 0))
        spare = page.insert_textbox(
            pymupdf.Rect(x0 + 10, y - 2, x1, y + 40), text, fontsize=9, lineheight=1.15
        )
        y += 40 - spare + 5

    # a worked example
    x0, x1 = edges[2] + pad, edges[3] - pad
    page.insert_text((x0, top + 8), "Example:", fontsize=10, fontname="hebo")
    page.insert_textbox(
        pymupdf.Rect(x0, top + 12, x1, top + 40),
        "If you think B is the right answer to question 1, shade it like this:",
        fontsize=9, lineheight=1.15,
    )
    row_y = rect.y1 - pad - 22
    page.insert_text((x0, row_y + 17), "1", fontsize=10, fontname="hebo")
    _draw_lozenge_row(page, x0 + 10, row_y, filled="B")


def _append_answer_sheets(doc: pymupdf.Document, numbers: list[int]) -> None:
    """The multiple-choice answer sheet, just before the multiple-choice section, with the
    instructions across the top, then one row per multiple-choice question, under its
    number in this paper."""
    right = PAGE_WIDTH - MARGIN
    for columns in _mcq_sheet_columns(numbers):
        page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        y = CONTENT_TOP

        _draw_instructions_band(
            page, pymupdf.Rect(MARGIN, y, right, y + _SHEET_INSTRUCTIONS_HEIGHT)
        )
        y += _SHEET_INSTRUCTIONS_HEIGHT + 18

        bar = pymupdf.Rect(MARGIN, y, right, y + 26)
        page.draw_rect(bar, color=None, fill=(0, 0, 0))
        heading = "Multiple-choice Answer Sheet"
        heading_w = pymupdf.get_text_length(heading, fontsize=15, fontname="hebo")
        page.insert_text(
            ((PAGE_WIDTH - heading_w) / 2, y + 18.5), heading, fontsize=15, fontname="hebo",
            color=(1, 1, 1),
        )
        y = bar.y1 + 14

        grid_w = len(columns) * _SHEET_COLUMN_WIDTH + (len(columns) - 1) * _SHEET_COLUMN_GAP
        x = (PAGE_WIDTH - grid_w) / 2
        for column in columns:
            _draw_answer_column(page, x, y, column)
            x += _SHEET_COLUMN_WIDTH + _SHEET_COLUMN_GAP


def _board(resolved: list["_ResolvedGroup | None"]) -> str:
    """The exam's board - one per exam (main.py refuses a mix)."""
    return next((r.paper.board for r in resolved if r is not None), "Cambridge")


def _subject_name(resolved: list["_ResolvedGroup | None"]) -> str:
    return next((r.paper.subject_name for r in resolved if r is not None and r.paper.subject_name), "")


_EDEXCEL_SESSION_ORDER = {"JAN": 0, "JUN": 1, "NOV": 2}


def _latest_periodic_table(out_dir: Path) -> tuple[str, BBox] | None:
    """The periodic table from the newest Edexcel paper in the bank that prints one - the
    Edexcel counterpart of assets/periodic_table.pdf, taken from the latest paper as that
    one was. Paper keys say their sitting (4CH1_1C_JUN_25), so only the newest few papers
    are opened."""
    candidates = []
    for paper_dir in Path(out_dir).iterdir():
        parts = paper_dir.name.split("_")
        if len(parts) == 4 and parts[0].startswith("4CH") and parts[2] in _EDEXCEL_SESSION_ORDER and parts[3].isdigit():
            candidates.append(((int(parts[3]), _EDEXCEL_SESSION_ORDER[parts[2]]), paper_dir))
    for _, paper_dir in sorted(candidates, reverse=True)[:12]:
        try:
            paper = Paper.model_validate_json((paper_dir / "paper.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not Path(paper.source_qp_path).exists():
            continue
        with pymupdf.open(paper.source_qp_path) as doc:
            found = periodic_table_region(doc)
        if found is not None:
            page_index, (x0, y0, x1, y1) = found
            return paper.source_qp_path, BBox(page=page_index, x0=x0, y0=y0, x1=x1, y1=y1)
    return None


def _find_data_page(resolved: list["_ResolvedGroup | None"], finder) -> tuple[str, BBox] | None:
    """A data page (the periodic table, the list of formulas) from the first source paper
    that prints one, as (source path, region). Papers that need one print it for their own
    questions; a composed paper drawing on any of them needs it just the same."""
    seen: set[str] = set()
    for r in resolved:
        if r is None or r.paper.source_qp_path in seen:
            continue
        path = r.paper.source_qp_path
        seen.add(path)
        if not Path(path).exists():
            continue
        found = _data_page_in(finder, path, _mtime_ns(path))
        if found is not None:
            page_index, (x0, y0, x1, y1) = found
            return path, BBox(page=page_index, x0=x0, y0=y0, x1=x1, y1=y1)
    return None


@lru_cache(maxsize=512)
def _scheme_layout(path: str, board: str, mtime_ns: int) -> MSLayout:
    """A source mark scheme's row layout, read once per file version - only ever read
    here. A classified booklet draws on every paper of a subject (131 schemes for one 0610
    topic, 30s to read), and each further topic would read them all again."""
    del mtime_ns  # part of the cache key only
    return scheme_layout(path, board)


@lru_cache(maxsize=4096)
def _data_page_in(finder, path: str, mtime_ns: int):
    """`finder` run over one source, remembered. Real finding (a classified booklet of 237
    questions from 131 0610 papers, none printing a data page): looking through every
    source's pages took 13 of the booklet's 26 seconds, and again for every topic."""
    del mtime_ns  # part of the cache key only
    with pymupdf.open(path) as doc:
        return finder(doc)


def _append_full_page(doc: pymupdf.Document, source_path: str, region: BBox) -> None:
    """A whole-page item (the periodic table) on a page of its own, centred in the content
    area and shrunk only if it wouldn't otherwise fit clear of the page furniture."""
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    clip = pymupdf.Rect(region.x0, region.y0, region.x1, region.y1)
    room = CONTENT_BOTTOM - CONTENT_TOP
    scale = min(1.0, CONTENT_WIDTH / clip.width, room / clip.height)
    width, height = clip.width * scale, clip.height * scale
    # Horizontally where the source page has it, measured from the page's centre - not
    # the content centred on its own bounds. Real-paper finding (0580 m25 qp 22): the list
    # of formulas runs x=50-473, so centring that block shifted the whole page 36pt right
    # and its heading, centred on the source page, came out visibly off-centre.
    left = PAGE_WIDTH / 2 + (clip.x0 - PAGE_WIDTH / 2) * scale
    top = CONTENT_TOP + (room - height) / 2
    trimmed = _trimmed_source(source_path, region.page, clip, None)
    try:
        page.show_pdf_page(pymupdf.Rect(left, top, left + width, top + height), trimmed, region.page, clip=clip)
    finally:
        trimmed.close()


def _page_ink(page: pymupdf.Page) -> pymupdf.Rect:
    """Everything printed on the page, sideways text included - the table's title and its
    gas-volume note run up the page, which crops._content_bbox doesn't count."""
    ink = pymupdf.Rect()
    for block in page.get_text("dict")["blocks"]:
        ink |= pymupdf.Rect(block["bbox"])
    for drawing in page.get_drawings():
        ink |= drawing["rect"]
    return ink


def _append_periodic_table(doc: pymupdf.Document, from_source: tuple[str, BBox], board: str = "Cambridge") -> None:
    """The standard periodic table (PERIODIC_TABLE_PDF); the first source paper's own
    only if that file is missing. An Edexcel exam always prints its source paper's own
    table (page 2 of every 4CH1 paper): the boards set theirs out differently, and an exam
    is never set from both."""
    if board == "Cambridge" and PERIODIC_TABLE_PDF.exists():
        with pymupdf.open(PERIODIC_TABLE_PDF) as table:
            ink = _page_ink(table[0])
        if not ink.is_empty:
            region = BBox(page=0, x0=ink.x0, y0=ink.y0, x1=ink.x1, y1=ink.y1)
            _append_full_page(doc, str(PERIODIC_TABLE_PDF), region)
            return
    _append_full_page(doc, *from_source)


def _format_duration(minutes: int) -> str:
    """Written the way a paper states its allowance: 75 -> "1 hour 15 minutes"."""
    hours, rest = divmod(minutes, 60)
    minute_text = f"{rest} minute{'' if rest == 1 else 's'}"
    if not hours:
        return minute_text
    hour_text = f"{hours} hour{'' if hours == 1 else 's'}"
    return hour_text if not rest else f"{hour_text} {minute_text}"


def _bullet(page: pymupdf.Page, x: float, y: float, text: str) -> float:
    # drawn rather than typeset: the base-14 fonts render U+2022 as a thin raised dot, not
    # the solid round bullet a real paper uses.
    page.draw_circle((x + 8, y - 3.5), 1.8, color=None, fill=(0, 0, 0))
    used = page.insert_textbox(
        pymupdf.Rect(x + 20, y - 10, PAGE_WIDTH - MARGIN, y + 40), text, fontsize=11, lineheight=1.25
    )
    # insert_textbox returns the unused height; two lines of wrapped text need more room
    return y + (16 if used > 24 else 30)


def _insert_right(
    page: pymupdf.Page, right: float, y: float, text: str, fontsize: float, fontname: str = "helv"
) -> None:
    width = pymupdf.get_text_length(text, fontsize=fontsize, fontname=fontname)
    page.insert_text((right - width, y), text, fontsize=fontsize, fontname=fontname)


BRAND_TEXT = "Classark Exam Builder"
BRAND_URL = "https://classark.com"
_BRAND_COLOR = (0.8, 0.1, 0.1)  # red, so the link stands out from the black page furniture
_BRAND_FONT = "hebo"
_FOOTER_SIZE = 9
_FOOTER_MIN_SIZE = 7
_FOOTER_SIDE_GAP = 12  # between a side of the footer and whatever sits in its middle


def _fit_footer_text(text: str, max_width: float) -> tuple[str, float]:
    """(text, fontsize) that fits `max_width`: shrunk a little first, then cut short."""
    for size in (_FOOTER_SIZE, 8, _FOOTER_MIN_SIZE):
        if pymupdf.get_text_length(text, fontsize=size) <= max_width:
            return text, size
    while text and pymupdf.get_text_length(text + "...", fontsize=_FOOTER_MIN_SIZE) > max_width:
        text = text[:-1]
    return (text.rstrip() + "...") if text else "", _FOOTER_MIN_SIZE


def _draw_footer_sides(
    page: pymupdf.Page, baseline_y: float, left_x: float, right_x: float,
    middle_width: float, note: str,
) -> None:
    """The builder's name, linked, at the foot's left, and the teacher's own note (a school
    name, a class, "Do not write in the margin") right-aligned at its right - each kept
    clear of the `middle_width` of text centred between them."""
    brand_width = pymupdf.get_text_length(BRAND_TEXT, fontsize=_FOOTER_SIZE, fontname=_BRAND_FONT)
    page.insert_text(
        (left_x, baseline_y), BRAND_TEXT, fontsize=_FOOTER_SIZE, fontname=_BRAND_FONT,
        color=_BRAND_COLOR,
    )
    page.insert_link({
        "kind": pymupdf.LINK_URI,
        "from": pymupdf.Rect(left_x, baseline_y - _FOOTER_SIZE, left_x + brand_width, baseline_y + 2),
        "uri": BRAND_URL,
    })
    note = " ".join(note.split())
    if not note:
        return
    middle_right = (page.rect.width + middle_width) / 2
    text, size = _fit_footer_text(note, right_x - middle_right - _FOOTER_SIDE_GAP)
    if text:
        width = pymupdf.get_text_length(text, fontsize=size)
        page.insert_text((right_x - width, baseline_y), text, fontsize=size)


def _add_page_furniture(
    doc: pymupdf.Document, exam_name: str, skip_pages: int, footer_note: str = "", board: str = "Cambridge"
) -> None:
    """Page number at the top and the paper's name at the foot of every content page, clear
    of the question content - where a real paper carries them. The cover is skipped, the
    way a real paper's front page carries no number of its own. The foot also carries the
    builder's name on the left and the teacher's `footer_note` on the right."""
    for index, page in enumerate(doc):
        if index < skip_pages:
            continue
        number = str(index + 1)
        width = pymupdf.get_text_length(number, fontsize=11, fontname="hebo")
        footer_y = PAGE_HEIGHT - MARGIN + 14
        if board == "Edexcel":
            # Edexcel numbers its pages at the foot, in the outer corner - left on an even
            # page, right on an odd one - just below the frame; the footer drops below it
            number_y = _EDEXCEL_FRAME.y1 + 14
            x = _EDEXCEL_FRAME.x0 if (index + 1) % 2 == 0 else _EDEXCEL_FRAME.x1 - width
            page.insert_text((x, number_y), number, fontsize=11, fontname="hebo")
            footer_y = PAGE_HEIGHT - 16
        else:
            page.insert_text(
                ((PAGE_WIDTH - width) / 2, MARGIN - 6), number, fontsize=11, fontname="hebo"
            )
        name_width = pymupdf.get_text_length(exam_name, fontsize=9)
        page.insert_text(((PAGE_WIDTH - name_width) / 2, footer_y), exam_name, fontsize=9)
        _draw_footer_sides(page, footer_y, MARGIN, PAGE_WIDTH - MARGIN, name_width, footer_note)


def _is_mcq(r: "_ResolvedGroup | None") -> bool:
    return r is not None and r.has_multiple_choice


def _starts_mcq_section(resolved: list[_ResolvedGroup | None], index: int) -> bool:
    """Whether question `index` is the first multiple-choice question."""
    return _is_mcq(resolved[index]) and (index == 0 or not _is_mcq(resolved[index - 1]))


def _section_numbers(resolved: list[_ResolvedGroup | None], first_number: int = 1) -> list[int]:
    """Each question's printed number. The written questions and the multiple-choice ones
    are numbered separately, each section from `first_number` - so the multiple-choice
    section starts again at 1, the way a multiple-choice paper numbers its own questions."""
    counters = {False: first_number, True: first_number}
    numbers = []
    for r in resolved:
        kind = _is_mcq(r)
        numbers.append(counters[kind])
        counters[kind] += 1
    return numbers


def _has_both_sections(resolved: list[_ResolvedGroup | None]) -> bool:
    kinds = {_is_mcq(r) for r in resolved}
    return kinds == {False, True}


def _resolve_all(
    groups: list[QuizGroup], out_dir: Path, first_number: int = 1
) -> list[_ResolvedGroup | None]:
    """Every group resolved and numbered, with the multiple-choice questions moved to the
    end (in the teacher's order among themselves, as are the written ones), as a section
    of their own after the written questions. Numbering follows the new order, so it has
    to be settled here, before anything is labelled - the QP, its answer sheet and the MS
    all number from this list."""
    papers: dict[str, Paper | None] = {}

    def resolve(ordered: list[QuizGroup], numbers: list[int]) -> list[_ResolvedGroup | None]:
        return [
            _resolve_group(group, out_dir, papers, number)
            for number, group in zip(numbers, ordered)
        ]

    sequential = list(range(first_number, first_number + len(groups)))
    resolved = resolve(groups, sequential)
    is_mcq = [_is_mcq(r) for r in resolved]
    order = sorted(range(len(groups)), key=lambda i: is_mcq[i])
    ordered = [resolved[i] for i in order]
    numbers = _section_numbers(ordered, first_number)
    if order == list(range(len(groups))) and numbers == sequential:
        return resolved
    # labels carry the question's number, so a moved or renumbered question is resolved
    # again under its printed number
    return resolve([groups[i] for i in order], numbers)


def _compact(doc: pymupdf.Document) -> bytes:
    """Every copied region brings its source page's fonts and images along, so a paper
    built from many regions of one source repeats them over and over - merging identical
    objects took a 9-page mark scheme from 3.5 MB to under 1 MB."""
    return doc.tobytes(garbage=4, deflate=True)


# Edexcel's question pages, as its own papers set them: a grey rounded frame round the
# content, "DO NOT WRITE IN THIS AREA" three times down the right margin, and "Turn over"
# at the foot of every page but the last. The frame sits clear of the content area
# (CONTENT_TOP..CONTENT_BOTTOM, MARGIN either side) and of the page furniture.
_FRAME_GREY = (0.62, 0.63, 0.64)
_EDEXCEL_FRAME = pymupdf.Rect(MARGIN - 16, MARGIN - 16, PAGE_WIDTH - MARGIN + 12, PAGE_HEIGHT - MARGIN + 2)


def _edexcel_page_style(doc: pymupdf.Document, pages: list[int]) -> None:
    last = max(pages) if pages else None
    for number in pages:
        page = doc[number]
        page.draw_rect(_EDEXCEL_FRAME, color=_FRAME_GREY, width=2, radius=0.03)
        x = _EDEXCEL_FRAME.x1 + 14
        for y in (PAGE_HEIGHT * 0.22, PAGE_HEIGHT * 0.5, PAGE_HEIGHT * 0.78):
            label = "DO NOT WRITE IN THIS AREA"
            half = pymupdf.get_text_length(label, fontsize=7, fontname="hebo") / 2
            page.insert_text((x, y + half), label, fontsize=7, fontname="hebo", rotate=90, color=_FRAME_GREY)
        if number != last:
            _insert_right(
                page, _EDEXCEL_FRAME.x1 - 10, _EDEXCEL_FRAME.y1 - 8, "Turn over", fontsize=9, fontname="hebo"
            )


def _finish(doc: pymupdf.Document, cover_y: float, footer: str, footer_note: str = "", board: str = "Cambridge") -> bytes:
    """`footer` names the paper *and* which of the two documents this is, since the two are
    handed out separately and a page on its own should say which one it came from - the
    cover carries the paper's name alone."""
    _bullet(
        doc[0], MARGIN, cover_y,
        f"This document has {doc.page_count} pages, including this one.",
    )
    _add_page_furniture(doc, footer, skip_pages=1, footer_note=footer_note, board=board)
    return _compact(doc)


def _keep_with_next(blocks: list[_Block], heights: list[float], index: int) -> float:
    """Height that must fit below block `index` before it's worth starting it on this page.

    Only a stem or heading keeps company, and it keeps it with everything up to and
    including the first real part it introduces - a stem followed by a heading keeps with
    both. Real-paper finding (0478 s20 Q7): when every block, parts included, insisted on
    fitting alongside the next one, (b)(i) wanted room for itself *and* (b)(ii), jumped to
    the next page, and left the (b) heading - placed only because (b)(i) fitted beneath it
    - stranded alone at the foot of the page. Parts may break between each other freely,
    the way a real paper splits (i) and (ii) across pages.
    """
    if not blocks[index].introduces:
        return 0.0
    keep = 0.0
    for following in range(index + 1, len(blocks)):
        keep += ITEM_GAP + heights[following]
        if not blocks[following].introduces:
            break
    # only insist they stay together if they could actually share a page - a diagram
    # taller than a page would otherwise bump its own heading forward forever.
    return keep if heights[index] + keep <= CONTENT_BOTTOM - CONTENT_TOP else 0.0


def compose_qp_pdf(
    groups: list[QuizGroup],
    out_dir: Path,
    title: str = "Practice Question Paper",
    duration_minutes: int | None = None,
    include_cover: bool = True,
    first_number: int = 1,
    footer_note: str = "",
    booklet: bool = False,
    progress: Callable[[], None] | None = None,
    subject_name: str = "",
) -> bytes:
    """`include_cover=False` and `first_number` exist for the question bank's single-
    question preview: the question laid out exactly as it will be in the paper, numbered as
    the question it would become, without a cover or page furniture around it.

    `booklet=True` is a classified (topical) booklet (classified.py) rather than an exam:
    its own cover (the topic as `title`, `subject_name`, questions and marks - no candidate
    boxes, no time), no multiple-choice answer sheet, and the sources list (with each
    question's reprints, QuizGroup.also_in) on its last pages. Everything else - crops,
    numbering, sections, data pages, the board's page style - is the exam's."""
    doc = pymupdf.open()
    resolved = _resolve_all(groups, out_dir, first_number)
    total_marks = sum(r.marks_total for r in resolved if r)
    # The whole paper carries its sources' data pages where they print them - the list of
    # formulas straight after the cover, the periodic table at the back. A single question
    # previewed on its own doesn't.
    formula_sheet = _find_data_page(resolved, formula_sheet_region) if include_cover else None
    periodic_table = _find_data_page(resolved, periodic_table_region) if include_cover else None
    if periodic_table is not None and _board(resolved) == "Edexcel":
        # the newest Edexcel paper's table, whichever papers the questions came from
        periodic_table = _latest_periodic_table(out_dir) or periodic_table
    cover_y = (
        _draw_cover(
            doc, title, len(resolved), total_marks,
            is_mark_scheme=False,
            duration_minutes=duration_minutes,
            has_multiple_choice=any(r.has_multiple_choice for r in resolved if r),
            has_periodic_table=periodic_table is not None,
            board=_board(resolved),
            subject_name=subject_name or _subject_name(resolved),
            booklet=booklet,
        )
        if include_cover
        else None
    )
    # One layout for the whole paper, not one per question: every question has to start at
    # the same left margin, the way a real paper sets them. Centring each question on its
    # own width instead makes narrower ones step progressively inward down the page.
    layout = _Layout.for_blocks([block for r in resolved if r for block in r.blocks])
    numbers = _section_numbers(resolved, first_number)
    mcq_numbers = [number for number, r in zip(numbers, resolved) if _is_mcq(r)]
    if formula_sheet is not None:
        _append_full_page(doc, *formula_sheet)
    flow = _PageFlow(doc)
    edexcel = _board(resolved) == "Edexcel"

    try:
        for position, r in enumerate(resolved):
            if progress is not None:
                progress()  # one call per question, as it starts
            quiz_number = numbers[position]
            if _starts_mcq_section(resolved, position):
                # the multiple-choice section starts on a page of its own, straight after
                # its answer sheet - never sharing a page with a written question
                if include_cover and not booklet:
                    flow.insert_pages(lambda d: _append_answer_sheets(d, mcq_numbers))
                else:
                    flow.start_new_page()
                if include_cover and _has_both_sections(resolved):
                    # its numbering starts again at 1, so say which section this is
                    flow.place_heading("Multiple-choice questions")
            if r is None:
                flow.place_placeholder(f"Question {quiz_number}: (source paper not found)")
                continue
            if not Path(r.paper.source_qp_path).exists():
                # Its extraction is in the bank but the PDF it was copied from has gone (moved
                # or deleted from the papers folder) - there's nothing to copy the question
                # from, so say which file is missing rather than fail the whole paper.
                flow.place_placeholder(
                    f"Question {quiz_number}: its question paper PDF "
                    f"({Path(r.paper.source_qp_path).name}) is missing from the papers folder"
                )
                continue
            for path in r.missing_paths:
                flow.place_placeholder(f"Question {quiz_number}: part {path!r} not found")
            if not r.blocks:
                continue
            heights = [_block_height(block, layout) for block in r.blocks]
            for index, block in enumerate(r.blocks):
                keep = _keep_with_next(r.blocks, heights, index)
                flow.place_block(r.paper.source_qp_path, block, layout, keep_with_next=keep)
            if edexcel and include_cover:
                pages = _source_answer_pages(r.paper.source_qp_path, r.question.question_number)
                if pages is not None:
                    # as much room as the source gave the question, for the share of its
                    # top-level parts chosen - room goes by part, never by subpart: (a)(i)
                    # and (a)(ii) share (a)'s page, as (i)(a) and (i)(b) share (i)'s - and
                    # always the rest of the question's own page
                    tops = {p.path.split(".")[0] for p in r.question.parts}
                    chosen = {path.split(".")[0] for path in r.part_paths} or tops
                    share = len(chosen & tops) / len(tops) if tops else 1.0
                    flow.place_answer_space(quiz_number, round(pages * min(share, 1.0)))
            if edexcel:
                flow.place_total(f"(Total for Question {quiz_number} = {r.marks_total} mark{'s' if r.marks_total != 1 else ''})")
            flow.cursor_y += QUESTION_GAP - ITEM_GAP

        if periodic_table is not None:
            board = next((r.paper.board for r in resolved if r is not None), "Cambridge")
            _append_periodic_table(doc, periodic_table, board)
        if edexcel:
            _edexcel_page_style(doc, flow.question_pages)
        if booklet and include_cover:
            _draw_sources(
                doc, resolved, "Question sources",
                "Where each question in this booklet was taken from, numbered as printed here.",
            )
        if cover_y is None:
            return doc.tobytes()
        return _finish(doc, cover_y, f"{title} - Question Paper", footer_note, board=_board(resolved))
    finally:
        doc.close()


# ---------------------------------------------------------------------------------------
# Mark scheme
#
# Laid out the way a real one is: landscape pages, the marking principles first, then one
# ruled Question | Answer | Marks table per question. Rows, header and principles are all
# copied as vector content from the source mark scheme (located by
# markscheme.parse_mark_scheme_layout), so answers keep their subscripts, bold mark codes
# and sketched graphs - only the question label is rewritten. A row that can't be located
# in its source falls back to a ruled row typeset from the extracted text.
# ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _MSGeometry:
    """Page size, the body band between running header and footer, and the standard
    table's left and right edges - per orientation, measured on the corpus."""

    width: float
    height: float
    content_top: float
    content_bottom: float
    table_x0: float
    table_x1: float


# Every landscape scheme sets its running header in y=20-48 and its footer from y=550, and
# its table 59-782 wide; every portrait one (multiple-choice, maths) its footer from y=796
# and its table 49-546.
LANDSCAPE_MS = _MSGeometry(PAGE_HEIGHT, PAGE_WIDTH, 56, 540, 62.0, 780.0)
PORTRAIT_MS = _MSGeometry(PAGE_WIDTH, PAGE_HEIGHT, 56, 786, 49.5, 546.0)

MS_TABLE_GAP = 20  # between one question's table and the next
# A copied row includes the rules above and below it; stacking the next row this far up
# lays its top rule over the previous row's bottom one, the way cells share a border.
RULE_OVERLAP = 0.5
# the fallback row's fixed columns, matching the real table's proportions
MS_QUESTION_COL, MS_MARKS_COL = 60.0, 46.0
# A source table this much narrower than the page's standard one is re-set at full width.
# The landscape tables measured 720-737pt wide, the portrait ones 497pt.
NARROW_TABLE_FRACTION = 0.85
# the header row's fill, as every scheme in the corpus draws it
HEADER_FILL = (0.949, 0.949, 0.949)
# how far inside a cell's borders its contents are cut from - clear of the thickest rule
# measured (0.8pt), and nowhere near any cell text
CELL_INSET = 1.5
# how close to a copied cell's top or foot a line has to be to count as the row's border
_CELL_EDGE = 4.0

_QP_NAME_RE = re.compile(r"(^|[_-])qp([_-]|$)", re.IGNORECASE)


def _source_ms_path(paper: Paper) -> str | None:
    """The mark scheme the paper was extracted with - recorded since `source_ms_path` was
    added, and otherwise found beside the question paper by the same qp -> ms naming
    convention `qbx calibrate` pairs them with."""
    if paper.source_ms_path and Path(paper.source_ms_path).exists():
        return paper.source_ms_path
    qp = Path(paper.source_qp_path)
    if not _QP_NAME_RE.search(qp.stem):
        return None
    ms = qp.with_name(_QP_NAME_RE.sub(r"\1ms\2", qp.stem) + qp.suffix)
    return str(ms) if ms.exists() else None


def _preamble_section(title: str) -> str:
    """Which marking-notes section a preamble page belongs to, keyed so the same section
    from two schemes compares equal. The shared ones need grouping: the generic principles
    run to one page in a maths scheme but two in a science one (the second headed "GENERIC
    MARKING PRINCIPLE 5:"), and their wording drifts between years ("descriptors" vs
    "descriptions"); the annotations guidance continues onto a page headed "Annotation
    Meaning". Everything else is keyed by its own heading."""
    key = " ".join(title.lower().split())
    if "generic marking principle" in key:
        return "generic marking principles"
    if key.startswith("annotation"):
        return "annotations guidance"
    return key


def _ms_row_label(quiz_number: int, ms_label: str, part_path: str, entry_path: str) -> str:
    """The new label for a mark scheme row. Usually the part's own new label, but the
    scheme doesn't always split a question the way the paper does: a row may go deeper
    (the paper's (b) marked as (b)(i) and (b)(ii)) - keep those extra levels - or cover a
    whole container (one row for (c)(i) and (c)(ii)) - then only the container's label."""
    new = re.findall(r"\([^)]*\)", ms_label)
    part_depth = len(part_path.split(".")) if part_path else 0
    entry_parts = entry_path.split(".") if entry_path else []
    if len(entry_parts) <= part_depth:
        return f"{quiz_number}{''.join(new[: len(entry_parts)])}"
    extra = "".join(f"({p})" for p in entry_parts[part_depth:])
    return f"{quiz_number}{''.join(new)}{extra}"


@dataclass
class _MSRow:
    label: str
    layout: MSRowLayout | None = None  # located in the source: copied
    text: str = ""  # otherwise typeset from the extracted answer
    marks: int | None = None


class _MSFlow:
    def __init__(self, doc: pymupdf.Document, geometry: _MSGeometry = LANDSCAPE_MS):
        self.doc = doc
        self.g = geometry
        self.page: pymupdf.Page | None = None
        self.cursor_y = geometry.content_top

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=self.g.width, height=self.g.height)
        self.cursor_y = self.g.content_top

    def room(self) -> float:
        return self.g.content_bottom - self.cursor_y

    def _scale(self, clip: pymupdf.Rect) -> float:
        return min(
            1.0,
            # a little wider than the standard table, so one that size copies unscaled
            (self.g.width - 2 * (self.g.table_x0 - 15)) / clip.width,
            (self.g.content_bottom - self.g.content_top) / clip.height,
        )

    def place_page_region(self, source_path: str, region: BBox, portrait: bool) -> None:
        """A whole source page's body (the marking principles) on a page of its own, in the
        source page's own orientation, at full size. Real-paper finding (0580 m25 MS 22): a
        portrait scheme's 748pt-tall principles page, set on a landscape page, ran off its
        foot and through the footer - and shrinking it to fit made it a third smaller than
        every page around it."""
        geometry = PORTRAIT_MS if portrait else LANDSCAPE_MS
        self.page = self.doc.new_page(width=geometry.width, height=geometry.height)
        clip = pymupdf.Rect(region.x0, region.y0, region.x1, region.y1)
        top = geometry.content_top - 8
        # 1.0 for every page measured; the guard only matters for an unusually tall one
        scale = min(1.0, geometry.width / clip.width, (geometry.content_bottom - top) / clip.height)
        left = (geometry.width - clip.width * scale) / 2
        self.copy(source_path, region, at=pymupdf.Rect(left, top, left + clip.width * scale, top + clip.height * scale))

    def copy(
        self,
        source_path: str,
        region: BBox,
        relabel: tuple[LabelRef, str] | None = None,
        at: pymupdf.Rect | None = None,
    ) -> None:
        """Copies a source region centred at the cursor (or into `at`), redrawing its label."""
        clip = pymupdf.Rect(region.x0, region.y0, region.x1, region.y1)
        scale = self._scale(clip)
        if at is None:
            left = (self.g.width - clip.width * scale) / 2
            at = pymupdf.Rect(left, self.cursor_y, left + clip.width * scale, self.cursor_y + clip.height * scale)
        label = relabel[0] if relabel is not None else None
        trimmed = _trimmed_source(source_path, region.page, clip, label, upright=True)
        try:
            self.page.show_pdf_page(at, trimmed, region.page, clip=clip)
        finally:
            trimmed.close()
        if relabel is not None:
            self._draw_centred_label(clip, at, scale, *relabel)
        self.cursor_y = at.y1

    def _draw_centred_label(
        self, clip: pymupdf.Rect, target: pymupdf.Rect, scale: float, label: LabelRef, text: str
    ) -> None:
        # centred where the original was - the Question column centres its labels, and the
        # new one is often a different length ("3(d)(ii)" -> "2(b)(ii)", "12" -> "1")
        size = label.size * scale
        font, fontfile = _label_font(label.bold, label.font)
        measure = pymupdf.Font(fontfile=fontfile) if fontfile else pymupdf.Font(font)
        centre_x = target.x0 + ((label.x0 + label.x1) / 2 - clip.x0) * scale
        baseline_y = target.y0 + ((label.origin_y or label.y1) - clip.y0) * scale
        self.page.insert_text(
            (centre_x - measure.text_length(text, fontsize=size) / 2, baseline_y),
            text, fontsize=size, fontname=font, fontfile=fontfile,
        )

    def height_of(self, region: BBox) -> float:
        clip = pymupdf.Rect(region.x0, region.y0, region.x1, region.y1)
        return clip.height * self._scale(clip)

    # -- fallback: a ruled row typeset from extracted text ---------------------------------

    def _columns(self) -> tuple[float, float, float, float]:
        return (
            self.g.table_x0,
            self.g.table_x0 + MS_QUESTION_COL,
            self.g.table_x1 - MS_MARKS_COL,
            self.g.table_x1,
        )

    def _text_cell(self, rect: pymupdf.Rect, text: str, bold: bool = False, align: str = "left", size: float = 10) -> float:
        """Typesets into `rect`; returns the height left over (negative: didn't fit).

        insert_htmlbox rather than insert_textbox: answers carry characters the base-14
        fonts can't encode (U+2212, an enthalpy change's delta), which come out as "?"
        through insert_textbox, while an embedded TTF renders them but makes every space
        extract as U+00A0 - breaking copy-paste. insert_htmlbox gets both right."""
        weight = "bold" if bold else "normal"
        spare, _ = self.page.insert_htmlbox(
            rect,
            html.escape(text).replace("\n", "<br/>"),
            css=f"* {{font-family: sans-serif; font-size: {size}px; line-height: 1.35;"
            f" font-weight: {weight}; text-align: {align};}}",
        )
        return spare

    def text_header(self) -> None:
        x0, q1, m0, x1 = self._columns()
        top, height = self.cursor_y, 25.0
        self.page.draw_rect(pymupdf.Rect(x0, top, x1, top + height), color=(0, 0, 0), fill=HEADER_FILL, width=0.5)
        for a, b, text in ((x0, q1, "Question"), (q1, m0, "Answer"), (m0, x1, "Marks")):
            self._text_cell(pymupdf.Rect(a + 4, top + 6, b - 4, top + height), text, bold=True, align="center", size=11)
        for x in (q1, m0):
            self.page.draw_line((x, top), (x, top + height), color=(0, 0, 0), width=0.5)
        self.cursor_y = top + height

    def text_row(self, row: _MSRow) -> bool:
        """Draws the row if it fits in what's left of the page; False if it doesn't."""
        x0, q1, m0, x1 = self._columns()
        top = self.cursor_y
        body = pymupdf.Rect(q1 + 6, top + 7, m0 - 6, self.g.content_bottom)
        # typeset once on a scratch page to measure, so a row that won't fit leaves no trace
        scratch = pymupdf.open()
        try:
            probe = scratch.new_page(width=self.g.width, height=self.g.height)
            spare, _ = probe.insert_htmlbox(
                body, html.escape(row.text).replace("\n", "<br/>"),
                css="* {font-family: sans-serif; font-size: 10px; line-height: 1.35;}",
            )
        finally:
            scratch.close()
        if spare < 0:
            return False
        bottom = max(body.y1 - spare + 7, top + 25)
        self._text_cell(pymupdf.Rect(body.x0, body.y0, body.x1, bottom), row.text)
        self._text_cell(pymupdf.Rect(x0 + 2, top + 7, q1 - 2, bottom), row.label, align="center")
        if row.marks is not None:
            self._text_cell(pymupdf.Rect(m0 + 2, top + 7, x1 - 6, bottom), str(row.marks), bold=True, align="right")
        self.page.draw_rect(pymupdf.Rect(x0, top, x1, bottom), color=(0, 0, 0), width=0.5)
        for x in (q1, m0):
            self.page.draw_line((x, top), (x, bottom), color=(0, 0, 0), width=0.5)
        self.cursor_y = bottom
        return True

    # -- one question's table ---------------------------------------------------------------

    def widened_columns(self, columns: list[float], marks_column: int) -> list[float]:
        """The source table's column rules spread to the page's standard width: the
        Question and Marks columns keep their own widths, and the text columns (Answer, and
        a maths scheme's Partial Marks) share the extra in proportion to their own."""
        widths = [b - a for a, b in zip(columns, columns[1:])]
        fixed = {0, marks_column}
        flexible = sum(w for i, w in enumerate(widths) if i not in fixed) or 1.0
        extra = (self.g.table_x1 - self.g.table_x0) - sum(widths)
        out = [self.g.table_x0]
        for i, w in enumerate(widths):
            out.append(out[-1] + w + (0.0 if i in fixed else extra * w / flexible))
        return out

    def widened_row(
        self,
        source_path: str,
        segment: BBox,
        columns: list[float],
        marks_column: int,
        label: LabelRef | None = None,
        text: str | None = None,
        header: bool = False,
        source_columns: list[float] | None = None,
    ) -> None:
        """A row from a narrower source table, set in the full-width grid: the grid is drawn
        here, and each cell's contents copied into its widened cell - at the same offset
        from the cell's left edge as in the source, the marks flush right, a header's
        headings centred. The Question cell's label is drawn afresh, in the source's font."""
        out = self.widened_columns(columns, marks_column)
        # the cells are cut from the source page's own columns, where they differ
        source = source_columns if source_columns and len(source_columns) == len(columns) else columns
        inset = CELL_INSET
        # a source page whose column is wider than the grid's has its row scaled to fit,
        # rather than its text running across the next column's rule
        scale = min(
            [1.0] + [
                (out[i + 1] - out[i] - 2 * inset) / (source[i + 1] - source[i] - 2 * inset)
                for i in range(len(columns) - 1)
                if (i > 0 or header) and source[i + 1] - source[i] > 2 * inset
            ]
        )
        top, height = self.cursor_y, (segment.y1 - segment.y0) * scale
        if header:
            self.page.draw_rect(pymupdf.Rect(out[0], top, out[-1], top + height), color=None, fill=HEADER_FILL)
        for i in range(len(columns) - 1):
            if i == 0 and not header:
                continue  # the label, redrawn below
            clip = pymupdf.Rect(source[i] + inset, segment.y0 + inset, source[i + 1] - inset, segment.y1 - inset)
            width = clip.width * scale
            if header:
                left = (out[i] + out[i + 1] - width) / 2
            elif i == marks_column:
                left = out[i + 1] - inset - width
            else:
                left = out[i] + inset
            target = pymupdf.Rect(left, top + inset * scale, left + width, top + (inset + clip.height) * scale)
            trimmed = _trimmed_source(
                source_path, segment.page, clip, None, upright=True, strip_rules=source_columns is not None
            )
            try:
                self.page.show_pdf_page(target, trimmed, segment.page, clip=clip)
            finally:
                trimmed.close()
        if text is not None and label is not None:
            font, fontfile = _label_font(label.bold, label.font)
            measure = pymupdf.Font(fontfile=fontfile) if fontfile else pymupdf.Font(font)
            width = measure.text_length(text, fontsize=label.size)
            baseline = top + ((label.origin_y or label.y1) - segment.y0) * scale
            self.page.insert_text(
                ((out[0] + out[1] - width) / 2, baseline), text, fontsize=label.size, fontname=font, fontfile=fontfile
            )
        self.page.draw_rect(pymupdf.Rect(out[0], top, out[-1], top + height), color=(0, 0, 0), width=0.5)
        for x in out[1:-1]:
            self.page.draw_line((x, top), (x, top + height), color=(0, 0, 0), width=0.5)
        self.cursor_y = top + height

    def grid_header(self, columns: list[float], marks_column: int, names: list[str]) -> None:
        """The header row of a table set in our own grid (`MSLayout.regrid`): the source's
        column names, centred and bold on the header fill, rather than its cells copied."""
        out = self.widened_columns(columns, marks_column)
        top, height = self.cursor_y, 26.0
        self.page.draw_rect(pymupdf.Rect(out[0], top, out[-1], top + height), color=None, fill=HEADER_FILL)
        for i, name in enumerate(names[: len(out) - 1]):
            box = pymupdf.Rect(out[i] + 2, top + 3, out[i + 1] - 2, top + height - 2)
            size = 9.0
            if pymupdf.get_text_length(name, fontsize=size, fontname="hebo") > box.width:
                box.y0 = top + 1  # two lines: "Question / number"
            self.page.insert_textbox(box, name, fontsize=size, fontname="hebo", align=pymupdf.TEXT_ALIGN_CENTER)
        self.page.draw_rect(pymupdf.Rect(out[0], top, out[-1], top + height), color=(0, 0, 0), width=0.5)
        for x in out[1:-1]:
            self.page.draw_line((x, top), (x, top + height), color=(0, 0, 0), width=0.5)
        self.cursor_y = top + height

    def place_heading(self, text: str) -> None:
        # never left alone at a page's foot: it needs room for a table header and a row
        if self.page is None or self.room() < 110:
            self.new_page()
        self.page.insert_text((self.g.table_x0, self.cursor_y + 14), text, fontsize=13, fontname="hebo")
        self.cursor_y += 26

    def place_table(self, source_path: str | None, layout: "MSLayout | None", rows: list[_MSRow]) -> None:
        header = layout.header if layout else None
        use_source_header = source_path is not None and header is not None
        # Real-paper finding: multiple-choice mark schemes are portrait, so their table is
        # ~497pt wide against ~720pt for every landscape one. Copied as-is it sat as a
        # narrow table in the middle of the page; scaled up to width, its text would be
        # half as big again. So it's set in the standard full-width grid instead.
        # Real-paper finding (0580 m25 MS 22): a maths scheme has four columns, not three -
        # so this works from however many rules the header row has, and which one is Marks.
        columns = layout.columns if layout else []
        marks_column = layout.marks_column if layout else None
        widen = (
            use_source_header
            and len(columns) >= 4
            and marks_column is not None
            and (
                layout.regrid
                or columns[-1] - columns[0] < NARROW_TABLE_FRACTION * (self.g.table_x1 - self.g.table_x0)
            )
        )
        page_columns = layout.page_columns if layout else {}

        def put_header() -> None:
            if widen and layout.regrid and any(layout.column_names):
                self.grid_header(columns, marks_column, layout.column_names)
            elif widen:
                self.widened_row(
                    source_path, header, columns, marks_column, header=True,
                    source_columns=page_columns.get(header.page),
                )
            elif use_source_header:
                self.copy(source_path, header)
                self.cursor_y -= RULE_OVERLAP
            else:
                self.text_header()

        header_height = self.height_of(header) if use_source_header else 25.0
        first = rows[0] if rows else None
        first_height = (
            self.height_of(first.layout.segments[0]) if first and first.layout else 40.0
        )
        # a header is never left at the foot of a page without its first row
        if self.page is None or header_height + first_height > self.room():
            self.new_page()
        put_header()

        for row in rows:
            if row.layout is not None and source_path is not None:
                label = row.layout.label
                for index, segment in enumerate(row.layout.segments):
                    if self.height_of(segment) > self.room():
                        self.new_page()
                        put_header()
                    if widen:
                        self.widened_row(
                            source_path, segment, columns, marks_column, label, row.label if index == 0 else None,
                            source_columns=page_columns.get(segment.page),
                        )
                        continue
                    relabel = (label, row.label) if index == 0 else None
                    self.copy(source_path, segment, relabel)
                    self.cursor_y -= RULE_OVERLAP
            elif not self.text_row(row):
                self.new_page()
                put_header()
                if not self.text_row(row):
                    # longer than a page on its own - show what fits rather than nothing
                    self.text_row(_MSRow(label=row.label, text=row.text[:1500] + " ...", marks=row.marks))
        self.cursor_y += RULE_OVERLAP + MS_TABLE_GAP


def _ms_page_furniture(doc: pymupdf.Document, title: str, footer_note: str = "") -> None:
    """A running header and "Page n of N" footer on every page after the cover, where a
    real mark scheme carries them - naming this paper, never a board (CLAUDE.md) - with the
    builder's name and the teacher's note either side of the page count."""
    total = doc.page_count
    for index, page in enumerate(doc):
        if index == 0:
            continue
        width = page.rect.width
        page.insert_text((MARGIN - 12, 34), title, fontsize=10, fontname="helv")
        heading = "Mark Scheme"
        heading_width = pymupdf.get_text_length(heading, fontsize=10, fontname="hebo")
        page.insert_text(((width - heading_width) / 2, 34), heading, fontsize=10, fontname="hebo")
        footer = f"Page {index + 1} of {total}"
        footer_width = pymupdf.get_text_length(footer, fontsize=8, fontname="helv")
        page.insert_text(((width - footer_width) / 2, page.rect.height - 30), footer, fontsize=8, fontname="helv")
        _draw_footer_sides(
            page, page.rect.height - 30, MARGIN - 12, width - MARGIN + 12, footer_width, footer_note
        )


# ---------------------------------------------------------------------------------------
# Sources list
#
# A page for the teacher, handed out with nothing: where each question of the composed
# paper was taken from, numbered as the paper prints it.
# ---------------------------------------------------------------------------------------

_SOURCE_COLUMNS = (("Q", 36), ("Paper", 175), ("Sitting", 100), ("Question", 64), ("Parts used", 120))


def _sitting_name(paper: Paper) -> str:
    session = session_name(paper.session)
    year = paper.year + 2000 if paper.year < 100 else paper.year
    if paper.board == "Edexcel" and paper.session == "NOV" and year == 2020:
        # Pearson's June 2020 series, postponed to the autumn: its covers carry the May date
        return f"{session} {year} (June 2020)"
    return f"{session} {year}"


def _parts_used(r: _ResolvedGroup) -> str:
    """"All" for the whole question, else the picked parts as the source paper labels
    them - "(a)(i), (b)"."""
    every = [p.path for p in r.question.parts]
    if not any(r.part_paths) or sorted(r.part_paths) == sorted(every):
        return "All"
    return ", ".join("".join(f"({piece})" for piece in path.split(".")) for path in r.part_paths)


# with reprints to list (a classified booklet), the same columns narrowed for "Also in"
_SOURCE_COLUMNS_WITH_REPRINTS = (
    ("Q", 30), ("Paper", 120), ("Sitting", 82), ("Question", 52), ("Parts used", 86), ("Also in", 125),
)


def _draw_sources(
    doc: pymupdf.Document, resolved: list["_ResolvedGroup | None"], title: str,
    intro: str = "Where each question in this paper was taken from.",
) -> None:
    """The sources list, on pages added to the end of `doc`: one row per printed question,
    in the composed paper's own order and numbering (written questions, then multiple
    choice). An "Also in" column appears when any question was reprinted elsewhere."""
    reprints = any(r is not None and r.also_in for r in resolved)
    columns = _SOURCE_COLUMNS_WITH_REPRINTS if reprints else _SOURCE_COLUMNS
    right = PAGE_WIDTH - MARGIN
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = CONTENT_TOP + 6
    page.insert_text((MARGIN, y), title, fontsize=16, fontname="hebo")
    y += 18
    page.insert_text((MARGIN, y), intro, fontsize=10)
    y += 22

    def header(page: pymupdf.Page, y: float) -> float:
        page.draw_rect(pymupdf.Rect(MARGIN, y, right, y + 20), color=None, fill=(0.92, 0.92, 0.92))
        x = MARGIN
        for name, width in columns:
            page.insert_text((x + 5, y + 14), name, fontsize=10, fontname="hebo")
            x += width
        return y + 20

    y = header(page, y)
    section = None
    for number, r in zip(_section_numbers(resolved), resolved):
        kind = "Multiple choice" if _is_mcq(r) else "Written questions"
        if r is None:
            cells = [str(number), "(source paper not found)", "", "", ""]
        else:
            paper_name = f"{r.paper.subject}/{r.paper.paper}{r.paper.variant}"
            if r.paper.paper_title:
                paper_name += f" {r.paper.paper_title}"
            cells = [
                str(number), paper_name, _sitting_name(r.paper),
                f"Q{r.question.question_number}", _parts_used(r),
            ]
        if reprints:
            cells.append(", ".join(r.also_in) if r is not None else "")
        # a row's height is its tallest wrapped cell
        heights = []
        for (name, width), text in zip(columns, cells):
            lines = max(1, -(-pymupdf.get_text_length(text, fontsize=9.5) // (width - 10)))
            heights.append(lines * 12 + 8)
        row_h = max(heights) + (18 if kind != section else 0)
        if y + row_h > CONTENT_BOTTOM:
            page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            y = header(page, CONTENT_TOP)
            section = None
        if kind != section:
            section = kind
            page.insert_text((MARGIN + 5, y + 13), kind, fontsize=9.5, fontname="hebo", color=(0.35, 0.35, 0.35))
            y += 18
        x = MARGIN
        height = max(heights)
        for (name, width), text in zip(columns, cells):
            page.insert_textbox(
                pymupdf.Rect(x + 5, y + 4, x + width - 5, y + height), text, fontsize=9.5,
                fontname="hebo" if name == "Q" else "helv", lineheight=1.15,
            )
            x += width
        y += height
        page.draw_line((MARGIN, y), (right, y), width=0.4, color=(0.7, 0.7, 0.7))


def compose_sources_pdf(
    groups: list[QuizGroup], out_dir: Path, title: str = "Practice Question Paper",
    footer_note: str = "",
) -> bytes:
    """Where each question came from - paper, sitting, question and the parts used - in the
    composed paper's own order and numbering (written questions, then multiple choice)."""
    doc = pymupdf.open()
    _draw_sources(doc, _resolve_all(groups, out_dir), title)
    try:
        _add_page_furniture(doc, f"{title} - Question Sources", skip_pages=0, footer_note=footer_note)
        return _compact(doc)
    finally:
        doc.close()


def compose_ms_pdf(
    groups: list[QuizGroup], out_dir: Path, title: str = "Practice Mark Scheme",
    footer_note: str = "", booklet: bool = False, subject_name: str = "",
    progress: Callable[[], None] | None = None,
) -> bytes:
    """`booklet`: a classified booklet's mark scheme - its cover only differs."""
    doc = pymupdf.open()
    resolved = _resolve_all(groups, out_dir)
    total_marks = sum(r.marks_total for r in resolved if r)
    cover_y = _draw_cover(
        doc, title, len(resolved), total_marks, is_mark_scheme=True,
        board=_board(resolved), subject_name=subject_name or _subject_name(resolved), booklet=booklet,
    )

    layouts: dict[str, MSLayout] = {}
    for r in resolved:
        path = _source_ms_path(r.paper) if r is not None else None
        if path is not None and path not in layouts:
            layouts[path] = _scheme_layout(path, r.paper.board, _mtime_ns(path))
    # Portrait only when every scheme drawn on is portrait (a maths-only paper), as those
    # schemes are themselves. Any landscape scheme makes it landscape: a narrower portrait
    # table widens to fit a landscape page, whereas a landscape one would have to shrink
    # by a third to fit a portrait page.
    all_portrait = bool(layouts) and not any(layout.landscape for layout in layouts.values())
    flow = _MSFlow(doc, PORTRAIT_MS if all_portrait else LANDSCAPE_MS)

    def layout_for(paper: Paper) -> tuple[str | None, MSLayout | None]:
        path = _source_ms_path(paper)
        if path is None:
            return None, None
        if path not in layouts:
            layouts[path] = _scheme_layout(path, paper.board, _mtime_ns(path))
        return path, layouts[path]

    # The marking notes of every mark scheme the paper draws on, in order of first use: a
    # Maths question brings its Mathematics-Specific Marking Principles and MARK SCHEME
    # NOTES, a Chemistry one its Science-Specific principles - but what every scheme shares
    # (the generic principles, the annotations guidance) appears once.
    # One scheme per subject: two years of the same subject carry the same notes, but not
    # always under the same headings (0620 s24 opens its science principles on a page headed
    # "GENERIC MARKING PRINCIPLE 5:", s23 on one headed "Science-Specific Marking
    # Principles"), so matching by heading alone repeated them.
    included_sections: set[str] = set()
    used_paths: list[str] = []
    used_subjects: set[str] = set()
    for r in resolved:
        path = _source_ms_path(r.paper) if r is not None else None
        if path is not None and path not in used_paths and r.paper.subject not in used_subjects:
            used_paths.append(path)
            used_subjects.add(r.paper.subject)
    for path in used_paths:
        layout = layouts[path]
        sections = [_preamble_section(title) for title in layout.preamble_titles]
        for region, section in zip(layout.preamble, sections):
            if section in included_sections:
                continue  # already in, from an earlier scheme
            # a preamble region spans its page's full width, which gives the orientation
            flow.place_page_region(path, region, portrait=region.x1 < PAGE_HEIGHT - 100)
        # only now: a section spanning several pages of this scheme keeps all of them
        included_sections.update(sections)
    flow.page = None  # the tables start on a page of their own

    both_sections = _has_both_sections(resolved)
    for position, (quiz_number, r) in enumerate(zip(_section_numbers(resolved), resolved)):
        if progress is not None:
            progress()
        if both_sections and (position == 0 or _starts_mcq_section(resolved, position)):
            # each section numbers from 1, so each is headed
            flow.place_heading("Multiple-choice questions" if _is_mcq(r) else "Written questions")
        if r is None:
            flow.place_table(None, None, [_MSRow(label=str(quiz_number), text="(source paper not found)")])
            continue
        path, layout = layout_for(r.paper)
        entries_by_id = {e.id: e for e in r.paper.mark_scheme}
        parts_by_id = {p.id: p for p in r.question.parts}
        joins_by_part: dict[str, list[str]] = {}
        for j in r.paper.joins:
            joins_by_part.setdefault(j.part_id, []).append(j.mark_scheme_entry_id)

        rows: list[_MSRow] = []
        placed: set[str] = set()
        for ms_label, marks, part_id in r.ms_entries:
            part = parts_by_id.get(part_id)
            entry_ids = [eid for eid in joins_by_part.get(part_id, []) if eid in entries_by_id]
            if not entry_ids:
                rows.append(_MSRow(label=f"{quiz_number}{ms_label}", text="(no mark scheme answer found)", marks=marks))
                continue
            for eid in entry_ids:
                if eid in placed:
                    continue  # one row marking several selected parts appears once
                placed.add(eid)
                entry = entries_by_id[eid]
                label = _ms_row_label(quiz_number, ms_label, part.path if part else "", entry.part_path)
                row_layout = layout.rows.get(eid) if layout else None
                if entry.segments and entry.label is not None:
                    # linked by hand in the Reviewer: the region the teacher drew
                    row_layout = MSRowLayout(segments=entry.segments, label=entry.label)
                if row_layout is not None:
                    rows.append(_MSRow(label=label, layout=row_layout))
                else:
                    rows.append(_MSRow(label=label, text=entry.answer_text or "(no answer text)", marks=entry.marks))
        for missing in r.missing_paths:
            rows.append(_MSRow(label=str(quiz_number), text=f"(part {missing!r} not found)"))
        if rows:
            flow.place_table(path, layout, rows)

    try:
        _bullet(doc[0], MARGIN, cover_y, f"This document has {doc.page_count} pages, including this one.")
        _ms_page_furniture(doc, f"{title} - Mark Scheme", footer_note)
        return _compact(doc)
    finally:
        doc.close()
