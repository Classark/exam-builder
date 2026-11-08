"""Pydantic models for the extracted question-bank data (see CLAUDE.md section 6)."""

from pydantic import BaseModel, model_validator

from qbx.sources import locate


class BBox(BaseModel):
    """One page-segment of a part's region, in PDF point coordinates (page.rect space) -
    same geometry crops.py renders to SVG. A part spanning a page break has more than one
    of these. Used by the reviewer UI (phase 7) to draw the overlay rectangle on the
    rendered page image."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class LabelRef(BaseModel):
    """A label glyph run ("4", "(c)", "(ii)") - where it sits on the source page, and the
    type style it's set in. quiz_export.py needs both: it deletes the original glyphs and
    redraws a new label in their place, which only looks like a real edit if it matches the
    source's own font size and weight rather than guessing."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    size: float = 11.0
    bold: bool = True
    # the first glyph's baseline origin - exactly where the original text sat, which a
    # bbox alone can't give (its edges carry font-dependent ascender/descender padding).
    origin_x: float = 0.0
    origin_y: float = 0.0
    # the source's font name ("Arial-BoldMT", "TimesNewRomanPS-BoldMT") - a redrawn label is
    # set in the same family, since a maths paper is set in Times where the rest use Arial
    font: str = ""


class QuestionPart(BaseModel):
    id: str
    question_id: str
    path: str  # e.g. "a.i"
    depth: int
    text: str
    image_path: str
    marks: int
    # The mark the question paper prints for this part itself - 0 where it prints none (the
    # mark is the group's or the question's, and `marks` came from the mark scheme); None
    # for a paper extracted before this was recorded. A composed paper prints `marks` where
    # the two differ.
    marks_printed: int | None = None
    topic_ids: list[str] = []
    segments: list[BBox] = []
    # This part's own leading label glyph run (e.g. "(c)"), in source-page coordinates -
    # None for a synthetic unlettered-question part, which has no label to relabel.
    label_bbox: LabelRef | None = None
    # Set only for a container's *first* child, whose `segments`/`text`/`label_bbox` above
    # absorb the container's intro (see tree.py). These are that leaf on its own - its own
    # marker and its own content, with the container's intro excluded - so a composed paper
    # can render the container heading once (Question.container_headings) and then each
    # selected subpart beneath it without repeating the intro.
    body_image_path: str = ""
    body_segments: list[BBox] = []
    body_label_bbox: LabelRef | None = None
    # Withdrawn by the board: "Due to an issue with Question 5, the question has been
    # removed from the question paper", printed in the paper or its mark scheme. Kept so the
    # Reviewer shows it, but never checked, offered in the bank or set in an exam.
    removed: bool = False


class ContainerHeading(BaseModel):
    """A "part"-tier anchor that has subparts nested inside it (e.g. "(c) All smartphones
    have a MAC address." above (c)(i) and (c)(ii)). It gets no QuestionPart of its own -
    the flat schema only holds leaves - but a composed paper still has to show it, since a
    subpart lifted out without its heading loses the context it depends on."""

    path: str  # the container's own path, e.g. "c"
    text: str
    image_path: str = ""
    segments: list[BBox] = []
    label_bbox: LabelRef | None = None


class Question(BaseModel):
    paper_key: str
    board: str
    subject: str
    question_number: int
    stem_text: str
    stem_image_path: str
    total_marks: int
    topic_ids: list[str] = []  # denormalised union of descendant parts' topics
    parts: list[QuestionPart] = []
    stem_segments: list[BBox] = []
    # This question's own leading number glyph run (e.g. "4"), in source-page coordinates.
    number_bbox: LabelRef | None = None
    container_headings: list[ContainerHeading] = []
    # every part withdrawn by the board (see QuestionPart.removed)
    removed: bool = False


class MarkSchemeEntry(BaseModel):
    id: str
    paper_key: str
    question_number: int
    part_path: str
    answer_text: str
    answer_image_path: str | None = None
    marks: int
    guidance_text: str = ""
    # the label as printed, when markscheme._entry_ids read it as a typo for another -
    # join.py falls back to it if the corrected label matches no part
    printed_path: str | None = None
    # A row linked by hand in the Reviewer (qbx/ms_links.py): where it sits on the upright
    # mark scheme pages, and its label there if it prints one - what composing copies.
    segments: list[BBox] = []
    label: LabelRef | None = None
    manual: bool = False


class QuestionPartMarkScheme(BaseModel):
    """Join table: a QuestionPart may map to multiple MarkSchemeEntry rows and vice versa."""

    part_id: str
    mark_scheme_entry_id: str


class Paper(BaseModel):
    paper_key: str
    board: str
    level: str
    subject: str
    paper: str
    variant: str
    session: str
    year: int
    cover_total_marks: int | None = None
    # the QP's own page count against its cover's "This document has N pages" - a file
    # missing pages loses questions no parser can recover
    qp_page_count: int | None = None
    cover_page_count: int | None = None
    source_qp_path: str = ""  # absolute path to the original QP PDF, for the reviewer UI
    # absolute path to the original MS PDF - a composed mark scheme copies its rows from it
    source_ms_path: str = ""
    # as the cover prints them ("Chemistry", "Paper 4 Theory (Extended)") - the question
    # bank's subject and paper folders are labelled with these
    subject_name: str = ""
    paper_title: str = ""
    questions: list[Question] = []
    mark_scheme: list[MarkSchemeEntry] = []
    joins: list[QuestionPartMarkScheme] = []

    @model_validator(mode="after")
    def _relocate_sources(self) -> "Paper":
        # a data folder copied to another computer keeps the old absolute paths - every
        # load of a paper finds its PDFs under the papers folder in use now (sources.py)
        self.source_qp_path = locate(self.source_qp_path)
        self.source_ms_path = locate(self.source_ms_path)
        return self
