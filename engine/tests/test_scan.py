from qbx.scan import plan_scan
from tests.test_pairing import _ms, _qp


def test_pairs_by_what_the_files_are_not_their_names(tmp_path):
    # added by hand, so named anything - including a name that says nothing at all
    _qp(tmp_path / "scan0001.pdf")
    _ms(tmp_path / "answers (1).pdf")
    _qp(tmp_path / "chem.pdf", code="0620/41", sitting="May/June 2024")
    _ms(tmp_path / "0620_s24_ms_41__1_.pdf", code="0620/41", sitting="May/June 2024")

    plan = plan_scan(tmp_path)

    pairs = {p.paper_key: (p.qp_path.name, p.ms_path.name) for p in plan.pairs}
    assert pairs == {
        "0580_22_FM_25": ("scan0001.pdf", "answers (1).pdf"),
        "0620_41_MJ_24": ("chem.pdf", "0620_s24_ms_41__1_.pdf"),
    }
    assert plan.unpaired == []


def test_files_it_cannot_pair_are_reported_with_the_reason(tmp_path):
    _qp(tmp_path / "lonely_qp.pdf", code="0620/42")
    _ms(tmp_path / "lonely_ms.pdf", code="0620/43")
    _qp(tmp_path / "copy_a.pdf")
    _qp(tmp_path / "copy_b.pdf")
    _ms(tmp_path / "ms.pdf")
    (tmp_path / "not_a_pdf.pdf").write_bytes(b"this is not a PDF")

    plan = plan_scan(tmp_path)
    reasons = {u.path.name: u.reason for u in plan.unpaired}

    assert [p.paper_key for p in plan.pairs] == ["0580_22_FM_25"]
    assert "its mark scheme isn't in the folder" in reasons["lonely_qp.pdf"]
    assert "its question paper isn't in the folder" in reasons["lonely_ms.pdf"]
    assert "second copy" in reasons["copy_b.pdf"]
    assert "couldn't be opened" in reasons["not_a_pdf.pdf"]


def test_pairing_reports_its_progress(tmp_path):
    # 1000 PDFs take ~30s to pair: the scan shows "n of total" so it's seen to move
    _qp(tmp_path / "qp.pdf")
    _ms(tmp_path / "ms.pdf")
    seen = []

    plan_scan(tmp_path, progress=lambda done, total: seen.append((done, total)))

    assert seen == [(0, 2), (1, 2), (2, 2)]


def test_a_rescan_reads_only_new_or_changed_files(tmp_path, monkeypatch):
    # user-reported: pairing 2,000 PDFs took minutes on every scan
    from qbx import scan

    papers = tmp_path / "papers"
    papers.mkdir()
    _qp(papers / "qp.pdf")
    _ms(papers / "ms.pdf")
    cache = tmp_path / "pairing.json"
    first = plan_scan(papers, cache_path=cache)

    read = []
    real = scan._read_file
    monkeypatch.setattr(scan, "_read_file", lambda path: read.append(path.name) or real(path))
    assert plan_scan(papers, cache_path=cache) == first
    assert read == []
    _qp(papers / "qp2.pdf", code="0620/41", sitting="May/June 2024")
    plan_scan(papers, cache_path=cache)
    assert read == ["qp2.pdf"]
