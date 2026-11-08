"""The full extraction pipeline: ingest -> pages -> anchors -> tree -> crops -> fingerprint
-> markscheme -> join -> validate -> paper.json/report.json. Shared by cli.py (`qbx extract`)
and main.py's upload endpoint (phase 7 reviewer) so there's exactly one place that wires the
modules together.
"""

import re
from pathlib import Path

import pymupdf

from qbx import edexcel, fixes, ingest, join, markscheme, markscheme_edexcel, ms_links, tree
from qbx.markscheme_edexcel import scheme_layout
from qbx.anchors import HEADER_FOOTER_MARGIN, find_anchor_spans, resolve_tiers
from qbx.crops import crop_to_svg, page_segments
from qbx.pairing import check_pair
from qbx.fingerprint import parse_footer, read_cover_titles
from qbx.pages import PageType, classify_document
from qbx.schema import BBox, MarkSchemeEntry, Paper, Question, QuestionPart, QuestionPartMarkScheme
from qbx.validate import Report, is_removed_notice, removed_target, validate_paper

_COVER_TOTAL_RE = re.compile(r"total mark for this paper is (\d+)", re.IGNORECASE)
_COVER_PAGES_RE = re.compile(r"document has (\d+) pages", re.IGNORECASE)
# Real-paper finding (0620 MCQ papers, 2016-2019): these covers never print a total - they
# say "There are forty questions on this paper" and "Each correct answer will score one
# mark" - so the cover-total check was silently skipped on every one. The two together are
# the total.
_MCQ_COUNT_RE = re.compile(r"there are (\w+(?:[- ]\w+)?) questions on this paper", re.IGNORECASE)
_MCQ_ONE_MARK_RE = re.compile(r"each correct answer will score one mark", re.IGNORECASE)
_NUMBER_WORDS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen "
        "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
} | {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}


def _number_from_words(text: str) -> int | None:
    """"forty", "thirty-five", "40" -> the number; None if it isn't one."""
    if text.isdigit():
        return int(text)
    words = re.split(r"[- ]", text.lower())
    if not all(w in _NUMBER_WORDS for w in words):
        return None
    return sum(_NUMBER_WORDS[w] for w in words)


def read_cover_total(cover_text: str) -> int | None:
    match = _COVER_TOTAL_RE.search(cover_text)
    if match:
        return int(match.group(1))
    count = _MCQ_COUNT_RE.search(cover_text)
    if count and _MCQ_ONE_MARK_RE.search(cover_text):
        return _number_from_words(count.group(1))
    return None


class ScannedPdfError(ValueError):
    """Raised when the QP has no extractable text layer."""


def _asset_name(question_number: int, label: str) -> str:
    """e.g. (1, "_stem") -> "q1_stem.svg", (1, "c.i") -> "q1c_i.svg"."""
    return f"q{question_number}{label.replace('.', '_')}.svg"


def _mark_removed(questions, mark_scheme, joins) -> None:
    """Questions and parts the board withdrew, by its notice: on the paper in place of the
    question ("5 Due to an issue with Question 5, the question has been removed from the
    question paper", 0478/22 MJ23, 0625/21 MJ24), or only in the scheme's row for a question
    printed in full (0478/22 MJ25 Q10, 0610/43 MJ26 5(a)). Neither can be marked, so neither
    is checked or set in an exam."""
    # each notice names what it withdraws ("Due to an issue with Question 5"), or says "this
    # question" of its own; a named one can sit in another row - 0478/22 MJ23's Q5 notice ran
    # on into 4(b)(ii)'s row
    targets = set()
    for question in questions:
        for part in question.parts:
            if is_removed_notice(part.text):
                targets.add(removed_target(part.text) or (question.question_number, part.path))
    for e in mark_scheme:
        if is_removed_notice(e.answer_text):
            targets.add(removed_target(e.answer_text) or (e.question_number, e.part_path))
    for question in questions:
        for part in question.parts:
            part.removed = any(
                number == question.question_number
                and (not path or part.path == path or part.path.startswith(path + "."))
                for number, path in targets
            )
        question.removed = bool(question.parts) and all(part.removed for part in question.parts)


