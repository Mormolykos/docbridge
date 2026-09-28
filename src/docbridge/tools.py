"""The tools as plain functions, testable without a transport.

Every function returns a typed result and never raises: a DocbridgeError
becomes `error` with a code from the declared vocabulary and a hint; anything
else becomes `internal_error` (the traceback goes to stderr, never to the
caller). `operation_completed` says the operation ran and wrote its outputs -
it says nothing about fidelity. Fidelity is the report's status, always.

Response volume (not evidence) is reduced in one place only: a report is kept
in full in this session's report store and summarised in `report_summary`; the
MCP layer sends the full report only with detail="full" or via get_report.
"""
from __future__ import annotations

import contextlib
import logging
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from . import normalize as N
from . import reading as RD
from .canonical import Canonical, Origin
from .compare import Side
from .contract import (AXES, CAPABILITIES, DISCLAIMER, EVIDENCE_INVARIANT, ErrorCode, Reason, REQUIRED_AXES,
                       DEFAULT_REQUIRED_AXES, SCHEMA_VERSION)
from .errors import DocbridgeError
from .extract import detect_format, extract, sha256_of
from .paths import (allowed_roots, atomic_target, default_output, max_input_bytes, resolve_input,
                    resolve_output, resolve_output_dir)
from .report import AxisResult, Difference, StatusT, ValidationReport, WarningItem
from .validate import build_report, concat_pdf, subset_pages

log = logging.getLogger("docbridge")

SUMMARY_DIFFERENCES = 10
REPORT_STORE_SIZE = 200
_REPORTS: OrderedDict[str, ValidationReport] = OrderedDict()


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolError(_Model):
    code: str = Field(description="A code from docbridge_contract().error_codes.")
    message: str
    hint: str | None = None


class ReportSummary(_Model):
    """The report, short. Never a different verdict: status, basis and every
    axis status are the full report's own; only lists are shortened, and each
    shortened list says how long it really is."""
    status: StatusT
    status_basis: str
    required_axes: list[str]
    axes: dict[str, str] = Field(description="axis -> status, with the reason in brackets when not PASS/FAIL")
    differences: list[Difference] = Field(description="The first differences, most important axes first.")
    differences_total: int
    differences_shown: int
    warnings: list[str] = Field(description="side:code of every warning; messages are in the full report")
    unsupported_checks: int = Field(description="How many checks were NOT_CHECKED or UNSUPPORTED (full list in the report)")
    summary: str
    full_report: str


class ConversionResult(_Model):
    schema_version: str
    tool: str
    operation_completed: bool = Field(
        description="The operation ran and wrote its outputs. Says NOTHING about fidelity: read the report status.")
    error: ToolError | None = None
    outputs: list[str] = Field(default_factory=list)
    report_id: str | None = Field(None, description="Pass to get_report for the full report (held for this server session).")
    report_summary: ReportSummary | None = None
    report: ValidationReport | None = Field(None, description="The full report. Over MCP only with detail='full'.")
    report_path: str | None = None
    conversion_notes: list[str] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


class ReportResult(_Model):
    schema_version: str
    tool: str
    operation_completed: bool
    error: ToolError | None = None
    report_id: str | None = None
    report: ValidationReport | None = None
    section: dict[str, Any] | None = None


class UnitOffset(_Model):
    unit: int
    label: str
    char_start: int


class Excerpt(_Model):
    text: str = Field(description="Verbatim: reading_text[char_start:char_end], nothing added or removed.")
    char_start: int
    char_end: int
    text_sha256: str
    first_unit: int
    last_unit: int
    units: list[UnitOffset]


class ReadResult(_Model):
    schema_version: str
    tool: str
    operation_completed: bool
    error: ToolError | None = None
    source: dict[str, Any] | None = None
    excerpt: Excerpt | None = None
    truncated: bool = Field(False, description="True if anything in the requested range was not returned.")
    remaining_chars: int = Field(0, description="Characters of the requested range after the excerpt.")
    next_cursor: str | None = Field(None, description="Pass as `cursor` to document_read to continue exactly.")
    omitted_before_chars: int = 0
    before_cursor: str | None = None
    unreadable_units: list[int] = Field(default_factory=list, description="Unreadable units inside this excerpt.")
    range_unreadable_units: list[int] = Field(
        default_factory=list, description="Every unreadable unit of the whole requested range, returned or not.")
    match: dict[str, Any] | None = None
    requested_units: list[int] | None = None
    invariant: str = EVIDENCE_INVARIANT


class SearchMatch(_Model):
    match_id: str
    char_start: int
    char_end: int
    unit: int
    end_unit: int
    label: str
    text: str
    context_before: str
    context_after: str


