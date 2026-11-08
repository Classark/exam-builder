"""Command line entry point.

    qbx extract papers/9702_42_MJ_23_qp.pdf --ms papers/9702_42_MJ_23_ms.pdf --out ./out
    qbx calibrate papers/
    qbx tag-eval 0620
"""

from collections import Counter
from pathlib import Path

import typer

from qbx.fingerprint import FingerprintError
from qbx.pairing import PairingError
from qbx.scan import plan_scan
from qbx.pipeline import ScannedPdfError, run_extraction

app = typer.Typer()


@app.command()
def extract(
    qp_path: Path,
    ms: Path = typer.Option(..., help="Path to the mark scheme PDF"),
    out: Path = typer.Option(Path("./out"), help="Output directory"),
) -> None:
    try:
        paper, report = run_extraction(qp_path, ms, out)
    except (ScannedPdfError, PairingError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(code=1)

    status = "passed" if report.passed else f"{len(report.issues)} issue(s)"
    typer.echo(f"wrote {out / paper.paper_key} ({status})")


@app.command()
def calibrate(
    papers_dir: Path = typer.Argument(Path("./papers"), help="Directory of QP/MS PDF pairs"),
    out: Path = typer.Option(Path("./out"), help="Output directory"),
) -> None:
    """Extract every QP/MS pair and report how each one came out, in one table.

    Every new paper so far has exposed a different hardcoded assumption - a page whose
    acknowledgements share space with questions, a header margin a point too deep - and
    each was noticed only by eyeballing a composed PDF. This turns "did it still work?"
    into one command over the whole corpus, and fails loudly when a paper regresses.

    The strongest signal is marks vs the cover total: a paper whose extracted marks
    reconcile with the total printed on its own cover, with every question joined to its
    mark scheme, is almost certainly parsed correctly (CLAUDE.md's validation rationale).
    Older papers don't print that total, so they fall back to the mark scheme's own marks -
    a paper that can't be reconciled against *either* counts as a failure rather than
    passing unchecked, since an unverified parse is the one most worth looking at.
    """
    # Paired by what each file is (scan.plan_scan), not by name: a folder of a few hundred
    # papers downloaded in bulk doesn't follow the _qp_/_ms_ convention.
    plan = plan_scan(papers_dir)
    if not plan.pairs:
        typer.echo(f"no QP/MS pairs found in {papers_dir}", err=True)
        raise typer.Exit(code=1)

    rows, failures = [], 0
    by_check: Counter[str] = Counter()
    by_subject: dict[str, list[int]] = {}  # subject -> [clean, total]
    crashes: Counter[str] = Counter()
    for index, pair in enumerate(plan.pairs, start=1):
        typer.echo(f"[{index}/{len(plan.pairs)}] {pair.paper_key}", err=True)
        tally = by_subject.setdefault(pair.paper_key[:4], [0, 0])
        tally[1] += 1
        try:
            paper, report = run_extraction(pair.qp_path, pair.ms_path, out)
        except (ScannedPdfError, FingerprintError, PairingError) as e:
            rows.append((pair.paper_key, f"FAILED: {e}"))
            by_check[f"refused: {type(e).__name__}"] += 1
            failures += 1
            continue
        except Exception as e:  # a crash on one paper mustn't end a run over hundreds
            rows.append((pair.paper_key, f"CRASHED: {type(e).__name__}: {e}"))
            crashes[f"{type(e).__name__}: {str(e)[:80]}"] += 1
            failures += 1
            continue
        for check in {issue.check for issue in report.issues}:
            by_check[check] += 1

        marks = sum(q.total_marks for q in paper.questions)
        # Papers before ~2020 print no total on the cover at all - that template only says
        # "The number of marks is given in brackets" (real-paper finding: 0620/41 s19). The
        # mark scheme's own marks are the fallback: independently parsed, from a different
        # document, by a different parser, and they agree exactly on every paper here.
        against, expected = ("cover", paper.cover_total_marks)
        if expected is None:
            against, expected = "ms", sum(e.marks for e in paper.mark_scheme) or None
        reconciles = "-" if expected is None else ("ok" if marks == expected else f"{marks}!={expected}")
        ms_questions = len({e.question_number for e in paper.mark_scheme})
        status = "ok" if report.passed else f"{len(report.issues)} issue(s)"
        if expected is not None and marks != expected:
            by_check[f"marks vs {against}"] += 1
        if not report.passed or expected is None or marks != expected:
            failures += 1
        else:
            tally[0] += 1
        rows.append(
            (
                paper.paper_key,
                f"questions={len(paper.questions):<4} parts={sum(len(q.parts) for q in paper.questions):<4} "
                f"marks={marks:<4} vs {against + '=' + reconciles:<17} ms questions={ms_questions:<4} {status}",
            )
        )

    width = max(len(name) for name, _ in rows)
    for name, detail in rows:
        typer.echo(f"{name:<{width}}  {detail}")

    # The summary is what makes a run over hundreds of papers actionable: which kind of
    # problem is most common, and which subjects it's in.
    typer.echo("\nby subject (clean/total):")
    for subject, (clean, total) in sorted(by_subject.items()):
        typer.echo(f"  {subject}  {clean}/{total}")
    if by_check:
        typer.echo("\npapers failing each check:")
        for check, count in by_check.most_common():
            typer.echo(f"  {count:>4}  {check}")
    if crashes:
        typer.echo("\ncrashes:")
        for message, count in crashes.most_common():
            typer.echo(f"  {count:>4}  {message}")
    if plan.unpaired:
        typer.echo(f"\n{len(plan.unpaired)} file(s) not paired:")
        for reason, count in Counter(u.reason.split(" - ")[-1] if " - " in u.reason else u.reason for u in plan.unpaired).most_common():
            typer.echo(f"  {count:>4}  {reason}")
    typer.echo(f"\n{len(rows) - failures}/{len(rows)} papers clean")
    if failures:
        raise typer.Exit(code=1)


@app.command("tag-eval")
def tag_eval(
    subject: str = typer.Argument(..., help="Subject code, e.g. 0620"),
    out: Path = typer.Option(Path(__file__).resolve().parents[2] / "out", help="Output directory"),
    styles: str = typer.Option("headings,full", help="Comma-separated: headings, outcomes, answers, full"),
    model: str = typer.Option("", help="Model id (default: the tagging model in Settings)"),
    misses: bool = typer.Option(False, help="List each part whose first topic missed"),
) -> None:
    """Tags the subject's hand-tagged parts in each prompt style and scores them against the
    hand tags. Nothing is saved. Uses Claude as set in Settings (Claude Code counts against
    the subscription's usage)."""
    from qbx import tag_eval as te
    from qbx.llm import load_settings, make_llm
    from qbx.topics import load_topics

    topics = load_topics(subject).topics
    if not topics:
        typer.echo(f"no topic list for {subject} - make one in Settings first", err=True)
        raise typer.Exit(code=1)
    papers = te.hand_tagged_papers(out, subject)
    if not papers:
        typer.echo(f"no hand-tagged {subject} papers in {out} - tag a few parts in the question bank first", err=True)
        raise typer.Exit(code=1)
    n_parts = sum(len(m) for _, m in papers)
    typer.echo(f"{len(papers)} paper(s), {n_parts} hand-tagged parts, {sum(bool(t.outcomes) for t in topics)}/{len(topics)} topics with outcomes")

    settings = load_settings()
    if model:
        settings.tag_model = model
    llm = make_llm(settings, "tag")
    for style in [s.strip() for s in styles.split(",") if s.strip()]:
        if style not in te.STYLES:
            typer.echo(f"unknown style {style!r}", err=True)
            raise typer.Exit(code=1)
        r = te.score(papers, llm, topics, style)
        pct = lambda n: f"{100 * n / r.replied:5.1f}%" if r.replied else "    -"
        typer.echo(
            f"{style:<9} replied {r.replied}/{r.parts}  first {pct(r.first)}  exact {pct(r.exact)}  "
            f"overlap {pct(r.overlap)}  area {pct(r.area)}"
        )
        if misses:
            for paper_key, key, want, got in r.misses:
                typer.echo(f"    {paper_key} {key}: hand {want}, Claude {got}")


if __name__ == "__main__":
    app()