def _without_stray_question_rows(questions, mark_scheme, joins) -> list:
    """Drops a row labelled only with its question's number that joins nothing, where the
    question is otherwise accounted for: every part joined and the parts' marks making its
    printed total. On International A Level that row is a page's spill - the previous
    question's notes or working read under this question's number ("Condone work in n
    instead of k", a fraction's "20" as its marks); 15 schemes were flagged for one
    (WMA11 June 2023 Q6, WMA13 June 2024 Q5, WFM01 January 2025 Q8)."""
    joined = {j.mark_scheme_entry_id for j in joins}
    parts_joined = {j.part_id for j in joins}
    settled = {
        q.question_number
        for q in questions
        if q.parts
        and all(part.id in parts_joined for part in q.parts)
        and all(part.marks is not None for part in q.parts)
        and sum(part.marks for part in q.parts) == q.total_marks
    }
    return [
        e for e in mark_scheme
        if e.part_path or e.id in joined or e.question_number not in settled
    ]


def _backfill_marks_from_mark_scheme(
    questions: list[Question],
    mark_scheme: list[MarkSchemeEntry],
    joins: list[QuestionPartMarkScheme],
) -> None:
    """Real-paper finding (0620 multiple-choice QP): some formats never print a `[n]`
    bracket in the question paper at all (each MCQ item is implicitly worth a fixed mark,
    stated only in the mark scheme). Rather than leave every such part at marks=0 and flag
    all of them, backfill from a confirmed 1:1 mark scheme join - the only case unambiguous
    enough to trust without a human. A part already joined to more than one entry (or vice
    versa) is left alone rather than guessed at.
    """
    entries_by_id = {entry.id: entry for entry in mark_scheme}
    entry_ids_by_part: dict[str, list[str]] = {}
    for j in joins:
        entry_ids_by_part.setdefault(j.part_id, []).append(j.mark_scheme_entry_id)

    for question in questions:
        # Only recompute total_marks if it already reflects a naive part-sum (or is 0) -
        # if it differs, tree.py found an explicit "[Total: n]" that must not be clobbered.
        # Only a question with no marks at all (a multiple-choice item): a printed total
        # can equal the marks its parts carry by coincidence - 4MA0 1F January 2012 Q2's
        # "6" over a "(4)" and a "(2)" - and was then recounted as 10.
        total_was_derived = question.total_marks == 0
        changed = False
        filled: set[str] = set()
        scheme_marks = {
            part.id: marks
            for part in question.parts
            if (marks := _scheme_marks(question, part, entry_ids_by_part.get(part.id, []), entries_by_id)) is not None
        }
        for part in question.parts:
            if part.marks > 0:
                continue
            if scheme_marks.get(part.id, 0) > 0:
                part.marks = scheme_marks[part.id]
                filled.add(part.id)
                changed = True
        if changed and total_was_derived:
            question.total_marks = sum(p.marks for p in question.parts)
        elif changed:
            _ungroup_marks(question, filled, scheme_marks)


_COVER_WORDS = ("candidate", "instructions", "information", "total marks", "paper reference")
_PRINTED_MARK_RE = re.compile(r"^\s*(?:\(\d{1,2}\)|\[\d{1,2}\])\s*$", re.MULTILINE)


def _question_document(doc, content_pages: set[int]) -> set[int]:
    """One PDF can hold two documents: Decision papers (WDM11 January 2023) bind the answer
    book - its own cover, each question's number over the networks and grids to draw on -
    in front of the question paper, which has a cover of its own. The answer book's numbers
    read as the questions, with none of their text or marks. Split at each later cover (a
    page naming three or more of candidate, instructions, information, total marks, paper
    reference) and keep the question pages of the document that prints the marks."""
    covers = [
        i for i in range(1, doc.page_count)
        if sum(word in doc[i].get_text("text").lower() for word in _COVER_WORDS) >= 3
    ]
    if not covers:
        return content_pages
    bounds = [0, *covers, doc.page_count]
    documents = [set(range(a, b)) & content_pages for a, b in zip(bounds, bounds[1:])]
    marks = [sum(len(_PRINTED_MARK_RE.findall(doc[i].get_text("text"))) for i in pages) for pages in documents]
    best = max(range(len(documents)), key=lambda k: marks[k])
    return documents[best] if marks[best] > 0 else content_pages


def _scheme_marks(question: Question, part, entry_ids: list[str], entries_by_id: dict) -> int | None:
    """What the scheme says a part is worth: its one row - or its own row and its
    container's, when the scheme marks the container's intro too (WST02 June 2022 5(a):
    "X~Po(7.5) B1" on (a), then (i) B1, (ii) M1 A1, under a "(4)" printed once for (i) and
    (ii)). The container's mark goes to its first part, whose region holds that intro."""
    entries = [entries_by_id[i] for i in entry_ids if i in entries_by_id]
    if len(entries) == 1:
        return entries[0].marks
    own = [e for e in entries if e.part_path == part.path]
    containers = [e for e in entries if e.part_path and part.path.startswith(e.part_path + ".")]
    if len(own) != 1 or len(own) + len(containers) != len(entries):
        return None
    marks = own[0].marks
    for container in containers:
        first = next((p for p in question.parts if p.path.startswith(container.part_path + ".")), None)
        if first is part:
            marks += container.marks
    return marks


