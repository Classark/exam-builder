"""Saving to the teacher's folder (qbx/saving.py, user-decided): Documents\\Classark Exam
Builder by default, chosen once after sign-in, one folder per exam, and a changed exam's
last save moved to Old versions."""

import pytest

from qbx import llm as llm_mod
from qbx import saving


@pytest.fixture
def save_root(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(saving, "documents_folder", lambda: tmp_path / "Documents")
    opened = []
    saving.set_opener(opened.append)
    yield tmp_path, opened
    saving.set_opener(saving._default_open)


def test_the_folder_defaults_to_documents_until_chosen(save_root):
    tmp, _ = save_root
    state = saving.status()
    assert state["folder"] == str(tmp / "Documents" / "Classark Exam Builder") and not state["chosen"]
    state = saving.set_folder(str(tmp / "Exams"))
    assert state["chosen"] and state["folder"] == str(tmp / "Exams") and (tmp / "Exams").is_dir()
    with pytest.raises(saving.SaveError):
        saving.set_folder("relative/folder")


def test_a_changed_exam_moves_its_last_save_to_old_versions(save_root):
    tmp, _ = save_root
    exam = tmp / "Documents" / "Classark Exam Builder" / "Year 10 test"
    first = saving.save_files("exam", "Year 10 test", None, {"Year 10 test - Question paper.pdf": b"v1"}, "a")
    assert first["archived"] is None and (exam / "Year 10 test - Question paper.pdf").read_bytes() == b"v1"
    # the same exam's mark scheme joins it; saving it again unchanged replaces in place
    saving.save_files("exam", "Year 10 test", None, {"Year 10 test - Mark scheme.pdf": b"ms1"}, "a")
    saving.save_files("exam", "Year 10 test", None, {"Year 10 test - Question paper.pdf": b"v1b"}, "a")
    assert not (exam / "Old versions").exists()

    (exam / "my notes.txt").write_text("the teacher's own")
    second = saving.save_files("exam", "Year 10 test", None, {"Year 10 test - Question paper.pdf": b"v2"}, "b")
    assert second["archived"].startswith("Version 1 (") and second["version"] == 2
    old = exam / "Old versions" / second["archived"]
    assert sorted(p.name for p in old.iterdir()) == ["Year 10 test - Mark scheme.pdf", "Year 10 test - Question paper.pdf"]
    assert (old / "Year 10 test - Question paper.pdf").read_bytes() == b"v1b"
    assert (exam / "Year 10 test - Question paper.pdf").read_bytes() == b"v2"
    assert (exam / "my notes.txt").exists()  # never moves what the app didn't write

    third = saving.save_files("exam", "Year 10 test", None, {"Year 10 test - Question paper.pdf": b"v3"}, "c")
    assert third["archived"].startswith("Version 2 (")


def test_names_windows_refuses_are_made_safe():
    assert saving.safe_name('Unit 3: "acids"/bases?') == "Unit 3 acids bases"
    assert saving.safe_name("CON") == "Exam CON" and saving.safe_name("  ...  ") == "Exam"


def test_only_saved_files_are_opened(save_root):
    tmp, opened = save_root
    saved = saving.save_files("exam", "Quiz", None, {"Quiz - Mark scheme.pdf": b"x"}, "a")
    path = saved["files"][0]["path"]
    assert saving.recent()[0]["path"] == path and saving.recent()[0]["exists"]
    saving.open_saved(path)
    assert opened == [path]
    with pytest.raises(saving.SaveError, match="isn't one the app saved"):
        saving.open_saved(str(tmp / "elsewhere.exe"))
