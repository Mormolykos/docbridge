"""The MCP server: docbridge's tools over stdio.

Run it:  python -m docbridge.server        (stdio transport, no port)

Conventions, shared with the estate's other servers (shale ADR 007):

1. Semantic errors are values (`error.code` from a declared vocabulary, plus a
   hint). Schema errors belong to the transport: an argument that does not fit
   the advertised schema never reaches a tool.
2. The schema version is in every tool description and every response.
3. Typed outputs: every tool advertises an outputSchema, so a client can check
   a response against the declaration instead of trusting it.

One addition, and the reason it exists: the SDK builds each tool's argument
model with pydantic's default `extra="ignore"`, so an argument the tool does
not have - `overwite=true`, say - would be dropped without a word and the
agent would believe it had been honoured. `_forbid_unknown_arguments` rebuilds
every argument model with `extra="forbid"` and strict types, and refuses to
start if that did not take effect.

Response size: tools that produce a report return `report_summary` and a
`report_id` by default (detail="summary"); the full report comes with
detail="full" or from get_report. That shortens docbridge's OWN output. Source
evidence is never shortened: reads are verbatim and every cut is stated.
"""
from __future__ import annotations

import logging
import sys
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from . import tools as T
from .contract import EVIDENCE_INVARIANT, SCHEMA_VERSION
from .report import ValidationReport  # noqa: F401  (re-exported for schema generation)

V = f"[docbridge schema {SCHEMA_VERSION}]"

INSTRUCTIONS = f"""docbridge {SCHEMA_VERSION}: document conversion with a fidelity report, and lossless reading.

Invariant: {EVIDENCE_INVARIANT}.

Reading a long PDF/DOCX/Markdown cheaply: document_outline for the map,
document_search for exact places (with page/line and a match_id),
document_read_around(match_id) for the surrounding pages verbatim, document_read
for any page/line/paragraph range. Every excerpt is exact, carries its offsets
and SHA-256; if a read is cut, `truncated` is true and `next_cursor` continues it.

Every conversion returns `operation_completed` (the file was written) and a
separate report status (what was verified). They are different claims.
report status is PASS, FAIL, NOT_CHECKED or UNSUPPORTED; PASS covers only the
required axes. NOT_CHECKED and UNSUPPORTED are never passes. By default you get
report_summary + report_id; get_report(report_id) returns the full report.
PDF text means the PDF's text layer, not the rendered page. There is no OCR.
Paths must be absolute and inside the server's allowed roots; sources are never
overwritten. docbridge_contract returns the whole vocabulary."""

Path_ = Annotated[str, Field(min_length=1, max_length=4096, description="Absolute file path.")]
OptPath = Annotated[str | None, Field(default=None, max_length=4096)]
MaxDiffs = Annotated[int, Field(ge=0, le=5000, description="Differences kept in the full report; the rest are counted.")]
Dpi = Annotated[int, Field(ge=18, le=300, description="Render resolution for the page-identity check.")]
Encoding = Annotated[str | None, Field(default=None, max_length=64,
                                       description="Text encoding of a .md/.txt input. Omit for UTF-8; "
                                                   "docbridge never guesses.")]
Detail = Annotated[Literal["summary", "full"],
                   Field(description="summary: report_summary + report_id (get_report has the rest). "
                                     "full: the whole report inline.")]
MaxChars = Annotated[int, Field(ge=100, le=500000, description="Most characters returned; a cut is always "
                                                               "stated, with a cursor to continue.")]


def _desc(text: str) -> str:
    return f"{text} {V}"


def _detail(res: T.ConversionResult, detail: str) -> T.ConversionResult:
    if detail != "full":
        res.report = None
    return res


