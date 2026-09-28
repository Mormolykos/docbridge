# docbridge — MCP contract, v0.2.4

Schema version **0.2.4**. Written 2026-09-25, revised 2026-09-27. This is the prose form of
`src/docbridge/contract.py`, `src/docbridge/report.py`, `src/docbridge/reading.py` and
`src/docbridge/server.py`. Where this file and the code disagree, the code wins and this
file is corrected in place (METHODOLOGY R3). `tests/test_docs.py` fails if a
status, reason, error code or tool named in the code is missing here.

v0.2 changes from v0.1: the reading tools (`document_outline`, `document_search`,
`document_read`, `document_read_around`), `get_report`, short reports by default
(`detail`), a lean `document_extract` (`kinds`), and the error codes `stale_reference` and
`report_not_found`.

v0.2.1 (repairs after the round-1 adversarial audit, 2026-09-25): references are bound to the
decoded reading text as well as the file, so reuse under another decoding is refused; reads
report `range_unreadable_units`, and an empty final unit is no longer hidden; literal search
counts every occurrence, overlapping ones included, and composes combining sequences on both
sides; raw-HTML links in Markdown are compared; raw HTML is read in document order;
equations anywhere in a DOCX and non-picture graphics are UNKNOWN; merge and split carry and
compare bookmark destinations, and split reports bookmarks it could not carry.

v0.2.2 (repairs after the round-2 adversarial audit, 2026-09-26): raw HTML in Markdown — blocks,
table cells, captions and inline tags — is read by one reader over one declared domain, with
link destinations taken from the parsed `href` attribute and every element outside the
domain UNKNOWN; Word ruby guide text is read and compared (marked `⟦ruby: …⟧` when read), and
text in any other place the DOCX reader does not read is UNKNOWN; search composes text at
Unicode normalization boundaries, so every canonically equivalent form is found; split
decides which bookmarks it carried by their identity in the source; every search runs under
a time bound (`search_timeout`); image orientation is verified from the drawing matrix and in
colour, and a measurement that cannot tell rotations apart is `inconclusive`, never a pass.

v0.2.3 (repairs after the round-3 adversarial audit, 2026-09-26): HTML headings, bold and italic, and
lists in Markdown are structure the checks compare, in every context; structure docbridge
reads but cannot model (a heading, list or table tag inside a paragraph, a table inside a
table cell, any element outside the HTML domain) makes its structure check NOT_CHECKED
(`unresolved_content`), never PASS. `images_to_pdf` verifies that each page is exactly the
planned construction, compares the stored samples with the source's colour kept (as RGB),
checks the colour space and the effective opacity on both sides (a source without alpha is
fully opaque), and reads a JPEG's bytes from its raw stream — so a CMYK JPEG no longer fails.

v0.2.4 (repairs after the round-4 adversarial audit, 2026-09-27): a DOCX list paragraph is a list
item wherever it sits — table cell (nested tables included), text box, footnote, or a
heading that carries numbering — keeping its other role; its numbering is resolved from the
paragraph and its style property by property, with the level a style is linked to. A table
nested in a DOCX cell makes the tables check NOT_CHECKED (`unresolved_content`), never PASS.
The `blocks` inventory of `document_extract` gains `list_item`.

## 0. The invariant

> **docbridge may reduce the volume of its own responses, but it never alters, summarizes,
> paraphrases, semantically filters, or silently truncates source evidence.**

- *Its own responses* are reports and inventories. Those are shortened by default; the full
  report is always retrievable (`get_report`, or `detail="full"`).
- *Source evidence* is document text. It is returned character for character. Nothing
  decides what is relevant; there is no summarizing path and no switch to turn one on.
- *Every cut is stated*: `truncated`, how many characters were held back, and a cursor that
  continues exactly where the cut was made.
- Every excerpt carries the source file's SHA-256, its character offsets, the units it spans
  and the SHA-256 of the returned text, so any reader can check it came back unchanged.

## 1. Two separate claims in every conversion response

| Field | Meaning |
|---|---|
| `operation_completed` | The operation ran and wrote its outputs. **Says nothing about fidelity.** |
| `report_summary.status` / `report.status` | What was verified about the output. |

A conversion can complete and still FAIL validation. The output file is kept; the report
says what is wrong with it. docbridge never deletes an output because it failed.

## 2. The four statuses

