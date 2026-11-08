"""Reads a typed exam request ("a quiz on electrolysis and rates, out of 40 marks, no
multiple choice") into an ExamSpec. Claude only interprets the sentence; which questions
go in is exam_builder.py's job. Only the sentence, the subject list and topic names are
sent - no paper content.
"""

import datetime

from pydantic import BaseModel

from qbx.exam_builder import ExamSpec
from qbx.llm import LLM
from qbx.topics import Topic, topics_prompt_list

DEFAULT_MARKS = 40
MAX_MARKS = 300


class _RequestReply(BaseModel):
    # plain types with empty defaults rather than Optional: simpler for structured output
    subject: str = ""
    topic_ids: list[str] = []
    total_marks: int = 0
    year_from: int = 0
    year_to: int = 0
    papers: list[str] = []
    question_style: str = "structured"
    # a set number of multiple-choice questions, when the request gives one; else 0
    mcq_count: int = 0
    name: str = ""
    # topics asked for that match nothing in the list, in the request's own words
    unmatched: list[str] = []


class ReadRequest(BaseModel):
    spec: ExamSpec
    name: str
    unmatched: list[str] = []


def build_prompt(text: str, subjects: list[dict], topics_by_subject: dict[str, list[Topic]]) -> str:
    this_year = datetime.date.today().year
    lines = [
        "A teacher typed this request for an exam, to be assembled from past-paper questions:",
        f'"{text.strip()}"',
        "",
        "Subjects available (code, name, papers):",
    ]
    for s in subjects:
        papers = ", ".join(f"{p['paper']} ({p['title']})" if p.get("title") else p["paper"] for p in s.get("papers", []))
        lines.append(f"{s['subject']}  {s.get('name', '')}  papers: {papers or 'any'}")
    for subject, topics in topics_by_subject.items():
        lines += ["", f"Syllabus topics for {subject} (id, name):", topics_prompt_list(topics)]
    lines += [
        "",
        "Reply with:",
        "- subject: the subject code it is for (the only one listed, if just one);",
        "- topic_ids: ids from that subject's topic list that the request asks for. Use the "
        "most general id that covers what was asked (a whole topic's id when a whole topic is "
        "asked for). Empty if no topic is named;",
        "- unmatched: any topics asked for that nothing in the list covers, in the request's words;",
        f"- total_marks: the total asked for, or 0 if not given;",
        f"- year_from / year_to: a range of exam years if one is asked for (it is {this_year} now; "
        "'last 3 years' means year_from = "
        f"{this_year - 3}), else 0;",
        "- papers: paper numbers if particular papers are asked for (e.g. 'paper 4' -> ['4']), else empty;",
        "- question_style: 'mcq' if only multiple-choice is wanted, 'any' if both are wanted, "
        "otherwise 'structured';",
        "- mcq_count: how many multiple-choice questions, if the request gives a number of them "
        "('only 4 multiple choice', '5 MCQs and the rest written') - question_style is then 'any'. "
        "0 if no number is given;",
        "- name: a short title for the exam, e.g. 'Electrolysis and rates of reaction'.",
    ]
    return "\n".join(lines)


def read_request(
    llm: LLM, text: str, subjects: list[dict], topics_by_subject: dict[str, list[Topic]], seed: int = 0
) -> ReadRequest:
    reply = llm.json(build_prompt(text, subjects, topics_by_subject), _RequestReply)
    codes = [s["subject"] for s in subjects]
    subject = reply.subject if reply.subject in codes else (codes[0] if len(codes) == 1 else None)
    known = {t.id for t in topics_by_subject.get(subject or "", [])}
    topic_ids = [t for t in dict.fromkeys(reply.topic_ids) if t in known]
    total = reply.total_marks if 0 < reply.total_marks <= MAX_MARKS else DEFAULT_MARKS
    style = reply.question_style if reply.question_style in ("structured", "mcq", "any") else "structured"
    mcq_count = reply.mcq_count if 0 < reply.mcq_count <= total else None
    if mcq_count is not None:
        style = "any"  # a number of MCQs means the rest are written
    spec = ExamSpec(
        subject=subject,
        topic_ids=topic_ids,
        total_marks=total,
        year_from=reply.year_from or None,
        year_to=reply.year_to or None,
        papers=[p.strip() for p in reply.papers if p.strip()],
        question_style=style,
        mcq_count=mcq_count,
        seed=seed,
    )
    name = " ".join(reply.name.split())[:60] or "Exam"
    return ReadRequest(spec=spec, name=name, unmatched=[u for u in reply.unmatched if u.strip()][:10])
