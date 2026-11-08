"""A synthetic mark scheme laid out like a real one: a cover, a marking-principles page,
then a landscape page holding a ruled Question | Answer | Marks table - rows separated by
0.5pt hairlines drawn one per cell, the way the real papers draw them."""

import pymupdf

TABLE_X0, QUESTION_X1, MARKS_X0, TABLE_X1 = 62, 122, 734, 780
HAIRLINE = 0.5


# a multiple-choice mark scheme's table: portrait, and much narrower
NARROW_COLUMNS = (49, 113, 496, 546)


def _separator(page: pymupdf.Page, y: float, columns) -> None:
    x0, q1, m0, x1 = columns
    for a, b in ((x0, q1), (q1, m0), (m0, x1)):
        page.draw_rect(pymupdf.Rect(a, y, b, y + HAIRLINE), color=None, fill=(0, 0, 0))


def _column_rules(page: pymupdf.Page, top: float, bottom: float, columns) -> None:
    for x in columns:
        page.draw_rect(pymupdf.Rect(x, top, x + HAIRLINE, bottom), color=None, fill=(0, 0, 0))


def write_ruled_ms(path, rows, narrow: bool = False) -> None:
    """rows: (label, answer, marks); each row is 25pt tall. `narrow` sets the table on a
    portrait page at the multiple-choice mark schemes' width."""
    columns = NARROW_COLUMNS if narrow else (TABLE_X0, QUESTION_X1, MARKS_X0, TABLE_X1)
    x0, q1, m0, x1 = columns
    width, height = (595, 842) if narrow else (842, 595)
    doc = pymupdf.open()
    doc.new_page(width=595, height=842).insert_text((72, 100), "Mark scheme cover")

    principles = doc.new_page(width=842, height=595)
    principles.insert_text((62, 30), "0000/11")
    principles.insert_text((62, 80), "Generic Marking Principles")
    principles.insert_text((62, 100), "Marks awarded are always whole marks.")
    principles.insert_text((360, 570), "Page 2 of 3")

    page = doc.new_page(width=width, height=height)
    page.insert_text((62, 30), "0000/11")
    top = 60.0
    _separator(page, top, columns)
    page.insert_text((x0 + 8, top + 17), "Question")
    page.insert_text(((q1 + m0) / 2 - 20, top + 17), "Answer")
    page.insert_text((m0 + 6, top + 17), "Marks")
    y = top + 25
    _separator(page, y, columns)
    for label, answer, marks in rows:
        page.insert_text((x0 + 18, y + 17), label)
        page.insert_text((q1 + 8, y + 17), answer)
        page.insert_text((x1 - 15, y + 17), str(marks))
        y += 25
        _separator(page, y, columns)
    _column_rules(page, top, y + HAIRLINE, columns)
    page.insert_text((260, height - 25), "Page 3 of 3")
    doc.save(str(path))
    doc.close()