| Status | Meaning |
|---|---|
| `PASS` | The property was compared and holds, **within the axis's stated `scope`**, and nothing else. |
| `FAIL` | The property was compared and does not hold. At least one difference is listed or counted. |
| `NOT_CHECKED` | Both formats can carry the property, but this run did not establish it. **Never a pass.** |
| `UNSUPPORTED` | At least one of the two formats cannot carry the property, or it is outside docbridge's declared domain. There is nothing to compare, and that is reported, not hidden. |

Every `NOT_CHECKED` and `UNSUPPORTED` carries a `reason` from this fixed vocabulary:

| Reason | Status | Meaning |
|---|---|---|
| `no_text_layer` | NOT_CHECKED | A PDF page shows content but has no extractable text. OCR would be needed; docbridge has none. |
| `unresolved_content` | NOT_CHECKED | A construct docbridge cannot read (scanned page, equation, OLE object, unsupported list-number format, ...) sits where a difference is. No verdict is possible across it. On a structure check: the document holds structure of that kind docbridge reads but does not model (the check's `details` count and locate it), so its inventory is incomplete. |
| `inferred_structure` | NOT_CHECKED | The target's structure was inferred by docbridge (PDF → Markdown headings, lists, tables); the source format does not encode it, so it cannot be validated against the source. |
| `needs_layout_engine` | NOT_CHECKED | The property only exists after rendering (DOCX page count, layout, typography). |
| `not_implemented` | NOT_CHECKED | Both formats carry the property; docbridge has no checker for it. |
| `too_large` | NOT_CHECKED | The differing region exceeds the alignment limit (250,000 tokens). Counts are still reported. |
| `check_failed` | NOT_CHECKED | The checker itself raised, or the output could not be read back. Never read as a pass. |
| `inconclusive` | NOT_CHECKED | The measurement ran but cannot tell a right result from a wrong one here (an image orientation no measurement can distinguish). Never read as a pass. |
| `absent_in_format` | UNSUPPORTED | At least one format cannot represent the property. |
| `not_carried` | UNSUPPORTED | The property is present in the source and docbridge deliberately does not carry or compare it (images in document conversions, headers/footers, comments). |

## 3. Axes and the global status

Every report has six axes: `text`, `numeric`, `identifier`, `structure`, `layout`, `image`,
each with `status`, `reason`, `scope`, `compared`, `not_compared`, `differences`, `details`.
The fields `text_status`, `numeric_status`, `identifier_status`, `structure_status`,
`layout_status` (plus `image_status`) repeat the axis statuses.

**Global status rule:**

1. `FAIL` if any axis is FAIL.
2. `PASS` if every axis in `required_axes` is PASS.
3. `UNSUPPORTED` if every required axis is UNSUPPORTED.
4. `NOT_CHECKED` otherwise.

`required_axes` per operation:

| Operation | Required for a global PASS |
|---|---|
| `markdown_to_docx`, `docx_to_markdown`, `txt_to_docx`, `pdf_to_markdown`, `validate_conversion`, `document_compare` | `text`, `numeric`, `identifier` |
| `pdf_merge`, `pdf_split` | `structure`, `layout` |
| `images_to_pdf` | `structure`, `image` |

`status_basis` states in one sentence which axes decided the global status and what the
PASS does not cover. `unsupported_checks` lists every axis, structure check, excluded text
class and unrepresented construct that was not established.

## 4. Short reports: `detail`, `report_summary`, `get_report`

Every tool that produces a report takes `detail` = `"summary"` (default) or `"full"`.

- `summary`: the response carries `report_id` and `report_summary` — the global `status`,
  `status_basis`, `required_axes`, every axis status with its reason, the first 10
  differences (most important axes first), `differences_total`, the warning codes, the
  number of unsupported checks, and the one-line `summary`. **The summary never states a
  different verdict from the full report**; it only shortens lists, and says how long they
  really are. `report` is null.
- `full`: `report` is the whole report inline (§6).
- `get_report(report_id, section)` returns the full report, or one section (`axes`,
  `structure_checks`, `differences`, `warnings`, `unsupported_checks`). Reports are held for
  the last 200 operations of the server session; `report_path` keeps one on disk.

## 5. Tools

All paths are absolute. Every tool returns a typed result; a transport-level error
(`isError`) means the arguments did not match the advertised schema. Unknown arguments
are **rejected**, not ignored, and types are strict (`"yes"` is not a boolean).

### Reading (read-only, lossless)

| Tool | Arguments (defaults) | Returns |
|---|---|---|
| `document_outline` | `input_path`, `encoding`, `max_items`=200 | unit kind and count, headings from the file's own markup (Markdown/DOCX) or its bookmarks (PDF — never inferred), tables with their unit, per-page text-layer status (PDF), unreadable units, warning codes |
| `document_search` | `input_path`, `query`, `regex`=false, `case_sensitive`=false, `max_matches`=20, `start_match`=0, `context_chars`=120, `encoding` | every match verbatim with `match_id`, unit, label, offsets, context before and after; `matches_total`; `next_start_match`; `unsearchable_units` — **no match in an unsearchable unit is not evidence of absence**. A literal query reports every occurrence, overlapping ones included ("aa" occurs twice in "aaa"); a regex reports leftmost non-overlapping matches. Every search runs in a separate process that is stopped after `DOCBRIDGE_SEARCH_TIMEOUT_S` (default 2 s); a stopped search returns `search_timeout` and no result, never a partial one. |
| `document_read` | `input_path`, `start`=1, `end` (default: to the end), `max_chars`=12000, `cursor`, `encoding` | the verbatim excerpt of units start..end, or of the range a `cursor` continues; `truncated`, `remaining_chars`, `next_cursor`, `unreadable_units` (inside this excerpt, empty units included), `range_unreadable_units` (every unreadable unit of the whole requested range, returned or not) |
| `document_read_around` | `input_path`, `match_id`, `before`, `after` (defaults: 1 page, 3 paragraphs, 15 lines), `max_chars`=12000, `encoding` | the match's units plus the surrounding ones, verbatim. The match is always inside the excerpt; anything cut on either side is stated (`omitted_before_chars` + `before_cursor`, `remaining_chars` + `next_cursor`) |
| `get_report` | `report_id`, `section`=`all` | §4 |

**Units and verbatim, per format:**

| Format | Unit | "Verbatim" means |
|---|---|---|
| PDF | page | each page's text layer exactly as PyMuPDF returns it — not the rendered glyphs. A page with visible content and no text layer is an empty unit listed in `unreadable_units`. |
| TXT, Markdown | line | the decoded file; the only change is CRLF/CR → LF. Markdown is returned as source, not rendered. |
| DOCX | paragraph | the paragraph as docbridge's declared DOCX reader extracts it (VALIDATION_MODEL.md §8). Unreadable content appears in place as `⟦UNREADABLE: reason⟧`, image alt text as `⟦image alt: …⟧`, ruby guide text after its base as `⟦ruby: …⟧` — marked, never dropped. |

Every `char_start`/`char_end` indexes the document's **reading text**: its units joined by
one newline. A `cursor` or `match_id` is bound to the file's SHA-256 **and** to the reading
text it indexes (`source.reading_sha256`); used against a changed file, or with a different
`encoding` that decodes the file to different text, it is refused with `stale_reference`.

**Search folding, identical on query and text:** the text is composed (NFC) segment by
segment, cutting only at Unicode normalization boundaries, so every canonically equivalent
form matches every other — decomposed and composed accents, Hangul jamo and syllables, Indic
two-part vowels — and each match maps back to exact original offsets; the ligatures and
invisible characters declared in VALIDATION_MODEL.md §3 are folded; whitespace in a literal
query matches any run of whitespace (so a phrase broken across lines or pages is found);
case is ignored unless `case_sensitive=true`. Nothing else is folded. Matches are returned
with their original text and offsets.

### Inventory

| Tool | Arguments (defaults) | Returns |
|---|---|---|
| `document_extract` | `input_path`, `kinds` (default none), `encoding`, `max_items`=200, `offset`=0, `text_offset`=0, `output_path` (.json), `overwrite` | Counts and warning codes always. Lists only for the requested kinds — `numbers`, `identifiers`, `dates`, `urls`, `emails`, `headings`, `tables`, `links`, `emphasis`, `images`, `pages`, `blocks`, `warnings`, `unsupported`, `normalized_text` — each paged from `offset` by `max_items`, with `truncated` (how many follow) and `next_offset`. A mechanical inventory, not a substitute for reading; no semantic interpretation. `output_path` writes everything, uncapped. |
| `docbridge_contract` | — | The vocabulary: statuses, reasons, error codes, required axes, format capabilities, normalization, reading definitions, the invariant, Pandoc availability, limits. |

### Conversions and validation (each takes `report_path`, `max_differences`=200, `detail`)

| Tool | Arguments (defaults) | Writes | What its report checks |
|---|---|---|---|
| `pdf_merge` | `inputs` (1–500 PDFs, in order), `output_path`, `overwrite`=false, `raster_dpi`=72 (18–300) | one PDF | page count; per output page against its source page: text layer, rotation, size, link/annotation/form-field counts, rendered pixels; bookmarks with their destinations (URL, page and position, file, named action), pages shifted, expected derived from the source files; text/numeric/identifier over all pages. |
| `pdf_split` | `input_path`, exactly one of `ranges` (`"N"`, `"N-M"`, `"N-"`) or `each_page`=true, `output_dir` (default `<dir>/<stem>_split`), `overwrite`, `raster_dpi` | `<stem>_p<a>-<b>.pdf` per range | page count per output; per page identity as for merge; bookmarks with destinations inside each range. A bookmark whose page is outside every range, or that has no page (a URL), is reported as not carried (`bookmarks_not_carried`), never dropped silently. |
| `images_to_pdf` | `inputs` (1–2000 JPEG/PNG/TIFF/BMP/GIF/WEBP), `output_path`, `page_size` = `image` \| `A4` \| `Letter`, `rotations` (0/90/180/270 clockwise, one per image), `apply_exif_orientation`=true, `overwrite` | one PDF | one page per image; page size; the page is exactly the planned construction (one image drawn once, nothing else on the page); the JPEG's raw stream identical to the file under exactly `/DCTDecode`, with the size and `/Decode` the JPEG needs, or the stored samples equal to the source's compared as RGB; the colour space (the source's own ICC profile, or MuPDF's default); the effective opacity (a source without alpha is fully opaque); orientation — exactly, from the image's drawing matrix, and visually, in colour. An orientation nothing can distinguish is NOT_CHECKED (`inconclusive`). |
| `pdf_to_markdown` | `input_path`, `output_path` (default `<stem>.md`), `overwrite`, `detect_tables`=true, `page_markers`=true | one `.md` | text/numeric/identifier against the PDF text layer; page count and page assignment via markers. Headings, lists, tables: NOT_CHECKED (`inferred_structure`). |
| `docx_to_markdown` | `input_path`, `output_path` (default `<stem>.md`), `overwrite` | one `.md` | text/numeric/identifier, links, headings, lists, tables, bold/italic. |
| `markdown_to_docx` | `input_path`, `output_path` (default `<stem>.docx`), `overwrite`, `encoding` | one `.docx` | as `docx_to_markdown`. |
| `txt_to_docx` | `input_path`, `output_path` (default `<stem>.docx`), `overwrite`, `encoding` | one `.docx` | text/numeric/identifier. |
| `document_compare` | `left_path`, `right_path`, `left_encoding`, `right_encoding` | nothing | Same checks as `validate_conversion`; left is reported as `source`, right as `target`. |
| `validate_conversion` | `source_path`, `target_path`, `source_encoding`, `target_encoding`, `overwrite` | optional JSON report | Every check possible for the format pair, for a conversion made by anything. |

`report_path` writes the full JSON report there; it must end in `.json` and is checked
**before** the operation runs, so a refused report path never leaves an output behind
without its report.

## 6. The full report

| Field | Content |
|---|---|
| `schema_version`, `operation`, `timestamp` | as named (timestamp UTC, ISO 8601) |
| `source`, `target` | lists of paths (merge has several sources, split several targets) |
| `source_format`, `target_format` | `pdf`, `docx`, `md`, `txt`, or `image` |
| `source_sha256`, `target_sha256` | SHA-256 of every file compared |
| `status`, `required_axes`, `status_basis` | §3 |
| `text_status` … `image_status` | §3 |
| `axes`, `structure_checks` | per-axis and per-check results with scope, counts, details |
| `source_page_count`, `target_page_count` | integer, or null where the format has no pages |
| `warnings` | `{side, code, message, location}`; side is `source`, `target` or `operation` |
| `differences` | `{axis, kind, source, target, source_location, target_location, source_context, target_context, classes, note}`; kind ∈ `missing`, `added`, `changed`, `moved`, `mismatch`, `unverifiable` |
| `differences_total`, `differences_truncated` | total found; how many were not listed because of `max_differences` |
| `unsupported_checks` | `{check, status, reason, detail}` for everything not established |
| `oracle` | what read each side — e.g. "PyMuPDF 1.27.2.3 text layer, content-stream order" |
| `normalization` | the declared rules the text was normalized with (VALIDATION_MODEL.md §3) |
| `tool_versions` | docbridge, Python, PyMuPDF/MuPDF, python-docx, markdown-it-py, Pillow, lxml, mcp, Pandoc |
| `summary` | one line: global status, each axis, the first number/identifier differences |

## 7. Error codes

Semantic errors are values: `error = {code, message, hint}`, `operation_completed = false`.

| Code | When |
|---|---|
| `invalid_argument` | an argument is valid JSON but not a valid request (bad page range, unit range outside the document, both/neither of `ranges`/`each_page`, unknown extract kind, invalid regex, malformed cursor, wrong input format for the tool) |
| `path_not_absolute` | a relative path |
| `path_traversal` | any `..` component, refused even if it resolves inside a root |
| `path_forbidden` | UNC share, device path, NTFS alternate data stream, reserved device name, NUL character |
| `path_outside_roots` | the resolved path (symlinks/junctions followed) is outside `DOCBRIDGE_ROOTS` |
| `input_not_found` | missing, or not a regular file |
| `input_too_large` | above `DOCBRIDGE_MAX_INPUT_MB` (default 200), or a DOCX that unpacks past 1 GiB or expands a part >200× |
| `output_exists` | the output exists and `overwrite` is false (split checks every part before writing any) |
| `would_overwrite_source` | the output would replace an input — refused even with `overwrite=true` |
| `output_extension_mismatch` | the output name has the wrong extension |
| `unsupported_format` | an extension docbridge does not read; `.docm`, `.dotx`, Strict Open XML |
| `format_mismatch` | the extension says one format, the bytes another |
| `malformed_input` | damaged file (unparseable PDF, zero pages, bad zip, bad XML, missing main part) |
| `encrypted_input` | a PDF needing a password, or an OLE container named `.docx` (password-protected Word or legacy `.doc`) |
| `undecodable_text` | `.md`/`.txt` not valid UTF-8 (or UTF-16 with BOM) and no `encoding` given — docbridge never guesses |
| `unsupported_image` | unreadable image, 16-bit samples, multi-frame, non-JPEG CMYK |
| `unsupported_content` | text DOCX cannot store (control characters) — listed with positions, never dropped |
| `dependency_missing` | Pandoc not found or older than 2.15 |
| `conversion_failed` | Pandoc returned an error or timed out (180 s) |
| `stale_reference` | a `cursor` or `match_id` was issued for a different version of the file, or for a different decoding of it |
| `report_not_found` | a `report_id` this server session does not hold |
| `search_timeout` | a search did not finish within its time bound and was stopped. No result is returned or implied — it is not evidence that the text is absent |
| `internal_error` | anything else; the traceback goes to the server's stderr only |

## 8. File behaviour and security

- **Roots.** Every input and output must resolve inside `DOCBRIDGE_ROOTS`
  (os.pathsep-separated). Default: the user's home directory.
- **Sources are never overwritten.** Outputs replace existing files only with
  `overwrite=true`, and always atomically (written beside the target, then moved).
  A failed operation leaves no partial file and no temporary file.
- **Deterministic names.** Defaults are derived from the source name only (§5).
- **Parent directories** of an output are created, inside the roots only.
- **No network.** docbridge makes no network calls. Pandoc runs with `--sandbox`
  (no reads beyond its input, no fetching); Markdown images are therefore never
  embedded, and the report says so.
- **No execution of document content.** No macros (`.docm` refused), no PDF JavaScript
  is enabled by docbridge, XML is parsed with entity resolution and network access off.
- **Malformed input never crashes the server**: every failure is an error value.
- **Encryption.** Password-protected inputs are refused. A PDF readable without a
  password but carrying owner restrictions is read with a warning; outputs do not carry
  the restrictions.
- **Caches.** The reading text of the last 8 documents is cached by content hash (a changed
  file is re-read); reports of the last 200 operations are held for `get_report`. Both live
  in the server process only.

## 9. Environment

| Variable | Default | Effect |
|---|---|---|
| `DOCBRIDGE_ROOTS` | user home | allowed directories |
| `DOCBRIDGE_MAX_INPUT_MB` | 200 | per-input size limit |
| `DOCBRIDGE_PANDOC` | PATH, then known install locations | the pandoc executable |
| `DOCBRIDGE_SEARCH_TIMEOUT_S` | 2 | the time bound on one search, in seconds |

## 10. Deliberately not in docbridge

OCR, any summarizing or relevance-filtering path, a local model in the retrieval path,
cloud storage, an editing UI, translation, signatures, legal interpretation, PDF form
filling, databases, authentication, PDF → DOCX, DOCX → PDF, image comparison inside
document conversions, typography and layout comparison for document conversions.
Postponed: a completeness guard that reports cross-references a passage points to (it may
only ever say what it found, never that nothing else matters).