class SearchResult(_Model):
    schema_version: str
    tool: str
    operation_completed: bool
    error: ToolError | None = None
    source: dict[str, Any] | None = None
    pattern: str | None = None
    case_sensitive: bool | None = None
    matches: list[SearchMatch] = Field(default_factory=list)
    matches_total: int = 0
    matches_returned: int = 0
    next_start_match: int | None = None
    unsearchable_units: list[int] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    invariant: str = EVIDENCE_INVARIANT


class OutlineResult(_Model):
    schema_version: str
    tool: str
    operation_completed: bool
    error: ToolError | None = None
    source: dict[str, Any] | None = None
    outline: dict[str, Any] | None = None
    invariant: str = EVIDENCE_INVARIANT


class ExtractResult(_Model):
    schema_version: str
    tool: str
    operation_completed: bool
    error: ToolError | None = None
    document: dict[str, Any] | None = None
    written_to: str | None = None
    disclaimer: str = DISCLAIMER


class ContractResult(_Model):
    schema_version: str
    tool: str
    operation_completed: bool = True
    error: ToolError | None = None
    contract: dict[str, Any]
    disclaimer: str = DISCLAIMER


@contextlib.contextmanager
def _boundary(result: BaseModel) -> Iterator[BaseModel]:
    try:
        yield result
    except DocbridgeError as e:
        result.error = ToolError(code=e.code.value, message=e.message, hint=e.hint)
    except Exception as e:  # the server must survive anything a document does to a library
        log.error("docbridge internal error in %s:\n%s", getattr(result, "tool", "?"), traceback.format_exc())
        result.error = ToolError(code=ErrorCode.INTERNAL_ERROR.value, message=f"{type(e).__name__}: {e}",
                                 hint="This is a docbridge defect or a document no library could read; "
                                      "the traceback is in the server's stderr log.")
    finally:
        if isinstance(result, ConversionResult) and result.report is not None and result.report_id is None:
            _store(result)


def _store(result: ConversionResult) -> None:
    rep = result.report
    rid = uuid.uuid4().hex[:16]
    _REPORTS[rid] = rep
    while len(_REPORTS) > REPORT_STORE_SIZE:
        _REPORTS.popitem(last=False)
    shown = rep.differences[:SUMMARY_DIFFERENCES]
    result.report_id = rid
    result.report_summary = ReportSummary(
        status=rep.status, status_basis=rep.status_basis, required_axes=rep.required_axes,
        axes={k: v.status + (f" ({v.reason})" if v.reason else "") for k, v in rep.axes.items()},
        differences=shown, differences_total=rep.differences_total, differences_shown=len(shown),
        warnings=sorted({f"{w.side}:{w.code}" for w in rep.warnings}),
        unsupported_checks=len(rep.unsupported_checks), summary=rep.summary,
        full_report=f"get_report(report_id='{rid}') returns the full report; or call again with detail='full'")


def _require_format(path: Path, expected: tuple[str, ...], what: str) -> str:
    fmt = detect_format(path)
    if fmt not in expected:
        raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, f"{what} must be {' or '.join(expected)}, got {fmt}",
                             f"Pass a {' or '.join('.' + e for e in expected)} file.")
    return fmt


def _write_report(result: ConversionResult, report_path: str | None, sources: list[Path],
                  outputs: list[Path], overwrite: bool) -> None:
    if not report_path or result.report is None:
        return
    rp = resolve_output(report_path, extensions=(".json",), overwrite=overwrite,
                        sources=sources + outputs, what="report_path")
    with atomic_target(rp) as tmp:
        tmp.write_text(result.report.model_dump_json(indent=2), encoding="utf-8")
    result.report_path = str(rp)


def _plan_report_path(report_path: str | None, sources: list[Path], overwrite: bool) -> None:
    """Refuse a bad report path BEFORE converting, so a refusal never leaves an
    output behind without its report."""
    if report_path:
        resolve_output(report_path, extensions=(".json",), overwrite=overwrite, sources=sources,
                       what="report_path")


def _extract_target(path: Path, operation: str, source: Canonical, max_differences: int,
                    result: ConversionResult) -> None:
    try:
        target = extract(path)
    except DocbridgeError as e:
        result.conversion_notes.append(f"the output was written but docbridge could not read it back: "
                                       f"{e.code.value}: {e.message}")
        result.report = _unreadable_target_report(operation, source, path, f"{e.code.value}: {e.message}")
        return
    result.report = build_report(operation, source, target, max_differences=max_differences)


def _unreadable_target_report(operation: str, source: Canonical, path: Path, why: str) -> ValidationReport:
    empty = Canonical(format=path.suffix.lower().lstrip(".") or "unknown", path=str(path),
                      sha256=sha256_of(path), size_bytes=path.stat().st_size, extractor=f"unreadable: {why}")
    failed = AxisResult(status="NOT_CHECKED", reason="check_failed",
                        scope=f"the output could not be read back ({why})")
    return build_report(operation, source, empty, text_axes_override={
        "text": failed, "numeric": failed, "identifier": failed})


