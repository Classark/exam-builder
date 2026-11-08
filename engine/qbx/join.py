"""Join question paper parts to mark scheme entries (many-to-many, CLAUDE.md section 6).

The mark scheme may split a QP part further ("3(b)" in the QP, but "(b)(i)"/"(b)(ii)" rows
in the MS) or collapse several QP parts into one row - a straight (question_number, path)
equality match handles the common case, but a QP/MS pair whose dotted paths are prefixes of
each other (one side more granular than the other) must still be linked.
"""

from qbx.schema import Question, QuestionPartMarkScheme


def _same_or_ancestor(a: str, b: str) -> bool:
    """True if a == b, or one path is a dotted-prefix of the other (e.g. "b" and "b.i")."""
    if a == b:
        return True
    return a.startswith(b + ".") or b.startswith(a + ".")


def _lone_later_subpart(entry, entries: list) -> bool:
    """A row labelled with a later numeral whose part has no (i) row - "6(e)(ii)" straight
    after 6(d)(i) (0620/32 MJ24), meaning 6(d)(ii). A part split into subparts always
    starts at (i), so this is a mislabelled row, not QP part 6(e)'s subpart: left for
    _pair_by_position to place.

    Not lone when the part's own row is there instead of its (i): 0620/42 ON19 and FM24
    print "1(a)", "1(a)(ii)", "1(a)(iii)"... with the first row's numeral left off."""
    head, _, last = entry.part_path.rpartition(".")
    if not head or last == "i" or last not in ("ii", "iii", "iv", "v", "vi", "vii", "viii"):
        return False
    return not any(e.part_path in (head, f"{head}.i") for e in entries)


def _pair_by_position(parts: list, entries: list, matched: dict[str, list[int]]) -> list[tuple[str, int]]:
    """(part id, entry index) pairs for the rows Cambridge mislabelled in the mark scheme.

    Real-paper finding (0620 2019-2024, seven schemes): a row in the right place under the
    wrong label - "4(b)(ii)" straight after 4(c)(i), "2(c)(iii)" after 2(c)(i) with no
    (ii), "6(b)(iv)", "6(b)(v)" after 6(c)(iii). The order of the rows is still the order
    of the parts, so a run of unmatched parts between two matched neighbours pairs with
    the run of unmatched rows between those neighbours' own rows - one for one, and only
    when the two runs are the same length. Anything less clear-cut stays unjoined for the
    report to flag.
    """
    matched_entries = {i for indices in matched.values() for i in indices}
    pairs = []
    k = 0
    while k < len(parts):
        if matched.get(parts[k].id):
            k += 1
            continue
        start = k
        while k < len(parts) and not matched.get(parts[k].id):
            k += 1
        run = parts[start:k]
        if start == 0 and k == len(parts):
            continue  # no matched neighbour at all: nothing to say the labels even agree
        # the rows between the matched neighbours' rows (the question's start/end if none)
        low = max(matched[parts[start - 1].id]) if start > 0 else -1
        high = min(matched[parts[k].id]) if k < len(parts) else len(entries)
        between = list(range(low + 1, high))
        if between and len(between) == len(run) and not any(i in matched_entries for i in between):
            pairs += [(part.id, i) for part, i in zip(run, between)]
    return pairs


_NUMERALS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def _kind(path: str) -> str:
    """How a part is numbered: by numerals ("ii"), by letters ("b"), or not at all."""
    last = path.split(".")[-1] if path else ""
    return "numeral" if last in _NUMERALS else "letter" if last else "none"


def _pair_renumbered(parts: list, entries: list, matched: dict[str, list[int]]) -> list[tuple[str, int]]:
    """A question the paper numbers (i), (ii), (iii) and the scheme (a), (b), (c) - or the
    other way round (Edexcel 4MA0 2FR January 2016 Q11, 4MA1 1FR January 2023 Q2): the
    same list counted two ways. Paired in order, but only when nothing in the question
    matched by label, both sides have as many, each side is numbered one way and the two
    ways differ, and every mark printed on both sides agrees."""
    if not parts or any(matched.get(p.id) for p in parts) or len(parts) != len(entries):
        return []
    qp = {_kind(p.path) for p in parts}
    ms = {_kind(e.part_path) for e in entries}
    if len(qp) != 1 or len(ms) != 1 or qp == ms or "none" in qp | ms:
        return []
    if any(p.marks and e.marks and p.marks != e.marks for p, e in zip(parts, entries)):
        return []
    return [(p.id, i) for i, p in enumerate(parts)]


def _align_by_marks(parts: list, entries: list) -> list[tuple[str, int]] | None:
    """The question paper and its scheme cutting one question up differently - the paper's
    (a) worth 2 answered as rows (a) and (b), 1 each; (a)(i), (a)(ii), (b) against (a),
    (b), (c); (b)(i) worth 2 against rows (b)(i) and (b)(ii) (Edexcel 4MA0, many papers).
    Both lists are in order and their marks must agree, so walk them together adding up
    marks, closing a group wherever the running sums meet: the finest common grouping,
    and the only one. Each part joins every row of its group.

    Only when every part and every row has a mark and the two sides total the same - a
    misread mark anywhere and this says nothing (None)."""
    if any(p.marks <= 0 for p in parts) or any(e.marks <= 0 for e in entries):
        return None
    if sum(p.marks for p in parts) != sum(e.marks for e in entries):
        return None
    pairs: list[tuple[str, int]] = []
    i = j = 0
    while i < len(parts):
        group_parts, group_rows = [parts[i]], [j]
        left, right = parts[i].marks, entries[j].marks
        i, j = i + 1, j + 1
        while left != right:
            if left < right:
                group_parts.append(parts[i])
                left += parts[i].marks
                i += 1
            else:
                group_rows.append(j)
                right += entries[j].marks
                j += 1
        pairs += [(p.id, row) for p in group_parts for row in group_rows]
    return pairs


