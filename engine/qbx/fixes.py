"""Corrections to an extracted paper, kept as a list of small operations.

Every change to a paper after extraction - a teacher's edit in the Reviewer or a fix Claude
proposed (qbx/ai_fix.py) - is one `Fix` in `out/<paper>/fixes.json`. The extraction as the
parser left it is kept in `paper.base.json`; the paper the app uses is always that base,
plus the hand-drawn mark scheme links (qbx/ms_links.py), plus every applied fix in order
(`rebuild`). So a fix survives "Re-process papers with issues", and any one can be undone.

A fix is only ever one of the operations below - none of them invents question or mark
scheme content, they move, join and number what the parser found. A fix proposed by Claude
is tried on a copy first and the six checks run again (`verify`); in Auto mode it is applied
only when it clears the issues it was meant for and raises none (CLAUDE.md: the LLM is a
validator, never the extractor - here the validators decide what Claude may change).
"""

import functools
import json
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from qbx.schema import BBox, Paper, QuestionPart, QuestionPartMarkScheme
from qbx.validate import validate_paper

FIXES_FILE = "fixes.json"
BASE_FILE = "paper.base.json"

Op = Literal[
    "set_marks",      # part_id, value
    "set_total",      # question_number, value
    "link",           # part_id, entry_id
    "unlink",         # part_id, entry_id
    "relabel_entry",  # entry_id, path - and joined to the part with that path
    "merge_parts",    # part_ids (consecutive, one question), optional path for the result
    "edit_part",      # part_id, fields {text, marks, path, segments}
    "split_part",     # part_id, at {page, y}; the lower half is "<part_id>-split" (optional
                      # path / value / fields.text for it)
    "add_part",       # question_number, path, value (marks), fields {text, segments}; goes
                      # before part_id when given, else last
    "delete_part",    # part_id - something read as a part that isn't one
    "set_entry_marks",  # entry_id, value - a mark scheme row's marks misread
    "delete_entry",   # entry_id - something read as a mark scheme row that isn't one
    "set_cover_total",  # value - the cover's total misread
    "renumber_question",  # question_number -> value
    "delete_question",  # question_number - something read as a question that isn't one
    "link_region",    # part_id, at {page, y0, y1}: the part's answer is that stretch of the
                      # mark scheme - a row the parser lost or filed elsewhere. Built like a
                      # teacher's drawn link (qbx/ms_links.py), never written by Claude
    "dismiss",        # the paper itself disagrees (Pearson's/Cambridge's own error): marks
                      # the issues checked, changes nothing
    "needs_person",   # Claude can't fix it from the text: a note on the issues, no change
]
# operations that change nothing in the paper
NOTES = {"dismiss", "needs_person"}


class Step(BaseModel):
    op: Op
    part_id: str | None = None
    part_ids: list[str] = []
    entry_id: str | None = None
    question_number: int | None = None
    value: int | None = None
    path: str | None = None
    fields: dict = {}
    at: dict = {}


class Fix(BaseModel):
    id: str = ""
    steps: list[Step]
    issues: list[str] = []  # the issue keys it's meant to clear
    reason: str = ""
    source: Literal["ai", "manual"] = "manual"
    # the model that suggested it ("" before this was kept): a fresh "Fix all" re-asks
    # about a suggestion an earlier, different model made
    model: str = ""
    # the version of the fixer itself (ai_fix.FIXER_VERSION) - what Claude was shown and could
    # do: a suggestion an older one made is asked again by "Fix all"
    fixer: int = 0
    # applied: part of the paper; suggested: waiting for the teacher; rejected / undone:
    # kept for the record (and so Claude isn't asked the same thing again)
    status: Literal["applied", "suggested", "rejected", "undone"] = "applied"
    # what trying it found: issues it clears, issues it would raise
    clears: list[str] = []
    raises: list[str] = []
    created_at: float = 0.0

    def model_post_init(self, _) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:10]
        if not self.created_at:
            self.created_at = time.time()


class FixError(ValueError):
    """A fix that doesn't fit the paper (a part or row it names isn't there)."""


# --- storage ---------------------------------------------------------------------------


