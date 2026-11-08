"""Claude reviews a paper's open issues and proposes fixes (qbx/fixes.py).

One call per paper, text only, thinking off (as tagging). Claude sees the questions with
issues - each part's id, label, marks and text - the mark scheme rows for them, with their
marking codes, which rows each part is linked to, the rows linked to nothing, and the issues.
It answers with groups of steps from a fixed list, each naming the issues it clears. The
list is every edit the Reviewer can make by hand: marks, totals, links, row labels and
marks, merging, splitting, adding and deleting parts, rows and questions. For the edits that
need a place on the page (a split, a new part's region) it also gets each question's text
lines with their page and y, read from the question paper.

Nothing it says is taken on trust: every group is tried on a copy and the six checks run
again (`fixes.verify`). In Auto mode a group that clears its issues and raises none is
applied; everything else waits in the Reviewer as a suggestion, with what trying it found.
"""

import re
from pathlib import Path
from typing import Literal

import pymupdf
from pydantic import BaseModel

from qbx import fixes
from qbx.anchors import page_blocks
from qbx.fixes import Fix, Step
from qbx.schema import Paper

MAX_TEXT = 260
MAX_ANSWER = 220
MAX_LINES = 60  # per question; a long question's lines past this are left out
ROUNDS = 2  # a second look after a round that applied something (review_paper)
# Bumped whenever what Claude is shown or may do changes, so "Fix all" asks again about
# what an older fixer could only leave a note on. 2: the scheme's own page lines, link_region,
# a second round (its "needs a person" notes mostly said "the row isn't among those shown").
# 3: the scheme pages actually found - 2 looked for them on the rows, which keep no position,
# and showed Claude none.
FIXER_VERSION = 3


