"""Scheme mark readings `qbx audit`'s ms_marks check found wrong (2026-11-18)."""

from qbx import markscheme_paper
from qbx.markscheme import MSSpan, _page_marks_columns, _row_marks, _split_marks_and_code
from qbx.schema import MarkSchemeEntry


def _span(text, x, y=100.0, page=0, bold=True):
    return MSSpan(text=text, page=page, x=x, y=y, bbox=(x, y, x + 6, y + 10), bold=bold)


def test_a_number_beside_its_partial_mark_code_is_the_row_s_mark():
    # 0580/41 MJ22 3(a)(i): "3" in Marks, "B2 for 4x + 6 = 30" under Partial Marks - read 5
    assert _row_marks([_span("3", 494), _span("B2", 494, 109)]) == 3


def test_plain_fraction_digits_beside_a_bold_mark_are_not_marks():
    # 0580/41 MJ22 7(d): "4" bold, then a fraction's "2" over "12" in Times - read 18
    spans = [_span("4", 497), _span("M1", 497, 109), _span("2", 494, 126, bold=False), _span("12", 490, 128, bold=False)]
    assert _row_marks(spans, partial_marks=True) == 4
    # several bold numbers are several marks (0580/42 ON20 1(b)(ii))
    assert _row_marks([_span("1", 322), _span("2", 322, 125)], partial_marks=True) == 3
    # outside maths a plain "1" in line with the rest is still a mark (0620/53 MJ20 2(f))
    assert _row_marks([_span("1", 774), _span("1", 774, 125, bold=False)]) == 2


def test_a_code_with_its_suffix_splits_from_the_mark():
    span = {"text": "4 B3FT for 7 or 8 correctly plotted points", "bbox": (322, 632, 517, 644), "size": 11.0, "origin": (322, 642)}
    pieces = _split_marks_and_code(span)
    assert [p["text"] for p in pieces][:2] == ["4", "B3FT for 7 or 8 correctly plotted points"]


def test_marks_listed_one_per_answer_are_each_a_mark():
    span = {"text": "1, 1", "bbox": (311, 610, 330, 618), "size": 9.0, "origin": (311, 617)}
    assert [p["text"] for p in _split_marks_and_code(span)] == ["1", "1"]


def test_a_page_setting_its_marks_column_elsewhere_uses_its_own():
    # 0580/33 MJ20: Marks at x 315 on pages 0-1, at 298 on page 2
    labels, spans = [], []
    for page, x in ((0, 315), (1, 315), (2, 298)):
        for i in range(4):
            label = _span(f"{page * 4 + i + 1}", 56, 100 + 40 * i, page)
            labels.append(label)
            spans += [label, _span("1", x, 100 + 40 * i, page)]
    columns = _page_marks_columns(labels, spans, document_x=315)
    assert columns[2] == 298 and columns[0] == 315
    # a page whose rightmost numbers don't line up (Partial Marks digits) keeps the document's
    scattered = [label for label in labels if label.page == 2]
    noisy = [*scattered] + [_span("1", 298 + 40 * i, s.y, 2) for i, s in enumerate(scattered)]
    assert 2 not in _page_marks_columns(scattered, noisy, document_x=315)


def _entry(path, marks):
    return MarkSchemeEntry(id=f"1.{path}", paper_key="", question_number=1, part_path=path, answer_text="", marks=marks)


def test_a_group_mark_on_the_last_row_is_shared_by_the_run():
    # 0452/13 MJ16 Q1: (a)-(j) a letter each, one "[10]" at the end
    paths = list("abcdefghij")
    entries = [_entry(p, 0) for p in paths[:-1]] + [_entry("j", 10)]
    markscheme_paper._share_group_marks(entries, paths)
    assert [e.marks for e in entries] == [1] * 10
    # one that doesn't divide stays as printed
    entries = [_entry("a", 0), _entry("b", 0), _entry("c", 4)]
    markscheme_paper._share_group_marks(entries, ["a", "b", "c"])
    assert [e.marks for e in entries] == [0, 0, 4]


def test_paper_layout_scheme_mark_phrases():
    assert markscheme_paper._TIMES_MARKS_RE.search("(j) B 10 ×(1) mark")["count"] == "10"
    assert markscheme_paper._MAX_MARKS_RE.search("legal identity (1) [Max 4] Both")["marks"] == "4"
