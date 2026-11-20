"""Reviewer backend (phase 7).

Serves extracted papers from OUT_DIR (default: ../out relative to this file, i.e. the
repo-root out/ that `qbx extract` writes to) to the React reviewer in ui/, and accepts
corrections that get written straight back into that paper's paper.json - no auth, no
database, matching CLAUDE.md's "no persistence layer" design.

    uv run uvicorn main:app --reload
"""

import asyncio
import base64
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

import pymupdf
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from qbx import llm as llm_mod
from qbx.classified import (
    ClassifiedSpec, TopicGroup, classify, in_scope, paper_label, parts_label, sitting_label, untagged_parts,
)
from qbx import account, api_key, app_version, batch_tagging, difficulty, edition, legal, mac_update, ms_links, saving, terms
from qbx import fixes as fixes_mod
from qbx.fixes import Fix, FixError, Step
from qbx.exam_builder import ExamSource, ExamSpec, build_preferring_new
from qbx.exam_adjust import apply_adjustment, exam_items, read_adjustment
from qbx.exam_chat import ChatMessage, read_turn
from qbx.exam_request import read_request
from qbx.fingerprint import FingerprintError, read_cover_titles
from qbx.library import Library, issue_key, read_reviewed
from qbx.markscheme_edexcel import scheme_layout
from qbx.pairing import PairingError
from qbx.pipeline import ScannedPdfError, apply_hand_links, run_extraction
from qbx.quiz_export import QuizGroup, compose_ms_pdf, compose_qp_pdf, compose_sources_pdf, is_multiple_choice
from qbx.scan import extract_pair, lower_priority, plan_scan, scan_workers
from qbx.snap import snap_segments
from qbx.schema import BBox, Paper
from qbx.tagging import merge_tags, question_needs_tagging, read_tags, tag_key, tag_questions, write_tags
from qbx.topics import Topic, load_topics, read_syllabus, save_topics, syllabus_pages
from qbx.validate import validate_paper

OUT_DIR = Path(os.environ.get("QBX_OUT_DIR", Path(__file__).parent.parent / "out")).resolve()
# Uploaded QP/MS PDFs land here - the same directory `qbx extract` reads real papers from -
# so an uploaded paper's source_qp_path stays valid for the page-image endpoint below.
PAPERS_DIR = Path(os.environ.get("QBX_PAPERS_DIR", Path(__file__).parent.parent / "papers")).resolve()
# User-curated quizzes, kept separate from OUT_DIR since re-running extraction can
# regenerate a paper's folder but must never touch what someone has built from it.
QUIZZES_DIR = Path(os.environ.get("QBX_QUIZZES_DIR", Path(__file__).parent.parent / "quizzes")).resolve()
RENDER_DPI = 150
# every paper in out/, indexed once and kept current - see qbx/library.py
LIBRARY = Library(OUT_DIR)
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")

_log = logging.getLogger("qbx.app")

# Requests being answered right now, for spotting one that never finishes. The app's window
# opens at most 6 connections to the server, so 6 stuck requests stop every later one -
# status polls included - and the app says its engine isn't answering while the engine
# answers anything else at once (user-reported twice during "Re-process papers with
# issues"; a fresh connection got /api/state in 0.01s).
STUCK_REQUEST_SECONDS = 15.0
_in_flight: dict[int, tuple[str, float]] = {}
_in_flight_lock = threading.Lock()
_in_flight_ids = iter(range(1, 1 << 62))


class _TrackRequests:
    """Plain ASGI middleware (not BaseHTTPMiddleware, which buffers streamed responses)."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        with _in_flight_lock:
            request_id = next(_in_flight_ids)
            _in_flight[request_id] = (f"{scope['method']} {scope['path']}", time.monotonic())
        try:
            await self.inner(scope, receive, send)
        finally:
            with _in_flight_lock:
                _in_flight.pop(request_id, None)


def stuck_requests(older_than: float = STUCK_REQUEST_SECONDS) -> list[dict]:
    now = time.monotonic()
    with _in_flight_lock:
        return [
            {"request": what, "seconds": round(now - started, 1)}
            for what, started in _in_flight.values()
            if now - started >= older_than
        ]


def _watch_stuck_requests() -> None:
    """Logs each request still running after STUCK_REQUEST_SECONDS, once, with every
    thread's stack - what it waits on is the answer to "why did the app stop answering"."""
    import traceback

    reported: set[int] = set()
    while True:
        time.sleep(5)
        now = time.monotonic()
        with _in_flight_lock:
            stuck = {
                i: (what, now - started)
                for i, (what, started) in _in_flight.items()
                if now - started >= STUCK_REQUEST_SECONDS and i not in reported
            }
            reported &= set(_in_flight)
        if not stuck:
            continue
        reported |= set(stuck)
        names = {t.ident: t.name for t in threading.enumerate()}
        stacks = "".join(
            f"--- thread {names.get(ident, ident)}\n" + "".join(traceback.format_stack(frame)[-15:])
            for ident, frame in sys._current_frames().items()
        )
        _log.error(
            "requests still running: %s\n%s",
            "; ".join(f"{what} ({seconds:.0f}s)" for what, seconds in stuck.values()),
            stacks,
        )


threading.Thread(target=_watch_stuck_requests, daemon=True, name="stuck-requests").start()


app = FastAPI(title="QBX Reviewer")
app.add_middleware(_TrackRequests)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)



@app.exception_handler(Exception)
async def _unexpected_error(request: Request, error: Exception) -> JSONResponse:
    """Anything a route didn't expect: the whole traceback to app.log, and a sentence for
    the teacher - never Starlette's bare "Internal Server Error" (user-asked: errors should
    read like a finished app's). `error_id` ties what the teacher sees to the log line."""
    error_id = uuid.uuid4().hex[:8]
    _log.error("unexpected error %s on %s %s", error_id, request.method, request.url.path, exc_info=error)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Something went wrong on our side. Please try again - if it keeps happening, "
            f"the details are saved in the app's log (reference {error_id}).",
            "error_id": error_id,
        },
    )


def _job_error(what: str, error: BaseException) -> str:
    """A background job's failure as a sentence for the teacher, its traceback to app.log
    under a reference the sentence gives. `what` names the job: "The scan"."""
    error_id = uuid.uuid4().hex[:8]
    _log.error("%s failed (%s)", what, error_id, exc_info=error)
    if isinstance(error, PermissionError):
        reason = "a file it needed is open in another program or can't be written to - close it and try again"
    elif isinstance(error, OSError) and getattr(error, "errno", None) == 28:
        reason = "the disk is full"
    elif isinstance(error, MemoryError):
        reason = "the computer ran out of memory - close other programs and try again"
    else:
        reason = f"something went wrong (reference {error_id} in the app's log)"
    return f"{what} stopped: {reason}."


