"""Spotting the same question printed in more than one paper.

Cambridge reuses questions: multiple-choice items across the core/extended papers and
years apart, and practical experiments between a Practical paper and the same sitting's
Alternative to Practical. An exam built from the whole bank could otherwise set the same
question twice. Questions found to be reprints of each other form a "duplicate group", and
exam_builder takes at most one question from a group.

Two questions are reprints when, from different papers:
- their whole text is mostly the same wording (WHOLE_SAME of their shingles), or
- they share a long part worded almost identically (PART_SAME) - a reused set-up inside
  an otherwise different question.

Measured on 398 0620 papers (6,931 questions): the whole-text rule finds 976 groups, the
largest 12 (one multiple-choice item in twelve papers). A looser 0.5 chained unrelated
questions into one group of 77; matching on any part of 8+ words, as first tried, chained
whole Paper 3s together through stock parts ("Complete the dot-and-cross diagram…") -
hence whole-text first, and only long parts on their own. The part rule adds 93 pairs,
nearly all practical/ATP twins. Two questions of one paper are never reprints.

Wording is compared as bottom-k MinHash sketches of 3-word shingles: one hash per
shingle, so signing the whole bank takes under a second.

Same answer, different question: Cambridge also asks for one thing in different words
("Define electrolysis" / "What is meant by electrolysis?", "Explain in terms of collisions
why the rate increases"), and the mark scheme gives the same answer to both. Those aren't
reprints - their wording differs - but an exam setting both asks the same thing twice. So
each part's mark scheme answer is signed too, as the set of its content words (mark codes,
numbers, single letters and stock words like "allow" left out), and two parts whose answers
share ANSWER_SAME of their words "clash": an exam takes at most one of them.

Measured on 14 papers (590 parts, 0620/0580/0478/0972): every pair at 0.5 or above, with 6+
content words, was one answer - electrolysis, metallic bonding, isotopes, collision theory
(temperature, and concentration), NOx in engines, a "steeper line from the origin" graph,
a polyester's ester link, Avogadro's law, an acid on a carbonate. The next ones down (0.46,
0.44) were only related: two different acid-solid observations, dot-and-cross diagrams of
different molecules. Answers of fewer than 6 content words (formulae, numbers, an MCQ's
letter) say too little to compare. Matching 2-word phrases instead found only 4 of those
pairs above any cut that kept the noise out - paraphrased answers share words, not phrases.

Clashes are pairs, not groups: unlike reprints they're not chained, since a part can share
an answer with two parts that share nothing with each other.
"""

import re
import zlib

from qbx.tagging import clean_text

SIGNATURE_SIZE = 32
SHINGLE = 3  # words per shingle
WHOLE_MIN_WORDS = 8
WHOLE_SAME = 0.7
PART_MIN_WORDS = 25
PART_SAME = 0.85
# a sketch value shared by more texts than this is a stock phrase, not a reprint - it's
# not used to find candidates, which keeps the search close to linear on a big bank
COMMON_VALUE = 40

_WORD_RE = re.compile(r"[a-z0-9]+")
# dotted answer lines, marks and labels carry no wording of their own
_NOISE_RE = re.compile(r"…|\[\d+\]|\((?:[a-z]|[ivx]+)\)")
# Page furniture that can reach a part's extracted text (never its crop): footers, and on
# a last page the copyright notice or the periodic table. Shared by every paper, it would
# make unrelated questions look alike.
_BOILERPLATE_RES = [
    re.compile(r"permission to reproduce.*", re.S),
    re.compile(r"the periodic table of elements.*", re.S),
    re.compile(r"© ucles \d{4}"),
    re.compile(r"\d{4}\s*/\s*\d{2}\s*/\s*[a-z]\s*/\s*[a-z]\s*/\s*\d{2}"),
    re.compile(r"\[?turn over|blank page|do not write in this margin"),
]


ANSWER_MIN_WORDS = 6
ANSWER_SAME = 0.5
# words an answer shares with nearly every other: mark scheme conventions, connectives
_ANSWER_STOP = set(
    "or and the of a an to in on is are be as by with that it its this for from at any each "
    "mark marks max correct allow accept ignore owtte ecf ora oe ft isw cao awrt answer final "
    "better partial only both one two three no not but if then".split()
)
# mark codes (M1, A1, B1, C1, P1, SC2), numbers, numerals and lone letters (formula symbols)
_ANSWER_NOISE_RE = re.compile(r"[a-z]|[mabcp]\d|sc\d|\d+|[ivx]+")


def _words(text: str) -> list[str]:
    text = clean_text(text).lower()
    for pattern in _BOILERPLATE_RES:
        text = pattern.sub(" ", text)
    return _WORD_RE.findall(_NOISE_RE.sub(" ", text))


