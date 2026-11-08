"""Pair up the PDFs in a folder - question papers with their own mark schemes - so a folder
of papers added by hand can be processed in one go.

Files dropped in by hand can be named anything, so pairing goes by what each file *is*,
read from its cover (pairing.identify): a question paper and a mark scheme pair when they
carry the same paper code, session and year. Nothing is guessed: a file that can't be
paired is reported with the reason, never extracted against the wrong partner.
"""

import json
from collections.abc import Callable
from pathlib import Path

import pymupdf
from pydantic import BaseModel

from qbx import edexcel
from qbx.pairing import DocumentIdentity, identify


class PlannedPair(BaseModel):
    paper_key: str  # what extraction will name it, e.g. "0620_41_MJ_24"
    qp_path: Path
    ms_path: Path


class Unpaired(BaseModel):
    path: Path
    reason: str


class ScanPlan(BaseModel):
    pairs: list[PlannedPair] = []
    unpaired: list[Unpaired] = []


def paper_key(identity: DocumentIdentity) -> str | None:
    """The key fingerprint.Fingerprint.paper_key will give the extracted paper - so a pair
    already extracted can be recognised before anything is run."""
    if not (identity.code and identity.session and identity.year):
        return None
    return f"{identity.code.replace('/', '_')}_{identity.session.replace('/', '')}_{identity.year % 100:02d}"


def _has_text(path: Path) -> bool:
    """Whether its cover has a text layer - what says what a file is. Edexcel's June 2019 1C
    download is page images for its first eight pages, cover included."""
    with pymupdf.open(path) as doc:
        return doc.page_count > 0 and bool(doc[0].get_text("text").strip())


def _read_file(path: Path) -> dict:
    """What one PDF is: {"identity": ...} or {"reason": why it can't be paired}. Run in a
    worker process for a large batch, so it takes and returns plain data."""
    try:
        identity = identify(path)
    except Exception as e:  # a damaged or password-protected PDF, say
        return {"reason": f"couldn't be opened ({e})"}
    if identity.kind == "insert":
        return {"reason": "an insert, not a question paper or mark scheme"}
    if identity.kind == "unknown":
        if not _has_text(path):  # Edexcel's June 2019 1C download is page images only
            return {"reason": "a scanned PDF (its cover is a picture, with no text to read)"}
        return {"reason": "couldn't tell whether it's a question paper or a mark scheme"}
    if paper_key(identity) is None:
        return {"reason": f"a {identity.kind}, but its paper code or sitting couldn't be read"}
    return {"identity": identity.model_dump()}


# Bumped whenever what a file reads as can change (pairing.py, edexcel.py, fingerprint.py),
# so a cache from an older build is read again rather than trusted
IDENTIFY_VERSION = 1
# below this many files to read, worker processes cost more to start than they save
_PARALLEL_FROM = 24


