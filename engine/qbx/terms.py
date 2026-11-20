"""Agreeing to Classark Exam Builder's terms (user-asked): the app asks once, before anything
else, and again whenever the terms change.

The terms are the website's (classark.com/exam-builder/terms, with the privacy policy and
refund policy beside them); they cover the Classark service and account. The program itself
stays under the AGPL, which no agreement can narrow (its section 10), so the prompt links the
licence rather than restating it.

VERSION is the terms' "last updated" date, kept equal to the site's EB_LEGAL_UPDATED
(theplatform-site, pages/outsider/examBuilder/legal/shared.js): change both together, and
every computer asks again. What was accepted, and when, is kept in preferences.json.
"""

from __future__ import annotations

import datetime

from qbx import saving

VERSION = "8 November 2026"

# the website's pages, opened in the browser from the prompt (account.open_web)
PAGES = {
    "terms": "/exam-builder/terms",
    "privacy": "/exam-builder/privacy",
    "refunds": "/exam-builder/refunds",
    "licence": "/exam-builder/licence",
}


def status() -> dict:
    prefs = saving._read_json(saving._prefs_path(), {})
    return {"version": VERSION, "accepted": prefs.get("terms_accepted") == VERSION}


def accept(version: str) -> dict:
    """Records the teacher's agreement - only to the version the prompt showed."""
    if version != VERSION:
        raise ValueError("The terms have changed since they were shown - read them again.")
    path = saving._prefs_path()
    prefs = saving._read_json(path, {})
    prefs.update(terms_accepted=VERSION, terms_accepted_at=datetime.datetime.now().astimezone().isoformat())
    saving._write_json(path, prefs)
    return status()
