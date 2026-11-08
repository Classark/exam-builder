# Classark Exam Builder

A desktop app for teachers: it reads exam past papers and their mark schemes (PDF), splits them into questions and parts, links each part to its mark scheme entry, and composes new exam papers, mark schemes and topic booklets from them.

- `engine/` - the Python engine (PyMuPDF) and its local web server (FastAPI)
- `ui/` - the app's interface (React, Vite, TypeScript, Tailwind)
- `package.py`, `packaging/` - building the Windows and macOS apps and installers

## Building

Needs [uv](https://docs.astral.sh/uv/) and Node.js.

```
cd ui && npm ci && cd ..
uv run --project engine python package.py exe
```

The app is written to `dist/ClassarkExamBuilder/`. Run from source with `uv run --project engine uvicorn main:app` (from `engine/`) and `npm run dev` (from `ui/`).

## Licence

Copyright (C) 2026 Classark. Licensed under the GNU Affero General Public License v3.0 - see [LICENSE](LICENSE). It comes with no warranty.

"Classark" and the Classark logo are trademarks of Classark. The licence covers the code, not the right to use them: a modified version must not be distributed under the Classark name or logo.

The Classark account service the app can sign in to is a separate program and is not part of this repository.