# ------------------------------------------------------------ conversions ---

def markdown_to_docx(input_path: str, output_path: str | None = None, overwrite: bool = False,
                     encoding: str | None = None, report_path: str | None = None,
                     max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="markdown_to_docx", operation_completed=False)
    with _boundary(r):
        from .convert import pandoc
        from .extract.text import decode_text
        src = resolve_input(input_path)
        _require_format(src, ("md",), "input_path")
        out = resolve_output(output_path or str(default_output(src, ".docx")), extensions=(".docx",),
                             overwrite=overwrite, sources=[src])
        _plan_report_path(report_path, [src, out], overwrite)
        source = extract(src, encoding)
        text = decode_text(src.read_bytes(), encoding)
        with atomic_target(out) as tmp:
            notes = pandoc.markdown_to_docx(text, tmp)
        r.operation_completed = True
        r.outputs = [str(out)]
        r.conversion_notes = [f"pandoc: {n}" for n in notes]
        if source.images:
            r.conversion_notes.append(f"{len(source.images)} images were not embedded (pandoc runs sandboxed "
                                      "and fetches nothing); each was replaced by its alt text")
        if any(w.code == "md_raw_html" for w in source.warnings):
            r.conversion_notes.append("the source contains raw HTML blocks; pandoc's DOCX writer drops raw "
                                      "HTML, so their text is expected to be reported missing")
        _extract_target(out, "markdown_to_docx", source, max_differences, r)
        _write_report(r, report_path, [src], [out], overwrite)
    return r


def docx_to_markdown(input_path: str, output_path: str | None = None, overwrite: bool = False,
                     report_path: str | None = None, max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="docx_to_markdown", operation_completed=False)
    with _boundary(r):
        from .convert import pandoc
        src = resolve_input(input_path)
        _require_format(src, ("docx",), "input_path")
        out = resolve_output(output_path or str(default_output(src, ".md")), extensions=(".md", ".markdown"),
                             overwrite=overwrite, sources=[src])
        _plan_report_path(report_path, [src, out], overwrite)
        source = extract(src)
        with atomic_target(out) as tmp:
            notes = pandoc.docx_to_markdown(src, tmp)
        r.operation_completed = True
        r.outputs = [str(out)]
        r.conversion_notes = [f"pandoc: {n}" for n in notes]
        if source.images:
            r.conversion_notes.append(f"{len(source.images)} images: their alt text is kept, the image files "
                                      "are not extracted (Markdown image links point at media/ paths that "
                                      "do not exist)")
        for con in source.unsupported:
            if con.kind in ("header_footer", "comment", "equation", "ole_object", "alt_chunk"):
                r.conversion_notes.append(f"{con.count} x {con.kind}: {con.detail}")
        _extract_target(out, "docx_to_markdown", source, max_differences, r)
        _write_report(r, report_path, [src], [out], overwrite)
    return r


def txt_to_docx(input_path: str, output_path: str | None = None, overwrite: bool = False,
                encoding: str | None = None, report_path: str | None = None,
                max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="txt_to_docx", operation_completed=False)
    with _boundary(r):
        from .convert.txt_docx import txt_to_docx as convert
        src = resolve_input(input_path)
        _require_format(src, ("txt",), "input_path")
        out = resolve_output(output_path or str(default_output(src, ".docx")), extensions=(".docx",),
                             overwrite=overwrite, sources=[src])
        _plan_report_path(report_path, [src, out], overwrite)
        source = extract(src, encoding)
        with atomic_target(out) as tmp:
            stats = convert(src, tmp, encoding)
        r.operation_completed = True
        r.outputs = [str(out)]
        r.conversion_notes = [f"{stats['paragraphs']} lines written as paragraphs, "
                              f"{stats['page_breaks']} form feeds as page breaks"]
        _extract_target(out, "txt_to_docx", source, max_differences, r)
        _write_report(r, report_path, [src], [out], overwrite)
    return r


def pdf_to_markdown(input_path: str, output_path: str | None = None, overwrite: bool = False,
                    detect_tables: bool = True, page_markers: bool = True,
                    report_path: str | None = None, max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="pdf_to_markdown", operation_completed=False)
    with _boundary(r):
        from .convert.pdf_md import pdf_to_markdown as convert
        src = resolve_input(input_path)
        _require_format(src, ("pdf",), "input_path")
        out = resolve_output(output_path or str(default_output(src, ".md")), extensions=(".md", ".markdown"),
                             overwrite=overwrite, sources=[src])
        _plan_report_path(report_path, [src, out], overwrite)
        source = extract(src)
        with atomic_target(out) as tmp:
            stats = convert(src, tmp, detect_tables=detect_tables, page_markers=page_markers)
        r.operation_completed = True
        r.outputs = [str(out)]
        r.conversion_notes = [
            f"{stats['pages']} pages; inferred {stats['headings']} headings (font size >= "
            f"1.2x the body size {stats['body_font_size']}), {stats['bullet_items']} bullet items, "
            f"{stats['ordered_items']} numbered items, {stats['tables']} tables - all inferred, none "
            "validated against the PDF, which does not encode them"]
        if stats["unreadable_pages"]:
            r.conversion_notes.append(f"pages {stats['unreadable_pages']} have no text layer: nothing was "
                                      "extracted from them (OCR required); they are marked in the Markdown")
        _extract_target(out, "pdf_to_markdown", source, max_differences, r)
        _write_report(r, report_path, [src], [out], overwrite)
    return r


