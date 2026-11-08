"""Tags each question part with syllabus topics, and records which earlier parts it needs.

A composed exam may use a part on its own (quiz_export prints the question's stem and the
part's container heading above it), so what matters is whether a part can be answered from
those alone. It can't when it uses an earlier part's answer, or a table, figure, equation or
experiment first introduced in an earlier part's text. `depends_on` lists those earlier
parts; exam_builder.py always takes a part together with them.

Two sources decide it, and the stricter wins:
- rules over the text (`rule_dependencies`): "your answer to (a)", "hence", "use Fig. 3.1"
  where Fig. 3.1 first appears in an earlier part. A rule's dependency can't be removed.
- Claude, which reads every part of the paper at once and catches the rest ("the
  student repeated experiment 1", a table shown in (a) and completed in (b)).

Topics come only from the subject's topic list (topics.py); any other id is dropped.

What Claude matches against (user-asked, "the syllabus isn't enough"): each topic's learning
outcomes, not just its heading, and each part's mark scheme answer - the answer says what's
actually assessed when the question's wording is generic ("Explain this observation") or its
diagram is lost from the text. Both can be switched off (`outcomes=`, `answers=`) for
`qbx tag-eval`, which measures them against hand-tagged papers.

Tags live in out/<paper>/tags.json, beside paper.json but separate from it like
review.json, keyed by question number and part path. Each records a hash of the text it
was made from, so re-extraction keeps them and a part whose text changed is re-tagged.
Tags edited by hand (`source: "manual"`) are never replaced.
"""

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel

from qbx.llm import LLM
from qbx.schema import Paper, Question, QuestionPart
from qbx.topics import Topic, topics_prompt_list

TAGS_FILE = "tags.json"
TAGS_FORMAT = 1
PART_TEXT_LIMIT = 1500
ANSWER_TEXT_LIMIT = 600
# question text per request: a whole paper is typically 10-25K characters, so this is one
# request per paper with room to spare
PROMPT_TEXT_LIMIT = 60_000
MAX_TOPICS_PER_PART = 3

_CONTROL_RE = re.compile(r"[\u0000-\u001f\u007f]")
_DOTS_RE = re.compile(r"(?:\.\s?){4,}")


def clean_text(text: str) -> str:
    """Extracted text as Claude should read it: the PDF's control characters (BEL after a
    marker) become spaces, and dotted answer lines become one ellipsis."""
    text = _DOTS_RE.sub(" … ", _CONTROL_RE.sub(" ", text).replace("\xa0", " "))
    return " ".join(text.split())


def tag_key(question_number: int, path: str) -> str:
    return f"{question_number}|{path}"


def _hash(*texts: str) -> str:
    return hashlib.sha1("\x1e".join(texts).encode("utf-8")).hexdigest()[:12]


def part_hash(question: Question, part: QuestionPart) -> str:
    return _hash(question.stem_text, part.path, part.text)


# -- storage -----------------------------------------------------------------------------


def read_tags(paper_dir: Path) -> dict[str, dict]:
    try:
        data = json.loads((paper_dir / TAGS_FILE).read_text(encoding="utf-8"))
        return data.get("parts", {}) if data.get("format") == TAGS_FORMAT else {}
    except (OSError, ValueError):
        return {}


def write_tags(paper_dir: Path, tags: dict[str, dict]) -> None:
    tmp = paper_dir / (TAGS_FILE + ".tmp")
    tmp.write_text(json.dumps({"format": TAGS_FORMAT, "parts": tags}, indent=1), encoding="utf-8")
    tmp.replace(paper_dir / TAGS_FILE)


def question_needs_tagging(question: Question, tags: dict[str, dict]) -> bool:
    for part in question.parts:
        entry = tags.get(tag_key(question.question_number, part.path))
        if entry is None:
            return True
        if entry.get("source") != "manual" and entry.get("text_hash") != part_hash(question, part):
            return True
    return False


# -- dependencies by rule ----------------------------------------------------------------