def _ungroup_marks(question: Question, filled: set[str], ms_marks: dict[str, int]) -> None:
    """Older papers (Mathematics A 4MA0) print one mark for a group of subparts, on its last:
    "(i) ... (ii) ... (iv) ... (4)" - the paper says what the group is worth, the mark
    scheme what each part is. Filling the earlier ones from the scheme then counted the
    group twice: 4MA0 1F January 2012 came to 116 of its 100. So a group's last subpart takes
    its own mark from the scheme, where the scheme's marks for the group add up to the one
    printed - and otherwise, where the parts overrun the question's printed total, gives up
    the marks filled in for the siblings before it."""
    groups: dict[str, list] = {}
    for part in question.parts:
        parent = part.path.rsplit(".", 1)[0] if "." in part.path else ""
        groups.setdefault(parent, []).append(part)
    for siblings in groups.values():
        last, earlier = siblings[-1], siblings[:-1]
        if last.id in filled or not earlier or not all(p.id in filled for p in earlier):
            continue
        given = sum(p.marks for p in earlier)
        own = ms_marks.get(last.id)
        if own is not None and own + given == last.marks:
            last.marks = own
        elif sum(p.marks for p in question.parts) > question.total_marks and last.marks > given:
            last.marks -= given


_NUMERALS = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii")


def _join_numbered_question(question: Question, mark_scheme: list[MarkSchemeEntry], boundaries: dict) -> None:
    """The same at the top: a question whose only parts are numerals, none printing a mark,
    answered by the scheme in one unlabelled row - "On your sketch show clearly (i) the
    intersections (ii) the turning point (Total for Question 20 is 4 marks)", 4MA1 1H
    January 2020. The items of one task: one part worth the question. Kept apart, each took
    the row's 4 and the question came to 8."""
    parts = question.parts
    if len(parts) < 2 or any(p.path not in _NUMERALS for p in parts) or any(p.marks for p in parts):
        return
    own = [e for e in mark_scheme if e.question_number == question.question_number]
    if not own or any(e.part_path for e in own):
        return
    first, last = parts[0], parts[-1]
    merged = first.model_copy(
        update={
            "id": f"{question.question_number}.",
            "path": "",
            "depth": 1,
            "text": " ".join(p.text for p in parts),
            "marks": question.total_marks,
            "marks_printed": None,
            "body_image_path": "",
            "body_segments": [],
            "body_label_bbox": None,
        }
    )
    start = boundaries.get(first.id, (None, None))[0]
    end = boundaries.get(last.id, (None, None))[1]
    for p in parts:
        boundaries.pop(p.id, None)
        boundaries.pop(f"{p.id}.body", None)
    if start is not None and end is not None:
        boundaries[merged.id] = (start, end)
    question.parts = [merged]


def _join_unsplit_groups(
    questions: list[Question], mark_scheme: list[MarkSchemeEntry], boundaries: dict[str, tuple]
) -> None:
    """A part whose numerals are items of one task, not subparts: "(a) On the grid, draw the
    lines (i) y = 1 (ii) x = 6 (iii) y = x + 1 (3)" (4MA1 2H June 2024 Q2) - one mark, printed
    on the last, and one mark scheme row for "2 (a)". Kept apart, the first ones had no mark
    and no row. Where neither the paper nor the scheme splits the group, it's one part.

    Nor where the scheme splits only its answers: International A Level prints one "[6]" after
    9(b)(ii) for (i) and (ii), and the scheme answers each in a row of its own, but marks
    only the last ("(6)" closing (b)) - its codes list alternative ways, so they can't share
    it out (WMA11 June 2025 Q9, WMA14 June 2021 Q1: (a)(i) flagged with no mark)."""
    rows = {(e.question_number, e.part_path) for e in mark_scheme}
    row_marks: dict[tuple[int, str], int] = {}
    for e in mark_scheme:
        key = (e.question_number, e.part_path)
        row_marks[key] = row_marks.get(key, 0) + e.marks
    for question in questions:
        _join_numbered_question(question, mark_scheme, boundaries)
        groups: dict[str, list[QuestionPart]] = {}
        for part in question.parts:
            if "." in part.path:
                groups.setdefault(part.path.rsplit(".", 1)[0], []).append(part)
        for parent, siblings in groups.items():
            if len(siblings) < 2:
                continue
            split = [path for q, path in rows if q == question.question_number and path.startswith(parent + ".")]
            if not split and (question.question_number, parent) not in rows:
                continue
            if split and any(row_marks.get((question.question_number, p.path), 0) for p in siblings[:-1]):
                continue  # the scheme splits it, marks and all
            if siblings[-1].marks <= 0 or any(p.marks for p in siblings[:-1]):
                continue  # the paper splits it
            first, last = siblings[0], siblings[-1]
            merged = first.model_copy(
                update={
                    "id": f"{question.question_number}.{parent}",
                    "path": parent,
                    "depth": parent.count(".") + 1,
                    "text": " ".join(p.text for p in siblings),
                    "marks": last.marks,
                    "marks_printed": last.marks_printed,
                    "body_image_path": "",
                    "body_segments": [],
                    "body_label_bbox": None,
                }
            )
            start = boundaries.get(first.id, (None, None))[0]
            end = boundaries.get(last.id, (None, None))[1]
            for p in siblings:
                boundaries.pop(p.id, None)
                boundaries.pop(f"{p.id}.body", None)
            if start is not None and end is not None:
                boundaries[merged.id] = (start, end)
            index = question.parts.index(first)
            question.parts = [p for p in question.parts if p not in siblings]
            question.parts.insert(index, merged)
            question.container_headings = [h for h in question.container_headings if h.path != parent]


