"""Load a PDF and classify it as digital (has a text layer) or scanned (image-only)."""

from pathlib import Path

import pymupdf

# A scanned page still tends to carry a handful of OCR-artifact or stamp characters;
# this threshold is a starting point, not calibrated against real papers yet (see phase -1).
MIN_CHARS_FOR_DIGITAL = 20


class Paper:
    def __init__(self, path: Path, doc: pymupdf.Document, is_digital: bool):
        self.path = path
        self.doc = doc
        self.is_digital = is_digital

    def close(self) -> None:
        self.doc.close()

    def __enter__(self) -> "Paper":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def load(pdf_path: Path) -> Paper:
    doc = pymupdf.open(pdf_path)
    is_digital = _detect_digital(doc)
    return Paper(pdf_path, doc, is_digital)


def _detect_digital(doc: pymupdf.Document) -> bool:
    sample_pages = doc[: min(3, doc.page_count)]
    for page in sample_pages:
        if len(page.get_text("text").strip()) >= MIN_CHARS_FOR_DIGITAL:
            return True
    return False
