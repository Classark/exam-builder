"""Debug and release builds (qbx/edition.py, user-asked): a debug build has Claude Code and
the Settings screen, for testing; a release build has neither, and until Classark accounts
exist, no Claude at all."""

import sys

import pytest
from fastapi.testclient import TestClient

from qbx import edition
from qbx import llm as llm_mod


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv("QBX_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("QBX_QUIZZES_DIR", str(tmp_path / "quizzes"))
    monkeypatch.setattr(llm_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(edition, "EDITION_FILE", tmp_path / "edition.txt")
    monkeypatch.delenv("QBX_EDITION", raising=False)
    llm_mod.set_llm_override(None)
    sys.modules.pop("main", None)
    import main

    return main


def test_running_from_source_is_debug_and_the_build_file_or_env_decides_otherwise(tmp_path, monkeypatch):
    monkeypatch.setattr(edition, "EDITION_FILE", tmp_path / "edition.txt")
    monkeypatch.delenv("QBX_EDITION", raising=False)
    assert edition.edition() == "debug" and edition.claude_available()
    (tmp_path / "edition.txt").write_text("release\n", encoding="utf-8")
    assert edition.is_release() and not edition.claude_available()
    monkeypatch.setenv("QBX_EDITION", "debug")
    assert edition.edition() == "debug"
    monkeypatch.setenv("QBX_EDITION", "nonsense")
    assert edition.edition() == "debug"


def test_a_debug_build_has_settings_and_claude(backend):
    client = TestClient(backend.app)
    state = client.get("/api/state").json()
    assert state["edition"] == "debug" and state["ai"] is True
    assert client.get("/api/settings").status_code == 200
    client.put("/api/settings", json={"enabled": True, "provider": "claude_code"})
    assert isinstance(llm_mod.get_llm("tag"), llm_mod.ClaudeLLM)


def test_a_release_build_has_no_settings_and_no_claude(backend, monkeypatch):
    client = TestClient(backend.app)
    llm_mod.save_settings(llm_mod.Settings(enabled=True, provider="claude_code"))
    monkeypatch.setenv("QBX_EDITION", "release")

    state = client.get("/api/state").json()
    assert state["edition"] == "release" and state["ai"] is False
    for method, url in [
        ("get", "/api/settings"), ("put", "/api/settings"), ("post", "/api/settings/check"),
        ("put", "/api/settings/api_key"), ("delete", "/api/settings/api_key"),
    ]:
        assert getattr(client, method)(url, **({"json": {}} if method in ("put",) else {})).status_code == 404
    # even with Claude Code switched on in a settings file left behind, nothing reaches it
    assert llm_mod.get_llm("tag") is None
    reply = client.post("/api/exam/from_request", json={"text": "10 marks on electrolysis"})
    assert reply.status_code == 400 and "isn't available in this version" in reply.json()["detail"]
