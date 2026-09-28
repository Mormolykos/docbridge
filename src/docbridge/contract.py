"""The vocabulary every docbridge report is written in.

DOCUMENT_MCP_CONTRACT.md is the prose form of this file. When the two disagree
this file wins and the document is corrected in place (METHODOLOGY R3).

Four statuses, never collapsed:

    PASS         the property was compared and holds
    FAIL         the property was compared and does not hold
    NOT_CHECKED  both formats can carry the property, but this run did not
                 establish it (no text layer, unreadable construct, heuristic
                 structure, no layout engine, ...). Never a pass.
    UNSUPPORTED  at least one of the two formats cannot carry the property, or
                 the input is outside v0.1's declared domain. There is nothing
                 to compare, and that is reported, not hidden.

A conversion that produced a file says nothing about fidelity. The operation
completing and the report passing are two different fields in every response.
"""
from __future__ import annotations

from enum import Enum

SCHEMA_VERSION = "0.2.4"

# The one rule the reading tools exist under (DOCUMENT_MCP_CONTRACT.md §0).
EVIDENCE_INVARIANT = ("docbridge may reduce the volume of its own responses, but it never alters, "
                      "summarizes, paraphrases, semantically filters, or silently truncates source evidence")


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_CHECKED = "NOT_CHECKED"
    UNSUPPORTED = "UNSUPPORTED"


class Reason(str, Enum):
    """Why an axis is NOT_CHECKED or UNSUPPORTED. A fixed vocabulary, so a
    caller can branch on it; free text goes in the axis `scope` and `details`."""

    # --- NOT_CHECKED: the property exists in both formats, this run could not decide it
    NO_TEXT_LAYER = "no_text_layer"            # PDF page with visible content and no extractable text (OCR required)
    UNRESOLVED_CONTENT = "unresolved_content"  # a construct docbridge cannot read sits where a difference is
    INFERRED_STRUCTURE = "inferred_structure"  # the target's structure was inferred; the source does not encode it (R7)
    NEEDS_LAYOUT_ENGINE = "needs_layout_engine"  # e.g. DOCX page count, typography
    NOT_IMPLEMENTED = "not_implemented"        # both formats carry it; v0.1 has no checker for it
    TOO_LARGE = "too_large"                    # alignment skipped above the size limit; counts still reported
    CHECK_FAILED = "check_failed"              # the checker itself raised; never read as a pass
    INCONCLUSIVE = "inconclusive"              # the measurement ran but cannot tell right from wrong here
    # --- UNSUPPORTED: nothing to compare
    ABSENT_IN_FORMAT = "absent_in_format"      # at least one format cannot represent the property
    NOT_CARRIED = "not_carried"                # present in the source, deliberately not carried by v0.1


NOT_CHECKED_REASONS = frozenset({
    Reason.NO_TEXT_LAYER, Reason.UNRESOLVED_CONTENT, Reason.INFERRED_STRUCTURE,
    Reason.NEEDS_LAYOUT_ENGINE, Reason.NOT_IMPLEMENTED, Reason.TOO_LARGE, Reason.CHECK_FAILED,
    Reason.INCONCLUSIVE,
})
UNSUPPORTED_REASONS = frozenset({Reason.ABSENT_IN_FORMAT, Reason.NOT_CARRIED})


class ErrorCode(str, Enum):
    """Semantic errors, returned as values (shale ADR 007, convention 3). A
    schema error never reaches a tool: the transport rejects it first."""

    INVALID_ARGUMENT = "invalid_argument"
    PATH_NOT_ABSOLUTE = "path_not_absolute"
    PATH_TRAVERSAL = "path_traversal"
    PATH_FORBIDDEN = "path_forbidden"          # UNC share, device path, alternate data stream, reserved name
    PATH_OUTSIDE_ROOTS = "path_outside_roots"
    INPUT_NOT_FOUND = "input_not_found"
    INPUT_TOO_LARGE = "input_too_large"
    OUTPUT_EXISTS = "output_exists"
    WOULD_OVERWRITE_SOURCE = "would_overwrite_source"
    OUTPUT_EXTENSION_MISMATCH = "output_extension_mismatch"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FORMAT_MISMATCH = "format_mismatch"        # the extension says one format, the bytes say another
    MALFORMED_INPUT = "malformed_input"
    ENCRYPTED_INPUT = "encrypted_input"
    UNDECODABLE_TEXT = "undecodable_text"
    UNSUPPORTED_IMAGE = "unsupported_image"
    UNSUPPORTED_CONTENT = "unsupported_content"  # e.g. characters DOCX cannot store
    DEPENDENCY_MISSING = "dependency_missing"
    CONVERSION_FAILED = "conversion_failed"
    STALE_REFERENCE = "stale_reference"        # a cursor or match_id issued for a different version of the file
    REPORT_NOT_FOUND = "report_not_found"      # a report_id this server session does not hold
    SEARCH_TIMEOUT = "search_timeout"          # a search exceeded its time bound and was stopped; no result
    INTERNAL_ERROR = "internal_error"


# Axes every validation report carries, in the order they are reported.
AXES = ("text", "numeric", "identifier", "structure", "layout", "image")

# What a global PASS covers, per operation. A global PASS requires every axis
# listed here to be PASS and no axis anywhere to be FAIL. Anything else that is
# NOT_CHECKED or UNSUPPORTED is listed in the report, never folded in.
REQUIRED_AXES: dict[str, tuple[str, ...]] = {
    "pdf_merge": ("structure", "layout"),
    "pdf_split": ("structure", "layout"),
    "images_to_pdf": ("structure", "image"),
}
DEFAULT_REQUIRED_AXES: tuple[str, ...] = ("text", "numeric", "identifier")


def required_axes(operation: str) -> tuple[str, ...]:
    return REQUIRED_AXES.get(operation, DEFAULT_REQUIRED_AXES)


FORMATS = ("pdf", "docx", "md", "txt")

# Which properties each format can carry, as docbridge v0.1 reads it.
#   yes         represented and extracted
#   no          the format has no such property
#   text_layer  PDF: what the text layer says, not what the page shows
#   markers     Markdown: only via docbridge's own page-marker comments
#   layout      DOCX: pages exist only after a layout engine runs
CAPABILITIES: dict[str, dict[str, str]] = {
    "txt": {"headings": "no", "lists": "no", "tables": "no", "links": "no",
            "emphasis": "no", "pages": "no", "image_alt": "no", "notes": "no"},
    "md": {"headings": "yes", "lists": "yes", "tables": "yes", "links": "yes",
           "emphasis": "yes", "pages": "markers", "image_alt": "yes", "notes": "no"},
    "docx": {"headings": "yes", "lists": "yes", "tables": "yes", "links": "yes",
             "emphasis": "yes", "pages": "layout", "image_alt": "yes", "notes": "yes"},
    "pdf": {"headings": "no", "lists": "no", "tables": "no", "links": "yes",
            "emphasis": "no", "pages": "yes", "image_alt": "no", "notes": "no"},
}

DISCLAIMER = (
    "docbridge never alters, summarizes or silently truncates source evidence. "
    "docbridge reports what it compared. PASS on an axis covers only that axis's "
    "stated scope; NOT_CHECKED and UNSUPPORTED are never passes. PDF text is the "
    "text layer as PyMuPDF decodes it, not the rendered glyphs. Nothing here "
    "interprets meaning: numbers are compared as characters, not as values."
)