# ---------------------------------------------------------- page operations ---

def pdf_merge(inputs: list[str], output_path: str, overwrite: bool = False, raster_dpi: int = 72,
              report_path: str | None = None, max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="pdf_merge", operation_completed=False)
    with _boundary(r):
        import pymupdf

        from .convert import pdf_ops
        from .extract.pdf import open_pdf
        srcs = [resolve_input(p, f"inputs[{i}]") for i, p in enumerate(inputs)]
        for i, s in enumerate(srcs):
            _require_format(s, ("pdf",), f"inputs[{i}]")
        out = resolve_output(output_path, extensions=(".pdf",), overwrite=overwrite, sources=srcs)
        _plan_report_path(report_path, srcs + [out], overwrite)
        src_canons = [extract(s) for s in srcs]
        with atomic_target(out) as tmp:
            info = pdf_ops.merge(srcs, tmp)
        r.operation_completed = True
        r.outputs = [str(out)]
        r.conversion_notes = list(info["warnings"])
        target = extract(out)
        docs = [open_pdf(s) for s in srcs]
        odoc = pymupdf.open(str(out))
        try:
            pairs, k = [], 0
            for s, d in zip(srcs, docs, strict=True):
                for j in range(d.page_count):
                    if k < odoc.page_count:
                        pairs.append((s.name, j + 1, d[j], k + 1, odoc[k]))
                    k += 1
            checks, layout, pdiffs, rows = pdf_ops.compare_pages(pairs, raster_dpi)
            outline, odiffs = pdf_ops.bookmark_check(pdf_ops.expected_merge_bookmarks(srcs),
                                                     pdf_ops.bookmark_sigs(odoc))
        finally:
            odoc.close()
            for d in docs:
                d.close()
        checks["bookmarks"] = outline
        layout.details["pages"] = rows[:500]
        layout.details["pages_truncated"] = max(0, len(rows) - 500)
        r.report = build_report("pdf_merge", concat_pdf(src_canons, "sources, in order"), target,
                                sources=src_canons, targets=[target], max_differences=max_differences,
                                axis_overrides={"layout": layout}, structure_overrides=checks,
                                extra_diffs=pdiffs + odiffs)
        _write_report(r, report_path, srcs, [out], overwrite)
    return r


