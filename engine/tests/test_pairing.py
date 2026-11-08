import pymupdf
import pytest

from qbx.pairing import PairingError, check_pair, identify


def _pdf(path, *pages):
    doc = pymupdf.open()
    for text in pages:
        doc.new_page(width=595, height=842).insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _qp(path, code="0580/22", sitting="February/March 2025"):
    return _pdf(path, f"Cambridge IGCSE\nMATHEMATICS {code}\nPaper 2\n{sitting}\nINSTRUCTIONS\nAnswer all questions.", "1 Work out 2 + 2. [1]")


def _ms(path, code="0580/22", sitting="February/March 2025"):
    return _pdf(path, f"Cambridge IGCSE\nMATHEMATICS {code}\nPaper 2\nMARK SCHEME\nMaximum Mark: 100\nPublished", f"{code} Mark Scheme {sitting}\nGeneric Marking Principles")


def test_identifies_each_kind_and_its_paper(tmp_path):
    qp, ms = identify(_qp(tmp_path / "a.pdf")), identify(_ms(tmp_path / "b.pdf"))
    assert (qp.kind, qp.code, qp.session, qp.year) == ("question paper", "0580/22", "F/M", 2025)
    assert (ms.kind, ms.code, ms.session, ms.year) == ("mark scheme", "0580/22", "F/M", 2025)


def test_a_matching_pair_passes(tmp_path):
    check_pair(_qp(tmp_path / "qp.pdf"), _ms(tmp_path / "ms.pdf"))


@pytest.mark.parametrize(
    "make_qp, make_ms, expected",
    [
        (_ms, _qp, "wrong way round"),  # swapped between the slots
        (_qp, _qp, "looks like a question paper"),  # two question papers
        (_ms, _ms, "looks like a mark scheme"),  # two mark schemes
    ],
)
def test_wrong_kinds_are_refused(tmp_path, make_qp, make_ms, expected):
    qp = make_qp(tmp_path / "first.pdf")
    ms = make_ms(tmp_path / "second.pdf", code="0580/21")  # differ, so not the same bytes
    with pytest.raises(PairingError, match=expected):
        check_pair(qp, ms)


def test_a_pair_from_different_papers_is_refused(tmp_path):
    qp = _qp(tmp_path / "qp.pdf")
    ms = _ms(tmp_path / "ms.pdf", code="0580/21", sitting="May/June 2024")
    with pytest.raises(PairingError, match="aren't the same paper.*0580/22 F/M 2025.*0580/21 M/J 2024"):
        check_pair(qp, ms)


def test_the_same_file_twice_is_refused(tmp_path):
    qp = _qp(tmp_path / "qp.pdf")
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(qp.read_bytes())
    with pytest.raises(PairingError, match="same file"):
        check_pair(qp, copy)


def test_files_it_cannot_identify_are_let_through(tmp_path):
    # another board's wording: nothing to check against, so no false refusal
    check_pair(_pdf(tmp_path / "a.pdf", "Some other board\nQuestion 1"), _pdf(tmp_path / "b.pdf", "Answers\n1 B"))


def test_identifies_the_newer_code_layout(tmp_path):
    qp = _pdf(
        tmp_path / "new_qp.pdf",
        "Cambridge IGCSE\nCHEMISTRY\nPaper 2\nMay/June 2026\nINSTRUCTIONS\nAnswer all questions.",
        "1 A question. [1]\n03_0620_22_2026_1.16",
    )
    ms = _pdf(tmp_path / "new_ms.pdf", "CHEMISTRY 0620/22\nMARK SCHEME\nMaximum Mark: 40\nMay/June 2026", "Generic Marking Principles")
    who = identify(qp)
    assert (who.kind, who.code, who.session, who.year) == ("question paper", "0620/22", "M/J", 2026)
    check_pair(qp, ms)  # the new-layout paper pairs with its own scheme


def test_a_mark_scheme_is_recognised_by_its_running_header(tmp_path):
    # every MS page carries "0620/12  Cambridge IGCSE – Mark Scheme  May/June 2021" over
    # "PUBLISHED" - enough on its own, even without the cover's "MARK SCHEME" / "Maximum Mark"
    for header in ("Cambridge IGCSE – Mark Scheme", "Cambridge IGCSE(9–1) – Mark Scheme"):
        path = _pdf(tmp_path / "ms.pdf", f"0620/12\n{header}\nPUBLISHED\nMay/June 2021\nQuestion Answer Marks")
        who = identify(path)
        assert (who.kind, who.code, who.session, who.year) == ("mark scheme", "0620/12", "M/J", 2021)
