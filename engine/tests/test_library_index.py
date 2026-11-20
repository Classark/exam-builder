"""The library index never keeps a request waiting (user-reported: after an index format
change the first /api/state rebuilt every paper inside the request, ~40s with no paper list
and nothing said, and the app looked dead)."""

import time

from qbx import library
from qbx.library import Library
from tests.test_main import _make_paper, _make_source_pdf, _write_paper


def _papers(tmp_path, n):
    source = tmp_path / "source.pdf"
    _make_source_pdf(source)
    for i in range(n):
        paper = _make_paper(source).model_copy(update={"paper_key": f"TEST_{i}"})
        _write_paper(tmp_path, paper)


def _wait(lib, seconds=20.0):
    deadline = time.monotonic() + seconds
    while lib._refreshing.locked() and time.monotonic() < deadline:
        time.sleep(0.02)


def test_a_few_papers_are_read_inline(tmp_path):
    _papers(tmp_path, 3)
    lib = Library(tmp_path)
    assert lib.refresh()
    assert len(lib.summaries()) == 3 and lib.progress() is None


def test_many_papers_are_read_in_the_background_and_published_as_they_go(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "INLINE_LIMIT", 2)
    monkeypatch.setattr(library, "PUBLISH_EVERY", 0.0)
    real = library.build_entry

    def slow(paper_dir):
        time.sleep(0.05)
        return real(paper_dir)

    monkeypatch.setattr(library, "build_entry", slow)
    _papers(tmp_path, 8)
    lib = Library(tmp_path)
    started = time.monotonic()
    lib.refresh()
    assert time.monotonic() - started < 0.3  # returned at once: the reading goes on behind it
    assert lib.refresh() is False  # a second refresh never waits on the first
    seen = set()
    while lib._refreshing.locked():
        seen.add(len(lib.summaries()))
        progress = lib.progress()
        assert progress is None or progress["total"] == 8
        time.sleep(0.01)
    assert len(lib.summaries()) == 8
    assert len(seen - {0, 8}) > 0  # papers appeared while the rest were still being read
    assert lib.progress() is None
    # and saved: a fresh start reads the index, nothing to rebuild
    again = Library(tmp_path)
    monkeypatch.setattr(library, "build_entry", lambda d: (_ for _ in ()).throw(AssertionError("rebuilt")))
    again.refresh()
    assert len(again.summaries()) == 8


def test_a_paper_written_during_a_rebuild_is_read_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "INLINE_LIMIT", 2)
    real = library.build_entry
    monkeypatch.setattr(library, "build_entry", lambda d: (time.sleep(0.05), real(d))[1])
    _papers(tmp_path, 6)
    lib = Library(tmp_path)
    lib.refresh()
    source = tmp_path / "source.pdf"
    _write_paper(tmp_path, _make_paper(source).model_copy(update={"paper_key": "NEW_1"}))
    lib.invalidate("NEW_1")
    assert lib.summary("NEW_1") is not None
    _wait(lib)
    assert lib.summary("NEW_1") is not None and len(lib.summaries()) == 7