def pdf_split(input_path: str, ranges: list[str] | None = None, each_page: bool = False,
              output_dir: str | None = None, overwrite: bool = False, raster_dpi: int = 72,
              report_path: str | None = None, max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="pdf_split", operation_completed=False)
    with _boundary(r):
        import pymupdf

        from .convert import pdf_ops
        from .extract.pdf import open_pdf
        if bool(ranges) == bool(each_page):
            raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, "pass exactly one of `ranges` or `each_page`",
                                 "ranges=['1-3','5'] makes two files; each_page=true makes one per page.")
        src = resolve_input(input_path)
        _require_format(src, ("pdf",), "input_path")
        sdoc = open_pdf(src)
        n = sdoc.page_count
        sdoc.close()
        spans = pdf_ops.parse_ranges(ranges, n) if ranges else [(i, i) for i in range(1, n + 1)]
        names = pdf_ops.split_names(src.stem, spans, n)
        if len(set(names)) != len(names):
            raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, "the same range is listed twice",
                                 "Each range becomes one file; list each once.")
        odir = resolve_output_dir(output_dir or str(src.parent / f"{src.stem}_split"))
        outs = [resolve_output(str(odir / name), extensions=(".pdf",), overwrite=overwrite, sources=[src])
                for name in names]
        _plan_report_path(report_path, [src] + outs, overwrite)
        source = extract(src)
        infos = []
        for (a, b), out in zip(spans, outs, strict=True):
            with atomic_target(out) as tmp:
                infos.append(pdf_ops.split_one(src, a, b, tmp))
        r.operation_completed = True
        r.outputs = [str(o) for o in outs]
        targets = [extract(o) for o in outs]
        sdoc = open_pdf(src)
        odocs = [pymupdf.open(str(o)) for o in outs]
        try:
            pairs, g = [], 0
            outline_results, odiffs = [], []
            carried: set[int] = set()
            for (a, b), od, info, out in zip(spans, odocs, infos, outs, strict=True):
                for k in range(b - a + 1):
                    g += 1
                    if k < od.page_count:
                        pairs.append((src.name, a + k, sdoc[a + k - 1], g, od[k]))
                expected_bm, indices = pdf_ops.expected_split_bookmarks(src, a, b)
                res, d = pdf_ops.bookmark_check(expected_bm, pdf_ops.bookmark_sigs(od))
                for x in d:
                    x.target_location = out.name
                outline_results.append(res)
                odiffs += d
                carried |= indices
                if info["toc_adjusted"]:
                    r.conversion_notes.append(f"{out.name}: bookmark hierarchy adjusted to start at level 1")
            # a bookmark outside every requested range (or with no page at all,
            # e.g. a URL) is in no output: said, never dropped silently. Identity
            # is the source index, so a bookmark carried under shifted page
            # numbers is never mistaken for a lost one.
            source_bm = pdf_ops.source_bookmarks(src)
            dropped = [s for i, s in enumerate(source_bm) if i not in carried]
            if dropped:
                r.conversion_notes.append(
                    f"{len(dropped)} bookmarks were not carried into any output (their page is outside every "
                    "requested range, or they have no page, e.g. a URL): "
                    + "; ".join(f"{s[1]!r} -> {s[3]}" for s in dropped[:10]) + (" ..." if len(dropped) > 10 else ""))
            checks, layout, pdiffs, rows = pdf_ops.compare_pages(pairs, raster_dpi)
        finally:
            sdoc.close()
            for od in odocs:
                od.close()
        bad = sum(1 for x in outline_results if x.status == "FAIL")
        checks["bookmarks"] = AxisResult(status="FAIL" if bad else "PASS",
                                         scope="each output's bookmarks equal the source's bookmarks inside its "
                                               "range - level, title, page and destination - page numbers shifted",
                                         compared=len(outline_results), differences=bad,
                                         details={"not_carried": len(dropped)})
        layout.details["pages"] = rows[:500]
        layout.details["pages_truncated"] = max(0, len(rows) - 500)
        expected = concat_pdf([subset_pages(source, list(range(a, b + 1)), f"{src.name} pages {a}-{b}")
                               for a, b in spans], f"{src.name}, requested ranges in order")
        extra_warnings = [WarningItem(side="operation", code="bookmarks_not_carried",
                                      message=f"{len(dropped)} bookmarks point outside every requested range or "
                                              "have no page (e.g. a URL) and are in no output")] if dropped else []
        r.report = build_report("pdf_split", expected, concat_pdf(targets, "outputs, in order"),
                                sources=[source], targets=targets, max_differences=max_differences,
                                axis_overrides={"layout": layout}, structure_overrides=checks,
                                extra_diffs=pdiffs + odiffs, extra_warnings=extra_warnings)
        _write_report(r, report_path, [src], outs, overwrite)
    return r


def images_to_pdf(inputs: list[str], output_path: str, page_size: str = "image",
                  rotations: list[int] | None = None, apply_exif_orientation: bool = True,
                  overwrite: bool = False, report_path: str | None = None,
                  max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="images_to_pdf", operation_completed=False)
    with _boundary(r):
        from .convert import images
        if rotations is not None and len(rotations) != len(inputs):
            raise DocbridgeError(ErrorCode.INVALID_ARGUMENT,
                                 f"{len(rotations)} rotations for {len(inputs)} images",
                                 "Pass one rotation per image, or none.")
        if page_size not in ("image", "A4", "Letter"):
            raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, f"page_size {page_size!r}", "Use image, A4 or Letter.")
        srcs = [resolve_input(p, f"inputs[{i}]") for i, p in enumerate(inputs)]
        out = resolve_output(output_path, extensions=(".pdf",), overwrite=overwrite, sources=srcs)
        _plan_report_path(report_path, srcs + [out], overwrite)
        plans = [images.plan_image(s, (rotations or [0] * len(srcs))[i] % 360, apply_exif_orientation, page_size)
                 for i, s in enumerate(srcs)]
        with atomic_target(out) as tmp:
            images.build(plans, tmp)
        r.operation_completed = True
        r.outputs = [str(out)]
        for p in plans:
            for t in p.transformations:
                r.conversion_notes.append(f"{p.path.name}: {t}")
        checks, image_axis, diffs, rows = images.verify(out, plans)
        image_axis.details["pages"] = rows[:500]
        target = extract(out)
        sources = [Canonical(format="image", path=str(s), sha256=sha256_of(s), size_bytes=s.stat().st_size,
                             extractor="Pillow (source pixels)") for s in srcs]
        combined = Canonical(format="image", path="images, in order", sha256="",
                             size_bytes=sum(c.size_bytes for c in sources), extractor="Pillow (source pixels)")
        no_text = AxisResult(status="UNSUPPORTED", reason="absent_in_format",
                             scope="images carry no text layer and v0.1 has no OCR; text inside images is never read")
        r.report = build_report(
            "images_to_pdf", combined, target, sources=sources, targets=[target],
            max_differences=max_differences,
            text_axes_override={"text": no_text, "numeric": no_text, "identifier": no_text},
            structure_overrides=checks,
            axis_overrides={"image": image_axis,
                            "layout": AxisResult(status="NOT_CHECKED", reason="not_implemented",
                                                 scope="nothing about placement is claimed beyond "
                                                       "structure.page_size and the image axis's orientation check")},
            extra_diffs=diffs)
        _write_report(r, report_path, srcs, [out], overwrite)
    return r