def _log_thread_crash(args: threading.ExceptHookArgs) -> None:
    """A background thread that dies of an error nothing caught: logged, not lost."""
    if args.exc_type is not SystemExit:
        _log.error("thread %s crashed", getattr(args.thread, "name", "?"), exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


threading.excepthook = _log_thread_crash


class ClientError(BaseModel):
    message: str = ""
    stack: str = ""
    component_stack: str = ""
    where: str = ""


@app.post("/api/client_error")
def log_client_error(report: ClientError) -> dict:
    """A crash in the UI itself (its error screen sends it), so app.log has both halves."""
    error_id = uuid.uuid4().hex[:8]
    _log.error(
        "UI error %s at %s: %s\n%s\n%s",
        error_id, report.where[:200], report.message[:2000], report.stack[:8000], report.component_stack[:8000],
    )
    return {"error_id": error_id}


def _paper_dir(paper_key: str) -> Path:
    paper_dir = OUT_DIR / paper_key
    if not (paper_dir / "paper.json").exists():
        raise HTTPException(status_code=404, detail=f"no such paper: {paper_key}")
    return paper_dir


def _load_paper(paper_key: str) -> Paper:
    return Paper.model_validate_json((_paper_dir(paper_key) / "paper.json").read_text(encoding="utf-8"))


def _save_paper(paper_key: str, paper: Paper) -> None:
    paper_dir = _paper_dir(paper_key)
    (paper_dir / "paper.json").write_text(paper.model_dump_json(indent=2), encoding="utf-8")
    report = validate_paper(paper)
    (paper_dir / "report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    LIBRARY.invalidate(paper_key)


def _find_part(paper: Paper, part_id: str):
    for question in paper.questions:
        for i, part in enumerate(question.parts):
            if part.id == part_id:
                return question, i, part
    return None, None, None


@app.get("/api/papers")
def list_papers() -> list[dict]:
    LIBRARY.refresh()
    return LIBRARY.summaries()


@app.get("/api/questions")
def list_questions(q: str | None = None, subject: str | None = None) -> list[dict]:
    """Every extracted question (with its parts nested), across all papers, for
    browsing/searching when building a quiz. Question-level, not part-level: selection
    starts at "Question 1", then narrows to specific parts within it (see QuestionBank.tsx),
    matching how a real paper is picked apart rather than a flat list of loose parts.
    """
    results = []
    for paper_dir in sorted(OUT_DIR.glob("*")):
        if not (paper_dir / "paper.json").exists():
            continue
        paper = Paper.model_validate_json((paper_dir / "paper.json").read_text(encoding="utf-8"))
        if subject and paper.subject != subject:
            continue
        for question in paper.questions:
            if q:
                haystack = question.stem_text.lower() + " " + " ".join(p.text for p in question.parts).lower()
                if q.lower() not in haystack:
                    continue
            subject_name, paper_title = _cover_titles(paper)
            results.append(
                {
                    "paper_key": paper.paper_key,
                    "board": paper.board,
                    "subject": paper.subject,
                    # what the question bank's folders are built from: subject -> year ->
                    # session -> paper (e.g. 0620 Chemistry / 2024 / May/June / 41)
                    "subject_name": subject_name,
                    "level": paper.level,
                    "year": paper.year,
                    "session": paper.session,
                    "paper": paper.paper,
                    "variant": paper.variant,
                    "paper_title": paper_title,
                    "question_number": question.question_number,
                    "stem_text": question.stem_text,
                    "stem_image_path": question.stem_image_path,
                    "total_marks": question.total_marks,
                    "parts": [
                        {"path": p.path, "marks": p.marks, "text": p.text, "image_path": p.image_path}
                        for p in question.parts
                    ],
                }
            )
    return results


@app.get("/api/bank")
def get_bank() -> dict:
    """Every paper with its questions in brief (number, marks, a snippet) - the question
    bank's folders. About a tenth of /api/questions' size: part texts load per question."""
    LIBRARY.refresh()
    return {"version": LIBRARY.version, "papers": LIBRARY.bank()}


@app.get("/api/search")
def search_questions(q: str, subject: str | None = None) -> list[dict]:
    """(paper_key, question_number) of every question whose text contains `q`."""
    LIBRARY.refresh()
    return LIBRARY.search(q, subject)


@app.get("/api/papers/{paper_key}/questions/{question_number}")
def get_question(paper_key: str, question_number: int) -> dict:
    paper = _load_paper(paper_key)
    question = next((x for x in paper.questions if x.question_number == question_number), None)
    if question is None:
        raise HTTPException(status_code=404, detail=f"no question {question_number} in {paper_key}")
    tags = read_tags(_paper_dir(paper_key))
    return {
        "paper_key": paper_key,
        "question_number": question.question_number,
        "stem_text": question.stem_text,
        "total_marks": question.total_marks,
        "parts": [
            {"path": p.path, "marks": p.marks, "text": p.text, "tags": tags.get(tag_key(question.question_number, p.path))}
            for p in question.parts
        ],
    }


_state_checked_at = 0.0


STATE_REFRESH_SECONDS = 10.0


@app.get("/api/state")
def get_state() -> dict:
    """What the UI polls: `version` changes whenever any paper is added, re-extracted or
    edited, and `scan` reports a running scan's progress. Cheap - one stat per paper, at
    most once a second."""
    global _state_checked_at
    # Everything this app writes updates the library itself (LIBRARY.invalidate); this
    # check only finds what changed out/ from outside it, and costs ~0.4s over 2,000+
    # papers - once a second, that was the app's steady load while it sat open.
    if time.monotonic() - _state_checked_at > STATE_REFRESH_SECONDS:
        _state_checked_at = time.monotonic()
        LIBRARY.refresh()
    _resume_batch_tagging()
    _start_watching()
    with _scan_lock:
        scan = json.loads(json.dumps(_scan_state))
    with _tag_lock:
        tagging = json.loads(json.dumps(_tag_state))
    with _classified_lock:  # classified booklets being composed (Classification mode)
        classified = dict(_classified_state)
    return {
        "version": LIBRARY.version, "n_papers": len(LIBRARY.summaries()),
        # papers being read into the library in the background ({done, total}, or None) -
        # the lists fill in as it goes (qbx/library.py, INLINE_LIMIT)
        "library": LIBRARY.progress(),
        "scan": scan, "tagging": tagging, "classified": classified,
        # papers with parts not yet tagged: the top bar's "Tag n papers", pressed by the teacher
        "untagged_papers": sum(1 for s in LIBRARY.summaries() if s["n_tagged"] < s["n_parts"]),
        # debug or release build, and whether Claude can be reached at all (qbx/edition.py)
        "edition": edition.edition(), "ai": edition.claude_available(),
        "account": account.status(), "app_version": legal.version(),
        # the oldest version Classark still allows (qbx/app_version.py): a release build
        # older than it shows "Please update" and nothing else
        "update": app_version.status(),
        # a Mac copy updating itself (qbx/mac_update.py): downloading, ready to restart into,
        # or to be downloaded by hand - None on Windows (the Store updates it) and in debug
        "app_update": mac_update.status(),
        # requests running for 15s+ (normally none): what a stuck window is waiting on
        "stuck_requests": stuck_requests(),
    }


# -- About & licences (qbx/legal.py: the app is AGPL-3.0, its source ships inside it) ------


@app.get("/api/about")
def get_about() -> dict:
    return {
        "product": legal.PRODUCT, "version": legal.version(), "edition": edition.edition(),
        "copyright": legal.COPYRIGHT, "license": legal.LICENSE_NAME, "notice": legal.NOTICE_BODY,
    }


@app.get("/api/about/license")
def get_license() -> Response:
    return Response(content=legal.license_text(), media_type="text/plain; charset=utf-8")


@app.get("/api/about/third_party")
def get_third_party() -> Response:
    return Response(content=legal.third_party_text(), media_type="text/plain; charset=utf-8")


@app.get("/api/about/source.zip")
def get_source_zip() -> Response:
    """The complete source of this version (AGPL section 6)."""
    data, name = legal.source_zip()
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# -- saving to the teacher's folder (qbx/saving.py) ------------------------------------------


def _saved(save) -> dict:
    try:
        return save()
    except saving.SaveError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@app.get("/api/saving")
def saving_status() -> dict:
    return saving.status()


@app.put("/api/saving")
def saving_set_folder(body: dict) -> dict:
    """The teacher's save folder: chosen once after signing in, changeable in Settings."""
    return _saved(lambda: saving.set_folder(str(body.get("folder") or "")))


@app.post("/api/saving/pick")
def saving_pick_folder() -> dict:
    """The app window's folder dialog; {"folder": null} if cancelled or there's no window."""
    return {"folder": saving.pick_folder()}


@app.get("/api/saved")
def saved_files() -> list[dict]:
    return saving.recent()


@app.post("/api/saved/open")
def saved_open(body: dict) -> dict:
    _saved(lambda: saving.open_saved(str(body.get("path") or "")))
    return {"ok": True}


@app.post("/api/saved/show")
def saved_show(body: dict) -> dict:
    _saved(lambda: saving.show_in_folder(str(body.get("path") or "")))
    return {"ok": True}


# -- the Classark account (qbx/account.py) -------------------------------------------------


@app.post("/api/account/sign_in")
def account_sign_in() -> dict:
    """Opens the browser at classark.com's sign-in page; /api/state's `account` follows it."""
    return account.start_sign_in()


@app.post("/api/account/cancel")
def account_cancel() -> dict:
    account.cancel_sign_in()
    return account.status()


@app.post("/api/account/sign_out")
def account_sign_out() -> dict:
    account.sign_out()
    return account.status()


@app.get("/api/account/balance")
def account_balance() -> dict:
    """The teacher's Claude credit, from the Classark server (amounts in micro-USD)."""
    try:
        return account.balance()
    except account.AccountError as e:
        raise HTTPException(status_code=401 if e.ended else 502, detail=str(e)) from e


@app.post("/api/account/open/{page}")
def account_open(page: str) -> dict:
    """Opens a Classark web page in the browser: the subscription or the credit top-up."""
    paths = {"subscription": "/exam-builder/account", "credit": "/exam-builder/account/credit", **terms.PAGES}
    if page not in paths:
        raise HTTPException(status_code=404, detail="No such page.")
    account.open_web(paths[page])
    return {"url": account.web_url(paths[page])}


@app.post("/api/update/open")
def update_open() -> dict:
    """Opens where the update is (config/app's examBuilderUpdateUrl), outside the app."""
    url = app_version.status()["url"]
    if not url or not url.startswith("https://"):
        raise HTTPException(status_code=404, detail="No update link is set.")
    account.open_url(url)
    return {"url": url}


@app.post("/api/update/install")
def update_install() -> dict:
    """Restart into the downloaded, checked update (Mac only, qbx/mac_update.py): the app
    closes, swaps itself for the new version and opens again. The teacher's papers, exams
    and settings aren't in the app and stay as they are."""
    if _scan_state.get("running") or _tag_state.get("running"):
        raise HTTPException(status_code=409, detail="Wait for the scan or tagging run to finish, then update.")
    try:
        mac_update.install()
    except mac_update.UpdateError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"ok": True}


@app.get("/api/terms")
def terms_status() -> dict:
    """Whether this computer has agreed to the current terms (qbx/terms.py)."""
    return terms.status()


@app.post("/api/terms/accept")
def terms_accept(body: dict) -> dict:
    try:
        return terms.accept(str(body.get("version") or ""))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


_cover_titles_cache: dict[str, tuple[str, str]] = {}


def _cover_titles(paper: Paper) -> tuple[str, str]:
    """(subject name, paper title) as the cover prints them. Recorded at extraction since
    these fields were added; for a paper extracted earlier, read once from its source PDF
    rather than asking for everything to be re-extracted."""
    if paper.subject_name:
        return paper.subject_name, paper.paper_title
    if paper.paper_key not in _cover_titles_cache:
        titles = ("", "")
        source = Path(paper.source_qp_path)
        if source.exists():
            with pymupdf.open(source) as doc:
                titles = read_cover_titles(doc[0].get_text("text"), paper.subject, paper.paper, paper.variant)
        _cover_titles_cache[paper.paper_key] = titles
    return _cover_titles_cache[paper.paper_key]


def _safe_upload_path(filename: str) -> Path:
    """Sanitises the uploaded filename and avoids clobbering an existing file of the same
    name (a different paper the reviewer already extracted)."""
    stem = _UNSAFE_FILENAME_RE.sub("_", Path(filename).stem) or "upload"
    suffix = Path(filename).suffix or ".pdf"
    target = PAPERS_DIR / f"{stem}{suffix}"
    counter = 1
    while target.exists():
        target = PAPERS_DIR / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


@app.post("/api/papers/upload")
async def upload_paper(
    qp_file: UploadFile = File(...), ms_file: UploadFile = File(...),
    insert_file: UploadFile | None = File(None),
) -> dict:
    """Saves the uploaded QP/MS into papers/ (same convention as a manually-added real
    paper) and runs the full pipeline, per CLAUDE.md's "upload-only" constraint - the tool
    only ever processes files the user hands it."""
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    with _uploads_lock:
        _uploads[0] += 1  # the folder watcher leaves these files to this request
    try:
        qp_path = _safe_upload_path(qp_file.filename or "qp.pdf")
        ms_path = _safe_upload_path(ms_file.filename or "ms.pdf")
        qp_path.write_bytes(await qp_file.read())
        ms_path.write_bytes(await ms_file.read())
        _forget_removed(qp_path, ms_path, Path(qp_file.filename or ""), Path(ms_file.filename or ""))
        insert_path = None
        if insert_file is not None and insert_file.filename:
            insert_path = _safe_upload_path(insert_file.filename)
            insert_path.write_bytes(await insert_file.read())
        result = _extract_upload(qp_path, ms_path)
        if insert_path is not None:
            _attach_uploaded_insert(insert_path, result["paper_key"])
        return result
    finally:
        with _uploads_lock:
            _uploads[0] -= 1


def _attach_uploaded_insert(insert_path: Path, paper_key: str) -> None:
    """An insert uploaded with its paper: checked against the paper like a pair is (its
    cover must say INSERT, for this paper), then attached. A wrong file is removed and
    refused - the paper itself stays extracted."""
    from qbx import inserts
    from qbx.pairing import identify
    from qbx.scan import paper_key as key_of

    try:
        identity = identify(insert_path)
    except Exception:
        identity = None
    if identity is None or identity.kind != "insert" or key_of(identity) != paper_key:
        insert_path.unlink(missing_ok=True)
        what = "isn't an insert" if identity is None or identity.kind != "insert" else f"is the insert for {key_of(identity)}"
        raise HTTPException(
            status_code=400,
            detail=f"The paper was added, but {insert_path.name} {what}, not {paper_key}'s - it wasn't attached.",
        )
    inserts.attach(OUT_DIR, paper_key, insert_path)
    LIBRARY.version += 1


def _extract_upload(qp_path: Path, ms_path: Path) -> dict:
    try:
        paper, report = run_extraction(qp_path, ms_path, OUT_DIR)
    except PairingError as e:
        # A wrong pair mustn't stay in papers/, where `qbx calibrate` would keep pairing it
        # by filename. Both paths are fresh (_safe_upload_path never overwrites), so this
        # only ever removes what this request just wrote.
        qp_path.unlink(missing_ok=True)
        ms_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ScannedPdfError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FingerprintError as e:
        raise HTTPException(status_code=400, detail=f"could not read paper metadata: {e}") from e

    LIBRARY.invalidate(paper.paper_key)
    return {
        "paper_key": paper.paper_key,
        "passed": report.passed,
        "n_issues": len(report.issues),
        "n_questions": len(paper.questions),
    }


def _has_issues(paper_dir: Path) -> bool:
    """Whether an extracted paper's report flagged anything - a missing or unreadable
    report counts, since there's nothing to say the paper is clean."""
    try:
        return not json.loads((paper_dir / "report.json").read_text(encoding="utf-8"))["passed"]
    except (OSError, ValueError, KeyError):
        return True


_scan_lock = threading.Lock()
_scan_state: dict = {"running": False}
_scan_thread: threading.Thread | None = None


def _relative_name(path: Path) -> str:
    """A scanned file as the papers folder holds it - "June 2025/Question-paper/..." for one
    filed in subfolders, whose own name alone wouldn't say which sitting it is."""
    try:
        return path.relative_to(PAPERS_DIR).as_posix()
    except ValueError:
        return path.name


def _run_scan(reprocess: bool) -> None:
    def update(**changes) -> None:
        with _scan_lock:
            _scan_state.update(changes)

    try:
        PAPERS_DIR.mkdir(parents=True, exist_ok=True)
        update(stage="Pairing the PDFs in the papers folder")
        plan = plan_scan(
            PAPERS_DIR,
            progress=lambda done, total: update(stage=f"Pairing the PDFs in the papers folder ({done} of {total})"),
            cache_path=OUT_DIR / ".qbx-pairing.json",
        )
        from qbx import inserts

        before = inserts.index(OUT_DIR)
        inserts.save_index(OUT_DIR, plan.inserts)
        if inserts.index(OUT_DIR) != before:
            LIBRARY.version += 1  # an insert found or gone: the papers' badges change
        removed = _removed_pdfs()
        todo = [
            pair
            for pair in plan.pairs
            if (
                not (OUT_DIR / pair.paper_key / "paper.json").exists()
                or (reprocess and _has_issues(OUT_DIR / pair.paper_key))
            )
            and not {pair.qp_path.name.lower(), pair.ms_path.name.lower()} & removed
        ]
        update(
            stage="Extracting",
            total=len(todo),
            skipped=[p.paper_key for p in plan.pairs if p not in todo],
            unpaired=[{"file": _relative_name(u.path), "reason": u.reason} for u in plan.unpaired],
        )
        # several papers at once, each in a process of its own (scan.scan_workers)
        workers = max(1, min(scan_workers(), len(todo)))
        pending = [pair.paper_key for pair in todo]

        def finished(pair, result: dict) -> None:
            if "error" not in result:
                # listed straight away: the UI's next poll sees the new version
                LIBRARY.invalidate(result["paper_key"])
            with _scan_lock:
                _scan_state["done"] += 1
                if pair.paper_key in pending:
                    pending.remove(pair.paper_key)
                running = pending[:workers]
                _scan_state["current"] = (
                    f"{running[0]} +{len(running) - 1} more" if len(running) > 1 else (running[0] if running else None)
                )
                if "error" in result:
                    _scan_state["failed"].append(
                        {"paper_key": pair.paper_key, "files": [_relative_name(pair.qp_path), _relative_name(pair.ms_path)], "error": result["error"]}
                    )
                else:
                    _scan_state["processed"].append(result)

        update(workers=workers)
        if workers == 1:
            for pair in todo:
                update(current=pair.paper_key)
                finished(pair, extract_pair(pair.qp_path, pair.ms_path, OUT_DIR))
        else:
            running = pending[:workers]
            update(current=f"{running[0]} +{len(running) - 1} more")
            with ProcessPoolExecutor(max_workers=workers, initializer=lower_priority) as pool:
                futures = {pool.submit(extract_pair, pair.qp_path, pair.ms_path, OUT_DIR): pair for pair in todo}
                for future in as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as e:  # the worker process itself died
                        result = {"error": f"unexpected error: {type(e).__name__}: {e}"}
                    finished(futures[future], result)
    except Exception as e:
        update(error=_job_error("The scan", e))
    finally:
        update(running=False, current=None, stage="Finished", finished_at=time.time())
        LIBRARY.refresh()
        LIBRARY.flush()
        _save_scan_report()
        # Tagging never starts by itself (user-decided): the top bar offers "Tag n papers"
        # (/api/state's untagged_papers) and the teacher presses it - nothing is sent to
        # Claude, or charged, before that.


# The last scan's report, kept on disk so "Last scan's results" opens it any time, even
# after a restart (user-asked). Two copies: the latest scan (what the folder holds now -
# its unpaired files and skipped pairs) and the latest that extracted or failed something,
# since the folder watcher's own scans usually find nothing new and would hide it.
_SCAN_REPORT_FILE = ".qbx-last-scan.json"


def _save_scan_report() -> None:
    with _scan_lock:
        latest = json.loads(json.dumps(_scan_state))
    path = OUT_DIR / _SCAN_REPORT_FILE
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        saved = {}
    saved["latest"] = latest
    if latest.get("processed") or latest.get("failed") or latest.get("error"):
        saved["with_work"] = latest
    try:
        path.write_text(json.dumps(saved), encoding="utf-8")
    except OSError:
        pass  # a report we couldn't keep isn't worth failing the scan over


@app.get("/api/papers/scan/last")
def last_scan_report() -> dict:
    """The last scan's report: what the latest scan that extracted something did, with what
    the folder holds now (unpaired files, pairs already done) from the latest scan of all.
    404 before any scan has finished."""
    try:
        saved = json.loads((OUT_DIR / _SCAN_REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    latest = saved.get("latest")
    if not latest:
        raise HTTPException(status_code=404, detail="No scan has finished yet.")
    report = dict(saved.get("with_work") or latest)
    report.update(unpaired=latest.get("unpaired", []), skipped=latest.get("skipped", []), checked_at=latest.get("finished_at"))
    return report


# --- Watching the papers folder (user-asked: no Scan button, papers appear by themselves) --
#
# A thread polls the papers folder's PDFs (name, size, modified time - a stat each, ~0.1s
# for 2,000 files) and starts a scan when they've changed and then stayed the same for one
# more look, so a folder still being copied in isn't scanned half-written. It scans once at
# start too. Moves are included: a scan re-reads only files it hasn't seen at that path.
# It never starts tagging: the teacher confirms that (see _run_scan).

WATCH_SECONDS = 3.0
_uploads = [0]  # "Add one paper" requests writing into the papers folder right now
_uploads_lock = threading.Lock()
_watch_started = False
_watch_lock = threading.Lock()
_REMOVED_FILE = ".qbx-removed.json"


def _papers_signature() -> frozenset:
    """Every PDF under papers/ with its size and modified time. Read from the folder
    listings, which carry both (free on Windows): a stat per file took 0.58s over 4,787
    PDFs, every WATCH_SECONDS while the app was open."""
    found = set()
    folders = [str(PAPERS_DIR)] if PAPERS_DIR.is_dir() else []
    while folders:
        try:
            entries = list(os.scandir(folders.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    folders.append(entry.path)
                elif entry.name.lower().endswith(".pdf"):
                    st = entry.stat()
                    found.add((entry.path, st.st_size, st.st_mtime_ns))
            except OSError:
                continue
    return frozenset(found)


def _watch_papers() -> None:
    scanned = None  # the folder as the last scan found it; None scans at start
    seen = None
    while True:
        try:
            if _scan_state.get("running"):
                # nothing to compare while a scan runs: it reads the folder itself
                time.sleep(WATCH_SECONDS)
                continue
            now = _papers_signature()
            busy = _uploads[0] > 0
            if not busy and now != scanned and now == seen:
                scanned = now
                scan_papers()
            seen = now
        except Exception:
            pass  # a folder mid-rename, a locked file: look again next time
        time.sleep(WATCH_SECONDS)


def _start_watching() -> None:
    """Started by the first /api/state, so only a running app watches (never a test or the
    CLI); QBX_WATCH_PAPERS=0 turns it off."""
    global _watch_started
    if os.environ.get("QBX_WATCH_PAPERS", "1") == "0":
        return
    with _watch_lock:
        if _watch_started:
            return
        _watch_started = True
    threading.Thread(target=_watch_papers, daemon=True, name="papers-watcher").start()


def _removed_pdfs() -> set[str]:
    """PDF names of papers deleted from the bank with their PDFs kept: never scanned again
    until the teacher adds the paper by hand."""
    try:
        return set(json.loads((OUT_DIR / _REMOVED_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _write_removed(names: set[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / _REMOVED_FILE).write_text(json.dumps(sorted(names)), encoding="utf-8")


def _remember_removed(*paths: str | None) -> None:
    _write_removed(_removed_pdfs() | {Path(p).name.lower() for p in paths if p})


def _forget_removed(*paths: Path) -> None:
    names = _removed_pdfs()
    if names & {p.name.lower() for p in paths}:
        _write_removed(names - {p.name.lower() for p in paths})


@app.post("/api/papers/open_folder")
def open_papers_folder() -> dict:
    """Opens the papers folder in Explorer / Finder, for copying PDFs in before a scan. The
    server runs on the user's own machine (127.0.0.1), so this opens it on their screen."""
    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(PAPERS_DIR)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(PAPERS_DIR)], check=False)
    return {"folder": str(PAPERS_DIR)}


@app.post("/api/papers/scan")
def scan_papers(reprocess: bool = False) -> dict:
    """Starts processing every question paper / mark scheme pair in the papers folder, in
    the background - progress and results are in /api/state's `scan`, and each paper is
    listed the moment it's extracted. Pairs are found by what each file's cover says it is
    (scan.plan_scan), so filenames don't matter. A pair already extracted is skipped, unless
    `reprocess`, which extracts again every pair whose report has issues. Clean papers are
    always skipped, so their Reviewer corrections are never replaced."""
    global _scan_thread
    with _scan_lock:
        # a scan whose thread has gone without finishing (killed mid-run) mustn't block
        # every later one until the server restarts
        if _scan_state.get("running") and not (_scan_thread and _scan_thread.is_alive()):
            _scan_state["running"] = False
        if not _scan_state.get("running"):
            _scan_state.clear()
            _scan_state.update(
                running=True, reprocess=reprocess, stage="Starting", folder=str(PAPERS_DIR),
                total=0, done=0, current=None, processed=[], skipped=[], failed=[], unpaired=[],
                started_at=time.time(),
            )
            _scan_thread = threading.Thread(target=_run_scan, args=(reprocess,), daemon=True)
            _scan_thread.start()
        return json.loads(json.dumps(_scan_state))


@app.get("/api/papers/{paper_key}")
def get_paper(paper_key: str) -> Paper:
    return _load_paper(paper_key)


@app.get("/api/papers/{paper_key}/report")
def get_report(paper_key: str) -> dict:
    """The validation report, each issue carrying its stable `key` and whether the reviewer
    has marked it checked."""
    paper = _load_paper(paper_key)
    report = validate_paper(paper).model_dump()
    checked = read_reviewed(_paper_dir(paper_key))
    for issue in report["issues"]:
        issue["key"] = issue_key(issue)
        issue["checked"] = issue["key"] in checked
    return report


@app.post("/api/papers/{paper_key}/review")
def review_issues(paper_key: str, body: dict) -> dict:
    """Marks report issues as checked (or not): {"keys": [...], "checked": true}. Stored in
    review.json beside paper.json, so a re-extraction keeps them - an issue key names its
    check, question and part, not anything the parser regenerates."""
    paper_dir = _paper_dir(paper_key)
    checked = read_reviewed(paper_dir)
    keys = set(body.get("keys", []))
    checked = checked | keys if body.get("checked", True) else checked - keys
    (paper_dir / "review.json").write_text(json.dumps({"checked": sorted(checked)}, indent=2), encoding="utf-8")
    LIBRARY.invalidate(paper_key)
    return {"checked": sorted(checked)}


@functools.lru_cache(maxsize=256)
def _render_page(pdf_path: str, mtime_ns: int, page_number: int, dpi: int) -> bytes | None:
    """PNG of one source page. Keyed on the file's mtime, so a replaced PDF re-renders;
    the reviewer flips between the same few pages constantly, and a render is ~70 ms."""
    with pymupdf.open(pdf_path) as doc:
        if not 0 <= page_number < doc.page_count:
            return None
        return doc[page_number].get_pixmap(dpi=dpi).tobytes("png")


def _page_response(pdf_path: str, page_number: int, missing: str) -> Response:
    source = Path(pdf_path) if pdf_path else None
    if source is None or not source.exists():
        raise HTTPException(status_code=404, detail=missing)
    png = _render_page(str(source), source.stat().st_mtime_ns, page_number, RENDER_DPI)
    if png is None:
        raise HTTPException(status_code=404, detail="page out of range")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/papers/{paper_key}/page/{page_number}")
def get_page_image(paper_key: str, page_number: int) -> Response:
    """page_number is 0-indexed, matching schema.BBox.page."""
    paper = _load_paper(paper_key)
    return _page_response(paper.source_qp_path, page_number, "source QP PDF not found on disk")


@app.get("/api/papers/{paper_key}/ms_page/{page_number}")
def get_ms_page_image(paper_key: str, page_number: int) -> Response:
    """A mark scheme page, upright - the orientation ms_rows' coordinates are in."""
    paper = _load_paper(paper_key)
    return _page_response(paper.source_ms_path, page_number, "source MS PDF not found on disk")


@functools.lru_cache(maxsize=64)
def _ms_rows(pdf_path: str, mtime_ns: int, board: str = "Cambridge") -> dict:
    layout = scheme_layout(pdf_path, board)
    return {entry_id: [s.model_dump() for s in row.segments] for entry_id, row in layout.rows.items()}


@app.get("/api/papers/{paper_key}/ms_rows")
def get_ms_rows(paper_key: str) -> dict:
    """Where each mark scheme row sits on the MS pages, by entry id - for highlighting a
    part's answer beside its question. Empty when the scheme has no ruled rows (an answer
    key): the reviewer shows the entries' text instead."""
    paper = _load_paper(paper_key)
    source = Path(paper.source_ms_path) if paper.source_ms_path else None
    if source is None or not source.exists():
        return {"rows": {}, "page_count": 0}
    rows = dict(_ms_rows(str(source), source.stat().st_mtime_ns, paper.board))
    # rows linked by hand carry their own regions
    rows.update({e.id: [s.model_dump() for s in e.segments] for e in paper.mark_scheme if e.segments})
    with pymupdf.open(source) as doc:
        page_count = doc.page_count
    return {"rows": rows, "page_count": page_count}


def _relink(paper_key: str, part_id: str, boxes: list[dict] | None) -> dict:
    """Sets (or with None, clears) the mark scheme region linked to a part by hand, then
    re-applies every link - see qbx/ms_links.py."""
    paper = _load_paper(paper_key)
    if not any(p.id == part_id for q in paper.questions for p in q.parts):
        raise HTTPException(status_code=404, detail=f"no part {part_id!r} in {paper_key}")
    source = Path(paper.source_ms_path) if paper.source_ms_path else None
    if source is None or not source.exists():
        raise HTTPException(status_code=400, detail="This paper's mark scheme PDF isn't on disk any more.")
    paper_dir = _paper_dir(paper_key)
    links = ms_links.read_links(paper_dir)
    if boxes:
        links[part_id] = [BBox(**b).model_dump() for b in boxes]
    else:
        links.pop(part_id, None)
    ms_links.write_links(paper_dir, links)
    layout = scheme_layout(str(source), paper.board)
    columns = layout.columns
    rows = {entry_id: row.segments for entry_id, row in layout.rows.items()}
    if boxes is None:
        # the part's own detected rows come back only from a re-extraction; until then it
        # simply has no link of its own
        paper = paper.model_copy(update={
            "mark_scheme": [e for e in paper.mark_scheme if e.id != f"manual.{part_id}"],
            "joins": [j for j in paper.joins if j.mark_scheme_entry_id != f"manual.{part_id}"],
        })
    paper = ms_links.apply_links(paper, links, str(source), columns, rows)
    _save_paper(paper_key, paper)
    entry = next((e for e in paper.mark_scheme if e.id == f"manual.{part_id}"), None)
    return {"entry": entry.model_dump() if entry else None}


@app.put("/api/papers/{paper_key}/parts/{part_id}/ms_link")
def link_mark_scheme(paper_key: str, part_id: str, body: dict) -> dict:
    """Links a part to the mark scheme region the teacher drew round its answer:
    {"segments": [BBox, ...]} in upright mark scheme page points."""
    boxes = body.get("segments") or []
    if not boxes:
        raise HTTPException(status_code=400, detail="Draw a box round the answer on the mark scheme first.")
    return _relink(paper_key, part_id, boxes)


@app.delete("/api/papers/{paper_key}/parts/{part_id}/ms_link")
def unlink_mark_scheme(paper_key: str, part_id: str) -> dict:
    return _relink(paper_key, part_id, None)


@app.get("/api/papers/{paper_key}/assets/{asset_path:path}")
def get_asset(paper_key: str, asset_path: str) -> Response:
    file_path = (_paper_dir(paper_key) / "assets" / asset_path).resolve()
    assets_dir = (_paper_dir(paper_key) / "assets").resolve()
    if assets_dir not in file_path.parents or not file_path.exists():
        raise HTTPException(status_code=404, detail="no such asset")
    return Response(content=file_path.read_bytes(), media_type="image/svg+xml")


def _apply_manual(paper_key: str, steps: list[Step]) -> tuple[Paper, Fix]:
    """Applies a Reviewer edit as a fix (qbx/fixes.py), so a re-extraction keeps it and it
    can be undone - the fix comes back too, for the Reviewer's Undo."""
    paper_dir = _paper_dir(paper_key)
    paper = _load_paper(paper_key)
    if fixes_mod.read_base(paper_dir) is None:
        fixes_mod.write_base(paper_dir, paper)  # extracted before fixes were kept
    fix = Fix(steps=steps, source="manual", status="applied")
    try:
        paper = fixes_mod.apply_fix(paper, fix)
    except FixError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    fixes_mod.write_fixes(paper_dir, fixes_mod.read_fixes(paper_dir) + [fix])
    _save_paper(paper_key, paper)
    return paper, fix


# What the Reviewer may change by hand through /edit: every fix step that edits the paper.
# Not link_region (a drawn link goes through ms_link, which keeps it across re-extraction
# on its own) and not Claude's notes.
_EDIT_OPS = {
    "link", "unlink", "relabel_entry", "merge_parts", "edit_part", "split_part", "add_part",
    "delete_part", "set_marks", "set_total", "set_entry_marks", "delete_entry",
    "set_cover_total", "renumber_question", "delete_question", "add_question", "withdraw_question",
}


@app.post("/api/papers/{paper_key}/edit")
def edit_paper(paper_key: str, body: dict) -> dict:
    """Any Reviewer edit, as fix steps ({"steps": [...]}), applied together as one fix -
    kept across re-extraction and undone in one go. Returns the fix's id for Undo."""
    raw = body.get("steps") or []
    if not raw:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    try:
        steps = [Step(**s) for s in raw]
    except Exception as e:  # pydantic: an unknown op or a malformed field
        raise HTTPException(status_code=400, detail=f"Not a change the Reviewer can make: {e}") from e
    refused = sorted({s.op for s in steps} - _EDIT_OPS)
    if refused:
        raise HTTPException(status_code=400, detail=f"Not a change the Reviewer can make: {', '.join(refused)}")
    _, fix = _apply_manual(paper_key, steps)
    return {"fix_id": fix.id}


@app.patch("/api/papers/{paper_key}/parts/{part_id}")
def update_part(paper_key: str, part_id: str, updates: dict) -> dict:
    if _find_part(_load_paper(paper_key), part_id)[2] is None:
        raise HTTPException(status_code=404, detail=f"no such part: {part_id}")
    fields = {k: updates[k] for k in ("text", "marks", "path", "segments") if k in updates}
    paper, fix = _apply_manual(paper_key, [Step(op="edit_part", part_id=part_id, fields=fields)])
    new_id = f"{part_id.split('.', 1)[0]}.{fields['path']}" if "path" in fields else part_id
    _, _, part = _find_part(paper, new_id)
    return {**(part or _find_part(paper, part_id)[2]).model_dump(), "fix_id": fix.id}


@app.post("/api/papers/{paper_key}/snap")
def snap_region(paper_key: str, body: dict) -> dict:
    """A region drawn on the question paper, its edges moved to the question's own content
    (qbx/snap.py): {"segments": [BBox, ...]} -> the same, snapped."""
    paper = _load_paper(paper_key)
    source = Path(paper.source_qp_path) if paper.source_qp_path else None
    boxes = [BBox(**b) for b in body.get("segments") or []]
    if source is None or not source.exists():
        return {"segments": [b.model_dump() for b in boxes]}
    return {"segments": [b.model_dump() for b in snap_segments(str(source), boxes)]}


@app.put("/api/papers/{paper_key}/questions/{question_number}/total")
def set_question_total(paper_key: str, question_number: int, body: dict) -> dict:
    """Sets a question's total by hand ({"total": n}) - the Reviewer's marks view."""
    value = body.get("total")
    if not isinstance(value, int) or value < 0:
        raise HTTPException(status_code=400, detail="The total must be a whole number.")
    paper, fix = _apply_manual(paper_key, [Step(op="set_total", question_number=question_number, value=value)])
    question = next(q for q in paper.questions if q.question_number == question_number)
    return {"question_number": question_number, "total_marks": question.total_marks, "fix_id": fix.id}


@app.post("/api/papers/{paper_key}/parts/{part_id}/merge")
def merge_parts(paper_key: str, part_id: str) -> dict:
    """Merges `part_id` with the next part in its question's part list."""
    question, index, part = _find_part(_load_paper(paper_key), part_id)
    if part is None:
        raise HTTPException(status_code=404, detail=f"no such part: {part_id}")
    if index + 1 >= len(question.parts):
        raise HTTPException(status_code=400, detail="no next part to merge with")
    paper, fix = _apply_manual(
        paper_key, [Step(op="merge_parts", part_ids=[part_id, question.parts[index + 1].id])]
    )
    return {**_find_part(paper, part_id)[2].model_dump(), "fix_id": fix.id}


@app.post("/api/papers/{paper_key}/parts/{part_id}/split")
def split_part(paper_key: str, part_id: str, at: dict) -> dict:
    """Splits `part_id` into two parts at the given {page, y} point. The reviewer edits
    each half's text/marks by hand afterward - there's no reliable way to auto-guess how
    a manually-corrected split should divide the original (imperfect) extracted text.
    """
    if _find_part(_load_paper(paper_key), part_id)[2] is None:
        raise HTTPException(status_code=404, detail=f"no such part: {part_id}")
    paper, fix = _apply_manual(paper_key, [Step(op="split_part", part_id=part_id, at={"page": at["page"], "y": at["y"]})])
    return {
        "first": _find_part(paper, part_id)[2].model_dump(),
        "second": _find_part(paper, f"{part_id}-split")[2].model_dump(),
        "fix_id": fix.id,
    }


class QuizItem(BaseModel):
    """One source question plus the subset of its parts to include, in the order they
    should appear - the composed paper relabels them a, b, c... regardless of their
    original letters (see quiz_export.QuizGroup)."""

    paper_key: str
    question_number: int
    part_paths: list[str] = []


class Quiz(BaseModel):
    name: str
    items: list[QuizItem] = []
    # set when the exam was built from a typed request: what was typed and how it was
    # read, so "Try different questions" can build it again with a new seed
    request: dict | None = None
    # the time allowance the teacher set, printed on the cover; None prints none
    duration_minutes: int | None = None
    # the conversation with Claude that made or changed it (the Claude tab), ChatMessage dicts
    chat: list[dict] = []
    # [paper key, question number] of every question the exam has held: Claude's changes and
    # "Try different questions" pick others first, rather than cycling back (user-reported)
    seen: list[tuple[str, int]] = []

    def remember(self, items) -> None:
        known = {tuple(k) for k in self.seen}
        for item in items:
            key = (item.paper_key, item.question_number)
            if key not in known:
                known.add(key)
                self.seen.append(key)


def _quiz_path(name: str) -> Path:
    safe = _UNSAFE_FILENAME_RE.sub("_", name).strip("_") or "quiz"
    return QUIZZES_DIR / f"{safe}.json"


def _load_quiz(name: str) -> Quiz:
    path = _quiz_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no such quiz: {name}")
    return Quiz.model_validate_json(path.read_text(encoding="utf-8"))


_QUIZ_WRITE_LOCK = threading.Lock()


def _save_quiz(quiz: Quiz) -> None:
    """Writes a temporary file and swaps it in, one save at a time. Writing in place let two
    saves of one exam overlap: both emptied the file, and the shorter one, finishing last,
    left the longer one's tail behind it - "} "chat": []\\n}" - and the exam no longer
    loaded (user-reported 500 on GET /api/quizzes/{name})."""
    QUIZZES_DIR.mkdir(parents=True, exist_ok=True)
    path = _quiz_path(quiz.name)
    with _QUIZ_WRITE_LOCK:
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(quiz.model_dump_json(indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                # Windows refuses to replace a file another thread has open for reading
                if attempt == 4:
                    raise
                time.sleep(0.05)


def _resolve_quiz_item(item: QuizItem, paper_cache: dict[str, Paper | None]) -> dict:
    """Looks the item's parts up fresh from its paper.json rather than storing a snapshot,
    so a quiz stays current if the source paper is re-extracted with fixes. A paper that's
    since been deleted, or a part path no longer present, is marked "missing" per-part
    rather than failing the whole quiz fetch.
    """
    if item.paper_key not in paper_cache:
        paper_json = OUT_DIR / item.paper_key / "paper.json"
        paper_cache[item.paper_key] = (
            Paper.model_validate_json(paper_json.read_text(encoding="utf-8"))
            if paper_json.exists()
            else None
        )
    paper = paper_cache[item.paper_key]
    question = next((q for q in paper.questions if q.question_number == item.question_number), None) if paper else None
    parts_by_path = {p.path: p for p in (question.parts if question else [])}

    parts = []
    for path in item.part_paths:
        part = parts_by_path.get(path)
        parts.append(
            {
                "part_path": path,
                "marks": part.marks if part else None,
                "text": part.text if part else None,
                "image_path": part.image_path if part else None,
                "missing": part is None,
            }
        )
    return {
        "paper_key": item.paper_key,
        "question_number": item.question_number,
        "stem_text": question.stem_text if question else None,
        "missing": paper is None or question is None,
        # the paper sets these in a section of their own, after the written questions -
        # the builder lists them the same way
        "mcq": paper is not None
        and any(is_multiple_choice(parts_by_path[p], paper) for p in item.part_paths if p in parts_by_path),
        "parts": parts,
    }


@app.get("/api/quizzes")
def list_quizzes() -> list[dict]:
    QUIZZES_DIR.mkdir(parents=True, exist_ok=True)
    result = []
    for path in sorted(QUIZZES_DIR.glob("*.json")):
        quiz = Quiz.model_validate_json(path.read_text(encoding="utf-8"))
        result.append(
            {
                "name": quiz.name,
                "n_items": len(quiz.items),
                "from_request": quiz.request is not None,
                "has_chat": bool(quiz.chat),
                # which questions it sets, so the builder can warn when one is reused
                "questions": [f"{item.paper_key}::{item.question_number}" for item in quiz.items],
            }
        )
    return result


@app.post("/api/quizzes")
def create_quiz(body: dict) -> Quiz:
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="quiz name is required")
    if _quiz_path(name).exists():
        raise HTTPException(status_code=400, detail=f"a quiz named {name!r} already exists")
    quiz = Quiz(name=name)
    _save_quiz(quiz)
    return quiz


@app.get("/api/quizzes/{name}")
def get_quiz(name: str) -> dict:
    quiz = _load_quiz(name)
    paper_cache: dict[str, Paper | None] = {}
    items = [_resolve_quiz_item(item, paper_cache) for item in quiz.items]
    total_marks = sum(p["marks"] or 0 for item in items for p in item["parts"])
    return {
        "name": quiz.name,
        "items": items,
        "total_marks": total_marks,
        "request": quiz.request,
        "duration_minutes": quiz.duration_minutes,
        "chat": quiz.chat,
    }


@app.put("/api/quizzes/{name}")
def update_quiz(name: str, body: dict) -> Quiz:
    """Replaces the quiz's item list wholesale - the reviewer sends the full ordered list
    after adding/removing/reordering, which is simpler than a set of add/remove/move
    endpoints for a list this short."""
    existing = _load_quiz(name)  # 404s if it doesn't exist
    items = [QuizItem(**item) for item in body.get("items", [])]
    LIBRARY.refresh()
    _one_board({LIBRARY.board_of(item.paper_key) or "" for item in items}, "its questions")
    quiz = existing.model_copy(update={"items": items})
    _save_quiz(quiz)
    return quiz


@app.put("/api/quizzes/{name}/duration")
def set_quiz_duration(name: str, body: dict) -> dict:
    """The time allowance for the cover, kept with the exam - `minutes` empty or 0 clears it."""
    quiz = _load_quiz(name)
    try:
        minutes = int(body.get("minutes") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="The time allowed must be a whole number of minutes.")
    quiz = quiz.model_copy(update={"duration_minutes": minutes if minutes > 0 else None})
    _save_quiz(quiz)
    return {"name": quiz.name, "duration_minutes": quiz.duration_minutes}


@app.post("/api/quizzes/{name}/rename")
def rename_quiz(name: str, body: dict) -> Quiz:
    """Saves the exam under its new name - items, order and any request kept - and removes
    the old file. A change of case alone is allowed: on Windows it's the same file."""
    quiz = _load_quiz(name)  # 404s if it doesn't exist
    new_name = str(body.get("name", "")).strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="quiz name is required")
    old_path, new_path = _quiz_path(name), _quiz_path(new_name)
    same_file = old_path == new_path or (new_path.exists() and new_path.samefile(old_path))
    if new_path.exists() and not same_file:
        raise HTTPException(status_code=400, detail=f"a quiz named {new_name!r} already exists")
    renamed = quiz.model_copy(update={"name": new_name})
    if same_file:
        old_path.unlink()  # so a case-only change reaches the file's own name too
    _save_quiz(renamed)
    if not same_file:
        old_path.unlink()
    return renamed


def _exams_using(paper_key: str) -> list[str]:
    QUIZZES_DIR.mkdir(parents=True, exist_ok=True)
    names = []
    for path in sorted(QUIZZES_DIR.glob("*.json")):
        quiz = Quiz.model_validate_json(path.read_text(encoding="utf-8"))
        if any(item.paper_key == paper_key for item in quiz.items):
            names.append(quiz.name)
    return names


@app.get("/api/papers/{paper_key}/used_in")
def paper_used_in(paper_key: str) -> dict:
    """The saved exams that set a question from this paper - shown before deleting it."""
    _paper_dir(paper_key)  # 404s for an unknown paper
    return {"exams": _exams_using(paper_key)}


@app.delete("/api/papers/{paper_key}")
def delete_paper(paper_key: str, delete_pdfs: bool = True) -> dict:
    """Removes a paper from the bank: its extracted folder (questions, crops, tags, review
    notes) and - unless `delete_pdfs` is false - its QP and MS PDFs, so "Scan papers folder"
    doesn't simply add it back. Only PDFs inside the papers folder are ever deleted; a
    source that lives anywhere else is left alone. Exams that used it keep their other
    questions and show this one as missing from its paper."""
    paper_dir = _paper_dir(paper_key)
    if paper_dir.resolve().parent != OUT_DIR.resolve():
        raise HTTPException(status_code=400, detail="That isn't a paper in the bank.")
    if _scan_state.get("running") or _tag_state.get("running"):
        raise HTTPException(status_code=409, detail="Wait for the scan or tagging run to finish, then delete.")
    paper = _load_paper(paper_key)
    removed_pdfs, kept_pdfs = [], []
    if delete_pdfs:
        papers_dir = PAPERS_DIR.resolve()
        for source in (paper.source_qp_path, paper.source_ms_path):
            if not source:
                continue
            path = Path(source).resolve()
            if path.is_relative_to(papers_dir) and path.is_file():
                path.unlink()
                removed_pdfs.append(path.name)
            elif path.exists():
                kept_pdfs.append(str(path))
    else:
        # its PDFs stay in the papers folder: the folder watcher mustn't add it straight back
        _remember_removed(paper.source_qp_path, paper.source_ms_path)
    shutil.rmtree(paper_dir)
    LIBRARY.invalidate(paper_key)
    return {"deleted": paper_key, "removed_pdfs": removed_pdfs, "kept_pdfs": kept_pdfs}


@app.post("/api/papers/{paper_key}/reprocess")
def reprocess_paper(paper_key: str) -> dict:
    """Extracts one paper again from its own QP and MS - to see a parser change on the
    paper in front of you without re-processing the bank. As in a scan, its hand-drawn
    links, fixes and tags are re-applied (run_extraction), and its review notes are kept."""
    paper_dir = _paper_dir(paper_key)
    if paper_dir.resolve().parent != OUT_DIR.resolve():
        raise HTTPException(status_code=400, detail="That isn't a paper in the bank.")
    if _scan_state.get("running") or _tag_state.get("running"):
        raise HTTPException(status_code=409, detail="Wait for the scan or tagging run to finish, then re-process.")
    paper = _load_paper(paper_key)
    qp, ms = Path(paper.source_qp_path or ""), Path(paper.source_ms_path or "")
    if not (paper.source_qp_path and qp.is_file() and paper.source_ms_path and ms.is_file()):
        raise HTTPException(status_code=409, detail="This paper's PDFs aren't where they were - scan the papers folder instead.")
    try:
        paper, report = run_extraction(qp, ms, OUT_DIR)
    except (ScannedPdfError, FingerprintError, PairingError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    LIBRARY.invalidate(paper.paper_key)
    return {"paper_key": paper.paper_key, "passed": report.passed, "n_issues": len(report.issues)}


@app.delete("/api/quizzes/{name}")
def delete_quiz(name: str) -> dict:
    path = _quiz_path(name)
    if path.exists():
        path.unlink()
    return {"deleted": name}


def _quiz_groups(quiz: Quiz) -> list[QuizGroup]:
    return [
        QuizGroup(paper_key=item.paper_key, question_number=item.question_number, part_paths=item.part_paths)
        for item in quiz.items
    ]


@app.get("/api/quizzes/{name}/export_qp.pdf")
def export_quiz_qp(name: str, duration_minutes: int | None = None, footer_note: str = "") -> Response:
    """With no `duration_minutes` given, the exam's own saved allowance is used."""
    """`duration_minutes` is the time allowance the teacher set for this paper; the cover
    renders it as e.g. "1 hour 15 minutes", and omits it when they didn't set one.
    `footer_note` is whatever the teacher wants printed at the right of every page's foot."""
    quiz = _load_quiz(name)
    if duration_minutes is None:
        duration_minutes = quiz.duration_minutes
    pdf_bytes = compose_qp_pdf(
        _quiz_groups(quiz), OUT_DIR, title=quiz.name, duration_minutes=duration_minutes,
        footer_note=footer_note,
    )
    safe_name = _UNSAFE_FILENAME_RE.sub("_", name).strip("_") or "quiz"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_qp.pdf"'},
    )


# Preview pages are for reading on screen, not printing - enough to read question text
# at a sidebar's width without shipping megabytes of PNG per keystroke.
PREVIEW_DPI = 90


@app.post("/api/preview/qp")
def preview_qp(body: dict) -> dict:
    """Renders an *unsaved* selection through the same composer as Export QP, so the question
    bank's previews show exactly what the exported paper will contain. `cover=false` with
    `first_number` is the single-question preview: that question alone, numbered as the
    question it would become."""
    items = [QuizItem(**item) for item in body.get("items", [])]
    groups = [
        QuizGroup(paper_key=i.paper_key, question_number=i.question_number, part_paths=i.part_paths)
        for i in items
        if i.part_paths
    ]
    if not groups:
        return {"pages": []}
    duration = body.get("duration_minutes")
    pdf_bytes = compose_qp_pdf(
        groups,
        OUT_DIR,
        title=body.get("title") or "Practice Question Paper",
        duration_minutes=int(duration) if duration else None,
        include_cover=bool(body.get("cover", True)),
        first_number=int(body.get("first_number", 1)),
        footer_note=str(body.get("footer_note") or ""),
    )
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        pages = [
            "data:image/png;base64,"
            + base64.b64encode(page.get_pixmap(dpi=PREVIEW_DPI).tobytes("png")).decode("ascii")
            for page in doc
        ]
    finally:
        doc.close()
    return {"pages": pages}


@app.get("/api/quizzes/{name}/export_ms.pdf")
def export_quiz_ms(name: str, footer_note: str = "") -> Response:
    quiz = _load_quiz(name)
    pdf_bytes = compose_ms_pdf(_quiz_groups(quiz), OUT_DIR, title=quiz.name, footer_note=footer_note)
    safe_name = _UNSAFE_FILENAME_RE.sub("_", name).strip("_") or "quiz"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_ms.pdf"'},
    )


@app.post("/api/quizzes/{name}/save")
def save_quiz(name: str, body: dict | None = None) -> dict:
    """Writes the exam into its folder in the save folder (qbx/saving.py): `kind` "qp" (the
    question paper and its sources list) or "ms". A changed exam's last save moves to Old
    versions first."""
    body = body or {}
    quiz = _load_quiz(name)
    if not quiz.items:
        raise HTTPException(status_code=400, detail="Add a question to this exam first.")
    footer_note = str(body.get("footer_note") or "")
    duration = body.get("duration_minutes")
    duration = int(duration) if duration else quiz.duration_minutes
    groups = _quiz_groups(quiz)
    title = saving.safe_name(quiz.name)
    if body.get("kind") == "ms":
        files = {f"{title} - Mark scheme.pdf": compose_ms_pdf(groups, OUT_DIR, title=quiz.name, footer_note=footer_note)}
    else:
        files = {
            f"{title} - Question paper.pdf": compose_qp_pdf(
                groups, OUT_DIR, title=quiz.name, duration_minutes=duration, footer_note=footer_note),
            f"{title} - Sources.pdf": compose_sources_pdf(groups, OUT_DIR, title=quiz.name, footer_note=footer_note),
        }
    # what makes two saves the same exam: its questions, time and footer
    content = {"items": [i.model_dump() for i in quiz.items], "duration": duration, "footer": footer_note}
    return _saved(lambda: saving.save_files("exam", quiz.name, None, files, saving.fingerprint(content)))


@app.get("/api/quizzes/{name}/export_sources.pdf")
def export_quiz_sources(name: str, footer_note: str = "") -> Response:
    """For the teacher: where each question of the exported paper was taken from, numbered
    as the paper prints it. The builder downloads it alongside the question paper."""
    quiz = _load_quiz(name)
    pdf_bytes = compose_sources_pdf(_quiz_groups(quiz), OUT_DIR, title=quiz.name, footer_note=footer_note)
    safe_name = _UNSAFE_FILENAME_RE.sub("_", name).strip("_") or "quiz"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_sources.pdf"'},
    )


# -- AI features (opt-in: nothing is sent until Claude is switched on in Settings) -------
# Claude, through the Anthropic API or Claude Code (qbx/llm.py), tags parts with syllabus topics and
# reads typed exam requests; choosing the questions and adding up marks is exam_builder.py's.


def _require_llm(job: str = "tag"):
    llm = llm_mod.get_llm(job)
    if llm is None:
        if not edition.claude_available():
            raise HTTPException(status_code=400, detail="Sign in to your Classark account to use Claude.")
        raise HTTPException(status_code=400, detail="Switch on Claude in Settings to use this.")
    return llm


def _debug_only() -> None:
    """Settings (and Claude Code with them) exist only in debug builds (qbx/edition.py)."""
    if edition.is_release():
        raise HTTPException(status_code=404, detail="Not available in this version.")


def _llm_http_error(e: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=str(e))


@app.get("/api/settings")
def get_settings() -> dict:
    _debug_only()
    # the API key itself never leaves the credential store: only whether it's set and its last 4
    return {
        **llm_mod.load_settings().model_dump(), "models": llm_mod.MODELS,
        "providers": llm_mod.PROVIDERS, "api_key": api_key.api_key_status(), "key_store": api_key.backend_name(),
    }


@app.put("/api/settings")
def put_settings(body: dict) -> dict:
    _debug_only()
    s = llm_mod.load_settings()
    if "enabled" in body:
        s.enabled = bool(body["enabled"])
    if body.get("provider") in llm_mod.PROVIDERS:
        s.provider = body["provider"]
    for field in ("tag_model", "request_model"):
        if body.get(field):
            s.__setattr__(field, str(body[field]).strip())
    llm_mod.save_settings(s)
    return get_settings()


class _Ping(BaseModel):
    ok: bool


@app.put("/api/settings/api_key")
def put_api_key(body: dict) -> dict:
    """Saves the Anthropic API key in the OS credential store (never settings.json)."""
    _debug_only()
    try:
        api_key.set_api_key(str(body.get("key") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except api_key.KeyStoreError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return api_key.api_key_status()


@app.delete("/api/settings/api_key")
def delete_api_key() -> dict:
    _debug_only()
    api_key.clear_api_key()
    return api_key.api_key_status()


@app.post("/api/settings/check")
def check_claude() -> dict:
    """One tiny request ("Test connection"): the API key works, or Claude Code is installed
    and logged in - on the chosen provider, even before Claude is switched on."""
    _debug_only()
    llm = llm_mod._override or llm_mod.make_llm(llm_mod.load_settings(), "tag")
    started = time.monotonic()
    try:
        llm.json("Reply with ok = true.", _Ping)
    except Exception as e:
        raise _llm_http_error(e) from e
    return {"ok": True, "seconds": round(time.monotonic() - started, 1)}


@app.get("/api/subjects")
def list_subjects() -> list[dict]:
    """Each subject in the bank, how much of it is tagged, and whether it has topics yet."""
    LIBRARY.refresh()
    rows = LIBRARY.subjects()
    for row in rows:
        row["n_topics"] = len(load_topics(row["subject"]).topics)
    return rows


def _checked_subject(subject: str) -> str:
    try:
        load_topics(subject)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return subject


@app.get("/api/topics/{subject}")
def get_topics(subject: str) -> dict:
    return load_topics(_checked_subject(subject)).model_dump()


@app.put("/api/topics/{subject}")
def put_topics(subject: str, body: dict) -> dict:
    topics = [Topic(**t) for t in body.get("topics", [])]
    result = save_topics(_checked_subject(subject), topics).model_dump()
    LIBRARY.version += 1  # the UI refetches, so new topic names show everywhere
    return result


@app.post("/api/topics/{subject}/from_syllabus")
async def topics_from_syllabus(subject: str, file: UploadFile = File(...)) -> dict:
    """Claude reads the topics and their learning outcomes out of an uploaded syllabus PDF
    (topics.read_syllabus). The result is returned for checking, not saved - the Settings
    screen saves it once it looks right."""
    _checked_subject(subject)
    llm = _require_llm("syllabus")  # a one-off, worth the stronger model
    try:
        pages = syllabus_pages(await file.read())
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Couldn't read that PDF: {e}") from e
    if len("".join(pages).strip()) < 200:
        raise HTTPException(status_code=400, detail="That PDF has almost no text - is it a scanned syllabus?")
    try:
        topics = await asyncio.to_thread(read_syllabus, llm, subject, pages)
    except Exception as e:
        raise _llm_http_error(e) from e
    return {"subject": subject, "topics": [t.model_dump() for t in topics]}


_tag_lock = threading.Lock()
_tag_state: dict = {"running": False}
# papers tagged at once - each request is a Claude Code process, and a subscription's limits
# are shared with everything else the user does in Claude, so this stays small
TAG_WORKERS = 2


def _tag_paper(llm, paper_key: str, numbers: list[int], topics: list[Topic]) -> None:
    paper = _load_paper(paper_key)
    questions = [q for q in paper.questions if q.question_number in numbers]
    fresh = tag_questions(llm, paper, questions, topics)
    if fresh:
        paper_dir = OUT_DIR / paper_key
        write_tags(paper_dir, merge_tags(read_tags(paper_dir), fresh))
        LIBRARY.invalidate(paper_key)


def _run_tagging(subject: str | None, llm) -> None:
    def update(**changes) -> None:
        with _tag_lock:
            _tag_state.update(changes)

    topic_lists: dict[str, list[Topic]] = {}

    def find_todo() -> list[tuple[str, list[int]]]:  # (paper key, question numbers)
        todo = []
        for paper_key, entry in LIBRARY.entries(subject):
            subj = entry["summary"]["subject"]
            if subj not in topic_lists:
                topic_lists[subj] = load_topics(subj).topics
            if not topic_lists[subj]:
                continue
            paper = _load_paper(paper_key)
            tags = read_tags(OUT_DIR / paper_key)
            numbers = [q.question_number for q in paper.questions if question_needs_tagging(q, tags)]
            if numbers:
                todo.append((paper_key, numbers))
        return todo

    try:
        LIBRARY.refresh()  # the index may not have been read yet on a fresh start
        if getattr(llm, "supports_batches", False):  # the API: one Message Batch, half price
            _tag_with_batches(llm, find_todo, topic_lists, update)
            return
        todo = find_todo()
        update(
            stage="Tagging",
            total=sum(len(n) for _, n in todo),
            papers_total=len(todo),
            no_topics=[s for s, t in topic_lists.items() if not t],
        )

        with ThreadPoolExecutor(max_workers=TAG_WORKERS) as pool:
            futures = {}
            queue = list(todo)
            while queue or futures:
                # keep TAG_WORKERS papers in flight, unless stopping or paused
                while queue and len(futures) < TAG_WORKERS and not _tag_state.get("stop"):
                    paper_key, numbers = queue.pop(0)
                    subj = LIBRARY.summary(paper_key)["subject"]
                    futures[pool.submit(_tag_paper, llm, paper_key, numbers, topic_lists[subj])] = (paper_key, numbers)
                    update(current=paper_key)
                if not futures:
                    break
                done_future = next(iter(as_completed(futures)))
                paper_key, numbers = futures.pop(done_future)
                try:
                    done_future.result()
                except llm_mod.UsageLimitError as e:
                    # stop taking new papers: the rest would fail the same way
                    update(stop=True, paused=str(e), resets_at=e.resets_at)
                    with _tag_lock:
                        _tag_state["failed"].append({"paper_key": paper_key, "error": str(e)[:300]})
                    continue
                except Exception as e:
                    with _tag_lock:
                        _tag_state["failed"].append({"paper_key": paper_key, "error": str(e)[:300]})
                    continue
                with _tag_lock:
                    _tag_state["done"] += len(numbers)
                    _tag_state["papers_done"] = _tag_state.get("papers_done", 0) + 1
    except Exception as e:
        update(error=_job_error("Tagging", e))
    finally:
        update(running=False, current=None, stage="Paused" if _tag_state.get("paused") else "Finished", finished_at=time.time())
        LIBRARY.refresh()


def _tag_with_batches(llm, find_todo, topic_lists: dict[str, list[Topic]], update) -> None:
    """Tagging on the API provider (qbx/batch_tagging.py), reporting into _tag_state."""

    def find_todo_counted():
        todo = find_todo()
        with _tag_lock:  # what's left, plus what was already collected this run
            _tag_state["total"] = _tag_state.get("done", 0) + sum(len(n) for _, n in todo)
            _tag_state["papers_total"] = _tag_state.get("papers_done", 0) + len(todo)
            _tag_state["no_topics"] = [s for s, t in topic_lists.items() if not t]
        return todo

    def add_done(n: int) -> None:
        with _tag_lock:
            _tag_state["done"] = _tag_state.get("done", 0) + n
            _tag_state["papers_done"] = _tag_state.get("papers_done", 0) + 1

    def fail(paper_key: str, error: str) -> None:
        with _tag_lock:
            _tag_state["failed"].append({"paper_key": paper_key, "error": error})

    def save(paper_key: str, fresh: dict) -> None:
        paper_dir = OUT_DIR / paper_key
        write_tags(paper_dir, merge_tags(read_tags(paper_dir), fresh))
        LIBRARY.invalidate(paper_key)

    def topics_for(paper_key: str) -> list[Topic]:
        subj = LIBRARY.summary(paper_key)["subject"]
        if subj not in topic_lists:
            topic_lists[subj] = load_topics(subj).topics
        return topic_lists[subj]

    hooks = batch_tagging.Hooks(
        load_paper=_load_paper, topics_for=topics_for, save=save, update=update,
        add_done=add_done, fail=fail, stopping=lambda: bool(_tag_state.get("stop")),
    )
    try:
        batch_tagging.run(llm, find_todo_counted, hooks)
    except llm_mod.UsageLimitError as e:
        update(stop=True, paused=str(e), resets_at=e.resets_at)
        return
    if batch_tagging.pending():  # stopped while a batch runs: it carries on at Anthropic
        update(paused="Claude's batch is still running - start tagging again to collect it.")


_resume_checked = False


def _resume_batch_tagging() -> None:
    """On the first /api/state after a start: a tagging batch the last run left behind is
    collected without waiting for anyone to press Tag."""
    global _resume_checked
    if _resume_checked:
        return
    _resume_checked = True
    try:
        if batch_tagging.pending() and llm_mod.active_settings().provider in ("api", "classark"):
            start_tagging()
    except Exception:
        pass  # Claude off, or no key: the batch waits for the next Tag press


@app.post("/api/tagging/start")
def start_tagging(subject: str | None = None) -> dict:
    """Tags every untagged (or changed) part, a paper per request, in the background;
    progress in /api/state's `tagging`. Subjects without a topic list are skipped. Safe to
    stop and start again: finished questions are never re-sent. Reaching the subscription's
    usage limit pauses it - start again once the limit resets."""
    llm = _require_llm("tag")
    with _tag_lock:
        if not _tag_state.get("running"):
            _tag_state.clear()
            _tag_state.update(
                running=True, stop=False, subject=subject, stage="Finding untagged questions",
                total=0, done=0, papers_total=0, papers_done=0, current=None, failed=[], no_topics=[],
                paused=None, resets_at=None, started_at=time.time(),
            )
            threading.Thread(target=_run_tagging, args=(subject, llm), daemon=True).start()
        return json.loads(json.dumps(_tag_state))


@app.post("/api/tagging/stop")
def stop_tagging() -> dict:
    with _tag_lock:
        if _tag_state.get("running"):
            _tag_state["stop"] = True
            _tag_state["stage"] = "Stopping"
        return json.loads(json.dumps(_tag_state))


# --- fixes: the Reviewer's edits (qbx/fixes.py) ---------------------------------------

@app.get("/api/papers/{paper_key}/fixes")
def list_fixes(paper_key: str) -> list[dict]:
    """Every edit recorded for a paper, newest first. Fixes Claude made before it stopped
    fixing papers (user-decided) are left out: they no longer apply."""
    return [f.model_dump() for f in reversed(fixes_mod.read_fixes(_paper_dir(paper_key))) if f.source != "ai"]


def _rebuild_paper(paper_key: str) -> Paper:
    """The paper again from the parser's result, the hand-drawn links and the applied fixes."""
    paper_dir = _paper_dir(paper_key)
    base = fixes_mod.read_base(paper_dir)
    if base is None:
        raise HTTPException(status_code=409, detail="This paper has no saved extraction to rebuild from - re-process it first.")
    paper = fixes_mod.rebuild(paper_dir, base, lambda p: apply_hand_links(p, paper_dir))
    _save_paper(paper_key, paper)
    return paper


def _find_fix(paper_key: str, fix_id: str) -> Fix:
    fix = next((f for f in fixes_mod.read_fixes(_paper_dir(paper_key)) if f.id == fix_id), None)
    if fix is None:
        raise HTTPException(status_code=404, detail=f"no fix {fix_id!r}")
    return fix


def _set_fix_status(paper_key: str, fix_id: str, status: str) -> Fix:
    paper_dir = _paper_dir(paper_key)
    all_fixes = fixes_mod.read_fixes(paper_dir)
    fix = next(f for f in all_fixes if f.id == fix_id)
    fix.status = status
    fixes_mod.write_fixes(paper_dir, all_fixes)
    LIBRARY.invalidate(paper_key)
    return fix


def _mark_checked(paper_key: str, keys: list[str], checked: bool) -> None:
    paper_dir = _paper_dir(paper_key)
    current = read_reviewed(paper_dir)
    current = current | set(keys) if checked else current - set(keys)
    (paper_dir / "review.json").write_text(json.dumps({"checked": sorted(current)}, indent=2), encoding="utf-8")
    LIBRARY.invalidate(paper_key)


def _only_notes(fix: Fix) -> bool:
    return all(step.op in fixes_mod.NOTES for step in fix.steps)


@app.post("/api/papers/{paper_key}/fixes/{fix_id}/undo")
def undo_fix(paper_key: str, fix_id: str) -> dict:
    """Takes an applied fix back out: the paper is rebuilt without it. A dismissed issue is
    reopened."""
    fix = _find_fix(paper_key, fix_id)
    if fix.status != "applied":
        return fix.model_dump()
    fix = _set_fix_status(paper_key, fix_id, "undone")
    if _only_notes(fix):
        if any(step.op == "dismiss" for step in fix.steps):
            _mark_checked(paper_key, fix.issues, False)
    else:
        _rebuild_paper(paper_key)
    return fix.model_dump()


@app.put("/api/papers/{paper_key}/tags")
def put_part_tags(paper_key: str, body: dict) -> dict:
    """A hand correction of one part's topics / what it depends on. Tagging never
    replaces a manual tag."""
    paper = _load_paper(paper_key)
    number, path = int(body["question_number"]), str(body.get("path", ""))
    question = next((q for q in paper.questions if q.question_number == number), None)
    if question is None or path not in [p.path for p in question.parts]:
        raise HTTPException(status_code=404, detail=f"no part {number} {path!r} in {paper_key}")
    order = [p.path for p in question.parts]
    earlier = set(order[: order.index(path)])
    known = {t.id for t in load_topics(paper.subject).topics}
    wanted_deps = set(body.get("depends_on", []))
    entry = {
        "topic_ids": [t for t in dict.fromkeys(body.get("topic_ids", [])) if t in known],
        "depends_on": [d for d in order if d in wanted_deps and d in earlier],
        "reason": "",
        "text_hash": "",
        "source": "manual",
    }
    paper_dir = _paper_dir(paper_key)
    tags = read_tags(paper_dir)
    tags[tag_key(number, path)] = entry
    write_tags(paper_dir, tags)
    LIBRARY.invalidate(paper_key)
    return entry


def _bank_subjects() -> list[dict]:
    """Subjects with their papers, for reading a request."""
    rows: dict[str, dict] = {}
    for _, entry in LIBRARY.entries():
        s = entry["summary"]
        row = rows.setdefault(
            s["subject"], {"subject": s["subject"], "board": s.get("board", ""), "name": s["subject_name"], "papers": {}}
        )
        row["papers"].setdefault(str(s["paper"]), s["paper_title"])
    return [
        {
            "subject": r["subject"],
            "board": r["board"],
            "name": r["name"],
            "papers": [{"paper": k, "title": v} for k, v in sorted(r["papers"].items())],
        }
        for r in rows.values()
    ]


def _picked_sources(body: dict) -> list[ExamSource]:
    """`sources` from the builder - [{subject, papers}] - or the older single `subject` +
    `papers`. A subject listed twice has its papers merged."""
    raw = body.get("sources")
    if raw is None and body.get("subject"):
        raw = [{"subject": body.get("subject"), "papers": body.get("papers") or []}]
    merged: dict[str, list[str]] = {}
    for item in raw or []:
        subject = str(item.get("subject") or "").strip()
        if not subject:
            continue
        papers = merged.setdefault(subject, [])
        for p in item.get("papers") or []:
            if str(p).strip() and str(p).strip() not in papers:
                papers.append(str(p).strip())
    return [ExamSource(subject=s, papers=p) for s, p in merged.items()]


def _understood(spec: ExamSpec, topics: list[Topic]) -> str:
    names = {t.id: t.name for t in topics}
    bits = [f"{spec.total_marks} marks"]
    if spec.topic_ids:
        bits.append(", ".join(names.get(t, t) for t in spec.topic_ids))
    if spec.sources:
        bits.append("; ".join(
            f"{src.subject} " + (f"paper {', '.join(src.papers)}" if src.papers else "any paper")
            for src in spec.sources
        ))
    elif spec.papers:
        bits.append("paper " + ", ".join(spec.papers))
    if spec.year_from or spec.year_to:
        bits.append(f"{spec.year_from or '…'}–{spec.year_to or '…'}")
    if spec.mcq_count:
        bits.append(f"{spec.mcq_count} multiple choice, the rest written")
    else:
        bits.append(
            {"structured": "no multiple choice", "mcq": "multiple choice only", "any": "any question type"}[spec.question_style]
        )
    if spec.difficulty in difficulty.LEVELS:
        bits.append(difficulty.LEVELS[spec.difficulty])
    return " · ".join(bits)


def _board_of_subject(subject: str | None) -> str | None:
    return next((s.get("board") for s in LIBRARY.subjects() if s["subject"] == subject), None)


def _one_board(boards: set[str], what: str) -> str | None:
    """The single board `boards` holds; a 400 naming them if there's more than one."""
    boards = {b for b in boards if b}
    if len(boards) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"An exam uses one exam board's papers only, but {what} come from "
            f"{' and '.join(sorted(boards))}.",
        )
    return next(iter(boards), None)


def _used_in_other_exams(name: str | None = None) -> set[tuple[str, int]]:
    """(paper key, question number) of every question set in a saved exam other than
    `name`. Claude-built exams never set these (user-asked): a class that sat one exam
    shouldn't meet its questions again in the next. Reprints of them are kept out too, by
    the builder's duplicate groups."""
    used: set[tuple[str, int]] = set()
    QUIZZES_DIR.mkdir(parents=True, exist_ok=True)
    for path in QUIZZES_DIR.glob("*.json"):
        try:
            quiz = Quiz.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if quiz.name != name:
            used.update((item.paper_key, item.question_number) for item in quiz.items)
    return used


def _build_from_spec(
    spec: ExamSpec, exclude: set[tuple[str, int]] | None = None, avoid: set[tuple[str, int]] | None = None
) -> tuple[dict, list[Topic]]:
    LIBRARY.refresh()
    topics = load_topics(spec.subject).topics if spec.subject else []
    subjects = [src.subject for src in spec.source_list()] or [spec.subject]
    # one board per exam, whatever else the spec says
    board = spec.board or _board_of_subject(spec.subject or subjects[0])
    entries = [
        entry for subject in subjects for entry in LIBRARY.entries(subject)
        if board is None or entry[1]["summary"].get("board") == board
    ]
    topics_by_subject = {s: load_topics(s).topics for s in subjects if s}
    result = build_preferring_new(
        entries, spec, topics, LIBRARY.duplicates(), topics_by_subject, LIBRARY.answer_clashes(), exclude, avoid=avoid
    )
    return result.__dict__, topics


def _unique_quiz_name(base: str) -> str:
    name, n = base, 2
    while _quiz_path(name).exists():
        name, n = f"{base} ({n})", n + 1
    return name


def _request_outcome(quiz: Quiz, built: dict, topics: list[Topic], unmatched: list[str]) -> dict:
    names = {t.id: t.name for t in topics}
    return {
        "name": quiz.name,
        "understood": quiz.request["understood"] if quiz.request else "",
        "total_marks": built["total_marks"],
        "target_marks": quiz.request["spec"]["total_marks"] if quiz.request else built["total_marks"],
        "exact": built["exact"],
        "n_items": len(built["items"]),
        "missing_topics": [names.get(t, t) for t in built["missing_topics"]] + list(unmatched),
    }


@app.post("/api/exam/from_request")
def exam_from_request(body: dict) -> dict:
    """A typed request in, a saved exam out: Claude reads the request, exam_builder picks
    the questions, and the exam is saved under a name taken from the request."""
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Type what the exam should cover.")
    llm = _require_llm("request")
    LIBRARY.refresh()
    subjects = _bank_subjects()
    if not subjects:
        raise HTTPException(status_code=400, detail="There are no papers in the bank yet.")
    # Subjects and papers picked in the builder: exactly these, whatever the sentence says,
    # and Claude reads the request against those subjects alone.
    board = str(body.get("board") or "").strip() or None
    if board:
        subjects = [s for s in subjects if s["board"] == board]
        if not subjects:
            raise HTTPException(status_code=400, detail=f"There are no {board} papers in the bank.")
    sources = _picked_sources(body)
    if sources:
        board_of = {s["subject"]: s["board"] for s in subjects}
        _one_board({board_of.get(src.subject, "") for src in sources}, "the subjects picked")
        in_bank = {s["subject"] for s in subjects}
        missing = [src.subject for src in sources if src.subject not in in_bank]
        if missing:
            raise HTTPException(status_code=400, detail=f"There are no {', '.join(missing)} papers in the bank.")
        picked = {src.subject for src in sources}
        subjects = [s for s in subjects if s["subject"] in picked]
    topics_by_subject = {s["subject"]: load_topics(s["subject"]).topics for s in subjects}
    topics_by_subject = {k: v for k, v in topics_by_subject.items() if v}
    seed = int(body.get("seed") or time.time_ns() % 1_000_000)
    try:
        read = read_request(llm, text, subjects, topics_by_subject, seed=seed)
    except Exception as e:
        raise _llm_http_error(e) from e
    if sources:
        subject = read.spec.subject if read.spec.subject in {src.subject for src in sources} else sources[0].subject
        read.spec = read.spec.model_copy(update={"subject": subject, "sources": sources, "papers": []})
    if read.spec.subject is None:
        raise HTTPException(status_code=400, detail="Couldn't tell which subject that's for - name the subject in the request.")
    read.spec = read.spec.model_copy(update={"board": board or _board_of_subject(read.spec.subject)})
    built, topics = _build_from_spec(read.spec, _used_in_other_exams())
    if not built["items"]:
        detail = "No questions in the bank match that request, leaving out those already in your other exams."
        if not topics:
            detail += " This subject has no topic list yet - add one in Settings, then tag its questions."
        raise HTTPException(status_code=400, detail=detail)
    quiz = Quiz(
        name=_unique_quiz_name(body.get("name") or f"{read.name} – {read.spec.total_marks} marks"),
        items=[QuizItem(**i) for i in built["items"]],
        request={
            "text": text,
            "spec": read.spec.model_dump(),
            "understood": _understood(read.spec, topics),
            "unmatched": read.unmatched,
        },
    )
    quiz.remember(quiz.items)
    _save_quiz(quiz)
    return _request_outcome(quiz, built, topics, read.unmatched)


@app.post("/api/quizzes/{name}/reroll")
def reroll_quiz(name: str) -> dict:
    """Builds an exam made from a request again, with different questions."""
    quiz = _load_quiz(name)
    if not quiz.request:
        raise HTTPException(status_code=400, detail="This exam wasn't built from a request.")
    spec = ExamSpec(**quiz.request["spec"])
    spec.seed += 1
    quiz.remember(quiz.items)
    built, topics = _build_from_spec(spec, _used_in_other_exams(name), {tuple(k) for k in quiz.seen})
    quiz.items = [QuizItem(**i) for i in built["items"]]
    quiz.remember(quiz.items)
    quiz.request["spec"] = spec.model_dump()
    _save_quiz(quiz)
    return _request_outcome(quiz, built, topics, quiz.request.get("unmatched", []))


def _spec_for(quiz: Quiz, total: int) -> ExamSpec:
    """The spec an exam was made from, or for one put together by hand, one read off its
    questions: their subject codes and board, no particular topics."""
    if quiz.request:
        return ExamSpec(**quiz.request["spec"])
    # the papers it already draws on, per subject: a hand-made exam from papers 2 and 4
    # mustn't get a paper 5 question when it's changed (user-reported)
    papers: dict[str, list[str]] = {}
    for summary in (LIBRARY.summary(i.paper_key) for i in quiz.items):
        if summary:
            used = papers.setdefault(summary["subject"], [])
            if str(summary["paper"]) not in used:
                used.append(str(summary["paper"]))
    subjects = list(papers)
    if not subjects:
        raise HTTPException(status_code=400, detail="Add a question to this exam first.")
    return ExamSpec(
        subject=subjects[0],
        sources=[ExamSource(subject=s, papers=papers[s]) for s in subjects],
        board=_board_of_subject(subjects[0]),
        question_style="any",
        total_marks=total,
    )


@app.post("/api/quizzes/{name}/adjust")
def adjust_quiz(name: str, body: dict) -> dict:
    """Changes an exam from a typed instruction ("replace Q3 with an electrolysis question",
    "make it 30 marks"): Claude reads the instruction (exam_adjust.py), code decides which
    questions come out and go in. Works on any exam, made from a request or by hand."""
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Type how the exam should change.")
    quiz = _load_quiz(name)
    if not quiz.items:
        raise HTTPException(status_code=400, detail="Add a question to this exam first.")
    llm = _require_llm("adjust")
    LIBRARY.refresh()
    items = exam_items([i.model_dump() for i in quiz.items], dict(LIBRARY.entries()))
    spec = _spec_for(quiz, sum(i.total for i in items))
    topics = load_topics(spec.subject).topics if spec.subject else []
    try:
        adjustment = read_adjustment(llm, text, items, spec, topics)
    except Exception as e:
        raise _llm_http_error(e) from e
    picked = {p for src in spec.source_list() for p in src.papers}
    if quiz.request and picked and adjustment.papers:
        # papers picked in the builder stand, whatever Claude read into the instruction
        adjustment.papers = [p for p in adjustment.papers if p in picked]
    subjects = [src.subject for src in spec.source_list()] or [spec.subject]
    board = spec.board or _board_of_subject(spec.subject or subjects[0])
    entries = [
        entry for subject in subjects for entry in LIBRARY.entries(subject)
        if board is None or entry[1]["summary"].get("board") == board
    ]
    topics_by_subject = {s: load_topics(s).topics for s in subjects if s}
    result = apply_adjustment(
        items, adjustment, spec, entries, topics, LIBRARY.duplicates(), topics_by_subject, LIBRARY.answer_clashes(),
        _used_in_other_exams(name),
        {tuple(k) for k in quiz.seen} | {(i.paper_key, i.question_number) for i in quiz.items},
    )
    if not result.items:
        raise HTTPException(status_code=400, detail="That would leave the exam with no questions.")
    quiz.remember(quiz.items)  # what it held before the change
    quiz.items = [QuizItem(**i) for i in result.items]
    quiz.remember(quiz.items)
    if quiz.request:
        # what a later "Try different questions" builds: the new total, and any new topics
        new_spec = spec.model_copy(update={
            "total_marks": result.target,
            "topic_ids": list(dict.fromkeys(spec.topic_ids + adjustment.topic_ids)) if spec.topic_ids else [],
            "difficulty": adjustment.difficulty or spec.difficulty,
        })
        quiz.request["spec"] = new_spec.model_dump()
        quiz.request["understood"] = _understood(new_spec, topics)
        quiz.request.setdefault("adjustments", []).append(text)
    _save_quiz(quiz)
    names = {t.id: t.name for t in topics}
    return {
        "name": quiz.name,
        "understood": adjustment.summary,
        "total_marks": result.total_marks,
        "target_marks": result.target,
        "exact": result.exact,
        "n_items": len(result.items),
        "removed": result.removed,
        "added": result.added,
        "missing_topics": [names.get(t, t) for t in result.missing_topics] + adjustment.unmatched,
    }


# the most of a conversation kept with its exam
MAX_SAVED_CHAT = 200


@app.post("/api/chat")
def exam_chat(body: dict) -> dict:
    """One turn of the Claude tab's conversation: the messages so far (the teacher's last),
    the open exam if any, and the board/subjects picked. Claude replies and may name one
    action - make, change or reroll - which runs through the same endpoints as the
    one-line request and "Change this exam". The conversation is saved with the exam it
    made or changed. Returns {message, action, quiz}: Claude's message (with what the
    action did, or why it couldn't) and the exam open after it."""
    try:
        messages = [ChatMessage.model_validate(m) for m in body.get("messages") or []]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"bad conversation: {e}") from e
    if not messages or messages[-1].role != "teacher" or not messages[-1].text.strip():
        raise HTTPException(status_code=400, detail="Type a message for Claude.")
    llm = _require_llm("chat")
    LIBRARY.refresh()
    name = str(body.get("quiz") or "").strip() or None
    quiz = _load_quiz(name) if name else None

    exam = None
    board = str(body.get("board") or "").strip() or None
    if quiz is not None:
        items = exam_items([i.model_dump() for i in quiz.items], dict(LIBRARY.entries()))
        spec = _spec_for(quiz, sum(i.total for i in items)) if quiz.items else None
        topics = load_topics(spec.subject).topics if spec and spec.subject else []
        exam = (quiz.name, items, spec, topics)
        if spec is not None:
            board = spec.board or board  # an exam keeps to its own board
    subjects = [s for s in _bank_subjects() if board is None or s["board"] == board]
    picked = {src.subject for src in _picked_sources(body)}
    if picked:
        subjects = [s for s in subjects if s["subject"] in picked] or subjects
    topics_by_subject = {s["subject"]: load_topics(s["subject"]).topics for s in subjects}
    topics_by_subject = {k: v for k, v in topics_by_subject.items() if v}
    try:
        turn = read_turn(llm, messages, subjects, topics_by_subject, exam)
    except Exception as e:
        raise _llm_http_error(e) from e

    outcome: dict | None = None
    error: str | None = None
    open_name = name
    try:
        if turn.action == "make":
            outcome = exam_from_request({"text": turn.instruction, "board": board or "", "sources": body.get("sources") or []})
            open_name = outcome["name"]
        elif turn.action == "change":
            outcome = adjust_quiz(name, {"text": turn.instruction})
        elif turn.action == "reroll":
            outcome = reroll_quiz(name)
    except HTTPException as e:
        error = str(e.detail)
    if turn.action == "reroll" and error and quiz is not None and not quiz.request:
        error = "This exam was put together by hand, so ask me to change particular questions instead."

    message = ChatMessage(role="claude", text=turn.reply, outcome=outcome, error=error)
    if open_name:
        saved = _load_quiz(open_name)
        saved.chat = [m.model_dump() for m in [*messages, message]][-MAX_SAVED_CHAT:]
        _save_quiz(saved)
    return {"message": message.model_dump(), "action": turn.action, "quiz": open_name}


# -- Classification mode: topical booklets (qbx/classified.py) ---------------------------
# Every past question on a topic, gathered into a booklet per topic, from the topic tags
# the parts already carry. Not an exam: no marks target, no avoiding other exams'
# questions, no Claude. Composing never reads or writes a saved exam.


def _classified_spec(body: dict) -> ClassifiedSpec:
    LIBRARY.refresh()
    sources = _picked_sources(body)
    if not sources:
        raise HTTPException(status_code=400, detail="Choose a subject first.")
    board_of = {s["subject"]: s.get("board", "") for s in LIBRARY.subjects()}
    missing = [src.subject for src in sources if src.subject not in board_of]
    if missing:
        raise HTTPException(status_code=400, detail=f"There are no {', '.join(missing)} papers in the bank.")
    board = _one_board({board_of[src.subject] for src in sources}, "the subjects picked")
    asked = str(body.get("board") or "").strip()
    if asked and board and asked != board:
        raise HTTPException(status_code=400, detail=f"{sources[0].subject} isn't a {asked} subject.")

    def year(key: str) -> int | None:
        try:
            return int(body[key]) if body.get(key) not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="A year must be a number, e.g. 2019.") from None

    style = str(body.get("question_style") or "any")
    if style not in ("any", "mcq", "structured"):
        raise HTTPException(status_code=400, detail=f"Unknown question style {style!r}.")
    topic_ids = [str(t).strip() for t in body.get("topic_ids") or [] if str(t).strip()]
    return ClassifiedSpec(
        board=board, subject=sources[0].subject, sources=sources, topic_ids=list(dict.fromkeys(topic_ids)),
        year_from=year("year_from"), year_to=year("year_to"), question_style=style,
    )


def _classified_entries(spec: ClassifiedSpec) -> list[tuple[str, dict]]:
    return [e for src in spec.sources for e in LIBRARY.entries(src.subject)]


def _classified(spec: ClassifiedSpec) -> list[TopicGroup]:
    topics = load_topics(spec.subject).topics
    known = {t.id for t in topics}
    unknown = [t for t in spec.topic_ids if t not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Not in {spec.subject}'s topic list: {', '.join(unknown)}.")
    topics_by_subject = {src.subject: load_topics(src.subject).topics for src in spec.sources}
    return classify(_classified_entries(spec), spec, topics, LIBRARY.duplicates(), topics_by_subject)


def _paper_names() -> dict[str, str]:
    """Paper key -> "0620/42 May/June 2023", as the preview and sources list name papers."""
    return {key: f"{paper_label(e['summary'])} {sitting_label(e['summary'])}" for key, e in LIBRARY.entries()}


@app.post("/api/classified/preview")
def classified_preview(body: dict) -> dict:
    """What each topic's booklet would hold: its questions (paper, question, parts used,
    reprints) and marks, plus how many parts in the chosen papers have no tags and so
    can't be classified."""
    spec = _classified_spec(body)
    groups = _classified(spec) if spec.topic_ids else []
    summaries = {key: entry["summary"] for key, entry in LIBRARY.entries()}
    names = _paper_names()

    def item_json(item) -> dict:
        s = summaries[item.paper_key]
        return {
            **item.ref(),
            "marks": item.marks,
            "mcq": item.mcq,
            "paper": paper_label(s),
            "sitting": sitting_label(s),
            "parts": parts_label(item),
            "also_in": [names[k] for k in item.also_in],
        }

    scope = in_scope(_classified_entries(spec), spec)
    return {
        "topics": [
            {
                "topic_id": g.topic_id, "name": g.name, "n_questions": len(g.items),
                "total_marks": g.total_marks, "items": [item_json(i) for i in g.items],
            }
            for g in groups
        ],
        "untagged_parts": untagged_parts(scope, spec),
        "n_parts": sum(len(parts) for _, e in scope for parts in e["parts"]),
    }


def _booklet_file_name(group: TopicGroup) -> str:
    return _UNSAFE_FILENAME_RE.sub("_", f"{group.topic_id} {group.name}").strip("_")[:80] or "topic"


def _subject_title(spec: ClassifiedSpec) -> str:
    """"Chemistry 0620" (or "Chemistry 0620, 0971"), for the booklet's cover."""
    names = {s["subject"]: s["name"] for s in LIBRARY.subjects()}
    codes = ", ".join(src.subject for src in spec.sources)
    return f"{names.get(spec.subject or '', '')} {codes}".strip()


def _booklet_jobs(body: dict, spec: ClassifiedSpec) -> list[tuple[TopicGroup, list[QuizGroup]]]:
    """Each topic with questions left, as the QuizGroups its booklet is composed from.
    `excluded`: [{topic_id, paper_key, question_number}] the teacher left out in the
    preview (with no topic_id: left out of every topic)."""
    left_out = set()
    for x in body.get("excluded") or []:
        try:
            left_out.add((str(x.get("topic_id") or ""), str(x["paper_key"]), int(x["question_number"])))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise HTTPException(
                status_code=400, detail="Each left-out question needs its paper_key and question_number."
            ) from None
    names = _paper_names()
    jobs = []
    for group in _classified(spec):
        items = [
            i for i in group.items
            if (group.topic_id, i.paper_key, i.question_number) not in left_out
            and ("", i.paper_key, i.question_number) not in left_out
        ]
        if items:
            jobs.append((group, [
                QuizGroup(
                    paper_key=i.paper_key, question_number=i.question_number, part_paths=i.part_paths,
                    also_in=[names[k] for k in i.also_in],
                )
                for i in items
            ]))
    return jobs


# Composing runs in a background thread, like a scan: measured on 0610 (131 papers), one
# topic of 237 questions took ~25s for its question booklet and ~60s for its mark scheme.
# Progress shows in /api/state's `classified`; the finished zip waits in a temp file
# until the next run replaces it.
_classified_lock = threading.Lock()
_classified_state: dict = {"running": False}
_CLASSIFIED_DIR = Path(tempfile.gettempdir()) / "qbx-classified"


def _write_booklets(
    jobs: list[tuple[TopicGroup, list[QuizGroup]]], subject_title: str, footer_note: str, path: Path
) -> None:
    def step() -> None:
        with _classified_lock:
            _classified_state["done"] = _classified_state.get("done", 0) + 1

    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for group, quiz_groups in jobs:
                name = _booklet_file_name(group)
                options = {
                    "title": group.name, "booklet": True, "subject_name": subject_title,
                    "footer_note": footer_note, "progress": step,
                }
                for kind, compose in (("question booklet", compose_qp_pdf), ("mark scheme", compose_ms_pdf)):
                    with _classified_lock:
                        _classified_state["current"] = f"{group.name}: {kind}"
                    pdf = compose(quiz_groups, OUT_DIR, **options)
                    archive.writestr(f"{name}_{'qp' if compose is compose_qp_pdf else 'ms'}.pdf", pdf)
        with _classified_lock:
            _classified_state.update(running=False, ready=True, current=None)
    except Exception as e:  # noqa: BLE001 - reported to the UI, not lost in a thread
        path.unlink(missing_ok=True)
        with _classified_lock:
            _classified_state.update(running=False, ready=False, current=None, error=_job_error("Making the booklets", e))


@app.post("/api/classified/export")
def classified_export(body: dict) -> dict:
    """Starts composing each chosen topic's question booklet and mark scheme into one zip,
    in the background; GET /api/classified/download/{id} fetches it once `ready`."""
    spec = _classified_spec(body)
    if not spec.topic_ids:
        raise HTTPException(status_code=400, detail="Choose at least one topic.")
    jobs = _booklet_jobs(body, spec)
    if not jobs:
        raise HTTPException(status_code=400, detail="No questions left to put in a booklet.")
    with _classified_lock:
        if _classified_state.get("running"):
            raise HTTPException(status_code=409, detail="Booklets are already being made - wait for those first.")
        job_id = int(_classified_state.get("id", 0)) + 1
        _CLASSIFIED_DIR.mkdir(parents=True, exist_ok=True)
        for old in _CLASSIFIED_DIR.glob("*.zip"):
            old.unlink(missing_ok=True)  # the previous run's, already offered
        zip_name = _UNSAFE_FILENAME_RE.sub("_", "_".join(s.subject for s in spec.sources) + "_topics")
        _classified_state.clear()
        _classified_state.update(
            id=job_id, running=True, ready=False, error=None, done=0,
            total=2 * sum(len(quiz_groups) for _, quiz_groups in jobs), n_topics=len(jobs),
            current=None, file_name=f"{zip_name}.zip",
            title=" ".join(s.subject for s in spec.sources) + " topic booklets",
            content_print=saving.fingerprint({k: v for k, v in body.items()}),
        )
        state = dict(_classified_state)
    path = _CLASSIFIED_DIR / f"{job_id}.zip"
    threading.Thread(
        target=_write_booklets,
        args=(jobs, _subject_title(spec), str(body.get("footer_note") or ""), path),
        daemon=True,
    ).start()
    return state


@app.get("/api/classified/state")
def classified_state() -> dict:
    with _classified_lock:
        return dict(_classified_state)


@app.post("/api/classified/save/{job_id}")
def classified_save(job_id: int) -> dict:
    """The finished booklets, written into the save folder's "Topic booklets" folder."""
    with _classified_lock:
        ready = _classified_state.get("ready") and _classified_state.get("id") == job_id
        title = _classified_state.get("title") or "Topic booklets"
        content_print = _classified_state.get("content_print") or ""
    path = _CLASSIFIED_DIR / f"{job_id}.zip"
    if not ready or not path.exists():
        raise HTTPException(status_code=404, detail="Those booklets aren't ready, or were replaced by a later run.")
    files = {}
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            stem = member.rsplit(".", 1)[0]
            label = " - Question booklet" if stem.endswith("_qp") else " - Mark scheme" if stem.endswith("_ms") else ""
            name = saving.safe_name(stem[:-3] if label else stem, "Topic") + label + ".pdf"
            files[name] = archive.read(member)
    return _saved(lambda: saving.save_files("booklets", title, "Topic booklets", files, content_print))


@app.get("/api/classified/download/{job_id}")
def classified_download(job_id: int) -> Response:
    with _classified_lock:
        ready = _classified_state.get("ready") and _classified_state.get("id") == job_id
        file_name = _classified_state.get("file_name", "topics.zip")
    path = _CLASSIFIED_DIR / f"{job_id}.zip"
    if not ready or not path.exists():
        raise HTTPException(status_code=404, detail="Those booklets aren't ready, or were replaced by a later run.")
    return Response(
        content=path.read_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


# The built UI (ui/dist, from `npm run build`), served by this same process when present -
# which is how the packaged app runs: one local server, one origin, no Node.js needed.
# Mounted last so every /api route above takes precedence. In development the UI is served
# by Vite instead, and this directory usually doesn't exist.
UI_DIR = Path(os.environ.get("QBX_UI_DIR", Path(__file__).parent.parent / "ui" / "dist")).resolve()


class _UIFiles(StaticFiles):
    """The built UI, with caching told apart: the page and anything not under assets/ is
    checked with the app on every load, while assets/ (content-hashed names) keeps for a
    year. Without this WebView2 cached index.html heuristically, by its Last-Modified, and
    after an update kept showing the old build's page and scripts (user-reported)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.replace("\\", "/").startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


if (UI_DIR / "index.html").exists():
    app.mount("/", _UIFiles(directory=UI_DIR, html=True), name="ui")
