"""Build the question/part/subpart hierarchy from anchors, spanning page breaks (CLAUDE.md 5.2).

A part's content runs from its anchor to the next anchor of equal or higher tier (question <
part < subpart in the hierarchy, so "higher" means shallower - a part's span isn't cut short
by a subpart nested inside it). Applied literally per anchor, a part *with* subparts absorbs
its subparts' text into its own raw span - which is why only leaf anchors (nothing deeper
nested inside them) become their own QuestionPart entry, per the flat `path: "a.i"` schema in
CLAUDE.md section 6. A container part's intro text (before its first subpart) has nowhere else
to go in that schema, so it's prepended to the first child's text rather than silently dropped.
"""

import re

from pydantic import BaseModel

from qbx.anchors import (
    HEADER_FOOTER_MARGIN,
    AnchorSpan,
    TierResult,
    bottom_limit,
    is_horizontal_line,
    is_machine_readable_mark,
    is_page_furniture_text,
    is_page_number,
    line_top_map,
    page_blocks,
    top_margin,
)
from qbx.schema import ContainerHeading, LabelRef, Question, QuestionPart

TIER_RANK = {"question": 0, "part": 1, "subpart": 2}
_NUMERALS = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii"]
# an answer line: dots, or International A Level's underscores ("_____", a page of them)
DOTTED_LINE_RE = re.compile(r"^(?:\.{8,}|_{8,})$")
TOTAL_MARKS_RE = re.compile(r"\[Total:\s*(\d+)\]", re.IGNORECASE)
# Not anchored to end-of-string: a real paper's mark bracket is sometimes followed by a
# "Working space" label before the next anchor, which would defeat a strict `\s*$` anchor
# and silently drop a real mark value. Every bracket anywhere in the text counts instead
# (see _marks_value).
# Some formats (multiple choice) have no bracket anywhere - validate.py's "marks_unknown"
# check only flags a part if marks is still 0 after this *and* pipeline.py's mark-scheme
# backfill, not on text shape alone.
MARKS_RE = re.compile(r"\[(\d+)\]")


def _marks_value(text: str) -> int:
    """Every "[n]" allocation in the text, summed - "[Total: n]" is a different pattern and
    never counted. Real-paper finding (0620/41 ON21, 5(d)(iii)): one part can carry two
    allocations - an answer line worth "[1]", then a bullet "Draw the structure... [2]" -
    and taking only the last read a 3-mark part as 2."""
    return sum(int(match.group(1)) for match in MARKS_RE.finditer(text))


class ResolvedAnchor(BaseModel):
    span: AnchorSpan
    tier: str

    @property
    def label(self) -> str:
        return self.span.text.strip("()")

    @property
    def position(self) -> tuple[int, float, float]:
        return (self.span.page, self.span.y0, self.span.x0)


class TextSpan(BaseModel):
    text: str
    page: int
    y0: float
    x0: float

    @property
    def position(self) -> tuple[int, float, float]:
        return (self.page, self.y0, self.x0)


def ordered_anchors(tier_results: list[TierResult]) -> list[ResolvedAnchor]:
    anchors = [
        ResolvedAnchor(span=span, tier=result.tier)
        for result in tier_results
        for span in result.spans
    ]
    anchors.sort(key=lambda a: a.position)
    return _drop_out_of_sequence_parts(_drop_out_of_sequence_questions(anchors))