# ------------------------------------------------------------- read-only ---

def validate_conversion(source_path: str, target_path: str, source_encoding: str | None = None,
                        target_encoding: str | None = None, report_path: str | None = None,
                        overwrite: bool = False, max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="validate_conversion", operation_completed=False)
    with _boundary(r):
        src = resolve_input(source_path, "source_path")
        tgt = resolve_input(target_path, "target_path")
        _plan_report_path(report_path, [src, tgt], overwrite)
        a, b = extract(src, source_encoding), extract(tgt, target_encoding)
        r.report = build_report("validate_conversion", a, b, max_differences=max_differences)
        r.operation_completed = True
        _write_report(r, report_path, [src, tgt], [], overwrite)
    return r


def document_compare(left_path: str, right_path: str, left_encoding: str | None = None,
                     right_encoding: str | None = None, max_differences: int = 200) -> ConversionResult:
    r = ConversionResult(schema_version=SCHEMA_VERSION, tool="document_compare", operation_completed=False)
    with _boundary(r):
        left = resolve_input(left_path, "left_path")
        right = resolve_input(right_path, "right_path")
        a, b = extract(left, left_encoding), extract(right, right_encoding)
        r.report = build_report("document_compare", a, b, max_differences=max_differences)
        r.conversion_notes = ["document_compare: 'source' is the left document and 'target' the right; "
                              "'missing' means present on the left only, 'added' on the right only"]
        r.operation_completed = True
    return r


EXTRACT_KINDS = ("numbers", "identifiers", "dates", "urls", "emails", "headings", "tables", "links",
                 "emphasis", "images", "pages", "blocks", "warnings", "unsupported", "normalized_text")
TEXT_PAGE_CHARS = 20000


def document_extract(input_path: str, kinds: list[str] | None = None, encoding: str | None = None,
                     max_items: int = 200, offset: int = 0, text_offset: int = 0,
                     output_path: str | None = None, overwrite: bool = False) -> ExtractResult:
    """Counts always; each list only when asked for in `kinds`, one page of it
    at a time (`offset`, `max_items`), with the rest stated and continuable.
    `output_path` writes everything, uncapped, to a JSON file."""
    r = ExtractResult(schema_version=SCHEMA_VERSION, tool="document_extract", operation_completed=False)
    with _boundary(r):
        import json
        unknown = sorted(set(kinds or []) - set(EXTRACT_KINDS))
        if unknown:
            raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, f"unknown kinds {unknown}",
                                 f"Choose from {list(EXTRACT_KINDS)}.")
        src = resolve_input(input_path)
        out = None
        if output_path:
            out = resolve_output(output_path, extensions=(".json",), overwrite=overwrite, sources=[src])
        canon = extract(src, encoding)
        if out is not None:
            full = describe(canon, EXTRACT_KINDS, max_items=None, offset=0, text_offset=0)
            with atomic_target(out) as tmp:
                tmp.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
            r.written_to = str(out)
        r.document = describe(canon, tuple(kinds or ()), max_items=max_items, offset=offset,
                              text_offset=text_offset)
        r.operation_completed = True
    return r


