"""An in-memory index of every extracted paper, so the reviewer and question bank don't
re-read hundreds of paper.json files on every request.

Measured on a real corpus (398 papers, 5.6 GB of out/): listing papers or questions meant
reading and validating every paper.json each time - ~0.5 s warm, several seconds cold
with Defender scanning the folder - and the question bank then shipped 10 MB of JSON per
search keystroke. The index keeps a small summary of each paper plus a lowercased search
haystack per question, re-reads a paper only when its files' mtimes change, and is saved to
one file in out/ so a cold start reads that instead of every paper.

`version` goes up whenever anything in the index changes - what the UI polls to know when to
refetch, so a paper appears the moment its extraction finishes.
"""

import json
import os
import threading
from pathlib import Path

from qbx.quiz_export import is_multiple_choice
from qbx import difficulty
from qbx.inserts import cover_needs_insert
from qbx.inserts import index as insert_index
from qbx.schema import Paper
from qbx.similar import answer_clashes, answer_signature, duplicate_groups, question_signatures
from qbx.tagging import TAGS_FILE, read_tags, tag_key
from qbx.validate import validate_paper

INDEX_FILE = ".qbx-index.json"
# bumped when the shape or meaning of an index entry changes, so an old saved index is
# rebuilt (5: "mcq" also trusts the cover title and the mark scheme's answer letter;
# 6: "answers", each part's mark scheme answer signed; 7: "board" in the summary)
INDEX_FORMAT = 10  # 8: needs_insert; 9: part difficulty; 10: needs_insert only when the cover says what the insert contains
STEM_SNIPPET = 160


def issue_key(issue: dict) -> str:
    """A report issue's identity, stable across re-extraction - what a "checked" mark is
    stored against in review.json."""
    return f"{issue['check']}|{issue.get('question_number')}|{issue.get('part_path')}"


def read_reviewed(paper_dir: Path) -> set[str]:
    try:
        return set(json.loads((paper_dir / "review.json").read_text(encoding="utf-8")).get("checked", []))
    except (OSError, ValueError):
        return set()


def _stamp(paper_dir: Path) -> list[int]:
    """What decides whether a paper must be re-read: its paper.json, review.json and
    tags.json mtimes."""
    stamp = []
    for name in ("paper.json", "review.json", TAGS_FILE):
        try:
            stamp.append((paper_dir / name).stat().st_mtime_ns)
        except OSError:
            stamp.append(0)
    return stamp


_STAMPED = ("paper.json", "review.json", TAGS_FILE)


def _stamps(out_dir: Path) -> dict[str, list[int]]:
    """`_stamp` for every extracted paper in out/, by key - one folder listing per paper.
    Listing a folder already gives each file's modified time (free on Windows), where a
    stat per file cost 4 calls a paper: 0.46s over 2,156 papers, once a second while the
    app was open."""
    found: dict[str, list[int]] = {}
    try:
        papers = [entry for entry in os.scandir(out_dir) if entry.is_dir()]
    except OSError:
        return found
    for paper in papers:
        times = {}
        try:
            for entry in os.scandir(paper.path):
                if entry.name in _STAMPED:
                    times[entry.name] = entry.stat().st_mtime_ns
        except OSError:
            continue
        if "paper.json" in times:
            found[paper.name] = [times.get(name, 0) for name in _STAMPED]
    return found


