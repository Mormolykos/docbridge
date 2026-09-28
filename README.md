# docbridge

An MCP server that converts documents — PDF, DOCX, Markdown, plain text, images — and
returns, with every conversion, a report of what was **checked**, what **failed**, what
was **never checked**, and what the formats **cannot carry**. It also reads long documents
cheaply without weakening them: outline, exact search and verbatim reads.

**NOT_CHECKED is never treated as PASS.** A check that could not run, or that met content
docbridge cannot read, is reported as `NOT_CHECKED` or `UNSUPPORTED` and is never counted as
a pass. Silent corruption is treated as worse than a refused conversion: a changed digit in
an organisation number (`923 456 781` → `923 456 718`), an amount (`€4,999` → `€4,599`) or a
date (`14/07/2024` → `14/07/2025`) is a FAIL, reported with both values, their locations and
their context.

docbridge went through **four rounds of adversarial audit**. Every confirmed finding — 21
across the four rounds — was fixed and is covered by regression tests. **282 tests pass.**

Version 0.2.4 · schema 0.2.4 · Python ≥ 3.10 · licence AGPL-3.0

## Tools

| Tool | Does |
|---|---|
| `docbridge_contract` | the vocabulary: statuses, reasons, error codes, what each format can carry — call it first |
| `document_outline` | the map of a document in a few hundred characters: pages/lines/paragraphs, headings from the file itself, tables, unreadable pages |
| `document_search` | exact search with page/line, offsets, context and a `match_id`; says which pages it could not search |
| `document_read` | verbatim text of a page/line/paragraph range, capped; a cut is stated and a cursor continues it exactly |
| `document_read_around` | the pages (or paragraphs, lines) around a search match, verbatim |
| `get_report` | the full report behind a `report_id` |
| `pdf_merge` | PDFs → one PDF, every page proven identical to its source page |
| `pdf_split` | PDF → one file per range, or per page |
| `images_to_pdf` | images → PDF, JPEG bytes unchanged, rotation without re-encoding |
| `pdf_to_markdown` | PDF text layer → Markdown with page markers (no OCR) |
| `docx_to_markdown` | DOCX → Markdown (Pandoc, sandboxed) |
| `markdown_to_docx` | Markdown → DOCX (Pandoc, sandboxed) |
| `txt_to_docx` | plain text → DOCX, one paragraph per line |
| `document_extract` | a mechanical inventory: counts, then only the lists asked for (numbers, dates, URLs, identifiers, tables, ...), paged |
| `document_compare` | two documents compared through their canonical forms |
| `validate_conversion` | the full report for a conversion made by anything |

## Supported conversions

| From | To | Tool | Notes |
|---|---|---|---|
| PDF | Markdown | `pdf_to_markdown` | the PDF's text layer, with page markers; no OCR |
| DOCX | Markdown | `docx_to_markdown` | via Pandoc |
| Markdown | DOCX | `markdown_to_docx` | via Pandoc |
| plain text | DOCX | `txt_to_docx` | one paragraph per line |
| images | PDF | `images_to_pdf` | JPEG bytes embedded unchanged; rotation without re-encoding |
| several PDFs | one PDF | `pdf_merge` | every page proven identical to its source page |
| PDF | several PDFs | `pdf_split` | by ranges, or one file per page |

A conversion made by any other tool can be checked with `validate_conversion`, and any two
documents compared with `document_compare`, across PDF, DOCX, Markdown and plain text.

## Reading a report

By default a tool returns `report_summary` and a `report_id`; `detail="full"` or
`get_report(report_id)` gives the whole report. The summary never states a different verdict.

```text
operation_completed          the file was written            (not a verdict)
status                       PASS | FAIL | NOT_CHECKED | UNSUPPORTED
required_axes                what a PASS covers for this operation
status_basis                 why, in one sentence
differences                  each change: axis, kind, before, after, location, context
unsupported_checks           everything that was not established
```

`NOT_CHECKED` and `UNSUPPORTED` are never passes. Full semantics:
[DOCUMENT_MCP_CONTRACT.md](DOCUMENT_MCP_CONTRACT.md) and
[VALIDATION_MODEL.md](VALIDATION_MODEL.md).

## Evidence preservation and lossless reading

docbridge may reduce the volume of its own responses, but it **never alters, summarizes,
paraphrases, semantically filters, or silently truncates source evidence.**