def describe(c: Canonical, kinds: tuple[str, ...], max_items: int | None, offset: int,
             text_offset: int) -> dict[str, Any]:
    """The canonical form as JSON. Lists are paged at `max_items` from `offset`;
    for each, `truncated` states how many items follow and `next_offset` where
    to continue (METHODOLOGY R18: what was not returned is counted)."""
    origins = frozenset(Origin) - {Origin.UNKNOWN, Origin.FURNITURE}
    side = Side(c, origins)
    truncated: dict[str, int] = {}
    next_offset: dict[str, int] = {}

    def page(name: str, items: list) -> list:
        if max_items is None:
            return items
        chunk = items[offset:offset + max_items]
        rest = len(items) - offset - len(chunk)
        if rest > 0:
            truncated[name] = rest
            next_offset[name] = offset + len(chunk)
        return chunk

    def loc(t) -> str:
        return side.locate(t)

    numbers = [{"value": t.core, "token": t.value, "classes": [x for x in t.classes if x != "number_bearing"],
                "location": loc(t), "origin": t.origin.value} for t in side.numeric if t.known]
    idents = [{"value": t.core, "classes": list(t.classes), "location": loc(t)} for t in side.identifier if t.known]

    def by(cls: str) -> list:
        return [{"value": t.core, "location": loc(t)} for t in side.text if t.known and cls in t.classes]

    builders = {
        "numbers": lambda: numbers,
        "identifiers": lambda: idents,
        "dates": lambda: by("date"),
        "urls": lambda: by("url"),
        "emails": lambda: by("email"),
        "headings": lambda: [{"level": b.level, "text": b.text(origins), "location": b.locator}
                             for b in c.headings()],
        "tables": lambda: [{"index": t.index, "rows": t.rows, "cols": t.cols, "source": t.source,
                            "cells": [c_.__dict__ for c_ in t.cells]} for t in c.tables],
        "links": lambda: [{"target": ln.target, "text": ln.text, "page": ln.page} for ln in c.links],
        "emphasis": lambda: [{"kind": e.kind, "text": e.text} for e in c.emphasis],
        "images": lambda: list(c.images),
        "pages": lambda: [p.__dict__ for p in (c.pages or [])],
        "blocks": lambda: [{"kind": b.kind.value, "text": b.text(origins), "page": b.page, "level": b.level,
                            "list_item": b.kind.value == "list_item" or b.list_item,
                            "list_depth": b.list_depth, "ordered": b.ordered, "table": b.table, "row": b.row,
                            "col": b.col, "locator": b.locator, "unknown": b.has_unknown(),
                            "origins": sorted({s.origin.value for s in b.segments})} for b in c.blocks],
        "warnings": lambda: [w.__dict__ for w in c.warnings],
        "unsupported": lambda: [u.__dict__ for u in c.unsupported],
    }
    doc: dict[str, Any] = {
        "format": c.format, "path": c.path, "sha256": c.sha256, "size_bytes": c.size_bytes,
        "extractor": c.extractor, "page_count": c.page_count,
        "counts": {"blocks": len(c.blocks), "tokens": len(side.text), "numbers": len(numbers),
                   "identifiers": len(idents), "headings": len(c.headings()), "tables": len(c.tables),
                   "list_items": len(c.list_items()), "links": len(c.links), "images": len(c.images),
                   "warnings": len(c.warnings), "unsupported": len(c.unsupported),
                   "unknown_tokens": sum(1 for t in side.text if not t.known)},
        "warning_codes": sorted({w.code for w in c.warnings}),
        "semantic_interpretation": "none: classes describe the shape of a token, never its meaning",
        "available_kinds": list(EXTRACT_KINDS),
    }
    for kind in kinds:
        if kind == "normalized_text":
            text = " ".join(t.display() for t in side.text)
            if max_items is None:
                doc["normalized_text"] = text
            else:
                chunk = text[text_offset:text_offset + TEXT_PAGE_CHARS]
                doc["normalized_text"] = chunk
                doc["normalized_text_total_chars"] = len(text)
                if text_offset + len(chunk) < len(text):
                    truncated["normalized_text"] = len(text) - text_offset - len(chunk)
                    next_offset["normalized_text"] = text_offset + len(chunk)
            continue
        doc[kind] = page(kind, builders[kind]())
    if c.outline is not None and "headings" in kinds:
        doc["outline"] = c.outline
    doc["truncated"] = truncated
    doc["next_offset"] = next_offset
    return doc


# ------------------------------------------------------------ reading ---
# Exact retrieval. Nothing below chooses what is relevant: it returns what was
# asked for, character for character, and states anything it did not return.

def _reading(input_path: str, encoding: str | None) -> RD.ReadingText:
    return RD.load(resolve_input(input_path), encoding)


def document_outline(input_path: str, encoding: str | None = None, max_items: int = 200) -> OutlineResult:
    r = OutlineResult(schema_version=SCHEMA_VERSION, tool="document_outline", operation_completed=False)
    with _boundary(r):
        rt = _reading(input_path, encoding)
        r.source = rt.source()
        r.outline = RD.outline(rt, max_items)
        r.operation_completed = True
    return r


def document_search(input_path: str, query: str, regex: bool = False, case_sensitive: bool = False,
                    max_matches: int = 20, start_match: int = 0, context_chars: int = 120,
                    encoding: str | None = None) -> SearchResult:
    r = SearchResult(schema_version=SCHEMA_VERSION, tool="document_search", operation_completed=False)
    with _boundary(r):
        rt = _reading(input_path, encoding)
        r.source = rt.source()
        found = RD.search(rt, query, regex, case_sensitive, max_matches, start_match, context_chars)
        r.pattern = found["pattern"]
        r.case_sensitive = found["case_sensitive"]
        r.matches = [SearchMatch(**m) for m in found["matches"]]
        r.matches_total = found["matches_total"]
        r.matches_returned = found["matches_returned"]
        r.next_start_match = found["next_start_match"]
        r.unsearchable_units = found["unsearchable_units"]
        r.notes = found["notes"]
        r.operation_completed = True
    return r


