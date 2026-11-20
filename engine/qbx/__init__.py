"""Classark Exam Builder's engine."""

import pymupdf

# Text boxes without PyMuPDF's ascender/descender correction - every threshold in the
# parser was measured this way. pymupdf4llm switches the correction off when it's imported,
# and PyMuPDF imports it whenever pymupdf_layout is installed (the dev venv), but the exe
# leaves both out (package.py), so the built app read every box ~0.6pt taller and parsed
# differently from tests and calibration: 0610/52 MJ16's "108 ± 1 mm" fell into the row above
# (user-reported). Set here so every process that imports the engine reads text the same.
pymupdf.TOOLS.unset_quad_corrections(True)