_NUMERAL = r"(?:viii|vii|vi|iv|v|ix|x|iii|ii|i)"
# "(b)", "(b)(ii)", "part (c)", "(ii)" - a reference to another part by its label
_LABEL_REF_RE = re.compile(rf"\(([a-z])\)(?:\s*\(({_NUMERAL})\))?|\(({_NUMERAL})\)")
# phrases that point at another part's content or answer, followed by its label
_ONE_LABEL = rf"(?:\([a-z]\)(?:\s*\({_NUMERAL}\))?|\({_NUMERAL}\))"
# ...and a list of them: "your observations from (a), (b) and (c)"
_POINTER_RE = re.compile(
    r"\b(?:answers?\s+(?:to|in|from|for)|(?:in|from|using|use|see|of|with|to)\s+(?:parts?\s+)?)"
    rf"\s*({_ONE_LABEL}(?:\s*(?:,|and|or)\s*{_ONE_LABEL})*)",
    re.I,
)
# needs the part immediately before, whatever its label
# (not a bare "your answer": "give your answer to 3 significant figures" needs nothing)
_PREVIOUS_RE = re.compile(r"\b(?:hence|(?:use|using|from|with)\s+your\s+(?:answers?|results?|values?))\b", re.I)
_EXHIBIT_RE = re.compile(r"\b(Fig(?:ure|\.)?|Table|Graph|Diagram)\s*(\d+\.\d+)", re.I)


def _strip_own_label(text: str) -> str:
    """A part's text starts with its own label(s) - "(b) … (i) …" - which aren't references."""
    return re.sub(rf"^\s*\d{{0,2}}\s*(?:\(([a-z]|{_NUMERAL})\)\s*)+", "", clean_text(text))


def _exhibits(text: str) -> set[str]:
    return {f"{m.group(1)[0].lower()}{m.group(2)}" for m in _EXHIBIT_RE.finditer(text)}


def _resolve_label(ref: str, part: QuestionPart, earlier: list[QuestionPart]) -> list[str]:
    m = _LABEL_REF_RE.search(ref)
    if not m:
        return []
    letter, numeral, lone_numeral = m.groups()
    # "(i)", "(v)", "(x)" are both letters and numerals: from inside a subpart, a numeral
    if letter and not numeral and "." in part.path and re.fullmatch(_NUMERAL, letter):
        letter, lone_numeral = None, letter
    if lone_numeral:  # "(ii)" means a sibling under this part's own letter
        head = part.path.split(".")[0] if "." in part.path else ""
        target = f"{head}.{lone_numeral}" if head else lone_numeral
        return [p.path for p in earlier if p.path == target]
    target = f"{letter}.{numeral}" if numeral else letter
    return [p.path for p in earlier if p.path == target or p.path.startswith(target + ".")]


def rule_dependencies(question: Question) -> dict[str, set[str]]:
    """Earlier parts each part certainly needs, by what its text says. Only earlier parts:
    a reference forward ("you will use this in (c)") isn't a dependency."""
    parts = question.parts
    stem_exhibits = _exhibits(clean_text(question.stem_text))
    # where each figure/table is first shown: the stem, a container heading (whose content
    # travels with that container's first leaf), or a part
    first_shown: dict[str, str | None] = {e: None for e in stem_exhibits}
    heading_first_leaf = {}
    for h in question.container_headings:
        leaf = next((p.path for p in parts if p.path.startswith(h.path + ".")), None)
        heading_first_leaf[h.path] = leaf
    deps: dict[str, set[str]] = {p.path: set() for p in parts}

    for index, part in enumerate(parts):
        earlier = parts[:index]
        body = _strip_own_label(part.text)
        if index == 0 and question.stem_text and body.startswith(clean_text(question.stem_text)[:40]):
            body = body[len(clean_text(question.stem_text)) :]
        own_heading = next(
            (h for h in question.container_headings if part.path.startswith(h.path + ".")), None
        )
        heading_exhibits = _exhibits(clean_text(own_heading.text)) if own_heading else set()

        for m in _POINTER_RE.finditer(body):
            for label in re.finditer(_ONE_LABEL, m.group(1)):
                deps[part.path].update(_resolve_label(label.group(0), part, earlier))
        if earlier and _PREVIOUS_RE.search(body):
            deps[part.path].add(earlier[-1].path)

        for exhibit in _exhibits(body):
            if exhibit in heading_exhibits:
                continue  # printed with this part's own container heading
            if exhibit in first_shown:
                owner = first_shown[exhibit]
                if owner is not None and owner != part.path:
                    deps[part.path].add(owner)
            else:
                first_shown[exhibit] = part.path
        for exhibit in heading_exhibits:
            first_shown.setdefault(exhibit, heading_first_leaf.get(own_heading.path) if own_heading else None)

    # needing a part means needing what that part needs
    for part in parts:
        stack = list(deps[part.path])
        while stack:
            d = stack.pop()
            for dd in deps.get(d, ()):
                if dd not in deps[part.path] and dd != part.path:
                    deps[part.path].add(dd)
                    stack.append(dd)
    return deps