def _stamp(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def _load_cache(cache_path: Path | None) -> dict:
    if cache_path is None:
        return {}
    try:
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved.get("files", {}) if saved.get("version") == IDENTIFY_VERSION else {}


def _save_cache(cache_path: Path | None, files: dict) -> None:
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": IDENTIFY_VERSION, "files": files}), encoding="utf-8")
        tmp.replace(cache_path)
    except OSError:
        pass  # only a cache: the next scan reads the files again


def plan_scan(
    folder: Path,
    progress: Callable[[int, int], None] | None = None,
    cache_path: Path | None = None,
) -> ScanPlan:
    """`progress(done, total)` is called as files are read, so a long pairing shows it's
    moving.

    Reading every cover took minutes once the folder held 2,000 PDFs (user-reported), on
    every scan. What each file is, is cached in `cache_path` by path, size and modified time
    - the path too, since an Edexcel file's sitting comes from its folder's name - so a
    re-scan opens only new or changed files; and those are read in parallel, with the
    scan's own worker count."""
    plan = ScanPlan()
    question_papers: dict[str, list[Path]] = {}
    mark_schemes: dict[str, list[Path]] = {}

    # Subfolders too: Edexcel papers are filed as "June 2025\Question-paper\...", and the
    # folder names are what say their sitting (qbx/edexcel.py).
    paths = sorted(folder.rglob("*.pdf"))
    cached = _load_cache(cache_path)
    results: dict[Path, dict] = {}
    stamps: dict[Path, list[int]] = {}
    to_read: list[Path] = []
    for path in paths:
        if edexcel.read_name(path.relative_to(folder)).kind == "report":
            results[path] = {"reason": "an examiner report, not a question paper or mark scheme"}
            continue
        try:
            stamps[path] = _stamp(path)
        except OSError as e:
            results[path] = {"reason": f"couldn't be opened ({e})"}
            continue
        hit = cached.get(str(path))
        if hit and hit.get("stamp") == stamps[path]:
            results[path] = hit["result"]
        else:
            to_read.append(path)

    done = len(paths) - len(to_read)
    if progress:
        progress(done, len(paths))
    workers = min(scan_workers(), len(to_read))
    if workers > 1 and len(to_read) >= _PARALLEL_FROM:
        from concurrent.futures import ProcessPoolExecutor, as_completed

        try:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_read_file, path): path for path in to_read}
                for future in as_completed(futures):
                    results[futures[future]] = future.result()
                    done += 1
                    if progress:
                        progress(done, len(paths))
        except Exception:
            # the pool itself failing (a worker that couldn't start) says nothing about the
            # files: whatever it didn't read is read here instead - never cached as unreadable
            pass
    for path in to_read:
        if path not in results:
            results[path] = _read_file(path)
            done += 1
            if progress:
                progress(done, len(paths))

    _save_cache(
        cache_path,
        {str(path): {"stamp": stamps[path], "result": results[path]} for path in paths if path in stamps},
    )

    for path in paths:
        result = results[path]
        if "reason" in result:
            plan.unpaired.append(Unpaired(path=path, reason=result["reason"]))
            continue
        identity = DocumentIdentity(**result["identity"])
        by_kind = question_papers if identity.kind == "question paper" else mark_schemes
        by_kind.setdefault(paper_key(identity), []).append(path)

    for key in sorted(set(question_papers) | set(mark_schemes)):
        qps, mss = question_papers.get(key, []), mark_schemes.get(key, [])
        if qps and mss:
            plan.pairs.append(PlannedPair(paper_key=key, qp_path=qps[0], ms_path=mss[0]))
            # the same paper added twice (an upload's "_1" copy, say) - one of each is enough
            for extra in qps[1:] + mss[1:]:
                plan.unpaired.append(Unpaired(path=extra, reason=f"a second copy of {key}"))
        elif qps:
            for path in qps:
                plan.unpaired.append(Unpaired(path=path, reason=f"question paper {key} - its mark scheme isn't in the folder"))
        else:
            for path in mss:
                plan.unpaired.append(Unpaired(path=path, reason=f"mark scheme {key} - its question paper isn't in the folder"))
    return plan


def scan_workers() -> int:
    """How many papers a scan extracts at once: every core but two, so the computer stays
    usable (16 cores here: a 300-paper scan went from ~33 minutes on one to a few). The
    `QBX_SCAN_WORKERS` environment variable overrides it; 1 runs them one at a time."""
    import os

    try:
        return max(1, int(os.environ["QBX_SCAN_WORKERS"]))
    except (KeyError, ValueError):
        return max(1, (os.cpu_count() or 1) - 2)


def extract_pair(qp_path: Path, ms_path: Path, out_dir: Path) -> dict:
    """One pair's extraction, in a scan's worker process. Errors come back as text: the
    pipeline's own exception types needn't survive the trip between processes."""
    from qbx.fingerprint import FingerprintError
    from qbx.pairing import PairingError
    from qbx.pipeline import ScannedPdfError, run_extraction

    try:
        paper, report = run_extraction(qp_path, ms_path, out_dir)
    except (ScannedPdfError, FingerprintError, PairingError) as e:
        return {"error": str(e)}
    except Exception as e:  # one bad PDF mustn't abort re-processing hundreds
        return {"error": f"unexpected error: {type(e).__name__}: {e}"}
    return {
        "paper_key": paper.paper_key,
        "n_questions": len(paper.questions),
        "passed": report.passed,
        "n_issues": len(report.issues),
    }
