"""A conversation with Claude about an exam (the app's Claude tab): the teacher says what
they want in their own words, over as many messages as it takes, and asks for changes.

Claude is still only a reader here. Each turn it replies to the teacher and names at most
one action - make a new exam, change the open one, or build it again with different
questions - with the instruction for it written out in full. The action itself runs
through the same code as the one-line request and "Change this exam" (exam_request.py,
exam_adjust.py, exam_builder.py), so questions still come only from the bank, the marks
still add up exactly, and nothing in the conversation can put a question in by hand.

Sent to Claude: the conversation so far, the subjects in the bank with their papers and
topic names, and the open exam's questions as exam_adjust sends them. No paper content
beyond the question snippets tagging already sends.
"""

from typing import Literal

from pydantic import BaseModel

from qbx.exam_adjust import _Item
from qbx.exam_builder import ExamSpec
from qbx.llm import LLM
from qbx.topics import Topic

# the conversation sent each turn - older messages are dropped past this
MAX_MESSAGES = 30
MAX_MESSAGE_CHARS = 2000

Action = Literal["none", "make", "change", "reroll"]


class ChatMessage(BaseModel):
    role: Literal["teacher", "claude"]
    text: str
    # for a Claude message that acted: what the action did (the outcome dict), or why it couldn't
    outcome: dict | None = None
    error: str | None = None


class _ChatReply(BaseModel):
    # plain types with empty defaults rather than Optional: simpler for structured output
    reply: str = ""
    action: str = "none"
    instruction: str = ""


class ChatTurn(BaseModel):
    reply: str
    action: Action = "none"
    instruction: str = ""


def _exam_lines(items: list[_Item], spec: ExamSpec | None, topics: list[Topic]) -> list[str]:
    names = {t.id: t.name for t in topics}
    total = sum(i.total for i in items)
    lines = [
        f"The exam open now ({total} marks). Written questions are Q1, Q2...; multiple-choice "
        "questions are MCQ1, MCQ2... Each part is listed as label (marks): topics.",
    ]
    for i in sorted(items, key=lambda i: (i.mcq, int(i.label.lstrip("MCQ") or 0))):
        lines.append(f"{i.label}: {i.total} marks, from {i.paper_key} question {i.question_number}. {i.stem}")
        for p in i.part_paths:
            tnames = ", ".join(names.get(t, t) for t in i.topics.get(p, [])) or "untagged"
            lines.append(f"    {p or '(whole question)'} ({i.marks.get(p, 0)}): {tnames}")
    if spec is not None and spec.topic_ids:
        lines.append("It was made for: " + ", ".join(names.get(t, t) for t in spec.topic_ids) + ".")
    return lines


def build_prompt(
    messages: list[ChatMessage],
    subjects: list[dict],
    topics_by_subject: dict[str, list[Topic]],
    exam: tuple[str, list[_Item], ExamSpec | None, list[Topic]] | None,
) -> str:
    lines = [
        "You are talking with a teacher who is putting together an exam from a bank of "
        "past-paper questions. You can't write questions: every question comes from the "
        "bank, chosen by the app to fit what is asked, and the marks always add up exactly. "
        "A new exam never repeats a question set in any of the teacher's other exams.",
        "",
        "Subjects in the bank (code, name, papers):",
    ]
    for s in subjects:
        papers = ", ".join(f"{p['paper']} ({p['title']})" if p.get("title") else p["paper"] for p in s.get("papers", []))
        lines.append(f"{s['subject']}  {s.get('name', '')}  papers: {papers or 'any'}")
    for subject, topics in topics_by_subject.items():
        lines.append(f"Topics for {subject}: " + "; ".join(t.name for t in topics))
    lines.append("")
    if exam is None:
        lines.append("No exam is open yet.")
    else:
        name, items, spec, topics = exam
        lines.append(f'The open exam is called "{name}".')
        lines += _exam_lines(items, spec, topics)
    lines += ["", "The conversation so far:"]
    for m in messages[-MAX_MESSAGES:]:
        who = "Teacher" if m.role == "teacher" else "You"
        text = " ".join(m.text.split())[:MAX_MESSAGE_CHARS]
        if m.error:
            text += f" [That didn't work: {m.error}]"
        lines.append(f"{who}: {text}")
    lines += [
        "",
        "Answer the teacher's last message. Reply with:",
        "- reply: what you say back - short, plain and friendly, no markdown. If you are "
        "acting, say what you are doing ('Making a 40-mark exam on electrolysis...'), never "
        "which questions will be in it - the app picks them after you answer;",
        "- action: one of",
        "    'make': build a new exam. Only once you know enough - at least which subject, "
        "when the bank has more than one - and when no exam is open, or the teacher asks for "
        "a new or separate one;",
        "    'change': change the open exam (replace, remove or add questions or parts, a "
        "new total, different topics, more or fewer multiple choice);",
        "    'reroll': the same exam again with different questions;",
        "    'none': just talk - answer a question, or ask what you need to know. Ask only "
        "when something essential is missing; a total of marks defaults to 40;",
        "- instruction: for 'make', the full request in one or two sentences, gathering "
        "everything agreed in the conversation (subject, topics, marks, years, papers, how "
        "many multiple choice), e.g. '0620 chemistry, electrolysis and rates of reaction, 40 "
        "marks, no multiple choice, 2020 onwards'. For 'change', the change in one sentence, "
        "naming questions as Q1, MCQ2 and parts by their labels, e.g. 'replace Q3 with an "
        "electrolysis question'. Empty otherwise.",
    ]
    return "\n".join(lines)


def read_turn(
    llm: LLM,
    messages: list[ChatMessage],
    subjects: list[dict],
    topics_by_subject: dict[str, list[Topic]],
    exam: tuple[str, list[_Item], ExamSpec | None, list[Topic]] | None,
) -> ChatTurn:
    reply = llm.json(build_prompt(messages, subjects, topics_by_subject, exam), _ChatReply)
    action = reply.action.strip().lower()
    if action not in ("make", "change", "reroll"):
        action = "none"
    # with no exam to change, a change can only be a new exam, and there's nothing to reroll
    if exam is None or not exam[1]:
        action = {"change": "make", "reroll": "none"}.get(action, action)
    instruction = " ".join(reply.instruction.split())[:MAX_MESSAGE_CHARS]
    if action in ("make", "change") and not instruction:
        action = "none"
    text = reply.reply.strip() or ("Done." if action != "none" else "Sorry, I didn't follow that - could you say it another way?")
    return ChatTurn(reply=text, action=action, instruction=instruction if action != "none" else "")