class AIBox(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class AIFields(BaseModel):
    text: str | None = None
    marks: int | None = None
    path: str | None = None
    segments: list[AIBox] | None = None


class AIAt(BaseModel):
    page: int
    y: float | None = None  # split_part: the line to split at
    y0: float | None = None  # link_region: the stretch of the mark scheme page
    y1: float | None = None


class AIStep(BaseModel):
    op: Literal[
        "set_marks", "set_total", "link", "unlink", "relabel_entry", "merge_parts",
        "edit_part", "split_part", "add_part", "delete_part",
        "set_entry_marks", "delete_entry", "set_cover_total", "renumber_question", "delete_question",
        "link_region",
    ]
    part_id: str | None = None
    part_ids: list[str] = []
    entry_id: str | None = None
    question_number: int | None = None
    value: int | None = None
    path: str | None = None
    fields: AIFields | None = None
    at: AIAt | None = None

    def to_step(self) -> Step:
        data = self.model_dump(exclude={"fields", "at"})
        data["fields"] = self.fields.model_dump(exclude_none=True) if self.fields else {}
        data["at"] = self.at.model_dump(exclude_none=True) if self.at else {}
        return Step(**data)


class AIGroup(BaseModel):
    issues: list[str]
    steps: list[AIStep]
    reason: str


class AINote(BaseModel):
    issues: list[str]
    kind: Literal["paper_error", "needs_person"]
    reason: str


class AIReview(BaseModel):
    fixes: list[AIGroup] = []
    notes: list[AINote] = []


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _box(b) -> str:
    return f"p{b.page} x{b.x0:.0f}-{b.x1:.0f} y{b.y0:.0f}-{b.y1:.0f}"


def question_lines(paper: Paper, numbers: set[int] | None) -> dict[int, list[str]]:
    """Each question's text lines as "p<page> y<top>: text", read from the question paper,
    for the questions in `numbers` (None: all). Empty when the source PDF isn't there."""
    source = Path(paper.source_qp_path) if paper.source_qp_path else None
    if source is None or not source.exists():
        return {}
    found: dict[int, list[str]] = {}
    try:
        with pymupdf.open(source) as doc:
            for question in paper.questions:
                if numbers is not None and question.question_number not in numbers:
                    continue
                boxes = list(question.stem_segments) + [s for p in question.parts for s in p.segments]
                seen, lines = set(), []
                for box in boxes:
                    if not 0 <= box.page < doc.page_count:
                        continue
                    clip = pymupdf.Rect(box.x0, box.y0, box.x1, box.y1)
                    for block in page_blocks(doc[box.page]):
                        for line in block.get("lines", []):
                            rect = pymupdf.Rect(line["bbox"])
                            if not rect.intersects(clip):
                                continue
                            text = " ".join("".join(s["text"] for s in line["spans"]).split())
                            key = (box.page, round(rect.y0))
                            if text and key not in seen:
                                seen.add(key)
                                lines.append((box.page, rect.y0, rect.x0, f"p{box.page} y{rect.y0:.0f}: {_clip(text, 120)}"))
                lines.sort()
                found[question.question_number] = [t for *_, t in lines[:MAX_LINES]]
    except (RuntimeError, ValueError):
        return {}
    return found


MAX_SCHEME_LINES = 200  # per question


def scheme_lines(paper: Paper, numbers: set[int] | None) -> dict[int, list[str]]:
    """The mark scheme pages each question's rows should be on, as "p<page> y<top>: text"
    lines (upright page points, the label column first on each line). Read from the PDF
    itself, not the parsed rows: when the parser lost or misfiled a question's rows, these
    are what Claude needs to find them (and link them with link_region). The pages are those
    holding the rows of the question and the questions either side - or, if none were found,
    every page between its neighbours' rows."""
    source = Path(paper.source_ms_path) if paper.source_ms_path else None
    if source is None or not source.exists():
        return {}
    from qbx.markscheme import _rotated_spans
    from qbx.markscheme_edexcel import _lines

    # rows keep no position in paper.json: their regions come from the scheme's layout (as for
    # composing) - read from the entries alone, no page was ever found and Claude was shown
    # nothing (user-reported: a "Fix all" of only "needs a person" notes)
    regions = {}
    try:
        _, regions = fixes._scheme_geometry(str(source), source.stat().st_mtime_ns, paper.board)
    except Exception:  # a scheme the layout can't read: the entries' own boxes, if any
        pass
    pages_of: dict[int, set[int]] = {}
    for entry in paper.mark_scheme:
        for box in [*entry.segments, *regions.get(entry.id, [])]:
            pages_of.setdefault(entry.question_number, set()).add(box.page)
    found: dict[int, list[str]] = {}
    try:
        with pymupdf.open(source) as doc:
            spans = _rotated_spans(doc)
    except (RuntimeError, ValueError):
        return {}
    all_lines = _lines(spans)
    first_table = min((page for pages in pages_of.values() for page in pages), default=0)
    for question in paper.questions:
        number = question.question_number
        if numbers is not None and number not in numbers:
            continue
        # its own rows' pages and the next (a row carried over), or with none of its own, from
        # the last page of the question before to the first of the one after
        # Where the scheme prints the question's own label ("5(i)") first: WMA14 January 2025's
        # Q5 is on page 13, and the neighbours' rows (a stray read as Q6 on page 12) pointed
        # short of it. Then its own rows' pages and the next (a row carried over); with none,
        # from the last page of the question before to the first of the one after.
        # (not before the first answer table: the general marking guidance numbers its rules
        # "6. ..." too)
        printed = re.compile(rf"^{number}\s*[(.]")
        labelled = {
            page for line in all_lines
            if line[0].page >= first_table and printed.match(" ".join(s.text for s in line).strip())
            for page in (line[0].page, line[0].page + 1)
        }
        own = pages_of.get(number)
        if labelled:
            pages = labelled | (set(range(min(own), max(own) + 2)) if own else set())
        elif own:
            pages = set(range(min(own), max(own) + 2))
        else:
            before, after = pages_of.get(number - 1), pages_of.get(number + 1)
            if not before and not after:
                continue
            start = max(before) if before else min(after) - 1
            end = min(after) if after else max(before) + 1
            pages = set(range(start, end + 1))
        out = []
        for line in _lines([s for s in spans if s.page in pages]):
            text = " ".join(" ".join(s.text for s in line).split())
            if text:
                out.append(f"p{line[0].page} y{min(s.bbox[1] for s in line):.0f}: {_clip(text, 140)}")
        found[number] = out[:MAX_SCHEME_LINES]
    return found


def build_prompt(
    paper: Paper, issues: dict[str, dict], tried: list[Fix], lines: dict[int, list[str]] | None = None,
    ms_lines: dict[int, list[str]] | None = None,
) -> str:
    numbers = {i["question_number"] for i in issues.values() if i.get("question_number") is not None}
    whole_paper = any(i.get("question_number") is None for i in issues.values())
    joined = {}
    for j in paper.joins:
        joined.setdefault(j.part_id, []).append(j.mark_scheme_entry_id)
    used = {j.mark_scheme_entry_id for j in paper.joins}

    out = [
        f"Paper {paper.paper_key} ({paper.board} {paper.subject_name or paper.subject}, {paper.paper_title}).",
        f"Cover total: {paper.cover_total_marks}. Question totals: "
        + ", ".join(f"Q{q.question_number}={q.total_marks}" for q in paper.questions) + ".",
        "",
        "A program extracted this paper's questions and mark scheme. Its checks found the issues "
        "below. Propose fixes, using only the facts shown. Rules:",
        "- Marks on the question paper are printed per part; older papers print one mark for a "
        "group of parts, and the mark scheme then splits it with marking codes: B1, M1, A1, P1 "
        "(and B2, M2...) are each worth their digit. A part's marks are its rows' codes added up.",
        "- A part with no mark scheme row: link it to the row that answers it (an unlinked row, "
        "or one whose label is a near miss). A row under the wrong label: relabel_entry it with "
        "the part's path.",
        "- Numbered items (i), (ii) that the paper marks together and the scheme answers in one "
        "row are one part: merge_parts them, path = their parent (e.g. 'a').",
        "- A question total is printed on the paper; only set_total if the parts and the cover "
        "clearly show it was misread.",
        "- A part the program missed, run into the part before or after it: split_part that part "
        "at {page, y} = the top of the missed label's line (from the question's LINES). The upper "
        "half keeps the old id; the lower half's id is '<old id>-split' - give it its path, marks "
        "(value) and fields.text in the split step. Then edit_part the upper half (fields path, "
        "marks, text) and fix the links: unlink rows from the wrong half, link them to the right one.",
        "- A part missing entirely (left in the stem, or not read at all): add_part with "
        "question_number, path, value (marks), fields.text and fields.segments (its region, from "
        "the LINES; trim the stem's or a neighbour's segments it overlaps with edit_part). "
        "part_id = the part it goes before, if any. Its id is '<question>.<path>'.",
        "- Something read as a part, row or question that isn't one (an axis label, a table "
        "number): delete_part / delete_entry / delete_question. A row's marks misread: "
        "set_entry_marks. The cover total misread: set_cover_total. A question number misread: "
        "renumber_question (question_number -> value).",
        "- edit_part changes a part's text, marks, path or region (fields.segments, page points as "
        "shown; pages count from 0).",
        "- A part whose answer isn't among the rows shown - the program lost the row, or filed it "
        "under another question or in a notes block - but is on the mark scheme: find it in the "
        "question's MARK SCHEME LINES and link_region the part to it, at {page, y0, y1} = from the "
        "top of the answer's first line to the top of the next row's first line (page and y as "
        "the lines show them). The program reads the row from that stretch itself. Then "
        "delete_entry any row that was a notes block or a stray read as this part's answer. Notes "
        "and guidance (\"M1: ...\", \"Notes\", \"Alt\") are never a part's answer; the answer row is "
        "in the table above them, with its marking codes (M1 A1 B1) and a mark like \"(3)\".",
        "- Steps in one fix run in order, each on the result of the one before.",
        "- If the paper and its mark scheme genuinely disagree (the board's own mistake), add a "
        "note of kind paper_error. Only when it truly can't be decided from what's shown (it "
        "turns on a figure, a page is missing), a note of kind needs_person. Never guess.",
        "- Each fix lists the issue keys it clears, exactly as given. Use only the part ids and "
        "row ids shown.",
        "",
        "ISSUES (key: detail):",
    ]
    out += [f"- {key}: {issue['detail']}" for key, issue in issues.items()]

    for question in paper.questions:
        if not whole_paper and question.question_number not in numbers:
            continue
        out += ["", f"QUESTION {question.question_number} (total {question.total_marks})"]
        if question.stem_text or question.stem_segments:
            region = "; ".join(_box(b) for b in question.stem_segments)
            out.append(f"  stem region=[{region}]: {_clip(question.stem_text, MAX_TEXT)}")
        for part in question.parts:
            printed = "" if part.marks_printed in (None, part.marks) else f", printed {part.marks_printed}"
            links = ", ".join(joined.get(part.id, [])) or "none"
            region = "; ".join(_box(b) for b in part.segments)
            out.append(
                f"  part id={part.id} path={part.path} marks={part.marks}{printed} rows=[{links}] "
                f"region=[{region}]: {_clip(part.text, MAX_TEXT)}"
            )
        if lines and lines.get(question.question_number):
            out.append("  LINES (page, top y: text):")
            out += [f"    {t}" for t in lines[question.question_number]]
        if ms_lines and ms_lines.get(question.question_number):
            out.append("  MARK SCHEME LINES (mark scheme page, top y: text - read from the page itself):")
            out += [f"    {t}" for t in ms_lines[question.question_number]]
        # the question's own rows, and its neighbours': a row the parser filed under the
        # wrong number is most often the next or previous question's - shown only its own,
        # Claude kept answering "no row for this question was shown"
        near = {question.question_number - 1, question.question_number, question.question_number + 1}
        rows = [e for e in paper.mark_scheme if e.question_number in near]
        if rows:
            out.append("  mark scheme rows (this question's, and the questions either side):")
        for entry in rows:
            flag = "" if entry.id in used else " (linked to nothing)"
            out.append(
                f"    row id={entry.id} Q{entry.question_number} label={entry.part_path or '-'} marks={entry.marks}{flag}: "
                f"{_clip(entry.answer_text, MAX_ANSWER)}"
                + (f" | notes: {_clip(entry.guidance_text, MAX_ANSWER)}" if entry.guidance_text else "")
            )
    # every row linked to nothing, whatever the issue: a part with no row is as often
    # answered by one of these as an orphaned row is looking for its part
    orphans = [e for e in paper.mark_scheme if e.id not in used and (whole_paper or e.question_number not in numbers)]
    if orphans:
        out += ["", "OTHER ROWS LINKED TO NOTHING:"]
        out += [
            f"  row id={e.id} Q{e.question_number} label={e.part_path or '-'} marks={e.marks}: "
            f"{_clip(e.answer_text, MAX_ANSWER)}"
            for e in orphans[:40]
        ]
    refused = [f for f in tried if f.source == "ai" and f.status in ("rejected", "undone")]
    if refused:
        out += ["", "ALREADY REFUSED BY THE TEACHER (don't propose again):"]
        out += [f"- {f.reason} ({', '.join(s.op for s in f.steps)})" for f in refused[:20]]
    return "\n".join(out)


def review_paper(
    llm, paper_dir: Path, mode: str, only: set[str] | None = None, save=None
) -> dict:
    """Asks Claude about one paper's open issues (`only`: just these keys), records its
    fixes in fixes.json, applies the safe ones in Auto mode, and returns counts.
    `save(paper)` writes the paper (the server's own, which also refreshes its index)."""
    paper = Paper.model_validate_json((paper_dir / "paper.json").read_text(encoding="utf-8"))
    checked = fixes.checked_keys(paper_dir)
    issues = fixes.open_issues(paper, checked)
    if only is not None:
        issues = {k: v for k, v in issues.items() if k in only}
    if not issues:
        return {"applied": 0, "suggested": 0, "notes": 0}
    existing = fixes.read_fixes(paper_dir)
    # a fresh review replaces Claude's earlier suggestions for the same issues
    asked = set(issues)
    kept = [
        f for f in existing
        if not (f.source == "ai" and f.status == "suggested" and set(f.issues) & asked)
    ]
    if fixes.read_base(paper_dir) is None:
        fixes.write_base(paper_dir, paper)  # extracted before fixes existed: start from here

    applied = suggested = notes = 0
    # Two rounds at most: one fix can make another possible (rows moved to the right
    # question, then linked), so after a round that applied something, Claude looks again at
    # what's still open - on the paper as it now is.
    for round_ in range(ROUNDS):
        if round_:
            issues = fixes.open_issues(paper, checked)
            if only is not None:
                issues = {k: v for k, v in issues.items() if k in only}
            noted = {k for f in kept if f.status == "suggested" for k in f.issues}
            issues = {k: v for k, v in issues.items() if k not in noted}
            if not issues:
                break
        numbers = None if any(i.get("question_number") is None for i in issues.values()) else {
            i["question_number"] for i in issues.values()
        }
        review = llm.json(
            build_prompt(paper, issues, kept, question_lines(paper, numbers), scheme_lines(paper, numbers)),
            AIReview,
        )
        paper, counts = _take(review, paper, issues, kept, checked, mode, llm)
        applied, suggested, notes = applied + counts[0], suggested + counts[1], notes + counts[2]
        if not counts[0]:
            break
    fixes.write_fixes(paper_dir, kept)
    if applied and save is not None:
        save(paper)
    return {"applied": applied, "suggested": suggested, "notes": notes}


def _take(review: AIReview, paper: Paper, issues: dict, kept: list[Fix], checked: set[str], mode: str, llm):
    """Records one round's answer in `kept`: each group tried on a copy, applied in Auto mode
    when safe. Returns the paper as it now is and (applied, suggested, notes)."""
    applied = suggested = notes = 0
    for group in review.fixes:
        fix = Fix(
            steps=[s.to_step() for s in group.steps],
            issues=[k for k in group.issues if k in issues],
            reason=group.reason,
            source="ai",
            model=getattr(llm, "model", ""),
            fixer=FIXER_VERSION,
            status="suggested",
        )
        if not fix.steps:
            continue
        trial, clears, raises = fixes.verify(paper, fix, checked)
        if trial is None:
            continue  # names parts or rows the paper doesn't have
        fix.clears, fix.raises = clears, raises
        if mode == "auto" and fixes.safe_to_apply(fix, clears, raises):
            fix.status = "applied"
            paper = trial
            applied += 1
        else:
            suggested += 1
        kept.append(fix)
    for note in review.notes:
        keys = [k for k in note.issues if k in issues]
        if not keys:
            continue
        op = "dismiss" if note.kind == "paper_error" else "needs_person"
        kept.append(Fix(steps=[Step(op=op)], issues=keys, reason=note.reason, source="ai", status="suggested", model=getattr(llm, "model", ""), fixer=FIXER_VERSION))
        notes += 1
    return paper, (applied, suggested, notes)