def text_signature(text: str, min_words: int = WHOLE_MIN_WORDS) -> list[int] | None:
    """The SIGNATURE_SIZE smallest hashes of the text's 3-word shingles, ascending; None
    for a text too short to tell a reprint from a coincidence."""
    words = _words(text)
    if len(words) < min_words:
        return None
    hashes = {zlib.crc32(" ".join(words[i : i + SHINGLE]).encode("utf-8")) for i in range(len(words) - SHINGLE + 1)}
    return sorted(hashes)[:SIGNATURE_SIZE]


def question_signatures(stem_text: str, part_texts: list[str]) -> dict:
    """What the library index keeps per question for duplicate-finding."""
    whole = text_signature(" ".join([stem_text, *part_texts]))
    parts = [text_signature(t, PART_MIN_WORDS) for t in part_texts] if len(part_texts) > 1 else []
    return {"whole": whole, "parts": [p for p in parts if p]}


def similarity(a: list[int], b: list[int]) -> float:
    union = sorted(set(a) | set(b))[:SIGNATURE_SIZE]
    shared = set(a) & set(b)
    return sum(1 for h in union if h in shared) / len(union) if union else 0.0


def duplicate_groups(questions: list[tuple[str, dict]]) -> list[int]:
    """questions: (paper key, question_signatures(...)) each. Returns a group id per
    question - equal ids for reprints of one another."""
    parent = list(range(len(questions)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def link(signatures: list[tuple[int, list[int]]], threshold: float) -> None:
        by_value: dict[int, list[int]] = {}  # sketch value -> indices into `signatures`
        for n, (_, sig) in enumerate(signatures):
            for value in sig:
                by_value.setdefault(value, []).append(n)
        # a pair sharing k values turns up k times: count them, then test each pair once.
        # Similar sketches share many values, so a pair sharing few can't qualify.
        shared_counts: dict[tuple[int, int], int] = {}
        for holders in by_value.values():
            if len(holders) < 2 or len(holders) > COMMON_VALUE:
                continue
            for x, a in enumerate(holders):
                for b in holders[x + 1 :]:
                    shared_counts[(a, b)] = shared_counts.get((a, b), 0) + 1
        for (a, b), count in shared_counts.items():
            (qi, sa), (qj, sb) = signatures[a], signatures[b]
            # a qualifying pair shares at least threshold x the smaller sketch's size (every
            # shared value in the union's bottom-k is in both sketches); half that is a
            # safe cut before the exact test
            if (
                count >= threshold * min(len(sa), len(sb)) / 2
                and questions[qi][0] != questions[qj][0]
                and find(qi) != find(qj)
                and similarity(sa, sb) >= threshold
            ):
                parent[find(qi)] = find(qj)

    link([(i, s["whole"]) for i, (_, s) in enumerate(questions) if s.get("whole")], WHOLE_SAME)
    link([(i, p) for i, (_, s) in enumerate(questions) for p in s.get("parts", [])], PART_SAME)
    return [find(i) for i in range(len(questions))]


def answer_signature(text: str) -> list[int] | None:
    """A mark scheme answer's content words, hashed: the SIGNATURE_SIZE smallest, ascending.
    None for an answer too short to compare."""
    words = {w for w in _words(text) if w not in _ANSWER_STOP and not _ANSWER_NOISE_RE.fullmatch(w)}
    if len(words) < ANSWER_MIN_WORDS:
        return None
    return sorted({zlib.crc32(w.encode("utf-8")) for w in words})[:SIGNATURE_SIZE]


def answer_clashes(answers: list[tuple[tuple, list[int]]]) -> list[tuple[int, int]]:
    """answers: (owner, answer_signature(...)) per part, owner being its (paper key,
    question number). Returns index pairs of parts from different questions whose answers
    are the same."""
    by_value: dict[int, list[int]] = {}
    for n, (_, sig) in enumerate(answers):
        for value in sig:
            by_value.setdefault(value, []).append(n)
    shared_counts: dict[tuple[int, int], int] = {}
    for holders in by_value.values():
        # a word in many answers ("electrons") can't be what makes two of them the same
        if len(holders) < 2 or len(holders) > COMMON_VALUE:
            continue
        for x, a in enumerate(holders):
            for b in holders[x + 1 :]:
                shared_counts[(a, b)] = shared_counts.get((a, b), 0) + 1
    pairs = []
    for (a, b), count in shared_counts.items():
        (owner_a, sa), (owner_b, sb) = answers[a], answers[b]
        if (
            count >= ANSWER_SAME * min(len(sa), len(sb)) / 2
            and owner_a != owner_b
            and similarity(sa, sb) >= ANSWER_SAME
        ):
            pairs.append((a, b))
    return pairs