def _drop_out_of_sequence_questions(anchors: list[ResolvedAnchor]) -> list[ResolvedAnchor]:
    """A question number that breaks the paper's ascending sequence isn't a question number.
    Real-paper finding (0620/52 and 0620/62 FM20): a graph's y-axis label is set as lines
    of its own left of the plot - "time to collect" / "40 cm3 of gas / s" - so the "40"
    starts a line at the question numbers' indent and read as question 40, taking the rest
    of question 1 or 2 with it (questions 1, 40, 2, 3).

    Keeps the longest strictly increasing run of question numbers in reading order, rather
    than demanding each be exactly one more than the last: a question whose number was
    missed mustn't take every later question with it.
    """
    questions = [i for i, a in enumerate(anchors) if a.tier == "question"]
    numbers = [int(anchors[i].label) for i in questions]
    if not numbers:
        return anchors
    # best[k]: (length, -numbers skipped) of the best increasing run ending at numbers[k] -
    # between runs of equal length, the one closer to counting 1, 2, 3 (so 1, 2 beats 1,
    # 40); back[k]: the run's previous index. O(n^2) is nothing at ~40 questions a paper.
    best = [(1, 1 - n) for n in numbers]
    back = [-1] * len(numbers)
    for k in range(len(numbers)):
        for j in range(k):
            if numbers[j] < numbers[k]:
                candidate = (best[j][0] + 1, best[j][1] - (numbers[k] - numbers[j] - 1))
                if candidate > best[k]:
                    best[k], back[k] = candidate, j
    k = max(range(len(numbers)), key=lambda i: best[i])
    keep = set()
    while k != -1:
        keep.add(questions[k])
        k = back[k]
    # dropped numbers stay as text: the line's content is collected regardless
    return [a for i, a in enumerate(anchors) if a.tier != "question" or i in keep]


def _drop_out_of_sequence_parts(anchors: list[ResolvedAnchor]) -> list[ResolvedAnchor]:
    """A part letter that can't be where it is in the (a), (b), (c)... sequence isn't a
    part. Real-paper finding (0620 MCQ papers MJ22/ON23, Q7 in five of them): an equation
    prints its state symbols on a line of their own beneath it - "(l) (l) (aq)" - so the
    "(l)" starts its line and passed as part (l) of a question with no parts at all,
    splitting the question and losing its mark.

    Cambridge letters every question's parts from (a), in order. So a question's first
    part must be (a) - or (b), should (a) itself have been missed - and a letter never
    goes backwards. Skipping forward is let through: a missed part mustn't take the rest
    of the question's parts with it.
    """
    kept: list[ResolvedAnchor] = []
    last_letter: str | None = None
    for anchor in anchors:
        if anchor.tier == "question":
            last_letter = None
        elif anchor.tier == "part":
            letter = anchor.label
            if letter in _NUMERALS and (last_letter is None or last_letter in _NUMERALS) and last_letter != "h":
                # parts numbered (i), (ii), (iii) rather than lettered (older Mathematics A
                # papers): the same rule, in numeral order
                order = _NUMERALS.index(letter)
                in_sequence = order <= 1 if last_letter is None else order > _NUMERALS.index(last_letter)
            else:
                in_sequence = letter in ("a", "b") if last_letter is None else letter > last_letter
            if not in_sequence:
                continue  # stays as text: the line's content is collected regardless
            last_letter = letter
        kept.append(anchor)
    return kept


def collect_text_spans(
    doc: object, content_pages: set[int] | None = None, margin: float = HEADER_FOOTER_MARGIN
) -> list[TextSpan]:
    """Every non-empty, non-dotted-answer-line text span in reading order, across page
    breaks. Uses each line's own bbox y0 (not each span's) so spans sharing a visual line
    sort together even if their individual bboxes jitter by a fraction of a point.

    Skips vertically-oriented lines (a "DO NOT WRITE IN THIS MARGIN" sidebar strip, on the
    left or right edge depending on the board) - see anchors.is_horizontal_line.
    """
    spans = []
    rights: list[float] = []
    for page_index, page in enumerate(doc):
        if content_pages is not None and page_index not in content_pages:
            continue
        page_height = page.rect.height
        row_tops = line_top_map(page)
        blocks = page_blocks(page)
        page_bottom = bottom_limit(
            (("".join(s["text"] for s in line["spans"]), line["bbox"]) for b in blocks for line in b.get("lines", [])),
            page_height,
            margin,
        )
        for block in blocks:
            for line in block.get("lines", []):
                if not is_horizontal_line(line):
                    continue
                line_y0 = row_tops.get(line["bbox"][1], line["bbox"][1])
                if line_y0 < top_margin(margin) or line_y0 > page_bottom:
                    continue
                if is_machine_readable_mark("".join(s["text"] for s in line["spans"])):
                    continue  # a per-page barcode; unreadable glyphs, never question text
                line_text = "".join(s["text"] for s in line["spans"])
                if is_page_number(line_text, line["bbox"], page.rect.width, page_height):
                    continue
                for span in line["spans"]:
                    text = span["text"]
                    if DOTTED_LINE_RE.match(text.strip()) or is_page_furniture_text(text):
                        continue
                    if text.strip():
                        spans.append(
                            TextSpan(text=text, page=page_index, y0=line_y0, x0=span["bbox"][0])
                        )
                        rights.append(span["bbox"][2])
    return _without_array_indices(spans, rights)


