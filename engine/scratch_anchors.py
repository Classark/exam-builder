"""Phase -1: verify that anchor spans cluster cleanly by x0 before building anything else.

Usage:
    uv run python scratch_anchors.py ../papers/some_paper_qp.pdf

Put 40-50 Cambridge PDFs in papers/ first. Reading the result: x0 values bunching around
three distinct numbers (e.g. 56, 71, 85) means the anchor-clustering approach holds.
Values smeared across a range mean anchor detection needs rethinking before phase 3.
"""

import re
import sys
from pathlib import Path

import pymupdf

QUESTION_NUMBER_RE = re.compile(r"^\d{1,2}$")
PART_RE = re.compile(r"^\([a-z]\)$")
SUBPART_RE = re.compile(r"^\((i|ii|iii|iv|v)\)$")


def find_anchor_spans(pdf_path: Path) -> list[tuple[str, float]]:
    doc = pymupdf.open(pdf_path)
    spans = []
    try:
        for page in doc:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if QUESTION_NUMBER_RE.match(text) or PART_RE.match(text) or SUBPART_RE.match(text):
                            spans.append((text, round(span["bbox"][0])))
    finally:
        doc.close()
    return spans


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: uv run python scratch_anchors.py <path-to-pdf>")
        raise SystemExit(1)

    spans = find_anchor_spans(Path(sys.argv[1]))
    for text, x0 in sorted(spans, key=lambda s: s[1]):
        print(f"x0={x0:>5}  {text!r}")


if __name__ == "__main__":
    main()
