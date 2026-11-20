"""Inserts: a question paper's separate booklet of source material - Business Studies'
case study (0450 Paper 2: "This Insert contains the case study material", the business's
story and Appendices 1-3), which every question on the paper is about.

An insert isn't extracted: it has no questions or marks. The scan attaches each one to its
question paper by the paper key on its cover (`scan.plan_scan`), and saves which insert
belongs to which paper in `out/.qbx-inserts.json` - kept apart from paper.json so an insert
added after its paper was extracted needs no re-extraction. A composed paper prints a
source's insert pages just before the first question taken from it (user-decided), so
each case study sits in front of the questions about it (`quiz_export.compose_qp_pdf`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pymupdf

from qbx.schema import BBox

INDEX = ".qbx-inserts.json"


def save_index(out_dir: Path, inserts: dict[str, Path]) -> None:
    """Every insert the scan found, by the paper key of the question paper it goes with."""
    path = out_dir / INDEX
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({key: str(p) for key, p in sorted(inserts.items())}, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # rebuilt by the next scan


def attach(out_dir: Path, paper_key: str, path: Path) -> None:
    """One insert added by hand ("Add one paper"), before the next scan lists it too."""
    current = {key: Path(p) for key, p in index(out_dir).items()}
    current[paper_key] = path
    save_index(out_dir, current)


# A question paper needs its insert when the cover says what's in it: "The insert contains
# the case study." (0450 Paper 2, every year 2016-2025; never on Paper 1). An insert the cover
# only lists - "... and 1 Insert.", "Insert (enclosed)" - has nothing to print: 0620 Paper 5's
# published insert is a page of "CAMBRIDGE INTERNATIONAL EXAMINATIONS" filler (0620/51 MJ18,
# user-reported), and 0455 Paper 2's (2016-2019) is the answer booklet. Over the bank: all 70
# 0450 Paper 2 covers say "contains"; none of the 28 0455 or 4 0620 covers do.
_NEEDS_INSERT_RE = re.compile(r"\bthe insert contains\b", re.IGNORECASE)


def cover_needs_insert(qp_path: str | Path) -> bool:
    """Whether the question paper says it comes with an insert."""
    try:
        with pymupdf.open(qp_path) as doc:
            return bool(len(doc)) and bool(_NEEDS_INSERT_RE.search(doc[0].get_text()))
    except Exception:
        return False


def index(out_dir: Path) -> dict[str, str]:
    return _index(out_dir)


def _index(out_dir: Path) -> dict[str, str]:
    try:
        return json.loads((out_dir / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def insert_for(out_dir: Path, paper_key: str) -> str | None:
    """The insert PDF for `paper_key`, if the scan found one and it's still there (moved
    under the papers folder is found by name, like any source PDF)."""
    from qbx.sources import locate

    recorded = _index(out_dir).get(paper_key)
    if not recorded:
        return None
    found = locate(recorded)
    return found if Path(found).is_file() else None


def content_pages(path: str) -> list[BBox]:
    """Each page of the insert worth printing, as its content's region: not the cover, not
    a blank page, and never the header, footer, barcode or copyright block around it."""
    from qbx.anchors import HEADER_FOOTER_MARGIN, HEADER_MARGIN
    from qbx.crops import _content_bbox, _content_limit, _page_bottom

    out = []
    with pymupdf.open(path) as doc:
        for page in doc:
            if page.number == 0:
                continue  # the insert's own cover: its instructions, not material
            bottom = min(_content_limit(page), _page_bottom(page, HEADER_FOOTER_MARGIN))
            # from the header margin down, with ink starting above it - the page number,
            # 26-40pt down - left out, as on a question's continuation page
            box = _content_bbox(page, pymupdf.Rect(0, HEADER_MARGIN, page.rect.width, bottom), header_top=True)
            if box is None or box.is_empty or box.height < 20:
                continue
            # the last page: "BLANK PAGE" over the copyright block, which is cut already
            if "BLANK PAGE" in page.get_text(clip=box):
                continue
            out.append(BBox(page=page.number, x0=box.x0, y0=box.y0, x1=box.x1, y1=box.y1))
    return out