# -- asking Claude -----------------------------------------------------------------------


class _PartTags(BaseModel):
    path: str
    topic_ids: list[str]
    depends_on: list[str]
    reason: str = ""


class _QuestionTags(BaseModel):
    question_number: int
    parts: list[_PartTags]


class _PaperTags(BaseModel):
    questions: list[_QuestionTags]


def _part_label(path: str) -> str:
    return "".join(f"({p})" for p in path.split(".")) if path else "(whole question)"


def part_answers(paper: Paper) -> dict[str, str]:
    """Each part's mark scheme answer text (every row joined to it), by part id."""
    entries = {e.id: e for e in paper.mark_scheme}
    answers: dict[str, list[str]] = {}
    for join in paper.joins:
        entry = entries.get(join.mark_scheme_entry_id)
        if entry is not None and entry.answer_text.strip():
            answers.setdefault(join.part_id, []).append(entry.answer_text)
    return {pid: clean_text(" / ".join(texts)) for pid, texts in answers.items()}


def _question_block(question: Question, answers: dict[str, str] | None = None) -> list[str]:
    stem = clean_text(question.stem_text)
    lines = [f"=== Question {question.question_number} ===", "Introduction (always shown):", stem or "(none)"]
    for h in question.container_headings:
        lines += [f"Heading of part ({h.path}) (always shown with its subparts):", clean_text(h.text)]
    for part in question.parts:
        text = clean_text(part.text)
        if stem and text.startswith(stem):
            text = text[len(stem) :].strip()
        lines += [f"Part {part.path or '-'} {_part_label(part.path)} [{part.marks} marks]:", text[:PART_TEXT_LIMIT]]
        answer = (answers or {}).get(part.id)
        if answer:
            lines.append(f"Mark scheme answer: {answer[:ANSWER_TEXT_LIMIT]}")
    return lines + [""]


def build_prompt(
    paper: Paper, questions: list[Question], topics: list[Topic], outcomes: bool = True, answers: bool = True
) -> str:
    with_outcomes = outcomes and any(t.outcomes for t in topics)
    answer_text = part_answers(paper) if answers else {}
    lines = [
        # the topic list first: it's the same for every paper of a subject, a cacheable prefix
        "Syllabus topics (id, name" + (", then the learning outcomes it covers" if with_outcomes else "") + "):",
        topics_prompt_list(topics, outcomes=with_outcomes),
        "",
        f"Subject: {paper.subject_name or paper.subject} ({paper.subject}), {paper.paper_title or 'paper ' + paper.paper}.",
        "Questions from one exam paper (the extracted text is imperfect: symbols and diagrams are lost)"
        + (", each part with the answer its mark scheme expects" if answer_text else "")
        + ":",
        "",
    ]
    for question in questions:
        lines += _question_block(question, answer_text)
    lines += [
        "For every part of every question above, reply with its question_number and:",
        "- path: exactly as given after 'Part' ('-' for a question with no lettered parts);",
        "- topic_ids: the syllabus topic it assesses - the most specific id in the list that "
        "covers it"
        + (", judged by which topic's learning outcomes the part tests" if with_outcomes else "")
        + (" and by what its mark scheme answer rewards, not by the context the question is set in" if answer_text else "")
        + f". Usually exactly one; add a second (at most {MAX_TOPICS_PER_PART} in all) only "
        "when answering the part genuinely requires knowledge from that topic too, not just "
        "because the question mentions it. Only ids from the list;",
        "- depends_on: the paths of EARLIER parts of the same question a student must see to "
        "answer this part, when shown only the question's introduction, its heading and the "
        "part itself. A part depends on an earlier one when it uses that part's answer, or a "
        "table, figure, graph, equation, method, data or experiment that is first introduced "
        "in that earlier part's text. Information in the introduction or a heading is not a "
        "dependency. If unsure, include the dependency;",
        "- reason: a few words on why, when depends_on is not empty.",
    ]
    return "\n".join(lines)


