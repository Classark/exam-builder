from qbx.pages import PageType, classify_page

COVER_TEXT = """Cambridge IGCSE
This document has 12 pages. Blank pages are indicated.
COMPUTER SCIENCE 0478/11
INSTRUCTIONS
Answer all questions.
INFORMATION
The total mark for this paper is 80.
"""

BLANK_TEXT = """12
0478/11/M/J/20
(c) UCLES 2020
BLANK PAGE
"""

ACK_TEXT = """12
0478/11/M/J/20
Permission to reproduce items where third-party owned material protected by copyright
"""


def test_cover_page_detected_despite_blank_pages_notice():
    # regression: "Blank pages are indicated" must not trigger the BLANK check
    assert classify_page(COVER_TEXT, page_number=1, page_count=12) == PageType.COVER


def test_blank_page_requires_standalone_line():
    assert classify_page(BLANK_TEXT, page_number=12, page_count=12) == PageType.BLANK


def test_acknowledgements_on_last_page():
    assert classify_page(ACK_TEXT, page_number=12, page_count=12) == PageType.ACKNOWLEDGEMENTS


def test_ordinary_question_page():
    assert classify_page("1 Explain the term RAM. [2]", page_number=5, page_count=12) == PageType.QUESTION


def test_insert_page():
    assert classify_page("0620/42/INSERT/M/J/23", page_number=3, page_count=12) == PageType.INSERT


def test_cover_check_only_applies_to_page_one():
    # "CANDIDATE"/"INFORMATION" appearing later shouldn't retroactively mark page 1 only
    assert classify_page("INFORMATION about candidates", page_number=4, page_count=12) == PageType.QUESTION


ACK_WITH_QUESTIONS_TEXT = """16
37 The half-life of the radioactive isotope caesium is 30 years.
A 10.0 g
38 Which list gives the names of the planets in the correct order?
A Mercury, Mars, Earth, Venus
Permission to reproduce items where third-party owned material protected by copyright
Cambridge Assessment International Education Copyright Acknowledgements Booklet.
(c) UCLES 2024
0972/11/O/N/24
"""


def test_last_page_with_questions_above_the_copyright_block_is_a_question_page():
    # real-paper finding (0972/11): the acknowledgements don't always get a page to
    # themselves - this paper runs questions 37-40 down the top of its final page and tucks
    # the copyright notice into the bottom margin beneath them. Classifying the page as
    # acknowledgements silently dropped four questions.
    assert (
        classify_page(ACK_WITH_QUESTIONS_TEXT, page_number=16, page_count=16)
        == PageType.QUESTION
    )


def test_last_page_question_with_its_number_alone_on_its_line_is_a_question_page():
    # real-paper finding (0620/61 MJ16): "4\t" on a line of its own, the question beneath
    text = (
        "8\n0620/61/M/J/16\n(c) UCLES 2016\n"
        "Permission to reproduce items where third-party owned material protected by copyright\n"
        "4\t\nCalcium burns in air to form calcium oxide.\n"
        "Plan an investigation to determine the maximum mass of oxygen.\n"
        ".................................................  [6]\n[Total: 6]\n"
    )
    assert classify_page(text, page_number=8, page_count=8) == PageType.QUESTION


_ELEMENTS = (
    "hydrogen helium lithium beryllium boron carbon nitrogen oxygen fluorine neon sodium "
    "magnesium aluminium silicon phosphorus sulfur chlorine argon potassium calcium scandium "
    "titanium vanadium chromium manganese iron cobalt nickel copper zinc gallium germanium "
    "arsenic selenium bromine krypton rubidium strontium yttrium zirconium niobium"
).split()


def test_periodic_table_page_is_its_own_page_type():
    from qbx.pages import is_periodic_table

    text = "The Periodic Table of Elements\nGroup\n" + "\n".join(_ELEMENTS)
    assert is_periodic_table(text)
    assert classify_page(text, page_number=16, page_count=16) == PageType.PERIODIC_TABLE


def test_question_about_the_periodic_table_is_still_a_question():
    from qbx.pages import is_periodic_table

    # quotes the heading's words and names a few elements, as questions do
    text = "3 Part of the Periodic Table of Elements is shown.\nsodium\nmagnesium\naluminium\n[2]"
    assert not is_periodic_table(text)
    # the heading alone, on its own line, isn't enough either without the table under it
    assert not is_periodic_table("The Periodic Table of Elements\nsodium\nchlorine")
    assert classify_page(text, page_number=4, page_count=16) == PageType.QUESTION


def test_list_of_formulas_page_is_its_own_page_type():
    from qbx.pages import is_formula_sheet

    text = "2\nList of formulas\nArea, A, of triangle, base b, height h.\nA = 1/2 bh"
    assert is_formula_sheet(text)
    assert classify_page(text, page_number=2, page_count=20) == PageType.FORMULA_SHEET
    # a question telling you to use it is still a question
    assert not is_formula_sheet("4 Use the list of formulas.\nList of formulas\n........ [2]")


def test_multiple_choice_questions_on_the_copyright_page_are_kept():
    # regression (0610/12 2024-2026): the last page holds questions 38-40 above the copyright
    # block, each number on a line of its own after its stem and no mark allocation
    text = (
        "Which process releases energy?\nA\nB\nC\nD\n40\n"
        "Permission to reproduce items where third-party owned material protected by copyright"
    )
    assert classify_page(text, page_number=16, page_count=16) == PageType.QUESTION
