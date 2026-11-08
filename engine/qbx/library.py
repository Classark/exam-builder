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
import threading
from pathlib import Path

from qbx.quiz_export import is_multiple_choice
from qbx.schema import Paper
from qbx.similar import answer_clashes, answer_signature, duplicate_groups, question_signatures
from qbx.tagging import TAGS_FILE, read_tags, tag_key
from qbx.validate import validate_paper

INDEX_FILE = ".qbx-index.json"
# bumped when the shape or meaning of an index entry changes, so an old saved index is
# rebuilt (5: "mcq" also trusts the cover title and the mark scheme's answer letter;
# 6: "answers", each part's mark scheme answer signed; 7: "board" in the summary)
INDEX_FORMAT = 7
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
        return {
            "path": part.path,
            "marks": part.marks,
            "topics": entry.get("topic_ids", []),
            "depends_on": entry.get("depends_on", []),
            "tagged": bool(entry),
        }

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
            "n_parts": sum(len(q.parts) for q in paper.questions),
            "n_tagged": sum(1 for q in paper.questions for p in q.parts if tag_key(q.question_number, p.path) in tags),
        },
        "questions": [
            {
                "n": q.question_number,
                "marks": q.total_marks,
                "stem": _snippet(q.stem_text or (q.parts[0].text if q.parts else ""), STEM_SNIPPET),
                "n_parts": len(q.parts),
                "topics": sorted({t for p in q.parts for t in tags.get(tag_key(q.question_number, p.path), {}).get("topic_ids", [])}),
            }
            for q in bank_questions
        ],
        # what the exam builder chooses from; kept server-side, out of /api/bank
        "parts": [[part_tags(q, p) for p in q.parts] for q in bank_questions],
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


class Library:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.version = 0
        self._entries: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._loaded = False
        self._duplicates: tuple[int, dict[tuple[str, int], int]] | None = None
        self._clashes: tuple[int, dict[tuple[str, int, str], set[int]]] | None = None

    # -- loading -----------------------------------------------------------------------

    def _load_saved(self) -> None:
        try:
            saved = json.loads((self.out_dir / INDEX_FILE).read_text(encoding="utf-8"))
            if saved.get("format") == INDEX_FORMAT:
                self._entries = saved["entries"]
        except (OSError, ValueError, KeyError):
            self._entries = {}

    def _save(self) -> None:
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.out_dir / (INDEX_FILE + ".tmp")
            tmp.write_text(json.dumps({"format": INDEX_FORMAT, "entries": self._entries}), encoding="utf-8")
            tmp.replace(self.out_dir / INDEX_FILE)
        except OSError:
            pass  # a cache only: the next start rebuilds it

    def refresh(self) -> bool:
        """Brings the index up to date with out/: re-reads changed papers, drops removed
        ones. A pass over unchanged papers is one stat per file. True if anything changed."""
        with self._lock:
            if not self._loaded:
                self._load_saved()
                self._loaded = True
                changed = True  # the first refresh always counts: nothing was served yet
            else:
                changed = False
            present = set()
            if self.out_dir.exists():
                for paper_dir in self.out_dir.iterdir():
                    if not (paper_dir / "paper.json").is_file():
                        continue
                    key = paper_dir.name
                    present.add(key)
                    entry = self._entries.get(key)
                    if entry is not None and entry["stamp"] == _stamp(paper_dir):
                        continue
                    try:
                        self._entries[key] = build_entry(paper_dir)
                    except (OSError, ValueError):
                        continue  # half-written by a running extraction: next pass
                    changed = True
            for key in set(self._entries) - present:
                del self._entries[key]
                changed = True
            if changed:
                self.version += 1
                self._save()
            return changed

    def invalidate(self, paper_key: str) -> None:
        """After this process itself wrote a paper's files: re-read it now."""
        with self._lock:
            self._entries.pop(paper_key, None)
        self.refresh()

    # -- queries -----------------------------------------------------------------------

    def summaries(self) -> list[dict]:
        with self._lock:
            return [self._entries[k]["summary"] for k in sorted(self._entries)]

    def summary(self, paper_key: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(paper_key)
            return entry["summary"] if entry else None

    def bank(self) -> list[dict]:
        """Every paper with its questions in brief - what the question bank's folders need."""
        with self._lock:
            return [
                {**self._entries[k]["summary"], "questions": self._entries[k]["questions"]}
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

    def duplicates(self) -> dict[tuple[str, int], int]:
        """(paper key, question number) -> duplicate group id, for every question reprinted
        in another paper (see similar.py). Worked out once per index version."""
        with self._lock:
            if self._duplicates is not None and self._duplicates[0] == self.version:
                return self._duplicates[1]
            keys, sigs = [], []
            for paper_key in sorted(self._entries):
                entry = self._entries[paper_key]
                for q, sig in zip(entry["questions"], entry["sigs"]):
                    keys.append((paper_key, q["n"]))
                    sigs.append((paper_key, sig))
            groups = duplicate_groups(sigs)
            sizes: dict[int, int] = {}
            for g in groups:
                sizes[g] = sizes.get(g, 0) + 1
            result = {k: g for k, g in zip(keys, groups) if sizes[g] > 1}
            self._duplicates = (self.version, result)
            return result

    def answer_clashes(self) -> dict[tuple[str, int, str], set[int]]:
        """(paper key, question number, part path) -> clash ids, for every part whose mark
        scheme answer is the same as another question's part (see similar.py). Two parts
        sharing an id must not both be set. Worked out once per index version."""
        with self._lock:
            if self._clashes is not None and self._clashes[0] == self.version:
                return self._clashes[1]
            keys, answers = [], []
            for paper_key in sorted(self._entries):
                entry = self._entries[paper_key]
                for q, parts in zip(entry["questions"], entry.get("answers", [])):
                    for path, sig in parts:
                        if sig:
                            keys.append((paper_key, q["n"], path))
                            answers.append(((paper_key, q["n"]), sig))
            result: dict[tuple[str, int, str], set[int]] = {}
            for clash_id, (a, b) in enumerate(answer_clashes(answers)):
                result.setdefault(keys[a], set()).add(clash_id)
                result.setdefault(keys[b], set()).add(clash_id)
            self._clashes = (self.version, result)
            return result
