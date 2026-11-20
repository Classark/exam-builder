import pytest

from qbx import llm as llm_mod
from qbx import terms


def test_terms_are_asked_until_agreed_and_again_when_they_change(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_mod, "CONFIG_DIR", tmp_path / "config")
    assert terms.status() == {"version": terms.VERSION, "accepted": False}

    with pytest.raises(ValueError):  # only the version the prompt showed
        terms.accept("1 January 2020")
    assert terms.accept(terms.VERSION)["accepted"]
    assert terms.status()["accepted"]

    monkeypatch.setattr(terms, "VERSION", "1 December 2026")  # the terms were updated
    assert not terms.status()["accepted"]
