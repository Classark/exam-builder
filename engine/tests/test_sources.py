"""A data folder copied to another computer keeps paper.json's absolute source paths;
loading a paper must find the PDFs under the papers folder in use now."""

from qbx.schema import Paper


def _paper(qp: str, ms: str = "") -> Paper:
    return Paper.model_validate({
        "paper_key": "0620_53_ON_25", "board": "Cambridge", "level": "IGCSE", "subject": "0620",
        "paper": "5", "variant": "3", "session": "ON", "year": 2025,
        "source_qp_path": qp, "source_ms_path": ms,
    })


def test_moved_data_folder_finds_pdfs_under_the_current_papers_folder(tmp_path, monkeypatch):
    papers = tmp_path / "papers"
    (papers / "June 2025" / "Question-paper").mkdir(parents=True)
    (papers / "0620 qp 53.pdf").write_bytes(b"%PDF")
    (papers / "June 2025" / "Question-paper" / "4CH1_1C.pdf").write_bytes(b"%PDF")
    monkeypatch.setenv("QBX_PAPERS_DIR", str(papers))

    old = r"C:\Users\someone\PycharmProjects\QBX\dist\QuestionBankBuilder data\papers"
    paper = _paper(old + r"\0620 qp 53.pdf", old + r"\June 2025\Question-paper\4CH1_1C.pdf")
    assert paper.source_qp_path == str(papers / "0620 qp 53.pdf")
    # kept at its place below "papers" - Edexcel's sitting comes from those folders
    assert paper.source_ms_path == str(papers / "June 2025" / "Question-paper" / "4CH1_1C.pdf")


def test_path_left_alone_when_it_exists_or_cannot_be_found(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_PAPERS_DIR", str(tmp_path / "papers"))
    there = tmp_path / "elsewhere.pdf"
    there.write_bytes(b"%PDF")
    assert _paper(str(there)).source_qp_path == str(there)
    gone = "/Users/someone/papers/gone.pdf"
    assert _paper(gone).source_qp_path == gone  # still reported missing, by its old name
    assert _paper("").source_qp_path == ""
