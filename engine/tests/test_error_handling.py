"""Errors read like a finished app's (user-asked): a sentence for the teacher, the details
in app.log under a reference the sentence gives."""

import logging

from fastapi.testclient import TestClient

import main as backend


def test_an_unexpected_error_is_a_sentence_and_logged(caplog):
    def boom():
        raise KeyError("question_number")

    backend.app.add_api_route("/api/_test_boom", boom)
    # ahead of the UI's static files, which answer any path they're reached with
    backend.app.router.routes.insert(0, backend.app.router.routes.pop())

    try:
        client = TestClient(backend.app, raise_server_exceptions=False)
        with caplog.at_level(logging.ERROR, logger="qbx.app"):
            response = client.get("/api/_test_boom")
    finally:
        backend.app.router.routes = [r for r in backend.app.router.routes if getattr(r, "path", "") != "/api/_test_boom"]
    assert response.status_code == 500
    body = response.json()
    assert "Something went wrong" in body["detail"] and body["error_id"] in body["detail"]
    assert "KeyError" not in body["detail"]
    # the traceback is in the log, under the same reference
    assert any(body["error_id"] in r.getMessage() and r.exc_info for r in caplog.records)


def test_a_ui_crash_is_logged(caplog):
    with caplog.at_level(logging.ERROR, logger="qbx.app"):
        response = TestClient(backend.app).post(
            "/api/client_error", json={"message": "x is undefined", "stack": "at Bank", "where": "/"}
        )
    assert response.status_code == 200
    assert any("x is undefined" in r.getMessage() for r in caplog.records)


def test_a_failed_job_says_what_stopped_and_why(caplog):
    with caplog.at_level(logging.ERROR, logger="qbx.app"):
        locked = backend._job_error("The scan", PermissionError(13, "denied", "paper.json"))
        bug = backend._job_error("Tagging", ValueError("bad"))
    assert locked.startswith("The scan stopped:") and "open in another program" in locked
    assert bug.startswith("Tagging stopped:") and "app's log" in bug and "ValueError" not in bug
    assert len([r for r in caplog.records if r.exc_info]) == 2
