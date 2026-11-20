from qbx import sources


def test_locate_finds_a_pdf_moved_into_a_subfolder(tmp_path, monkeypatch):
    papers = tmp_path / "papers"
    moved = papers / "Chemistry IGCSE" / "0620_s25_qp_12.pdf"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"%PDF-1.7")
    monkeypatch.setenv("QBX_PAPERS_DIR", str(papers))
    monkeypatch.setattr(sources, "_names_root", None)
    recorded = str(tmp_path / "old" / "papers" / "0620_s25_qp_12.pdf")
    assert sources.locate(recorded) == str(moved)


def test_locate_leaves_a_pdf_that_is_nowhere(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_PAPERS_DIR", str(tmp_path / "papers"))
    monkeypatch.setattr(sources, "_names_root", None)
    recorded = str(tmp_path / "papers" / "gone.pdf")
    assert sources.locate(recorded) == recorded
