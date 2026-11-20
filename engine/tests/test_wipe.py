"""Settings' "Delete all my data" (qbx/wipe.py)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qbx import wipe


@pytest.fixture
def app_data(tmp_path, monkeypatch):
    """A packaged app's data folder with work in it, and saved PDFs elsewhere."""
    folder = tmp_path / "Library" / "Application Support" / "ClassarkExamBuilder"
    for name in ("papers/0620_s24_qp_41.pdf", "out/0620_41_MJ_24/paper.json", "quizzes/Exam 1.json",
                 "config/settings.json", "updates/1.0.1/x", "app.log", "webview/Local Storage/x"):
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(name, encoding="utf-8")
    saved = tmp_path / "Documents" / "Classark Exam Builder" / "Exam 1" / "Exam 1 - Question paper.pdf"
    saved.parent.mkdir(parents=True)
    saved.write_text("pdf", encoding="utf-8")
    quits, removers, forgotten = [], [], []
    monkeypatch.setattr(wipe, "_remove_after_exit", removers.append)
    monkeypatch.setattr(wipe, "_forget_credentials", lambda: forgotten.append(True))
    monkeypatch.setattr(wipe.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: fn()})())
    wipe.set_app(folder, lambda: quits.append(True))
    yield folder, saved, quits, removers, forgotten
    monkeypatch.setattr(wipe, "_folder", None)


def test_deletes_the_teachers_data_signs_out_and_closes(app_data):
    folder, saved, quits, removers, forgotten = app_data
    wipe.delete_all()
    # what the running app holds open is left to the process that waits for it to quit
    assert sorted(p.name for p in folder.iterdir()) == ["app.log", "webview"]
    assert removers == [folder.resolve()]
    assert forgotten and quits
    assert saved.read_text(encoding="utf-8") == "pdf"  # the teacher's saved PDFs stay


@pytest.mark.parametrize(
    "folder",
    ["papers", "PycharmProjects/QBX", "ClassarkExamBuilder-empty"],
    ids=["not the app's folder name", "a repository", "nothing of the app's in it"],
)
def test_never_deletes_a_folder_that_isnt_the_apps(tmp_path, monkeypatch, folder):
    target = tmp_path / folder
    (target / ("out" if "empty" not in folder else "other")).mkdir(parents=True)
    monkeypatch.setattr(wipe, "_folder", None)
    wipe.set_app(target, lambda: None)
    try:
        assert not wipe.available()
        with pytest.raises(wipe.WipeError):
            wipe.delete_all()
        assert any(target.iterdir())
    finally:
        monkeypatch.setattr(wipe, "_folder", None)


def test_running_from_source_offers_nothing(monkeypatch):
    monkeypatch.setattr(wipe, "_folder", None)
    assert not wipe.available()


def test_the_route_needs_the_typed_word(app_data):
    import main

    folder = app_data[0]
    client = TestClient(main.app)
    assert client.get("/api/state").json()["can_delete_data"] is True
    assert client.post("/api/data/delete_all", json={"confirm": "yes"}).status_code == 400
    assert (folder / "papers").exists()
    assert client.post("/api/data/delete_all", json={"confirm": "Delete"}).json() == {"ok": True}
    assert not (folder / "papers").exists()