def _fill_read(r: ReadResult, rt: RD.ReadingText, out: dict) -> None:
    r.source = rt.source()
    ex = out["excerpt"]
    r.excerpt = Excerpt(**{**ex, "units": [UnitOffset(**u) for u in ex["units"]]})
    for key in ("truncated", "remaining_chars", "next_cursor", "omitted_before_chars", "before_cursor",
                "unreadable_units", "range_unreadable_units"):
        setattr(r, key, out[key])
    r.match = out.get("match")
    r.requested_units = out.get("requested_units")
    r.operation_completed = True


def document_read(input_path: str, start: int = 1, end: int | None = None, max_chars: int = 12000,
                  cursor: str | None = None, encoding: str | None = None) -> ReadResult:
    r = ReadResult(schema_version=SCHEMA_VERSION, tool="document_read", operation_completed=False)
    with _boundary(r):
        rt = _reading(input_path, encoding)
        _fill_read(r, rt, RD.read(rt, start, end, max_chars, cursor))
    return r


def document_read_around(input_path: str, match_id: str, before: int | None = None, after: int | None = None,
                         max_chars: int = 12000, encoding: str | None = None) -> ReadResult:
    r = ReadResult(schema_version=SCHEMA_VERSION, tool="document_read_around", operation_completed=False)
    with _boundary(r):
        rt = _reading(input_path, encoding)
        _fill_read(r, rt, RD.around(rt, match_id, before, after, max_chars))
    return r


REPORT_SECTIONS = ("all", "axes", "structure_checks", "differences", "warnings", "unsupported_checks")


def get_report(report_id: str, section: str = "all") -> ReportResult:
    r = ReportResult(schema_version=SCHEMA_VERSION, tool="get_report", operation_completed=False)
    with _boundary(r):
        rep = _REPORTS.get((report_id or "").strip())
        if rep is None:
            raise DocbridgeError(ErrorCode.REPORT_NOT_FOUND, f"no report {report_id!r} in this server session",
                                 f"Reports are held for the last {REPORT_STORE_SIZE} operations of this session; "
                                 "use report_path to keep one on disk.")
        if section not in REPORT_SECTIONS:
            raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, f"unknown section {section!r}",
                                 f"Choose from {list(REPORT_SECTIONS)}.")
        r.report_id = report_id
        if section == "all":
            r.report = rep
        else:
            value = getattr(rep, section)
            if isinstance(value, dict):
                r.section = {k: (v.model_dump() if hasattr(v, "model_dump") else v) for k, v in value.items()}
            else:
                r.section = {section: [v.model_dump() for v in value]}
        r.operation_completed = True
    return r


def contract() -> ContractResult:
    from .convert.pandoc import find_pandoc, pandoc_version
    from .versions import tool_versions
    return ContractResult(schema_version=SCHEMA_VERSION, tool="docbridge_contract", contract={
        "statuses": {
            "PASS": "the property was compared and holds, within the axis's stated scope",
            "FAIL": "the property was compared and does not hold",
            "NOT_CHECKED": "both formats can carry the property, but this run did not establish it; never a pass",
            "UNSUPPORTED": "a format cannot carry the property, or it is outside docbridge's declared domain; "
                           "nothing to compare",
        },
        "reasons": [r_.value for r_ in Reason],
        "error_codes": [e.value for e in ErrorCode],
        "axes": list(AXES),
        "required_axes": {**{k: list(v) for k, v in REQUIRED_AXES.items()},
                          "default (conversions, validate_conversion, document_compare)": list(DEFAULT_REQUIRED_AXES)},
        "global_status_rule": "FAIL if any axis FAILs; PASS if every required axis is PASS; UNSUPPORTED if every "
                              "required axis is UNSUPPORTED; NOT_CHECKED otherwise",
        "capabilities": CAPABILITIES,
        "normalization": N.declared_rules(),
        "dependencies": {"pandoc": {"path": find_pandoc(), "version": pandoc_version(),
                                    "needed_by": ["markdown_to_docx", "docx_to_markdown"]},
                         "versions": tool_versions()},
        "limits": {"allowed_roots": [str(p) for p in allowed_roots()], "max_input_bytes": max_input_bytes(),
                   "ocr": "none", "report_store": f"the last {REPORT_STORE_SIZE} reports of this server session"},
        "evidence_invariant": EVIDENCE_INVARIANT,
        "reading": {"unit_kind": RD.UNIT_KIND, "verbatim_means": RD.VERBATIM_MEANS,
                    "read_around_default_units": RD.AROUND_DEFAULT,
                    "offsets": "every char_start/char_end indexes the document's reading text: its units "
                               "joined by one newline",
                    "search_folding": "whitespace in a literal query matches any whitespace run; the declared "
                                      "ligatures and invisible characters are folded; nothing else"},
    })
