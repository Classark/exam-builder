import json

from qbx import app_version


def test_versions_compare_by_number():
    assert app_version.older("0.1.9", "0.1.10")
    assert not app_version.older("0.2.0", "0.1.10")
    assert not app_version.older("1.0", "1.0.0")


def test_an_older_copy_must_update(monkeypatch, tmp_path):
    doc = tmp_path / "app.json"
    doc.write_text(json.dumps({"fields": {
        "minExamBuilderVersion": {"stringValue": "99.0.0"},
        "examBuilderUpdateUrl": {"stringValue": "https://apps.microsoft.com/x"},
    }}))
    monkeypatch.setenv("QBX_CONFIG_URL", doc.as_uri())
    app_version._fetch()
    status = app_version.status()
    assert status["required"] and status["minimum"] == "99.0.0" and status["url"] == "https://apps.microsoft.com/x"


def test_no_answer_means_no_minimum(monkeypatch):
    monkeypatch.setenv("QBX_CONFIG_URL", "http://127.0.0.1:9/nothing")
    monkeypatch.setitem(app_version._state, "minimum", None)
    app_version._fetch()
    assert not app_version.status()["required"]


def test_a_mac_gets_its_own_download_link_when_one_is_set(monkeypatch):
    from qbx import app_version

    monkeypatch.setattr(app_version, "_state", {"checked_at": 1e18, "minimum": "2.0.0", "url": "https://apps.microsoft.com/x",
                                                "mac_url": "https://classark.com/exam-builder/mac", "mac_version": None})
    monkeypatch.setattr(app_version.sys, "platform", "darwin")
    assert app_version.status()["url"] == "https://classark.com/exam-builder/mac"
    monkeypatch.setattr(app_version.sys, "platform", "win32")
    assert app_version.status()["url"] == "https://apps.microsoft.com/x"
    app_version._state["mac_url"] = None
    monkeypatch.setattr(app_version.sys, "platform", "darwin")
    assert app_version.status()["url"] == "https://apps.microsoft.com/x"