def build_server():
    from mcp.server import MCPServer
    from mcp_types import ToolAnnotations

    server = MCPServer("docbridge", title="docbridge", version=SCHEMA_VERSION, instructions=INSTRUCTIONS,
                       description="Document conversion with PASS/FAIL/NOT_CHECKED/UNSUPPORTED fidelity reports, "
                                   "and lossless outline/search/read.")
    writes = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    reads = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @server.tool(name="docbridge_contract", annotations=reads, description=_desc(
        "The vocabulary every response uses: statuses, reasons, error codes, which axes a global PASS covers "
        "per operation, what each format can carry, the declared text normalization, what 'verbatim' means "
        "per format, and whether Pandoc is available. Call this before interpreting a report."))
    def docbridge_contract() -> T.ContractResult:
        return T.contract()

    # ---------------------------------------------------------- reading ---

    @server.tool(name="document_outline", annotations=reads, description=_desc(
        "The structural map of a PDF/DOCX/MD/TXT in a few hundred characters: unit kind and count (pages, "
        "lines or paragraphs), headings from the file's own markup or PDF bookmarks (never inferred), tables, "
        "per-page text-layer status, unreadable units. Use it to decide what to read."))
    def document_outline(input_path: Path_, encoding: Encoding = None,
                         max_items: Annotated[int, Field(ge=1, le=10000)] = 200) -> T.OutlineResult:
        return T.document_outline(input_path, encoding, max_items)

    @server.tool(name="document_search", annotations=reads, description=_desc(
        "Exact search inside a PDF/DOCX/MD/TXT. Returns each match verbatim with its page/line/paragraph, "
        "character offsets, surrounding context and a match_id for document_read_around. Whitespace in the "
        "query matches any line break or spacing. Units that cannot be read are listed: no match there is not "
        "evidence of absence."))
    def document_search(input_path: Path_, query: Annotated[str, Field(min_length=1, max_length=2000)],
                        regex: bool = False, case_sensitive: bool = False,
                        max_matches: Annotated[int, Field(ge=1, le=500)] = 20,
                        start_match: Annotated[int, Field(ge=0)] = 0,
                        context_chars: Annotated[int, Field(ge=0, le=5000)] = 120,
                        encoding: Encoding = None) -> T.SearchResult:
        return T.document_search(input_path, query, regex, case_sensitive, max_matches, start_match,
                                 context_chars, encoding)

    @server.tool(name="document_read", annotations=reads, description=_desc(
        "Verbatim text of units start..end (PDF pages, MD/TXT lines, DOCX paragraphs; end omitted = to the "
        "end), up to max_chars. Never summarized or filtered. The excerpt carries offsets and SHA-256; if cut, "
        "truncated=true, remaining_chars says how much is left and next_cursor continues exactly."))
    def document_read(input_path: Path_, start: Annotated[int, Field(ge=1)] = 1,
                      end: Annotated[int | None, Field(default=None, ge=1)] = None,
                      max_chars: MaxChars = 12000,
                      cursor: Annotated[str | None, Field(default=None, max_length=200)] = None,
                      encoding: Encoding = None) -> T.ReadResult:
        return T.document_read(input_path, start, end, max_chars, cursor, encoding)

    @server.tool(name="document_read_around", annotations=reads, description=_desc(
        "Verbatim text around a search match: the match's units plus `before`/`after` units (defaults: 1 page, "
        "3 paragraphs, 15 lines). The match is always inside the excerpt; anything left out on either side is "
        "stated with a cursor."))
    def document_read_around(input_path: Path_, match_id: Annotated[str, Field(min_length=1, max_length=200)],
                             before: Annotated[int | None, Field(default=None, ge=0, le=1000)] = None,
                             after: Annotated[int | None, Field(default=None, ge=0, le=1000)] = None,
                             max_chars: MaxChars = 12000, encoding: Encoding = None) -> T.ReadResult:
        return T.document_read_around(input_path, match_id, before, after, max_chars, encoding)

    @server.tool(name="get_report", annotations=reads, description=_desc(
        "The full validation report behind a report_id (held for the last 200 operations of this server "
        "session), or one section of it: axes, structure_checks, differences, warnings, unsupported_checks."))
    def get_report(report_id: Annotated[str, Field(min_length=1, max_length=64)],
                   section: Literal["all", "axes", "structure_checks", "differences", "warnings",
                                    "unsupported_checks"] = "all") -> T.ReportResult:
        return T.get_report(report_id, section)

    @server.tool(name="document_extract", annotations=writes, description=_desc(
        "A mechanical inventory of one PDF/DOCX/MD/TXT - not a substitute for reading it. Counts always; lists "
        "only for the kinds asked (numbers, identifiers, dates, urls, emails, headings, tables, links, emphasis, "
        "images, pages, blocks, warnings, unsupported, normalized_text), paged by offset/max_items with the rest "
        "stated. output_path writes everything to JSON."))
    def document_extract(input_path: Path_,
                         kinds: Annotated[list[Literal["numbers", "identifiers", "dates", "urls", "emails", "headings",
                                                       "tables", "links", "emphasis", "images", "pages", "blocks",
                                                       "warnings", "unsupported", "normalized_text"]] | None,
                                          Field(default=None)] = None,
                         encoding: Encoding = None, max_items: Annotated[int, Field(ge=1, le=100000)] = 200,
                         offset: Annotated[int, Field(ge=0)] = 0, text_offset: Annotated[int, Field(ge=0)] = 0,
                         output_path: OptPath = None, overwrite: bool = False) -> T.ExtractResult:
        return T.document_extract(input_path, kinds, encoding, max_items, offset, text_offset, output_path,
                                  overwrite)

    # ------------------------------------------------------ conversions ---

    @server.tool(name="pdf_merge", annotations=writes, description=_desc(
        "Merge PDFs, in the order given, into one PDF. Bookmarks, links, annotations and form fields are "
        "carried. The report checks page count, and that every output page is pixel-identical (rendered), "
        "text-identical and rotation-identical to the source page it came from."))
    def pdf_merge(inputs: Annotated[list[Path_], Field(min_length=1, max_length=500,
                                                       description="PDF paths, in output order.")],
                  output_path: Path_, overwrite: bool = False, raster_dpi: Dpi = 72,
                  report_path: OptPath = None, max_differences: MaxDiffs = 200,
                  detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.pdf_merge(inputs, output_path, overwrite, raster_dpi, report_path, max_differences), detail)

    @server.tool(name="pdf_split", annotations=writes, description=_desc(
        "Split a PDF into one file per page range ('1-3', '5', '7-' to the end) or one file per page "
        "(each_page=true). Files are named <stem>_p<first>-<last>.pdf. The report checks each output's page "
        "count and that every page is identical to its source page."))
    def pdf_split(input_path: Path_,
                  ranges: Annotated[list[Annotated[str, Field(max_length=32)]] | None,
                                    Field(default=None, max_length=10000,
                                          description="1-based ranges; exactly one of ranges/each_page.")] = None,
                  each_page: bool = False, output_dir: OptPath = None, overwrite: bool = False,
                  raster_dpi: Dpi = 72, report_path: OptPath = None,
                  max_differences: MaxDiffs = 200, detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.pdf_split(input_path, ranges, each_page, output_dir, overwrite, raster_dpi, report_path,
                                   max_differences), detail)

    @server.tool(name="images_to_pdf", annotations=writes, description=_desc(
        "One PDF page per image (JPEG, PNG, TIFF, BMP, GIF, WEBP). JPEGs are embedded byte-for-byte; EXIF "
        "orientation and the optional clockwise rotations are applied by page placement, never by "
        "re-encoding. The report checks bytes or pixels (colour and alpha separately) and orientation."))
    def images_to_pdf(inputs: Annotated[list[Path_], Field(min_length=1, max_length=2000)],
                      output_path: Path_,
                      page_size: Literal["image", "A4", "Letter"] = "image",
                      rotations: Annotated[list[Literal[0, 90, 180, 270]] | None,
                                           Field(default=None, description="Clockwise degrees, one per image.")] = None,
                      apply_exif_orientation: bool = True, overwrite: bool = False,
                      report_path: OptPath = None, max_differences: MaxDiffs = 200,
                      detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.images_to_pdf(inputs, output_path, page_size, rotations, apply_exif_orientation, overwrite,
                                       report_path, max_differences), detail)

    @server.tool(name="pdf_to_markdown", annotations=writes, description=_desc(
        "Markdown from a PDF's text layer, with page markers. Headings, lists and tables are INFERRED "
        "(reported NOT_CHECKED); the text, numbers and identifiers are validated against the PDF. Pages "
        "with no text layer are marked, never guessed (no OCR)."))
    def pdf_to_markdown(input_path: Path_, output_path: OptPath = None, overwrite: bool = False,
                        detect_tables: bool = True, page_markers: bool = True,
                        report_path: OptPath = None, max_differences: MaxDiffs = 200,
                        detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.pdf_to_markdown(input_path, output_path, overwrite, detect_tables, page_markers,
                                         report_path, max_differences), detail)

    @server.tool(name="docx_to_markdown", annotations=writes, description=_desc(
        "Markdown (CommonMark + pipe tables + strikethrough) from a DOCX, via sandboxed Pandoc. The report "
        "checks text, numbers, identifiers, links, headings, lists, tables and bold/italic, and lists what "
        "Markdown cannot carry (headers/footers, comments, equations, images)."))
    def docx_to_markdown(input_path: Path_, output_path: OptPath = None, overwrite: bool = False,
                         report_path: OptPath = None, max_differences: MaxDiffs = 200,
                         detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.docx_to_markdown(input_path, output_path, overwrite, report_path, max_differences), detail)

    @server.tool(name="markdown_to_docx", annotations=writes, description=_desc(
        "DOCX from Markdown (CommonMark + pipe tables + strikethrough) via sandboxed Pandoc: headings, "
        "paragraphs, bold/italic, lists, tables, links, code blocks. Images are never fetched (alt text is "
        "kept). The report checks text, numbers, identifiers and structure."))
    def markdown_to_docx(input_path: Path_, output_path: OptPath = None, overwrite: bool = False,
                         encoding: Encoding = None, report_path: OptPath = None,
                         max_differences: MaxDiffs = 200, detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.markdown_to_docx(input_path, output_path, overwrite, encoding, report_path,
                                          max_differences), detail)

    @server.tool(name="txt_to_docx", annotations=writes, description=_desc(
        "DOCX from plain text: one paragraph per line, tabs kept, form feeds as page breaks. Characters DOCX "
        "cannot store are refused with their positions, never dropped. The report checks text and numbers."))
    def txt_to_docx(input_path: Path_, output_path: OptPath = None, overwrite: bool = False,
                    encoding: Encoding = None, report_path: OptPath = None,
                    max_differences: MaxDiffs = 200, detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.txt_to_docx(input_path, output_path, overwrite, encoding, report_path, max_differences),
                       detail)

    @server.tool(name="document_compare", annotations=reads, description=_desc(
        "Compare two documents (any of PDF, DOCX, MD, TXT) through their canonical forms: missing, added and "
        "changed text, numbers and identifiers, structure and page-count differences, and what could not be "
        "compared. Read-only."))
    def document_compare(left_path: Path_, right_path: Path_, left_encoding: Encoding = None,
                         right_encoding: Encoding = None, max_differences: MaxDiffs = 200,
                         detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.document_compare(left_path, right_path, left_encoding, right_encoding, max_differences),
                       detail)

    @server.tool(name="validate_conversion", annotations=writes, description=_desc(
        "Given a source document and a converted one (made by anything), run every check that is possible "
        "for the format pair and return the validation report: text, numeric and identifier fidelity, "
        "structure, and every property NOT_CHECKED or UNSUPPORTED. Writes nothing unless report_path is set."))
    def validate_conversion(source_path: Path_, target_path: Path_, source_encoding: Encoding = None,
                            target_encoding: Encoding = None, report_path: OptPath = None,
                            overwrite: bool = False, max_differences: MaxDiffs = 200,
                            detail: Detail = "summary") -> T.ConversionResult:
        return _detail(T.validate_conversion(source_path, target_path, source_encoding, target_encoding, report_path,
                                             overwrite, max_differences), detail)

    _forbid_unknown_arguments(server)
    return server


def _forbid_unknown_arguments(server) -> None:
    manager = getattr(server, "_tool_manager", None)
    if manager is None:
        raise RuntimeError("docbridge: the MCP SDK's tool manager is not where docbridge expects it; "
                           "argument hardening cannot be applied, refusing to start")
    for tool in manager.list_tools():
        base = tool.fn_metadata.arg_model
        strict = type(base.__name__, (base,), {
            "model_config": ConfigDict(extra="forbid", strict=True, arbitrary_types_allowed=True),
            "__module__": base.__module__,
        })
        tool.fn_metadata.arg_model = strict
        tool.parameters = strict.model_json_schema(by_alias=True)
        if tool.parameters.get("additionalProperties") is not False:
            raise RuntimeError(f"docbridge: tool {tool.name} still accepts unknown arguments; refusing to start")


def main() -> None:  # pragma: no cover - exercised by the protocol tests as a subprocess
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(asctime)s docbridge %(levelname)s %(message)s")
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
