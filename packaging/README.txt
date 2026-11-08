Classark Exam Builder
=====================

Turns exam question papers and their mark schemes (PDF) into a searchable question bank,
and builds new practice papers - question paper and mark scheme - from the questions you
pick.

Running it (Windows)
--------------------
1. Unzip this folder anywhere, e.g. your Documents.
2. Double-click start.bat.
   - If it says "uv" is missing, run the one-line install command it shows, then run
     start.bat again.
   - The first run downloads Python and the libraries it needs (internet required once).
3. Your browser opens at http://127.0.0.1:8000. Close the start.bat window to stop it.

Adding papers
-------------
In the Reviewer tab, use "+ Add paper" and choose a question paper and its own mark scheme.
The tool checks they belong together before extracting.

Where your data lives
---------------------
Everything stays on this computer, next to start.bat:
  papers\   the PDFs you add
  out\      what was extracted from them
  quizzes\  the papers you build
Back these folders up to keep your work. The download ships with none of them - it
contains no exam content.
