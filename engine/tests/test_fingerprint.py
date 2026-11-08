import pytest

from qbx.fingerprint import FingerprintError, parse_footer


def test_parses_a_level_footer():
    fp = parse_footer("Cambridge International AS & A Level 9702/42/M/J/23")
    assert fp.level == "A Level"
    assert fp.paper == "4"
    assert fp.variant == "2"
    assert fp.session == "M/J"
    assert fp.year == 23
    assert fp.paper_key == "9702_42_MJ_23"


def test_parses_igcse_footer():
    fp = parse_footer("0620/61/O/N/22")
    assert fp.level == "IGCSE"


def test_fm_session_requires_variant_2():
    parse_footer("9702/22/F/M/24")  # does not raise
    with pytest.raises(FingerprintError):
        parse_footer("9702/21/F/M/24")


def test_raises_when_no_code_present():
    with pytest.raises(FingerprintError):
        parse_footer("no paper code in this footer")


def test_specimen_papers_have_their_own_session():
    from qbx.fingerprint import parse_footer

    fp = parse_footer("0620/02/SP/23")
    assert fp.session == "SP" and fp.year == 23
    assert fp.paper_key == "0620_02_SP_23"


def test_cover_titles_come_from_the_lines_around_the_paper_code():
    from qbx.fingerprint import read_cover_titles

    # as the covers print them - control characters and all ("CHEMISTRY\x08")
    cover = "Cambridge IGCSE\n* 1182455718 *\nCHEMISTRY\x08\n0620/41\nPaper 4 Theory (Extended)\x08\nMay/June 2024"
    assert read_cover_titles(cover, "0620", "4", "1") == ("Chemistry", "Paper 4 Theory (Extended)")
    cover = "COMPUTER SCIENCE\n0478/11\nPaper 1 Theory\n"
    assert read_cover_titles(cover, "0478", "1", "1") == ("Computer Science", "Paper 1 Theory")
    # nothing to go on: empty rather than a guess
    assert read_cover_titles("no code here", "0620", "4", "1") == ("", "")


def test_newer_code_layout_takes_its_session_from_the_cover():
    # 03_0620_22_2026_1.16: document number, subject, paper+variant, year, version - and
    # no session, which the cover supplies
    from qbx.fingerprint import parse_footer

    fp = parse_footer("03_0620_22_2026_1.16", cover_text="CHEMISTRY\n0620/22\nMay/June 2026")
    assert (fp.subject, fp.paper, fp.variant, fp.session, fp.year) == ("0620", "2", "2", "M/J", 26)
    assert fp.paper_key == "0620_22_MJ_26"

    specimen = parse_footer("03_0620_22_2026_1.16", cover_text="Specimen Paper\nFor examination from 2026")
    assert specimen.session == "SP" and specimen.paper_key == "0620_22_SP_26"


def test_newer_code_layout_without_a_sitting_says_so():
    import pytest

    from qbx.fingerprint import FingerprintError, parse_footer

    with pytest.raises(FingerprintError, match="doesn't include the session"):
        parse_footer("03_0620_22_2026_1.16", cover_text="CHEMISTRY")


def test_the_old_layout_still_reads_as_before():
    from qbx.fingerprint import parse_footer

    fp = parse_footer("0620/41/M/J/24", cover_text="October/November 2099 - ignored")
    assert (fp.session, fp.year, fp.paper_key) == ("M/J", 24, "0620_41_MJ_24")


def test_single_month_sittings_are_read():
    # the March 2021 schemes print "March 2021", not "February/March 2021"
    from qbx.fingerprint import read_sitting

    assert read_sitting("CHEMISTRY 0620/12 March 2021") == ("F/M", 2021)
    assert read_sitting("June 2026") == ("M/J", 2026)
    assert read_sitting("February/March 2025") == ("F/M", 2025)