# an "[n]" away from the mark column: an array's index in pseudocode, not a mark
_INDEX_RE = re.compile(r"\[(\d+)\]")
_MARK_END_RE = re.compile(r"\[\d+\]\s*$")
# how far short of the mark column a span ends to be inline (the index measured 62pt short)
_INLINE_REACH = 30.0


def _without_array_indices(spans: list[TextSpan], rights: list[float]) -> list[TextSpan]:
    """0478/21 MJ26 Q4 read 54 marks: "sort the array Names[50]" counted as a "[50]" mark
    beside the real "[4]". Text can't tell them apart - spans join without a space, so real
    marks follow words all the time ("web server[5]", "START[6]"). Where they sit can: every
    mark ends at the paper's right margin (x 546 there), the index mid-line (484). So does a
    trace table's heading row, "Letter[1] [2] ... [8]" each its own span (0478/23 MJ25 Q6
    read 44 marks for 8). An "[n]" in a span ending short of that column is set in
    full-width brackets - it reads the same, and is no mark."""
    ends = sorted(r for span, r in zip(spans, rights) if _MARK_END_RE.search(span.text))
    if not ends:
        return spans
    column = ends[len(ends) // 2]
    return [
        span.model_copy(update={"text": _INDEX_RE.sub(r"［\1］", span.text)})
        if right < column - _INLINE_REACH and _INDEX_RE.search(span.text)
        else span
        for span, right in zip(spans, rights)
    ]


def _boundary_end(
    anchors: list[ResolvedAnchor], index: int, starts: list[tuple] | None = None
) -> tuple[int, float, float] | None:
    """Where the next anchor with tier <= this one's starts (question is tier 0, so it stops
    at the next question; a part stops at the next part or question; a subpart stops at
    literally the next anchor of any kind) - its lead-in included (`lead_in_starts`)."""
    current_rank = TIER_RANK[anchors[index].tier]
    for k in range(index + 1, len(anchors)):
        if TIER_RANK[anchors[k].tier] <= current_rank:
            return starts[k] if starts is not None else anchors[k].position
    return None


_LEAD_IN_WORDS = 3


def lead_in_starts(anchors: list[ResolvedAnchor], text_spans: list[TextSpan]) -> list[tuple]:
    """Where each anchor's content starts: its own position, or earlier for a part whose
    lead-in sits above its label. "(a) What fraction is shaded? (2)  3/10 of a triangle is
    shaded.  (b) What fraction is not shaded?" - the line after (a)'s mark is (b)'s (Edexcel
    4MA0, user-reported across many papers): a part's content ends at its last mark, and
    what follows before the next label belongs to that next part. Never for a question:
    what sits before a question number is the last one's "(Total for Question n...)"."""
    starts = [a.position for a in anchors]
    for k in range(1, len(anchors)):
        if anchors[k].tier == "question":
            continue
        # by position: the text layer isn't stored top to bottom (a graph's axis numbers
        # come after the mark below them)
        between = sorted(
            (s for s in text_spans if anchors[k - 1].position <= s.position < anchors[k].position),
            key=lambda s: s.position,
        )
        last_mark = max(
            (n for n, s in enumerate(between) if MARKS_RE.search(s.text) or TOTAL_MARKS_RE.search(s.text)),
            default=None,
        )
        if last_mark is None:
            # A part with no mark of its own - one "(3)" for (i) and (ii) - has no mark to
            # end at, but its answer space ends it as well: wording past the space, on the
            # lines just above the next label, leads that label in ("The area of the
            # enclosure PQRS is A m2." before 15(a)(ii), 4MA0 4H January 2014)
            after = _after_answer_space(between, anchors[k])
            if _reads_as_prose(after) and after[0].position > anchors[k - 1].position:
                starts[k] = after[0].position
            continue
        after = _past_options([s for s in between[last_mark + 1 :] if s.text.strip()])
        # only wording leads a part in - "3/10 of a triangle is shaded." - never a figure's
        # labels: a graph's axis numbers, "x", "y" and "O" after its mark belong to it
        if _reads_as_prose(after) and after[0].position > anchors[k - 1].position:
            starts[k] = after[0].position
    return starts


def _reads_as_prose(spans: list[TextSpan]) -> bool:
    """Some printed line holds a sentence's worth of words. Counted per line, not over the
    lot: a grid's axis titles, "Cumulative" / "frequency" down the side and "Weight (w kg)"
    along the foot, are four words between them but never more than two on a line - counted
    together they took 4MA0 3H June 2016 11(b)'s grid, printed after its "(2)", to (c)
    (user-reported). A lead-in is a sentence: "3/10 of a triangle is shaded." """
    lines: dict[tuple[int, float], list[str]] = {}
    for span in spans:
        lines.setdefault((span.page, round(span.y0)), []).append(span.text)
    return any(len(re.findall(r"[A-Za-z]{2,}", " ".join(texts))) >= _LEAD_IN_WORDS for texts in lines.values())


_OPTION_RE = re.compile(r"^\s*([A-D])(?:\s|$)")


def _past_options(after: list[TextSpan]) -> list[TextSpan]:
    """What follows a multiple-choice part's options. Edexcel prints the mark before them -
    "(a) Which process is affected...? (1)", then A-D - so the options went to the next part
    as its lead-in (4BI1 2B November 2020, user-reported)."""
    letters = [(n, m.group(1)) for n, s in enumerate(after) if (m := _OPTION_RE.match(s.text))]
    if [letter for _, letter in letters[:4]] != ["A", "B", "C", "D"] or letters[0][0] != 0:
        return after
    last = after[letters[3][0]]
    return [s for s in after if (s.page, round(s.y0)) > (last.page, round(last.y0))]


# an answer space: this much empty height between two lines of one part (measured: 50-110pt
# on Mathematics A; lines of one sentence or a stem are 12-24pt apart)
_ANSWER_SPACE = 36.0
# the lead-in sits right above its label
_LEAD_IN_REACH = 30.0


def _after_answer_space(between: list[TextSpan], label: ResolvedAnchor) -> list[TextSpan]:
    """The lines after the last answer space before `label`, when they run up to it."""
    lines: dict[tuple[int, float], list[TextSpan]] = {}
    for span in between:
        if span.text.strip():
            lines.setdefault((span.page, round(span.y0)), []).append(span)
    keys = sorted(lines)
    if len(keys) < 2:
        return []
    page, label_y = label.position[0], label.position[1]
    last = keys[-1]
    if last[0] != page or not 0 < label_y - last[1] <= _LEAD_IN_REACH:
        return []
    for i in range(len(keys) - 1, 0, -1):
        upper, lower = keys[i - 1], keys[i]
        if upper[0] == lower[0] and lower[1] - upper[1] >= _ANSWER_SPACE:
            return [s for key in keys[i:] for s in sorted(lines[key], key=lambda s: s.x0)]
    return []


def _has_nested_child(anchors: list[ResolvedAnchor], index: int, end: tuple | None) -> bool:
    current_rank = TIER_RANK[anchors[index].tier]
    for later in anchors[index + 1 :]:
        if end is not None and later.position >= end:
            break
        if TIER_RANK[later.tier] > current_rank:
            return True
    return False


def _text_between(
    text_spans: list[TextSpan], start: tuple[int, float, float], end: tuple | None
) -> str:
    pieces = [
        s.text
        for s in text_spans
        if s.position >= start and (end is None or s.position < end)
    ]
    return "".join(pieces).strip()


def _label_bbox(span: AnchorSpan) -> LabelRef:
    x0, y0, x1, y1 = span.bbox
    return LabelRef(
        page=span.page, x0=x0, y0=y0, x1=x1, y1=y1, size=span.size, bold=span.bold,
        origin_x=span.origin[0], origin_y=span.origin[1], font=span.font,
    )


def _dotted_path(anchors: list[ResolvedAnchor], index: int) -> str:
    """Build e.g. "3.a.i" by walking back to the nearest enclosing question/part."""
    labels = [anchors[index].label]
    current_rank = TIER_RANK[anchors[index].tier]
    for earlier in reversed(anchors[:index]):
        earlier_rank = TIER_RANK[earlier.tier]
        if earlier_rank < current_rank:
            labels.append(earlier.label)
            current_rank = earlier_rank
        if current_rank == TIER_RANK["question"]:
            break
    return ".".join(reversed(labels))


def build_tree(
    tier_results: list[TierResult],
    text_spans: list[TextSpan],
    boundaries_out: dict[str, tuple[tuple, tuple | None]] | None = None,
) -> list[Question]:
    """boundaries_out, if given, is filled in-place mapping each Question ("Q<n>") and
    QuestionPart (its `id`) to the (start, end) position span crops.py should render -
    the same span used to build that node's `text`, including a container's intro being
    folded into its first child's crop the same way it's folded into the text.

    It also gets two extra kinds of entry, both for composing custom papers (quiz_export):
    "<q>.<path>.heading" for a container's own intro region, and "<part id>.body" for a
    container's first child *without* that absorbed intro - so a subpart can be lifted out
    on its own, under its heading, without the intro being shown twice.
    """
    anchors = ordered_anchors(tier_results)
    questions: dict[str, Question] = {}
    order: list[str] = []
    pending_container_text: dict[int, str] = {}  # anchor index -> intro text to prepend
    pending_container_start: dict[int, tuple] = {}  # anchor index -> crop start override
    pending_container_label: dict[int, BBox] = {}  # anchor index -> label bbox override
    explicit_total: set[str] = set()  # question keys with a real "[Total: n]" annotation

    starts = lead_in_starts(anchors, text_spans)
    for i, anchor in enumerate(anchors):
        end = _boundary_end(anchors, i, starts)
        raw_text = _text_between(text_spans, starts[i], end)
        is_container = _has_nested_child(anchors, i, end)

        intro_text = raw_text
        intro_end = end  # narrowed below for a container; stays the full boundary otherwise
        if is_container:
            # keep this container's intro text (everything up to its first child) to
            # prepend onto whichever leaf comes first, since the flat schema has no slot
            # for an intermediate node's own text.
            first_child_index = next(
                j
                for j in range(i + 1, len(anchors))
                if TIER_RANK[anchors[j].tier] > TIER_RANK[anchor.tier]
            )
            intro_end = starts[first_child_index]
            intro_text = _text_between(text_spans, starts[i], intro_end)
            pending_container_text[first_child_index] = (
                pending_container_text.get(first_child_index, "") + intro_text + " "
            ).strip()
            pending_container_start.setdefault(first_child_index, starts[i])
            pending_container_label.setdefault(first_child_index, _label_bbox(anchor.span))

        if anchor.tier == "question":
            key = anchor.label
            # [Total: n] usually sits after every part, so search the full boundary span
            # (raw_text) rather than just the intro before the first part begins.
            question = Question(
                paper_key="",
                board="",
                subject="",
                question_number=int(anchor.label),
                stem_text=raw_text if not is_container else intro_text,
                stem_image_path="",
                total_marks=0,
                number_bbox=_label_bbox(anchor.span),
            )
            if boundaries_out is not None:
                boundaries_out[f"Q{question.question_number}"] = (starts[i], intro_end)
            total_match = TOTAL_MARKS_RE.search(raw_text)
            if total_match:
                question.total_marks = int(total_match.group(1))
                explicit_total.add(key)
            if not is_container:
                # No lettered parts at all (e.g. a single table/matching item worth its own
                # mark allocation) - the question's own "[n]" is its total, unless it
                # prints a "[Total: n]" too.
                if not total_match:
                    marks_value = _marks_value(raw_text)
                    if marks_value:
                        question.total_marks = marks_value
                        explicit_total.add(key)
                # Real-paper finding (0620/41 and 43 MJ21, Q2): an unlettered question that
                # *does* print "[Total: n]" used to skip this and get no part at all, so its
                # mark scheme row had nothing to join to.
                #
                # Give it a synthetic leaf part (empty path) so its mark scheme entry -
                # which also uses an empty part_path for an unlettered question - has
                # something to join to (join.py / validate.py's orphan check otherwise
                # flag it, since a bare Question carries no QuestionPart of its own).
                synthetic_id = f"{question.question_number}."
                question.parts.append(
                    QuestionPart(
                        id=synthetic_id,
                        question_id=str(question.question_number),
                        path="",
                        depth=1,
                        text=raw_text,
                        image_path="",
                        marks=question.total_marks,
                    )
                )
                if boundaries_out is not None:
                    boundaries_out[synthetic_id] = (starts[i], end)
            questions[key] = question
            order.append(key)
            continue

        if is_container:
            # No QuestionPart for a container - the flat schema only holds leaves, and its
            # intro text moves to the first leaf. It still gets a ContainerHeading, so a
            # composed paper can show "(c) All smartphones have a MAC address." above a
            # subpart that was lifted out without its siblings.
            container_dotted = _dotted_path(anchors, i)
            container_q_key, _, container_path = container_dotted.partition(".")
            container_question = questions.get(container_q_key)
            if container_question is not None and container_path:
                container_question.container_headings.append(
                    ContainerHeading(
                        path=container_path,
                        text=intro_text,
                        label_bbox=_label_bbox(anchor.span),
                    )
                )
                if boundaries_out is not None:
                    key = f"{container_question.question_number}.{container_path}.heading"
                    boundaries_out[key] = (starts[i], intro_end)
            continue

        text = raw_text
        if i in pending_container_text:
            text = f"{pending_container_text[i]} {text}".strip()

        # A trailing "[Total: n]" belongs to the enclosing question, not this leaf - if the
        # last leaf before the next question absorbed it, cut it back off before looking
        # for this leaf's own trailing "[n]".
        total_match = TOTAL_MARKS_RE.search(text)
        if total_match:
            text = text[: total_match.start()].strip()

        # Only the leaf's own marks, not those of the container intro folded in above: a
        # group's total can be printed once on its intro, "Choose from the letters A-E...
        # (4)" over four one-mark subparts with none of their own (Edexcel 4CH0 2C June
        # 2012 Q2(b), 1C Q11(a)) - counted for the first subpart, the group came to 7.
        own = raw_text
        own_total = TOTAL_MARKS_RE.search(own)
        if own_total:
            own = own[: own_total.start()]
        marks = _marks_value(own)

        dotted_path = _dotted_path(anchors, i)
        question_key, _, path = dotted_path.partition(".")
        question = questions.get(question_key)
        if question is None:
            continue  # a leaf anchor with no enclosing question - malformed input, skip it
        path = path or anchor.label
        part = QuestionPart(
            id=f"{question.question_number}.{path}",
            question_id=str(question.question_number),
            path=path,
            depth=path.count(".") + 1,
            text=text,
            image_path="",
            marks=marks,
            marks_printed=marks,
            label_bbox=pending_container_label.get(i, _label_bbox(anchor.span)),
        )
        if i in pending_container_start:
            # this leaf absorbed its container's intro above; keep a pointer to the leaf on
            # its own as well, so a composed paper can pair it with a ContainerHeading
            # instead of repeating the intro.
            part.body_label_bbox = _label_bbox(anchor.span)
        question.parts.append(part)
        if question_key not in explicit_total:
            question.total_marks = sum(p.marks for p in question.parts)
        if boundaries_out is not None:
            crop_start = pending_container_start.get(i, starts[i])
            boundaries_out[part.id] = (crop_start, end)
            if i in pending_container_start:
                boundaries_out[f"{part.id}.body"] = (starts[i], end)

    for key in order:
        _share_group_marks(questions[key], recount=key not in explicit_total)
    return [questions[key] for key in order]


def _share_group_marks(question: Question, recount: bool) -> None:
    """A group whose mark is printed once, on its intro, over subparts with none of their own
    - "(6)" over six one-word answers (Edexcel 4CH0 1C June 2016 Q4(a)) - shares it out
    when it divides evenly. Otherwise they're left at 0, for the mark scheme to fill in."""
    for heading in question.container_headings:
        intro = TOTAL_MARKS_RE.split(heading.text)[0]
        group = _marks_value(intro)
        leaves = [p for p in question.parts if p.path.startswith(heading.path + ".")]
        if not group or not leaves or any(p.marks for p in leaves) or group % len(leaves):
            continue
        for part in leaves:
            part.marks = group // len(leaves)
    if recount:
        question.total_marks = sum(p.marks for p in question.parts)