def apply_hand_links(paper: Paper, paper_dir: Path) -> Paper:
    """`paper` with the mark scheme rows the teacher linked by hand (qbx/ms_links.py)."""
    links = ms_links.read_links(paper_dir)
    if not links or not paper.source_ms_path or not Path(paper.source_ms_path).exists():
        return paper
    layout = scheme_layout(paper.source_ms_path, paper.board)
    return ms_links.apply_links(
        paper, links, paper.source_ms_path, layout.columns,
        {entry_id: row.segments for entry_id, row in layout.rows.items()},
    )


def run_extraction(qp_path: Path, ms_path: Path, out_dir: Path) -> tuple[Paper, Report]:
    """Runs the full pipeline and writes <out_dir>/<paper_key>/{paper.json,report.json,
    assets/*.svg}. Raises ScannedPdfError, qbx.fingerprint.FingerprintError or
    qbx.pairing.PairingError on predictable bad input (a caller like an API endpoint should
    catch these and return a clean 4xx rather than a 500).

    The pairing is checked first: a mismatched pair would otherwise extract "successfully"
    into a report of unjoined parts and orphaned rows that looks like a parser bug.
    """
    qp_identity, ms_identity = check_pair(qp_path, ms_path)
    is_edexcel = edexcel.BOARD in (qp_identity.board, ms_identity.board)
    with ingest.load(qp_path) as loaded:
        if not loaded.is_digital:
            raise ScannedPdfError(f"{qp_path} has no extractable text layer (scanned PDF)")
        doc = loaded.doc

        page_types = classify_document(doc)
        content_pages = {i for i, pt in enumerate(page_types) if pt == PageType.QUESTION}
        content_pages = _question_document(doc, content_pages)

        spans = find_anchor_spans(doc, content_pages=content_pages)
        text_spans = tree.collect_text_spans(doc, content_pages=content_pages)
        if is_edexcel:
            # a page set off from the rest is measured back into line before clustering
            shifts = edexcel.page_shifts(text_spans)
            for span in spans:
                span.x0 -= shifts.get(span.page, 0.0)
            text_spans = edexcel.normalise_marks(text_spans)
        tier_results = resolve_tiers(spans)

        boundaries: dict[str, tuple] = {}
        questions = tree.build_tree(tier_results, text_spans, boundaries_out=boundaries)

        footer_page = min(content_pages) if content_pages else 0
        # the cover too: the newer code layout (03_0620_22_2026_1.16) leaves the session to it
        if is_edexcel:
            # no footer code: the sitting from the folder and file names, the paper from the
            # cover (qbx/edexcel.py)
            fp = edexcel.fingerprint(
                qp_path,
                "\n".join(doc[i].get_text("text") for i in range(min(2, doc.page_count))),
                "\n".join(page.get_text("text") for page in doc),
                ms_identity,
            )
        else:
            fp = parse_footer(doc[footer_page].get_text("text"), cover_text=doc[0].get_text("text"))

        cover_page = next(
            (i for i, pt in enumerate(page_types) if pt == PageType.COVER), None
        )
        cover_total = None
        cover_pages = None
        subject_name, paper_title = read_cover_titles(
            doc[cover_page if cover_page is not None else 0].get_text("text"),
            fp.subject, fp.paper, fp.variant,
        )
        if is_edexcel:
            with pymupdf.open(ms_path) as ms_doc:
                ms_cover = ms_doc[0].get_text("text") if ms_doc.page_count else ""
            subject_name = edexcel.subject_name(ms_cover) or (
                "Chemistry" if "chemistry" in doc[0].get_text("text").lower() else ""
            )
            paper_title = f"Paper {fp.paper}{fp.variant}"
        if cover_page is not None:
            cover_total = read_cover_total(doc[cover_page].get_text("text"))
            pages = _COVER_PAGES_RE.search(doc[cover_page].get_text("text"))
            cover_pages = int(pages.group(1)) if pages else None

        for question in questions:
            question.paper_key = fp.paper_key
            question.board = fp.board
            question.subject = fp.subject

        mark_scheme = (markscheme_edexcel if is_edexcel else markscheme).parse_mark_scheme(ms_path)
        for entry in mark_scheme:
            entry.paper_key = fp.paper_key
        _join_unsplit_groups(questions, mark_scheme, boundaries)
        joins = join.join_parts_to_mark_scheme(questions, mark_scheme)
        _backfill_marks_from_mark_scheme(questions, mark_scheme, joins)
        mark_scheme = _without_stray_question_rows(questions, mark_scheme, joins)
        _mark_removed(questions, mark_scheme, joins)

        paper_dir = out_dir / fp.paper_key
        assets_dir = paper_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        def _segments(start, end) -> list[BBox]:
            return [
                BBox(page=p, x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1)
                for p, r in page_segments(doc, start, end, HEADER_FOOTER_MARGIN)
            ]

        for question in questions:
            start, end = boundaries.get(f"Q{question.question_number}", (None, None))
            if start is not None:
                # A question whose number shares a line with its first part has no stem of
                # its own, so there's nothing to crop - real-paper finding (0620 s24, "5
                # (a) Lead(II) bromide..."). Leave the image path empty rather than
                # inventing a zero-height crop.
                written = crop_to_svg(
                    doc, start, end, assets_dir / _asset_name(question.question_number, "_stem")
                )
                if written:
                    question.stem_image_path = written[0].relative_to(paper_dir).as_posix()
                    question.stem_segments = _segments(start, end)
            for heading in question.container_headings:
                start, end = boundaries.get(
                    f"{question.question_number}.{heading.path}.heading", (None, None)
                )
                if start is None:
                    continue
                written = crop_to_svg(
                    doc,
                    start,
                    end,
                    assets_dir / _asset_name(question.question_number, f"{heading.path}_heading"),
                )
                if written:
                    heading.image_path = written[0].relative_to(paper_dir).as_posix()
                    heading.segments = _segments(start, end)
            for part in question.parts:
                start, end = boundaries.get(part.id, (None, None))
                if start is None:
                    continue
                written = crop_to_svg(
                    doc, start, end, assets_dir / _asset_name(question.question_number, part.path)
                )
                if written:
                    part.image_path = written[0].relative_to(paper_dir).as_posix()
                    part.segments = _segments(start, end)

                body_start, body_end = boundaries.get(f"{part.id}.body", (None, None))
                if body_start is not None:
                    written = crop_to_svg(
                        doc,
                        body_start,
                        body_end,
                        assets_dir / _asset_name(question.question_number, f"{part.path}_body"),
                    )
                    if written:
                        part.body_image_path = written[0].relative_to(paper_dir).as_posix()
                        part.body_segments = _segments(body_start, body_end)

        paper = Paper(
            paper_key=fp.paper_key,
            board=fp.board,
            level=fp.level,
            subject=fp.subject,
            paper=fp.paper,
            variant=fp.variant,
            session=fp.session,
            year=fp.year,
            cover_total_marks=cover_total,
            qp_page_count=doc.page_count,
            cover_page_count=cover_pages,
            source_qp_path=str(qp_path.resolve()),
            source_ms_path=str(Path(ms_path).resolve()),
            subject_name=subject_name,
            paper_title=paper_title,
            questions=questions,
            mark_scheme=mark_scheme,
            joins=joins,
        )
    paper_dir.mkdir(parents=True, exist_ok=True)
    # the parser's own result, kept so any later fix can be undone (qbx/fixes.py); then the
    # rows linked by hand and the fixes made since, which all survive a re-extraction
    fixes.write_base(paper_dir, paper)
    paper = fixes.rebuild(paper_dir, paper, lambda p: apply_hand_links(p, paper_dir))
    report = validate_paper(paper)
    (paper_dir / "paper.json").write_text(paper.model_dump_json(indent=2), encoding="utf-8")
    (paper_dir / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return paper, report
