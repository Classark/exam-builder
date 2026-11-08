"""Classify each page as cover/blank/question/insert/acknowledgements (CLAUDE.md section 5.3).

Must run before anchor detection so cover pages, blank pages and acknowledgement pages never
leak stray digits/parts into the anchor clustering (see engine/scratch_anchors.py findings:
barcode digits on the cover page would otherwise pollute the question-number cluster).

Classifies on the whole page's text, not just the "first" text block: PyMuPDF's block order
follows the PDF content stream, not top-to-bottom reading order, so on a real Cambridge cover
page the barcode/title blocks come before the INSTRUCTIONS/INFORMATION/CANDIDATE block that
actually signals "this is a cover page".
"""

import re
from enum import Enum

import pymupdf

from qbx.anchors import HEADER_FOOTER_MARGIN

# A question's opening line: its number, then the question itself ("37 The half-life of...").
# Deliberately 1-2 digits so a year in a copyright notice can't pass for a question number,
# and it must be followed by real text on the *same* line - `\s` would match the newline and
# let a bare page-number line pair up with whatever followed it.
_QUESTION_LINE_RE = re.compile(r"(?m)^[ \t]*\d{1,2}[ \t]+\S")


class PageType(str, Enum):
    COVER = "cover"
    BLANK = "blank"
    QUESTION = "question"
    INSERT = "insert"
    ACKNOWLEDGEMENTS = "acknowledgements"
    PERIODIC_TABLE = "periodic_table"
    FORMULA_SHEET = "formula_sheet"


# The formulae page maths papers (0580) print straight after the cover. Its heading as a line
# of its own, on a page carrying no mark allocation - a question can say "use the list of
# formulas", but a page of questions always has an "[n]".
_FORMULA_SHEET_HEADING_RE = re.compile(
    # Edexcel's: "Formulae sheet – Higher Tier", "FORMULAE SHEET – FOUNDATION TIER" (4MA0/4MA1)
    r"(?mi)^\s*(list of formulae?s?|mathematical formulae|formulae? (list|sheet))"
    r"(\s*[–-]\s*(higher|foundation) tier)?\s*$"
)
_MARK_ALLOCATION_RE = re.compile(r"\[\d+\]")
_QUESTION_TOTAL_RE = re.compile(r"\(Total\s+for\s*Question\s+\d+", re.IGNORECASE)


def is_formula_sheet(page_text: str) -> bool:
    return bool(_FORMULA_SHEET_HEADING_RE.search(page_text)) and not _MARK_ALLOCATION_RE.search(
        page_text
    )


# Edexcel's 4CH1 papers head theirs "The Periodic Table of the Elements", on page 2
_PERIODIC_TABLE_HEADING_RE = re.compile(r"(?mi)^\s*the periodic table of (?:the )?elements\s*$")
# Named elements, every one of which the printed table labels by name under its symbol.
_ELEMENT_NAMES = frozenset(
    """hydrogen helium lithium beryllium boron carbon nitrogen oxygen fluorine neon sodium
    magnesium aluminium silicon phosphorus sulfur chlorine argon potassium calcium scandium
    titanium vanadium chromium manganese iron cobalt nickel copper zinc gallium germanium
    arsenic selenium bromine krypton rubidium strontium yttrium zirconium niobium molybdenum
    technetium ruthenium rhodium palladium silver cadmium indium tin antimony tellurium
    iodine xenon caesium barium hafnium tantalum tungsten rhenium osmium iridium platinum
    gold mercury thallium lead bismuth polonium astatine radon francium radium""".split()
)
# A full table names all 88 elements above; a question showing "part of the Periodic Table"
# names a handful. Every real one in the corpus (8 papers) names all 88.
PERIODIC_TABLE_MIN_ELEMENTS = 40


def is_periodic_table(page_text: str) -> bool:
    """The data page some papers (0620 Chemistry) print at the back: its own heading as a
    line of its own *and* most of the elements named - either alone is too weak, since
    questions quote the heading's words ("in the Periodic Table") and show fragments."""
    if not _PERIODIC_TABLE_HEADING_RE.search(page_text):
        return False
    named = {line.strip().lower() for line in page_text.splitlines()} & _ELEMENT_NAMES
    return len(named) >= PERIODIC_TABLE_MIN_ELEMENTS


