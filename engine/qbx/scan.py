"""Pair up the PDFs in a folder - question papers with their own mark schemes - so a folder
of papers added by hand can be processed in one go.

Files dropped in by hand can be named anything, so pairing goes by what each file *is*,
read from its cover (pairing.identify): a question paper and a mark scheme pair when they
carry the same paper code, session and year. Nothing is guessed: a file that can't be
paired is reported with the reason, never extracted against the wrong partner.
"""

import json
import sys
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
    # an insert (a case study booklet) by the paper key of its question paper - qbx/inserts.py
    inserts: dict[str, Path] = {}


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
    if identity.kind == "unknown":
        if not _has_text(path):  # Edexcel's June 2019 1C download is page images only
            return {"reason": "a scanned PDF (its cover is a picture, with no text to read)"}
        return {"reason": "couldn't tell whether it's a question paper or a mark scheme"}
    if paper_key(identity) is None:
        return {"reason": f"a {identity.kind}, but its paper code or sitting couldn't be read"}
    return {"identity": identity.model_dump()}


# Bumped whenever what a file reads as can change (pairing.py, edexcel.py, fingerprint.py),
# so a cache from an older build is read again rather than trusted
IDENTIFY_VERSION = 4  # 4: inserts identified, for their question papers
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
    inserts: dict[str, list[Path]] = {}

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
            with ProcessPoolExecutor(max_workers=workers, initializer=lower_priority) as pool:
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
        if identity.kind == "insert":
            inserts.setdefault(paper_key(identity), []).append(path)
            continue
        by_kind = question_papers if identity.kind == "question paper" else mark_schemes
        by_kind.setdefault(paper_key(identity), []).append(path)

    for key, found in sorted(inserts.items()):
        if key not in question_papers:
            for path in found:
                plan.unpaired.append(Unpaired(path=path, reason=f"insert for {key} - its question paper isn't in the folder"))
            continue
        plan.inserts[key] = found[0]
        for extra in found[1:]:
            plan.unpaired.append(Unpaired(path=extra, reason=f"a second copy of {key}'s insert"))

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


# Memory per worker process. Measured peak: ~150 MB per paper (0620/42 MJ22 146 MB, 4MA1
# 1H June 2019 151 MB, 125 MB of it the imports); 200 leaves room for big A3 papers.
WORKER_MB = 200
# kept free for the app's own window, the server and the rest of the computer. Was 1024
# with 300 a worker: a 16-core, 16 GB computer with a browser open (1.3 GB free) scanned
# one paper at a time (user-reported: "quite slow now")
RESERVE_MB = 512
# however little is free: Windows pages a couple of 150 MB workers out happily, and one at
# a time is 5-10x slower than the computer can go
MIN_WORKERS = 2


def available_memory_mb() -> int | None:
    """Memory free for new processes right now, or None if the system won't say."""
    import os
    import sys

    try:
        if sys.platform == "win32":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("available", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                            ("page_available", ctypes.c_ulonglong), ("virtual_total", ctypes.c_ulonglong),
                            ("virtual_available", ctypes.c_ulonglong), ("extended", ctypes.c_ulonglong)]

            status = _Status()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.available // 2**20
            return None
        if sys.platform == "darwin":
            # macOS reports no "available" figure through sysconf; it compresses and frees
            # memory freely, so half the total is a fair stand-in
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 2 // 2**20
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES") // 2**20
    except (OSError, ValueError, AttributeError):
        return None


def lower_priority() -> None:
    """A scan worker's initializer: below normal priority, so the app's window and server
    always come first and a scan only takes the CPU nothing else wants (user-reported: the
    app was unbearable to use while a scan ran on every core but two). Takes nothing from a
    scan's speed on an otherwise idle computer."""
    import os

    try:
        if sys.platform == "win32":
            import ctypes

            BELOW_NORMAL_PRIORITY_CLASS = 0x4000
            kernel32 = ctypes.windll.kernel32
            # the process handle is pointer-sized: as a plain int it's cut to 32 bits, and fails
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(10)
    except Exception:
        pass  # the scan still runs, just at normal priority


def scan_workers() -> int:
    """How many papers a scan extracts at once, set from the computer: every core but two,
    so it stays usable (16 cores here: a 300-paper scan went from ~33 minutes on one to a
    few), and no more than the free memory holds at WORKER_MB each after RESERVE_MB. The
    `QBX_SCAN_WORKERS` environment variable overrides it; 1 runs them one at a time."""
    import os

    try:
        return max(1, int(os.environ["QBX_SCAN_WORKERS"]))
    except (KeyError, ValueError):
        pass
    by_cores = (os.cpu_count() or 1) - 2
    free = available_memory_mb()
    by_memory = (free - RESERVE_MB) // WORKER_MB if free is not None else by_cores
    return max(min(MIN_WORKERS, max(1, by_cores)), min(by_cores, by_memory))


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
