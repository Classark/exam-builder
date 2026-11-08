"""Mark scheme rows linked to a question part by hand, in the Reviewer.

When a part joins no mark scheme row - a label the parser couldn't read, a row Pearson or
Cambridge printed under the wrong label - the teacher finds the answer on the mark scheme
and draws a box round it. That box becomes the part's mark scheme entry: its text is read
from inside it, and a composed mark scheme copies the region like any detected row.

Links are kept in `ms_links.json` beside paper.json, keyed by part id, so re-extracting the
paper (a parser fix, a reprocess) keeps them: `pipeline.run_extraction` applies them again.
Both boards, same mechanism.
"""

import json
from pathlib import Path

import pymupdf

from qbx.markscheme import _rotated_spans
from qbx.schema import BBox, LabelRef, MarkSchemeEntry, Paper, QuestionPartMarkScheme

LINKS_FILE = "ms_links.json"
# a span is inside a drawn box when this much of its own height is
_INSIDE = 0.5


def read_links(paper_dir: Path) -> dict[str, list[dict]]:
    """part id -> the boxes drawn for it (BBox dicts, upright page coordinates)."""
    path = Path(paper_dir) / LINKS_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("links", {})
    except (OSError, ValueError):
        return {}


def write_links(paper_dir: Path, links: dict[str, list[dict]]) -> None:
    (Path(paper_dir) / LINKS_FILE).write_text(json.dumps({"links": links}, indent=2), encoding="utf-8")


def _inside(span_bbox: tuple, box: BBox) -> bool:
    x0, y0, x1, y1 = span_bbox
    overlap_y = min(y1, box.y1) - max(y0, box.y0)
    centre_x = (x0 + x1) / 2
    return overlap_y >= _INSIDE * (y1 - y0) and box.x0 <= centre_x <= box.x1


def _widen(box: BBox, columns: list[float]) -> BBox:
    """A box drawn round part of a row - just the answer, say - widened to the table's full
    width, so a composed scheme copies the whole row, label and marks included."""
    if len(columns) < 2 or box.x1 < columns[0] or box.x0 > columns[-1]:
        return box
    return box.model_copy(update={"x0": min(box.x0, columns[0]), "x1": max(box.x1, columns[-1])})


def make_entry(paper: Paper, part_id: str, boxes: list[BBox], ms_path: str, columns: list[float]) -> MarkSchemeEntry:
    """The entry a drawn box makes: its text, the part's marks, and - for composing - the
    box at the table's width with the label printed in its first column, if any."""
    part = next(p for q in paper.questions for p in q.parts if p.id == part_id)
    question = next(q for q in paper.questions if any(p.id == part_id for p in q.parts))
    drawn = sorted(boxes, key=lambda b: (b.page, b.y0))
    segments = [_widen(b, columns) for b in drawn]
    with pymupdf.open(ms_path) as doc:
        spans = _rotated_spans(doc)

    def within(box: BBox) -> list:
        return sorted(
            (s for s in spans if s.page == box.page and _inside(s.bbox, box)),
            key=lambda s: (round(s.bbox[1]), s.bbox[0]),
        )

    # the answer is what the teacher boxed; the label, what the widened row's first column holds
    in_label_column = lambda s: len(columns) >= 2 and s.bbox[0] < columns[1] - 2  # noqa: E731
    texts = [s.text for box in drawn for s in within(box) if not in_label_column(s)]
    label_spans = [s for s in within(segments[0]) if in_label_column(s)]
    first = segments[0]
    if label_spans:
        top = min(s.bbox[1] for s in label_spans)
        label_spans = [s for s in label_spans if s.bbox[1] - top < 14]  # its own line only
        style = label_spans[0]
        label = LabelRef(
            page=first.page,
            x0=min(s.bbox[0] for s in label_spans), y0=min(s.bbox[1] for s in label_spans),
            x1=max(s.bbox[2] for s in label_spans), y1=max(s.bbox[3] for s in label_spans),
            size=style.size, bold=style.bold, font=style.font, origin_x=style.origin[0], origin_y=style.origin[1],
        )
    else:
        # nothing printed to replace: the composed label goes in at the box's top left
        x = (columns[0] if columns else first.x0) + 4
        label = LabelRef(page=first.page, x0=x, y0=first.y0 + 4, x1=x, y1=first.y0 + 4, size=10.0, origin_x=x, origin_y=first.y0 + 14)
    return MarkSchemeEntry(
        id=f"manual.{part_id}",
        paper_key=paper.paper_key,
        question_number=question.question_number,
        part_path=part.path,
        answer_text=" ".join(texts).strip(),
        marks=part.marks,
        segments=segments,
        label=label,
        manual=True,
    )


def _overlaps(a: BBox, b: BBox) -> bool:
    """`a` covers at least half of `b`'s height, on the same page."""
    if a.page != b.page:
        return False
    return min(a.y1, b.y1) - max(a.y0, b.y0) >= 0.5 * (b.y1 - b.y0)


def apply_links(
    paper: Paper, links: dict[str, list[dict]], ms_path: str, columns: list[float],
    rows: dict[str, list[BBox]] | None = None,
) -> Paper:
    """`paper` with each linked part's joins replaced by its drawn row. A link to a part the
    paper no longer has (re-extraction renamed it) is left in the file, unused.

    `rows` (entry id -> its region, from the scheme's layout) lets a drawn box take the
    place of the detected row it covers when nothing joined to that row - a row printed
    under the wrong label, the usual reason a part needed linking - so it isn't left behind
    as an orphan."""
    part_ids = {p.id for q in paper.questions for p in q.parts}
    entries = [e for e in paper.mark_scheme if not e.manual]
    joins = list(paper.joins)
    for part_id, raw in links.items():
        if part_id not in part_ids or not raw:
            continue
        entry = make_entry(paper, part_id, [BBox(**b) for b in raw], ms_path, columns)
        entries.append(entry)
        joins = [j for j in joins if j.part_id != part_id]
        joins.append(QuestionPartMarkScheme(part_id=part_id, mark_scheme_entry_id=entry.id))
    used = {j.mark_scheme_entry_id for j in joins}
    # a detected row the link replaced, joined to nothing else now, is dropped rather than
    # reported as orphaned: the teacher has said where this part's answer is
    replaced = {e.id for e in entries if not e.manual and e.id not in used}
    replaced &= {
        j.mark_scheme_entry_id for j in paper.joins if j.part_id in links and j.part_id in part_ids
    }
    if rows:
        drawn = [s for e in entries if e.manual for s in e.segments]
        replaced |= {
            e.id for e in entries
            if not e.manual and e.id not in used
            and any(_overlaps(box, row) for box in drawn for row in rows.get(e.id, []))
        }
    entries = [e for e in entries if e.id not in replaced]
    return paper.model_copy(update={"mark_scheme": entries, "joins": joins})