def classify_page(page_text: str, page_number: int, page_count: int) -> PageType:
    """page_number is 1-indexed."""
    text = page_text.upper()
    lines = [line.strip() for line in text.splitlines()]

    # A standalone "BLANK PAGE" line, not e.g. "...Blank pages are indicated" in the cover
    # page's document-length notice.
    if "BLANK PAGE" in lines:
        return PageType.BLANK
    # Not question content - its atomic numbers (1, 2, 3...) are exactly the shape of
    # question-number anchors, so it must never reach anchor detection.
    if is_periodic_table(page_text):
        return PageType.PERIODIC_TABLE
    # likewise all digits and no questions: exponents, fractions, "1/2 bh"
    if is_formula_sheet(page_text):
        return PageType.FORMULA_SHEET
    if page_number == 1 and ("CANDIDATE" in text or "INFORMATION" in text):
        return PageType.COVER
    if "/INSERT" in text:
        return PageType.INSERT
    # Real-paper finding (0972/11): the copyright block doesn't always get a page to
    # itself - this paper runs questions 37-40 down the top of its final page and tucks the
    # acknowledgements into the bottom margin beneath them. Treating the whole page as
    # acknowledgements silently dropped four questions, and the paper still validated
    # against its own mark scheme as "orphaned MS entries" rather than "missing questions".
    # Questions win: a page carrying any is a question page, and the copyright block on it
    # is page furniture for crops.py to leave out.
    #
    # Real-paper finding #2 (0620/61-62 2016-2020): the planning question (4) sits on the
    # last page above the copyright block with its number alone on its line ("4\t", the text
    # starting on the next), so the opening-line test missed it and dropped the question. A
    # mark allocation says "question page" just as surely - no copyright block carries one.
    if page_number == page_count and "COPYRIGHT" in text:
        # Edexcel's own: its marks are "(5)", but every question closes with its total
        # (4CH0 2C June 2015 sets 6(d) above the copyright notice on its last page)
        # Real-paper finding #3 (0610/12, /13, /22, /23, 2024-2026 multiple choice): the text
        # layer gives each question's number on a line of its own, after its stem, and an
        # MCQ has no mark allocation - so questions 38-40 went with the page. Its options
        # are as sure a sign: A, B, C and D each a line of their own.
        if (
            not _QUESTION_LINE_RE.search(page_text)
            and not _MARK_ALLOCATION_RE.search(page_text)
            and not _QUESTION_TOTAL_RE.search(page_text)
            and not {"A", "B", "C", "D"} <= set(lines)
        ):
            return PageType.ACKNOWLEDGEMENTS
    return PageType.QUESTION


def periodic_table_region(doc: object) -> tuple[int, tuple[float, float, float, float]] | None:
    """The paper's periodic table, if it prints one - see `data_page_region`."""
    return data_page_region(doc, is_periodic_table, reversed(range(doc.page_count)))


def formula_sheet_region(doc: object) -> tuple[int, tuple[float, float, float, float]] | None:
    """The paper's list of formulas, if it prints one - see `data_page_region`."""
    return data_page_region(doc, is_formula_sheet, range(doc.page_count))


_MARGIN_STRIP_RE = re.compile(r"do not write in (?:this|the) (?:area|margin)", re.IGNORECASE)


def data_page_region(
    doc: object, is_kind, page_order
) -> tuple[int, tuple[float, float, float, float]] | None:
    """(page index, bbox) of the first page in `page_order` that `is_kind` accepts:
    everything drawn or written on it except its running page number and footer (paper
    code, copyright line), which sit in the same header/footer bands anchors.py excludes."""
    for index in page_order:
        page = doc[index]
        if not is_kind(page.get_text("text")):
            continue
        top, bottom = HEADER_FOOTER_MARGIN, page.rect.height - HEADER_FOOTER_MARGIN
        width, height = page.rect.width, page.rect.height
        # Not the page's own furniture: Edexcel's frame round the page and its "DO NOT WRITE
        # IN THIS AREA" margin strip would otherwise be copied along with the table
        # (user-reported, 4CH1 periodic table pages).
        rects = [
            d["rect"] for d in page.get_drawings()
            if not (d.get("fill") is None and d["rect"].width >= 0.8 * width and d["rect"].height >= 0.8 * height)
        ]
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(span["text"] for span in line["spans"])
                if not text.strip() or _MARGIN_STRIP_RE.search(text):
                    continue  # the strip's labels, and the blank tabs between them
                rects.append(line["bbox"])
        inside = [pymupdf.Rect(r) for r in rects]
        inside = [r for r in inside if r.y0 >= top and r.y1 <= bottom and not r.is_empty]
        if not inside:
            return None
        bounds = inside[0]
        for rect in inside[1:]:
            bounds |= rect
        return index, (bounds.x0, bounds.y0, bounds.x1, bounds.y1)
    return None


def classify_document(doc: object) -> list[PageType]:
    page_count = doc.page_count
    return [
        classify_page(page.get_text("text"), page_number=i + 1, page_count=page_count)
        for i, page in enumerate(doc)
    ]