def join_parts_to_mark_scheme(
    questions: list[Question], mark_scheme: list["MarkSchemeEntry"]  # noqa: F821
) -> list[QuestionPartMarkScheme]:
    joins = []
    for question in questions:
        entries = [e for e in mark_scheme if e.question_number == question.question_number]
        matched: dict[str, list[int]] = {}
        exact_rows = [
            next((i for i, e in enumerate(entries) if e.part_path == part.path), None)
            for part in question.parts
        ]
        for k, part in enumerate(question.parts):
            # A looser match must still be in order. Real-paper finding (0620/32 ON20): part
            # 4(b), unsplit, took the row mislabelled "4(b)(ii)" as a subpart of its own -
            # though that row comes after 4(c)(i)'s, where 4(c)(ii)'s belongs.
            later_exact = min((r for r in exact_rows[k + 1 :] if r is not None), default=len(entries))
            for i, entry in enumerate(entries):
                if not _same_or_ancestor(part.path, entry.part_path):
                    continue
                if part.path != entry.part_path and i > later_exact:
                    continue
                if _lone_later_subpart(entry, entries):
                    continue
                matched.setdefault(part.id, []).append(i)
                joins.append(QuestionPartMarkScheme(part_id=part.id, mark_scheme_entry_id=entry.id))
        for part_id, i in _pair_by_position(question.parts, entries, matched):
            matched.setdefault(part_id, []).append(i)
            joins.append(QuestionPartMarkScheme(part_id=part_id, mark_scheme_entry_id=entries[i].id))
        for part_id, i in _pair_renumbered(question.parts, entries, matched):
            matched.setdefault(part_id, []).append(i)
            joins.append(QuestionPartMarkScheme(part_id=part_id, mark_scheme_entry_id=entries[i].id))
        # One answer for the whole question, its parts unlabelled - Edexcel 4CH0 2C January
        # 2019 prints Q7's (a), (b) and (c) as one block under "7". Every part the rows
        # above didn't place shares it: that's where their answers are.
        whole = [i for i, e in enumerate(entries) if e.part_path == ""]
        if len(whole) == 1 and len(entries) == 1:
            for part in question.parts:
                if part.path and part.id not in matched:
                    matched.setdefault(part.id, []).append(whole[0])
                    joins.append(QuestionPartMarkScheme(part_id=part.id, mark_scheme_entry_id=entries[whole[0]].id))
        # The other way round: the paper prints the question with no parts at all, just its
        # "(Total for Question 12 is 2 marks)", and the scheme labels its row "12 (a)" anyway
        # (4MA0 2FR June 2018). With one unlettered part there's nowhere else for the
        # question's rows to go.
        # Every row, even when one matched already: 4MA0 1FR June 2013 Q5 prints three
        # answers on one line under one mark, and the scheme gives each a row, "5", "(ii)",
        # "(iii)" - the first matched the part and the other two were left orphaned.
        if len(question.parts) == 1 and question.parts[0].path == "":
            only = question.parts[0]
            have = set(matched.get(only.id, []))
            for i, entry in enumerate(entries):
                if i in have:
                    continue
                matched.setdefault(only.id, []).append(i)
                joins.append(QuestionPartMarkScheme(part_id=only.id, mark_scheme_entry_id=entry.id))
        # Labels that still leave the question part-joined: the marks decide (_align_by_marks)
        joined_parts = {p.id for p in question.parts if matched.get(p.id)}
        used_rows = {i for indices in matched.values() for i in indices}
        if question.parts and entries and (
            len(joined_parts) < len(question.parts) or len(used_rows) < len(entries)
        ):
            aligned = _align_by_marks(question.parts, entries)
            if aligned is not None:
                ids = {p.id for p in question.parts}
                joins = [j for j in joins if j.part_id not in ids]
                matched = {}
                for part_id, i in aligned:
                    matched.setdefault(part_id, []).append(i)
                    joins.append(QuestionPartMarkScheme(part_id=part_id, mark_scheme_entry_id=entries[i].id))
        # Last, a row whose label markscheme.py corrected as a typo, where the correction
        # found no part: the label as printed was right after all. Real-paper finding
        # (0620/62 ON17): "3(e)" on two rows - both (e)'s, not (e) and (f), as the QP has no
        # (f). Exact matches only: this is already a second guess.
        taken = {i for indices in matched.values() for i in indices}
        for i, entry in enumerate(entries):
            if i in taken or entry.printed_path is None:
                continue
            for part in question.parts:
                if part.path == entry.printed_path:
                    joins.append(QuestionPartMarkScheme(part_id=part.id, mark_scheme_entry_id=entry.id))
    return joins
