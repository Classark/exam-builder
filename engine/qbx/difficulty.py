"""How hard a question part is: 1 Easy, 2 Medium, 3 Hard, 4 Very hard (user-decided, four
levels).

Claude rates each part while tagging (tagging.py: from its wording and what its mark scheme
rewards, for the full range of students taking the qualification) - the rating that counts.
Where there's none (a part not tagged yet, a teacher's own tags, Claude switched off) it's
estimated here from what the paper says about itself, the same every time:

- the command word: "State", "Name", "Identify" ask for recall; "Explain", "Calculate",
  "Describe" for a method or a reason; "Evaluate", "Discuss", "Justify", "Prove" for
  judgement - the boards' own scale of demand;
- the marks: a one-mark part is a single point, six marks an extended answer.

Never the paper's tier (user-reported): Core and Extended papers share questions, as do
Edexcel's Foundation and Higher, and a question is as hard in one as in the other. So the
level is the question's own, on one scale for everyone taking the qualification, and reprints
share one level (Library.bank, exam_builder.candidate_units).

Choosing questions by difficulty is code (exam_builder.py): an exam aims at a level, and
"harder" means a level above the questions it replaces.
"""

from __future__ import annotations

import math
import re

LEVELS = {1: "Easy", 2: "Medium", 3: "Hard", 4: "Very hard"}
NAMES = {name.lower(): level for level, name in LEVELS.items()}

_RECALL = re.compile(
    r"^(?:state|name|identify|give|define|write down|write|list|complete|tick|circle|"
    r"label|choose|select|underline|draw a ring|measure|read|record|which|what is|what are|"
    r"how many|find the value)\b",
    re.IGNORECASE,
)
_JUDGEMENT = re.compile(
    r"^(?:evaluate|discuss|justify|assess|analyse|analyze|prove|derive|to what extent|"
    r"do you (?:think|agree)|recommend|consider|critically|comment on)\b",
    re.IGNORECASE,
)
# A part's text opens with its labels and maybe a lead-in; the command word is the first
# word of its first sentence that is one
_WORD_START = re.compile(r"(?:^|[.:]\s+|\)\s*)([A-Z][a-z]+(?:\s+[a-z]+){0,2})")


def nearest(x: float) -> int:
    """Halves round up (Python's round() sends 1.5 and 2.5 both to 2)."""
    return math.floor(x + 0.5)


def _command_level(text: str) -> float:
    for match in _WORD_START.finditer(text[:400]):
        words = match.group(1)
        if _RECALL.match(words):
            return 1.0
        if _JUDGEMENT.match(words):
            return 3.0
    return 2.0


def estimate(text: str, marks: int, mcq: bool = False) -> int:
    """A part's difficulty from its own wording and marks - 1 to 4."""
    score = 2.0 if mcq else _command_level(text)
    if not mcq:
        if marks <= 1:
            score -= 0.5
        elif marks >= 6:
            score += 1.0
        elif marks >= 4:
            score += 0.5
    return max(1, min(4, nearest(score)))


def part_level(tag: dict | None, estimated: int) -> int:
    """Claude's rating where it gave one, else the estimate."""
    rated = (tag or {}).get("difficulty")
    return rated if isinstance(rated, int) and 1 <= rated <= 4 else estimated


def question_level(parts: list[dict]) -> int | None:
    """A question's level from its parts' entries (each with "difficulty" and "marks"),
    weighted by marks, to the nearest level; None with no parts."""
    if not parts:
        return None
    weights = [(p.get("difficulty", 2), max(p.get("marks", 0), 1)) for p in parts]
    return nearest(sum(d * w for d, w in weights) / sum(w for _, w in weights))


def shared_levels(levels: dict[tuple[str, int], float], groups: dict[tuple[str, int], int]) -> dict[tuple[str, int], float]:
    """Each question's level, reprints given the average of their copies' - one question,
    one level, whichever paper printed it. `groups`: Library.duplicates."""
    by_group: dict[int, list[float]] = {}
    for key, level in levels.items():
        if key in groups:
            by_group.setdefault(groups[key], []).append(level)
    return {
        key: sum(by_group[groups[key]]) / len(by_group[groups[key]]) if key in groups else level
        for key, level in levels.items()
    }


def parse(value: object) -> int | None:
    """A level from 1-4 or its name ("hard", "very hard"); None for anything else."""
    if isinstance(value, int):
        return value if 1 <= value <= 4 else None
    if isinstance(value, str):
        text = value.strip().lower().replace("_", " ").replace("-", " ")
        if text.isdigit():
            return parse(int(text))
        return NAMES.get(text)
    return None