def read_fixes(paper_dir: Path) -> list[Fix]:
    try:
        raw = json.loads((paper_dir / FIXES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    fixes = []
    for item in raw.get("fixes", []):
        try:
            fixes.append(Fix.model_validate(item))
        except ValueError:
            continue
    return fixes


def write_fixes(paper_dir: Path, fixes: list[Fix]) -> None:
    (paper_dir / FIXES_FILE).write_text(
        json.dumps({"fixes": [f.model_dump() for f in fixes]}, indent=2), encoding="utf-8"
    )


def read_base(paper_dir: Path) -> Paper | None:
    try:
        return Paper.model_validate_json((paper_dir / BASE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_base(paper_dir: Path, paper: Paper) -> None:
    (paper_dir / BASE_FILE).write_text(paper.model_dump_json(indent=2), encoding="utf-8")


# --- applying --------------------------------------------------------------------------


def _part(paper: Paper, part_id: str | None):
    for question in paper.questions:
        for index, part in enumerate(question.parts):
            if part.id == part_id:
                return question, index, part
    raise FixError(f"no part {part_id!r}")


def _entry(paper: Paper, entry_id: str | None):
    for entry in paper.mark_scheme:
        if entry.id == entry_id:
            return entry
    raise FixError(f"no mark scheme row {entry_id!r}")


def _question(paper: Paper, number: int | None):
    for question in paper.questions:
        if question.question_number == number:
            return question
    raise FixError(f"no question {number}")


def _recount(question) -> None:
    question.total_marks = sum(p.marks for p in question.parts)


def apply_step(paper: Paper, step: Step) -> None:
    """Applies one step to `paper` in place. Raises FixError when it doesn't fit."""
    if step.op in NOTES:
        return
    if step.op == "set_marks":
        if step.value is None or step.value < 0:
            raise FixError("set_marks needs a mark")
        _, _, part = _part(paper, step.part_id)
        part.marks = step.value
    elif step.op == "set_total":
        question = next((q for q in paper.questions if q.question_number == step.question_number), None)
        if question is None or step.value is None or step.value < 0:
            raise FixError(f"no question {step.question_number}")
        question.total_marks = step.value
    elif step.op == "link":
        _part(paper, step.part_id)
        _entry(paper, step.entry_id)
        if not any(j.part_id == step.part_id and j.mark_scheme_entry_id == step.entry_id for j in paper.joins):
            paper.joins.append(QuestionPartMarkScheme(part_id=step.part_id, mark_scheme_entry_id=step.entry_id))
    elif step.op == "unlink":
        before = len(paper.joins)
        paper.joins = [
            j for j in paper.joins if not (j.part_id == step.part_id and j.mark_scheme_entry_id == step.entry_id)
        ]
        if len(paper.joins) == before:
            raise FixError(f"{step.part_id} isn't linked to {step.entry_id}")
    elif step.op == "relabel_entry":
        entry = _entry(paper, step.entry_id)
        if not step.path:
            raise FixError("relabel_entry needs a path")
        entry.part_path = step.path
        target = next(
            (p for q in paper.questions if q.question_number == entry.question_number
             for p in q.parts if p.path == step.path),
            None,
        )
        paper.joins = [j for j in paper.joins if j.mark_scheme_entry_id != entry.id]
        if target is not None:
            paper.joins.append(QuestionPartMarkScheme(part_id=target.id, mark_scheme_entry_id=entry.id))
    elif step.op == "merge_parts":
        _merge(paper, step)
    elif step.op == "edit_part":
        question, _, part = _part(paper, step.part_id)
        for field in ("text", "marks", "path"):
            if field in step.fields:
                setattr(part, field, step.fields[field])
        if "segments" in step.fields:
            part.segments = [BBox(**s) if isinstance(s, dict) else s for s in step.fields["segments"]]
        if "marks" in step.fields:
            _recount(question)
    elif step.op == "split_part":
        _split(paper, step)
    elif step.op == "add_part":
        _add(paper, step)
    elif step.op == "delete_part":
        question, index, part = _part(paper, step.part_id)
        del question.parts[index]
        paper.joins = [j for j in paper.joins if j.part_id != part.id]
    elif step.op == "set_entry_marks":
        if step.value is None or step.value < 0:
            raise FixError("set_entry_marks needs a mark")
        _entry(paper, step.entry_id).marks = step.value
    elif step.op == "delete_entry":
        entry = _entry(paper, step.entry_id)
        paper.mark_scheme = [e for e in paper.mark_scheme if e.id != entry.id]
        paper.joins = [j for j in paper.joins if j.mark_scheme_entry_id != entry.id]
    elif step.op == "set_cover_total":
        if step.value is None or step.value < 0:
            raise FixError("set_cover_total needs a mark")
        paper.cover_total_marks = step.value
    elif step.op == "renumber_question":
        question = _question(paper, step.question_number)
        if step.value is None or step.value < 1 or any(q.question_number == step.value for q in paper.questions):
            raise FixError(f"can't renumber question {step.question_number} as {step.value}")
        question.question_number = step.value
        paper.questions.sort(key=lambda q: q.question_number)
    elif step.op == "delete_question":
        question = _question(paper, step.question_number)
        gone = {p.id for p in question.parts}
        paper.questions = [q for q in paper.questions if q is not question]
        paper.joins = [j for j in paper.joins if j.part_id not in gone]
    elif step.op == "link_region":
        _link_region(paper, step)
    else:  # pragma: no cover - Literal keeps this out
        raise FixError(f"unknown operation {step.op}")


@functools.lru_cache(maxsize=8)
def _scheme_geometry(ms_path: str, mtime_ns: int, board: str) -> tuple[list[float], dict[str, list[BBox]]]:
    """The scheme's columns and each detected row's region, read once per file."""
    from qbx.markscheme_edexcel import scheme_layout

    layout = scheme_layout(ms_path, board)
    return list(layout.columns), {entry_id: row.segments for entry_id, row in layout.rows.items()}


def _link_region(paper: Paper, step: Step) -> None:
    """The part's answer is the stretch of the mark scheme between y0 and y1 on that page:
    an entry is made from it as from a teacher's drawn box - its text read from the page, the
    region at the table's width for composing - and the part linked to it alone. A detected
    row the region covers that joins nothing else is dropped, as for a drawn link."""
    from qbx import ms_links

    _, _, part = _part(paper, step.part_id)
    ms_path = paper.source_ms_path
    if not ms_path or not Path(ms_path).exists():
        raise FixError("the mark scheme PDF isn't on disk")
    try:
        page, y0, y1 = int(step.at["page"]), float(step.at["y0"]), float(step.at["y1"])
    except (KeyError, TypeError, ValueError):
        raise FixError("link_region needs at {page, y0, y1}")
    if y1 - y0 < 4:
        raise FixError("link_region's region is empty")
    columns, rows = _scheme_geometry(ms_path, Path(ms_path).stat().st_mtime_ns, paper.board)
    x0, x1 = (columns[0], columns[-1]) if len(columns) >= 2 else (0.0, 10_000.0)
    box = BBox(page=page, x0=x0, y0=y0, x1=x1, y1=y1)
    entry = ms_links.make_entry(paper, part.id, [box], ms_path, columns)
    entry = entry.model_copy(update={"id": f"region.{part.id}", "manual": False})
    if not entry.answer_text:
        raise FixError("link_region's region holds no text")
    if not part.marks:
        # nothing printed on the paper: the region's own marking codes say what it's worth
        from qbx.markscheme_edexcel import _MARK_COLUMN_CODE_RE

        entry.marks = sum(int(m.group(1)) for m in _MARK_COLUMN_CODE_RE.finditer(entry.answer_text))
    joined_elsewhere = {j.mark_scheme_entry_id for j in paper.joins if j.part_id != part.id}
    covered = {
        entry_id for entry_id, boxes in rows.items()
        if entry_id not in joined_elsewhere and any(ms_links._overlaps(box, b) for b in boxes)
    }
    paper.mark_scheme = [e for e in paper.mark_scheme if e.id not in covered and e.id != entry.id] + [entry]
    paper.joins = [j for j in paper.joins if j.part_id != part.id and j.mark_scheme_entry_id not in covered]
    paper.joins.append(QuestionPartMarkScheme(part_id=part.id, mark_scheme_entry_id=entry.id))


def _merge(paper: Paper, step: Step) -> None:
    if len(step.part_ids) < 2:
        raise FixError("merge_parts needs two or more parts")
    found = [_part(paper, pid) for pid in step.part_ids]
    question = found[0][0]
    indexes = sorted(index for q, index, _ in found if q is question)
    if len(indexes) != len(found) or indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        raise FixError("only parts next to each other in one question can be merged")
    parts = [question.parts[i] for i in indexes]
    first = parts[0]
    path = step.path or first.path
    if any(p.path == path for p in question.parts if p not in parts):
        raise FixError(f"question {question.question_number} already has a part {path}")
    merged = first.model_copy(update={
        "id": f"{question.question_number}.{path}",
        "path": path,
        "depth": path.count(".") + 1,
        "text": " ".join(p.text for p in parts).strip(),
        # what the paper prints for the group when it prints any: (i), (ii) carrying the
        # group's mark each, from one shared scheme row, would otherwise count it three times
        "marks": printed if (printed := sum(p.marks_printed or 0 for p in parts)) else sum(p.marks for p in parts),
        "marks_printed": printed or first.marks_printed,
        "segments": [s for p in parts for s in p.segments],
        "body_image_path": "",
        "body_segments": [],
        "body_label_bbox": None,
    })
    old_ids = {p.id for p in parts}
    question.parts[indexes[0] : indexes[-1] + 1] = [merged]
    joins, seen = [], set()
    for j in paper.joins:
        part_id = merged.id if j.part_id in old_ids else j.part_id
        if (part_id, j.mark_scheme_entry_id) not in seen:
            seen.add((part_id, j.mark_scheme_entry_id))
            joins.append(QuestionPartMarkScheme(part_id=part_id, mark_scheme_entry_id=j.mark_scheme_entry_id))
    paper.joins = joins
    parent = path
    question.container_headings = [h for h in question.container_headings if h.path != parent]


def _split(paper: Paper, step: Step) -> None:
    question, index, part = _part(paper, step.part_id)
    try:
        split_page, split_y = step.at["page"], step.at["y"]
    except KeyError:
        raise FixError("split_part needs a page and y") from None
    first_segments, second_segments = [], []
    for segment in part.segments:
        if segment.page < split_page or (segment.page == split_page and segment.y1 <= split_y):
            first_segments.append(segment)
        elif segment.page > split_page or (segment.page == split_page and segment.y0 >= split_y):
            second_segments.append(segment)
        else:
            top, bottom = segment.model_copy(), segment.model_copy()
            top.y1 = split_y
            bottom.y0 = split_y
            first_segments.append(top)
            second_segments.append(bottom)
    if not first_segments or not second_segments:
        raise FixError(f"{part.id} has no content on both sides of page {split_page} y {split_y}")
    new_id = f"{part.id}-split"
    if any(p.id == new_id for p in question.parts):
        raise FixError(f"{part.id} has already been split")
    label = part.label_bbox
    label_below = label is not None and (label.page, label.y0) >= (split_page, split_y)
    part.segments = first_segments
    new_part = part.model_copy(update={
        "id": new_id,
        "path": step.path or f"{part.path}-split",
        "depth": (step.path or part.path).count(".") + 1,
        "segments": second_segments,
        "marks": step.value or 0,
        "marks_printed": step.value or 0,
        "text": step.fields.get("text", part.text),
        "label_bbox": label if label_below else None,
        "body_image_path": "",
        "body_segments": [],
        "body_label_bbox": None,
    })
    if label_below:
        part.label_bbox = None
    question.parts.insert(index + 1, new_part)


def _add(paper: Paper, step: Step) -> None:
    question = _question(paper, step.question_number)
    if not step.path:
        raise FixError("add_part needs a path")
    new_id = f"{question.question_number}.{step.path}"
    if any(p.path == step.path or p.id == new_id for p in question.parts):
        raise FixError(f"question {question.question_number} already has a part {step.path}")
    segments = [BBox(**s) if isinstance(s, dict) else s for s in step.fields.get("segments", [])]
    part = QuestionPart(
        id=new_id, question_id=str(question.question_number), path=step.path,
        depth=step.path.count(".") + 1, text=step.fields.get("text", ""), image_path="",
        marks=step.value or 0, marks_printed=step.value or 0, segments=segments,
    )
    index = len(question.parts)
    if step.part_id:
        found, index, _ = _part(paper, step.part_id)
        if found is not question:
            raise FixError(f"{step.part_id} isn't in question {question.question_number}")
    question.parts.insert(index, part)


def apply_fix(paper: Paper, fix: Fix) -> Paper:
    """A copy of `paper` with every step of `fix` applied (all or nothing)."""
    trial = paper.model_copy(deep=True)
    for step in fix.steps:
        apply_step(trial, step)
    return trial


def apply_all(paper: Paper, fixes: list[Fix]) -> Paper:
    """`paper` with every applied fix, in order. One that no longer fits (a re-extraction
    renamed the part it names) is skipped, not fatal - the rest still apply.

    Claude's fixes are re-checked against the paper they now land on: each patched a
    parser mistake, and once the parser stops making it the fix undoes a right answer.
    4MA0 3H January 2017: with 12(b)(i)'s scheme row read as 0, Claude set the part to 0;
    once the row read 1, re-applying that took Q12 to 3 of its 4. So an AI fix applies only
    while an issue it was made for is still there. It stays in fixes.json as it was -
    should the parser regress, it applies again. A teacher's own edit always applies.

    Not "and raises none": Claude's fixes chain (a split, then the new part's marks in the
    next fix), and the split alone raises what the next one clears. And as a backstop, the
    paper with every fix as before wins if it comes out with fewer issues."""
    every = _apply(paper, fixes, recheck=False)
    rechecked = _apply(paper, fixes, recheck=True)
    if len(validate_paper(every).issues) < len(validate_paper(rechecked).issues):
        return every
    return rechecked


def _apply(paper: Paper, fixes: list[Fix], recheck: bool) -> Paper:
    for fix in fixes:
        if fix.status != "applied":
            continue
        if recheck and fix.source == "ai" and fix.issues:
            if not set(open_issues(paper, set())) & set(fix.issues):
                continue
        try:
            paper = apply_fix(paper, fix)
        except FixError:
            continue
    return paper


# --- checking --------------------------------------------------------------------------


def open_issues(paper: Paper, checked: set[str]) -> dict[str, dict]:
    """The paper's unchecked issues, by key."""
    from qbx.library import issue_key  # library imports the exporter; keep this module light

    found = {}
    for issue in validate_paper(paper).issues:
        data = issue.model_dump()
        key = issue_key(data)
        if key not in checked:
            found[key] = data
    return found


def verify(paper: Paper, fix: Fix, checked: set[str]) -> tuple[Paper | None, list[str], list[str]]:
    """Tries `fix` on a copy: (the fixed paper or None if it doesn't fit, the issue keys it
    clears, the issue keys it raises). A dismiss clears its own issues by marking them."""
    before = open_issues(paper, checked)
    if all(step.op in NOTES for step in fix.steps):
        if any(step.op == "dismiss" for step in fix.steps):
            return paper, sorted(k for k in fix.issues if k in before), []
        return paper, [], []
    try:
        after_paper = apply_fix(paper, fix)
    except FixError:
        return None, [], []
    after = open_issues(after_paper, checked)
    return after_paper, sorted(set(before) - set(after)), sorted(set(after) - set(before))


def safe_to_apply(fix: Fix, clears: list[str], raises: list[str]) -> bool:
    """Auto mode's test: a change to the paper that clears every issue it was meant for (or,
    when it names none, at least one) and raises none. A dismiss never applies itself - it
    hides a problem rather than fixing it, so a person decides."""
    if any(step.op in NOTES for step in fix.steps) or raises or not clears:
        return False
    return all(key in clears for key in fix.issues)


def rebuild(paper_dir: Path, base: Paper, finish_links) -> Paper:
    """The paper as the app shows it: `base`, with the mark scheme links re-applied
    (`finish_links(paper) -> paper`), then every applied fix."""
    return apply_all(finish_links(base), read_fixes(paper_dir))


def checked_keys(paper_dir: Path) -> set[str]:
    from qbx.library import read_reviewed

    return read_reviewed(paper_dir)