- Reads are verbatim. Every excerpt carries the file's SHA-256, its offsets and the SHA-256
  of the returned text.
- A cut is always stated: `truncated`, `remaining_chars`, and a `next_cursor` that continues
  at exactly the next character. A cursor or `match_id` issued for another version of the
  file is refused with `stale_reference`.
- Content docbridge cannot read — a page with no text layer, an equation, an embedded
  object — becomes an UNKNOWN sentinel that matches nothing, so no comparison can pass
  across it.
- Sources are never overwritten; outputs are written atomically.
- Every path must be absolute and inside the allowed roots (`DOCBRIDGE_ROOTS`).
- A PDF's text means its text layer, not what the page shows. There is no OCR.

```text
document_outline(path)                      the map: units, headings, tables, unreadable pages
document_search(path, "tax residence")      exact matches, each with a match_id and its page
document_read_around(path, match_id)        the surrounding pages, verbatim
document_read(path, start=12, end=16)       any range, verbatim; follow next_cursor if truncated
```

## Install

Python ≥ 3.10. Pandoc ≥ 2.15 is needed only for `markdown_to_docx` and `docx_to_markdown`;
docbridge finds it on `PATH`, or set `DOCBRIDGE_PANDOC` to the executable. Every other tool
works without it.

```bash
git clone https://github.com/Mormolykos/docbridge.git
cd docbridge
python -m venv .venv
.venv/bin/pip install -e ".[dev]"      # Windows: .venv\Scripts\pip install -e ".[dev]"
```

`requirements.lock` pins the exact environment the test suite ran in.

## MCP server setup

docbridge is a stdio MCP server: `python -m docbridge.server`, or the `docbridge-mcp`
command the package installs. For clients that use the common `mcpServers` configuration:

```json
{
  "mcpServers": {
    "docbridge": {
      "command": "/path/to/docbridge/.venv/bin/python",
      "args": ["-m", "docbridge.server"],
      "env": { "DOCBRIDGE_ROOTS": "/path/to/your/documents" }
    }
  }
}
```

On Windows, `command` is `C:\\path\\to\\docbridge\\.venv\\Scripts\\python.exe`.
`DOCBRIDGE_ROOTS` lists the folders docbridge may read and write, separated by the system's
path separator (`:` on macOS and Linux, `;` on Windows); by default it is the user's home
folder. Optional: `DOCBRIDGE_PANDOC` (the Pandoc executable) and `DOCBRIDGE_SEARCH_TIMEOUT_S`
(the time bound on one search, default 2 s; a stopped search returns `search_timeout`, never
a partial result).

## Tests

```bash
.venv/bin/python -m pytest              # 282 tests, including the property fuzz
.venv/bin/python scripts/e2e_fresh.py   # a fresh end-to-end run over the real MCP protocol
```

`DOCBRIDGE_FUZZ_N` sets the property-fuzz iterations (default 300). Pandoc-dependent tests
skip with a stated reason if Pandoc is absent — a skip is not a pass.

## Documentation

- [DOCUMENT_MCP_CONTRACT.md](DOCUMENT_MCP_CONTRACT.md) — every tool, argument, status, reason
  and error code.
- [VALIDATION_MODEL.md](VALIDATION_MODEL.md) — what each check compares, the declared
  domains, the known limits, and the four audit rounds with their findings and repairs.

## Layout

```text
src/docbridge/
  contract.py      statuses, reasons, error codes, required axes, format capabilities
  normalize.py     the declared domain: tokens, numbers, identifiers
  canonical.py     the one shape every format is read into
  reading.py       lossless reading: outline, search, verbatim reads, cursors
  _search_worker.py  one search in its own process, so it can be stopped (time bound)
  extract/         readers: text.py, markdown.py, html.py (raw HTML in Markdown), docx.py, pdf.py
  compare.py       token views, sequence alignment, structure checks
  validate.py      builds a report and the global verdict
  report.py        the report as typed models (the output schema)
  convert/         pandoc.py, pdf_md.py, txt_docx.py, pdf_ops.py, images.py
  paths.py         path policy, atomic writes
  tools.py         the sixteen tools as plain functions
  server.py        the MCP server
tests/             programmatic fixtures (builders.py) and 16 test files
scripts/e2e_fresh.py
```

## Licence

AGPL-3.0 — see [LICENSE](LICENSE). docbridge imports PyMuPDF, which is licensed under the
AGPL-3.0.