def _snippet(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_entry(paper_dir: Path) -> dict:
    paper = Paper.model_validate_json((paper_dir / "paper.json").read_text(encoding="utf-8"))
    report = validate_paper(paper).model_dump()
    # what the board withdrew is never offered: the bank and builder see the rest
    bank_questions = [
        q.model_copy(update={"parts": [p for p in q.parts if not p.removed]})
        for q in paper.questions
        if not q.removed
    ]
    for q, original in zip(bank_questions, [q for q in paper.questions if not q.removed]):
        if len(q.parts) != len(original.parts):
            q.total_marks = sum(p.marks for p in q.parts)
    checked = read_reviewed(paper_dir)
    open_issues = [i for i in report["issues"] if issue_key(i) not in checked]
    tags = read_tags(paper_dir)
    ms_entries = {e.id: e for e in paper.mark_scheme}
    answer_text: dict[str, list[str]] = {}
    for join in paper.joins:
        if join.mark_scheme_entry_id in ms_entries:
            answer_text.setdefault(join.part_id, []).append(ms_entries[join.mark_scheme_entry_id].answer_text)

    def part_tags(q, part) -> dict:
        entry = tags.get(tag_key(q.question_number, part.path), {})
        estimated = difficulty.estimate(part.text, part.marks, is_multiple_choice(part, paper))
        return {
            "path": part.path,
            "marks": part.marks,
            # 1 Easy .. 4 Very hard: Claude's rating, or the estimate (qbx/difficulty.py)
            "difficulty": difficulty.part_level(entry, estimated),
            "rated": isinstance(entry.get("difficulty"), int),
            "topics": entry.get("topic_ids", []),
            "depends_on": entry.get("depends_on", []),
            "tagged": bool(entry),
        }

    part_entries = [[part_tags(q, p) for p in q.parts] for q in bank_questions]
    return {
        "stamp": _stamp(paper_dir),
        "summary": {
            "paper_key": paper.paper_key,
            "passed": report["passed"],
            "n_issues": len(report["issues"]),
            "n_open": len(open_issues),
            "n_questions": len(paper.questions),
            "board": paper.board,
            "subject": paper.subject,
            "subject_name": paper.subject_name,
            "paper_title": paper.paper_title,
            "level": paper.level,
            "paper": paper.paper,
            "variant": paper.variant,
            "session": paper.session,
            "year": paper.year,
            "total_marks": sum(q.total_marks for q in paper.questions),
            "cover_total_marks": paper.cover_total_marks,
            # read off the cover for a paper extracted before Paper.needs_insert existed, or
            # marked by the older, looser rule (any "1 Insert" on the cover - inserts.py)
            "needs_insert": (
                paper.needs_insert is not False
                and (
                    cover_needs_insert(paper.source_qp_path)
                    if paper.source_qp_path and Path(paper.source_qp_path).is_file()
                    else bool(paper.needs_insert)
                )
            ),
            "n_parts": sum(len(q.parts) for q in paper.questions),
            "n_tagged": sum(1 for q in paper.questions for p in q.parts if tag_key(q.question_number, p.path) in tags),
        },
        "questions": [
            {
                "n": q.question_number,
                # 1 Easy .. 4 Very hard, weighted by marks (qbx/difficulty.py)
                "difficulty": difficulty.question_level(part_entries[i]),
                "marks": q.total_marks,
                "stem": _snippet(q.stem_text or (q.parts[0].text if q.parts else ""), STEM_SNIPPET),
                "n_parts": len(q.parts),
                "topics": sorted({t for p in q.parts for t in tags.get(tag_key(q.question_number, p.path), {}).get("topic_ids", [])}),
            }
            for i, q in enumerate(bank_questions)
        ],
        # what the exam builder chooses from; kept server-side, out of /api/bank
        "parts": part_entries,
        "mcq": [any(is_multiple_choice(p, paper) for p in q.parts) for q in bank_questions],
        # wording sketches, for finding the same question reprinted in another paper
        "sigs": [question_signatures(q.stem_text, [p.text for p in q.parts]) for q in bank_questions],
        # each part's mark scheme answer signed, for two questions with the same answer
        "answers": [
            [[p.path, answer_signature(" ".join(answer_text.get(p.id, [])))] for p in q.parts]
            for q in bank_questions
        ],
        # search runs server-side over these, so the texts never have to reach the browser
        "haystacks": [
            (q.stem_text + " " + " ".join(p.text for p in q.parts)).lower() for q in bank_questions
        ],
    }


# A refresh that has more papers than this to read runs in a background thread, so no request
# waits on it (user-reported: after an index format change the first /api/state rebuilt all
# 2,156 papers inside the request - ~40s holding the lock, no paper list, no progress, and
# the app looked dead). A handful - a scan's paper, an edit - are read inline as before.
INLINE_LIMIT = 20
# while a background rebuild runs, what it has read so far is published this often
PUBLISH_EVERY = 1.0
# The saved index is a cache, written at most this often. Saving all 2,156 papers' entries
# takes ~1.7s and holds the interpreter for up to 0.9s at a time, and a scan saved it after
# every paper it extracted: the server barely got a turn, every poll timed out, and the app
# said its engine wasn't answering (user-reported, "Re-process papers with issues"). Lost
# on a close, it costs only re-reading the papers changed since.
SAVE_EVERY = 30.0


class Library:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.version = 0
        self._entries: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._refreshing = threading.Lock()  # one refresh at a time, never held by a reader
        self._save_lock = threading.Lock()
        self._loaded = False
        self._progress: dict | None = None  # {"done", "total"} while a background rebuild runs
        self._dirty = False  # changed since last saved (SAVE_EVERY)
        self._saver: threading.Thread | None = None
        # duplicates / answer clashes: (the questions they were worked out from, result)
        self._derived_cache: dict[str, tuple[tuple, object]] = {}
        self._derived_locks = {"duplicates": threading.Lock(), "clashes": threading.Lock()}

    # -- loading -----------------------------------------------------------------------

    def _load_saved(self) -> dict[str, dict]:
        try:
            saved = json.loads((self.out_dir / INDEX_FILE).read_text(encoding="utf-8"))
            if saved.get("format") == INDEX_FORMAT:
                return saved["entries"]
        except (OSError, ValueError, KeyError):
            pass
        return {}

    def _save(self) -> None:
        with self._lock:
            entries = dict(self._entries)  # entries are replaced, never changed in place
        with self._save_lock:
            try:
                self.out_dir.mkdir(parents=True, exist_ok=True)
                tmp = self.out_dir / (INDEX_FILE + ".tmp")
                tmp.write_text(json.dumps({"format": INDEX_FORMAT, "entries": entries}), encoding="utf-8")
                tmp.replace(self.out_dir / INDEX_FILE)
            except OSError:
                pass  # a cache only: the next start rebuilds it

    def _save_later(self) -> None:
        """Saved within SAVE_EVERY seconds, by a thread of its own - never by the caller."""
        with self._lock:
            self._dirty = True
            if self._saver is not None:
                return
            self._saver = threading.Thread(target=self._save_loop, name="library-save", daemon=True)
        self._saver.start()

    def _save_loop(self) -> None:
        import time

        while True:
            time.sleep(SAVE_EVERY)
            self.flush()

    def flush(self) -> None:
        """Saves now if anything changed since the last save (the end of a scan)."""
        with self._lock:
            dirty, self._dirty = self._dirty, False
        if dirty:
            self._save()

    def progress(self) -> dict | None:
        """{"done", "total"} while papers are being read into the index in the background."""
        return dict(self._progress) if self._progress else None

    def refresh(self) -> bool:
        """Brings the index up to date with out/: re-reads changed papers, drops removed
        ones. A pass over unchanged papers is one stat per file. True if anything changed.

        Never waits for another refresh, and never holds the readers' lock while reading
        papers: with more than INLINE_LIMIT to read it starts a background rebuild and
        returns at once, and that rebuild publishes what it has read every PUBLISH_EVERY
        seconds (each a version bump, so the UI fills in as it goes)."""
        if not self._refreshing.acquire(blocking=False):
            return False  # one is running already; its version bumps tell the UI
        background = False
        try:
            changed = False
            if not self._loaded:
                loaded = self._load_saved()
                with self._lock:
                    self._entries = loaded
                    self._loaded = True
                changed = True  # the first refresh always counts: nothing was served yet
            with self._lock:
                stamps = {k: e["stamp"] for k, e in self._entries.items()}
            now = _stamps(self.out_dir)
            present = set(now)
            todo = [self.out_dir / key for key, stamp in now.items() if stamps.get(key) != stamp]
            gone = set(stamps) - present
            if gone:
                with self._lock:
                    for key in gone:
                        self._entries.pop(key, None)
                changed = True
            if len(todo) > INLINE_LIMIT:
                background = True
                if changed:
                    with self._lock:
                        self.version += 1
                threading.Thread(target=self._rebuild, args=(todo,), name="library-index", daemon=True).start()
                return changed
            built = self._read(todo)
            if built:
                with self._lock:
                    self._entries.update(built)
                changed = True
            if changed:
                with self._lock:
                    self.version += 1
                self._save_later()
            return changed
        finally:
            if not background:
                self._refreshing.release()

    @staticmethod
    def _read(paper_dirs: list[Path]) -> dict[str, dict]:
        built = {}
        for paper_dir in paper_dirs:
            try:
                built[paper_dir.name] = build_entry(paper_dir)
            except (OSError, ValueError):
                continue  # half-written by a running extraction: next pass
        return built

    def _rebuild(self, todo: list[Path]) -> None:
        """A background rebuild, holding `_refreshing` (taken by the refresh that started it)."""
        import time

        try:
            self._progress = {"done": 0, "total": len(todo)}
            pending: dict[str, dict] = {}
            last = time.monotonic()
            for i, paper_dir in enumerate(todo):
                pending.update(self._read([paper_dir]))
                self._progress = {"done": i + 1, "total": len(todo)}
                if pending and time.monotonic() - last >= PUBLISH_EVERY:
                    with self._lock:
                        self._entries.update(pending)
                        self.version += 1
                    pending, last = {}, time.monotonic()
            with self._lock:
                self._entries.update(pending)
                self.version += 1
            self._save()
        except Exception:  # never leave the index locked: the next refresh tries again
            import logging

            logging.getLogger("qbx.library").exception("rebuilding the library index failed")
        finally:
            self._progress = None
            with self._lock:
                self.version += 1
            self._refreshing.release()

    def invalidate(self, paper_key: str) -> None:
        """After this process itself wrote a paper's files: re-read it now - that paper
        alone, so a background rebuild running at the time doesn't delay it."""
        if not self._loaded:
            self.refresh()
        paper_dir = self.out_dir / paper_key
        built = self._read([paper_dir]) if (paper_dir / "paper.json").is_file() else {}
        with self._lock:
            if built:
                self._entries.update(built)
            else:
                self._entries.pop(paper_key, None)
            self.version += 1
        self._save_later()

    # -- queries -----------------------------------------------------------------------

    def _with_insert(self, summary: dict, attached: dict[str, str]) -> dict:
        """Whether the scan found the paper's insert - from out/.qbx-inserts.json, which
        changes without the paper's own files changing, so it's never in the saved index."""
        return {**summary, "has_insert": summary["paper_key"] in attached}

    def summaries(self) -> list[dict]:
        attached = insert_index(self.out_dir)
        with self._lock:
            return [self._with_insert(self._entries[k]["summary"], attached) for k in sorted(self._entries)]

    def summary(self, paper_key: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(paper_key)
            return self._with_insert(entry["summary"], insert_index(self.out_dir)) if entry else None

    def bank(self) -> list[dict]:
        """Every paper with its questions in brief - what the question bank's folders need."""
        with self._lock:
            attached = insert_index(self.out_dir)
        groups = self.duplicates(wait=False)
        with self._lock:
            # a reprinted question shows one level, whichever paper it's in (Core and Extended
            # papers share questions)
            levels = difficulty.shared_levels(
                {(k, q["n"]): q["difficulty"] for k, e in self._entries.items() for q in e["questions"]
                 if q.get("difficulty")},
                groups,
            )

            def brief(k: str, q: dict) -> dict:
                level = levels.get((k, q["n"]))
                return {**q, "difficulty": difficulty.nearest(level)} if level is not None else q

            return [
                {**self._with_insert(self._entries[k]["summary"], attached),
                 "questions": [brief(k, q) for q in self._entries[k]["questions"]]}
                for k in sorted(self._entries)
            ]

    def search(self, query: str, subject: str | None = None, limit: int = 500) -> list[dict]:
        needle = query.lower().strip()
        hits = []
        with self._lock:
            for key in sorted(self._entries):
                entry = self._entries[key]
                if subject and entry["summary"]["subject"] != subject:
                    continue
                for question, haystack in zip(entry["questions"], entry["haystacks"]):
                    if needle in haystack:
                        hits.append({"paper_key": key, "question_number": question["n"]})
                        if len(hits) >= limit:
                            return hits
        return hits

    def entries(self, subject: str | None = None) -> list[tuple[str, dict]]:
        """(paper key, index entry) for every paper, or one subject's - what exam_builder
        chooses from. Entries are shared, not copied: read them, never change them."""
        with self._lock:
            return [
                (k, self._entries[k])
                for k in sorted(self._entries)
                if subject is None or self._entries[k]["summary"]["subject"] == subject
            ]

    def subjects(self) -> list[dict]:
        seen: dict[str, dict] = {}
        with self._lock:
            for entry in self._entries.values():
                s = entry["summary"]
                row = seen.setdefault(
                    s["subject"],
                    {"subject": s["subject"], "board": s.get("board", ""), "name": "", "n_papers": 0, "n_parts": 0, "n_tagged": 0},
                )
                row["name"] = row["name"] or s["subject_name"]
                row["n_papers"] += 1
                row["n_parts"] += s["n_parts"]
                row["n_tagged"] += s["n_tagged"]
        return sorted(seen.values(), key=lambda r: r["subject"])

    def board_of(self, paper_key: str) -> str | None:
        summary = self.summary(paper_key)
        return summary.get("board") if summary else None

    def boards(self) -> list[str]:
        with self._lock:
            return sorted({e["summary"].get("board", "") for e in self._entries.values()} - {""})

    # Reprints and answer clashes are worked out over every question in the library: ~5.6s
    # over 2,156 papers. They were worked out again at every version (each paper a scan
    # finished, each second of a background read, each edit) and inside the readers' lock,
    # so /api/state and every other reader waited too. The window's 6 connections filled
    # with bank requests and polls queued behind them, and the app said its engine wasn't
    # answering while it answered anything else at once (user-reported, twice, during
    # "Re-process papers with issues"). Now they're kept until a paper.json changes - their
    # only input - and worked out outside the lock, one at a time.

    def _content_key(self) -> tuple:
        with self._lock:
            return tuple((k, e["stamp"][0]) for k, e in sorted(self._entries.items()))

    def _derived(self, name: str, compute, wait: bool):
        """`compute(entries)`'s result for the library's current questions. With wait=False
        and an earlier result, that one is returned at once and the new one is worked out in
        the background - for the bank, which mustn't keep a poll waiting."""
        key = self._content_key()
        cached = self._derived_cache.get(name)
        if cached is not None and cached[0] == key:
            return cached[1]
        if cached is not None and not wait:
            if self._derived_locks[name].acquire(blocking=False):
                def refresh() -> None:
                    try:
                        self._compute_derived(name, compute)
                    finally:
                        self._derived_locks[name].release()

                threading.Thread(target=refresh, name=f"library-{name}", daemon=True).start()
            return cached[1]
        with self._derived_locks[name]:
            cached = self._derived_cache.get(name)
            if cached is not None and cached[0] == self._content_key():
                return cached[1]
            return self._compute_derived(name, compute)

    def _compute_derived(self, name: str, compute):
        key = self._content_key()
        with self._lock:
            entries = [(k, self._entries[k]) for k in sorted(self._entries)]
        result = compute(entries)  # outside the readers' lock: seconds over a big library
        self._derived_cache[name] = (key, result)
        return result

    def duplicates(self, wait: bool = True) -> dict[tuple[str, int], int]:
        """(paper key, question number) -> duplicate group id, for every question reprinted
        in another paper (see similar.py)."""

        def compute(entries):
            keys, sigs = [], []
            for paper_key, entry in entries:
                for q, sig in zip(entry["questions"], entry["sigs"]):
                    keys.append((paper_key, q["n"]))
                    sigs.append((paper_key, sig))
            groups = duplicate_groups(sigs)
            sizes: dict[int, int] = {}
            for g in groups:
                sizes[g] = sizes.get(g, 0) + 1
            return {k: g for k, g in zip(keys, groups) if sizes[g] > 1}

        return self._derived("duplicates", compute, wait)

    def answer_clashes(self) -> dict[tuple[str, int, str], set[int]]:
        """(paper key, question number, part path) -> clash ids, for every part whose mark
        scheme answer is the same as another question's part (see similar.py). Two parts
        sharing an id must not both be set."""

        def compute(entries):
            keys, answers = [], []
            for paper_key, entry in entries:
                for q, parts in zip(entry["questions"], entry.get("answers", [])):
                    for path, sig in parts:
                        if sig:
                            keys.append((paper_key, q["n"], path))
                            answers.append(((paper_key, q["n"]), sig))
            result: dict[tuple[str, int, str], set[int]] = {}
            for clash_id, (a, b) in enumerate(answer_clashes(answers)):
                result.setdefault(keys[a], set()).add(clash_id)
                result.setdefault(keys[b], set()).add(clash_id)
            return result

        return self._derived("clashes", compute, True)