def _batches(questions: list[Question], answers: dict[str, str] | None = None) -> list[list[Question]]:
    """A paper's questions in as few requests as fit comfortably - usually one."""
    batches: list[list[Question]] = [[]]
    size = 0
    for q in questions:
        n = len("\n".join(_question_block(q, answers)))
        if batches[-1] and size + n > PROMPT_TEXT_LIMIT:
            batches.append([])
            size = 0
        batches[-1].append(q)
        size += n
    return [b for b in batches if b]


def _finish(question: Question, got: _QuestionTags | None, known: set[str]) -> dict[str, dict]:
    """Claude's reply for one question, checked: only listed topics, only earlier parts,
    the rules' dependencies added, and each dependency's own dependencies with it."""
    by_path = {p.path: p for p in (got.parts if got else [])}
    if "" not in by_path and "-" in by_path:  # an unlettered question's one part
        by_path[""] = by_path["-"]
    rules = rule_dependencies(question)
    order = [p.path for p in question.parts]
    deps: dict[str, set[str]] = {}
    result = {}
    for index, part in enumerate(question.parts):
        reply = by_path.get(part.path)
        topic_ids = [t for t in dict.fromkeys(reply.topic_ids if reply else []) if t in known][:MAX_TOPICS_PER_PART]
        earlier = set(order[:index])
        deps[part.path] = {d for d in (reply.depends_on if reply else []) if d in earlier} | rules[part.path]
        result[part.path] = (topic_ids, (reply.reason if reply else "")[:200])
    for path in order:  # closure: needing a part means needing what it needs
        stack = list(deps[path])
        while stack:
            for d in deps.get(stack.pop(), ()):
                if d not in deps[path]:
                    deps[path].add(d)
                    stack.append(d)
    return {
        tag_key(question.question_number, part.path): {
            "topic_ids": result[part.path][0],
            "depends_on": [p for p in order if p in deps[part.path]],
            "reason": result[part.path][1] if deps[part.path] else "",
            "text_hash": part_hash(question, part),
            "source": "claude",
        }
        for part in question.parts
    }


def tag_questions(
    llm: LLM, paper: Paper, questions: list[Question], topics: list[Topic], outcomes: bool = True, answers: bool = True
) -> dict[str, dict]:
    """Fresh tags for every part of the given questions of one paper, keyed by tag_key -
    one request per batch of questions rather than per question, which is what keeps a
    whole bank (400 papers, ~7,000 questions) to ~400 requests."""
    result: dict[str, dict] = {}
    for prompt, batch in tagging_prompts(paper, questions, topics, outcomes, answers):
        result.update(apply_reply(batch, llm.json(prompt, _PaperTags), topics))
    return result


def tagging_prompts(
    paper: Paper, questions: list[Question], topics: list[Topic], outcomes: bool = True, answers: bool = True
) -> list[tuple[str, list[Question]]]:
    """The requests tagging these questions takes - usually one per paper - each with the
    questions it covers. batch_tagging.py sends them as one Message Batch."""
    answer_text = part_answers(paper) if answers else {}
    return [
        (build_prompt(paper, batch, topics, outcomes, answers), batch) for batch in _batches(questions, answer_text)
    ]


def apply_reply(questions: list[Question], reply: "_PaperTags", topics: list[Topic]) -> dict[str, dict]:
    """Claude's reply to one tagging prompt as checked tags, keyed by tag_key."""
    known = {t.id for t in topics}
    by_number = {q.question_number: q for q in reply.questions}
    result: dict[str, dict] = {}
    for question in questions:
        got = by_number.get(question.question_number)
        if got is None:
            continue  # left untagged: the next run asks again
        result.update(_finish(question, got, known))
    return result


PaperTags = _PaperTags  # the reply's schema, for batch_tagging.py


def tag_question(llm: LLM, paper: Paper, question: Question, topics: list[Topic]) -> dict[str, dict]:
    return tag_questions(llm, paper, [question], topics)


def merge_tags(existing: dict[str, dict], fresh: dict[str, dict]) -> dict[str, dict]:
    """Fresh tags over old ones, except that manual edits always stay."""
    merged = dict(existing)
    for key, entry in fresh.items():
        if merged.get(key, {}).get("source") != "manual":
            merged[key] = entry
    return merged
