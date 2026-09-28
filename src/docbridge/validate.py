"""Builds a ValidationReport from two canonical documents.

The only place a global verdict is computed. Rule (contract.REQUIRED_AXES):

    FAIL         if any axis FAILs
    PASS         if every axis the operation requires is PASS
    UNSUPPORTED  if every required axis is UNSUPPORTED
    NOT_CHECKED  otherwise - something required was not established

A global PASS therefore always names what it covers (`required_axes`), and
everything NOT_CHECKED or UNSUPPORTED is listed in `unsupported_checks`.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from . import normalize as N
from .canonical import Canonical, Origin
from .compare import (Side, TooLarge, _key_core, _key_text, _seq_check, check_emphasis,
                      check_headings, check_lists, check_pages, check_tables, diff_view,
                      link_targets, stream_origins, _cap)
from .contract import SCHEMA_VERSION, Status, required_axes
from .report import AxisResult, Difference, UnsupportedItem, ValidationReport, WarningItem
from .versions import tool_versions

_AXIS_ORDER = ("numeric", "identifier", "text", "structure", "layout", "image")

_SCOPES = {
    "text": "every whitespace-delimited token of both documents, in order, after the declared "
            "normalization (text classes compared: {origins})",
    "numeric": "every token containing a Unicode number character, sentence punctuation stripped "
               "from its edges, in order - membership by character class, not by pattern",
    "identifier": "URLs, e-mail addresses, dates, times, percentages, currency amounts, numbers of "
                  f"{N.LONG_NUMBER_DIGITS}+ digits, space-grouped numbers, letter-digit codes and file "
                  "names, in order; plus hyperlink targets not visible in their own link text",
}

_EXCLUDED_ORIGIN_STATUS = {
    "furniture": ("UNSUPPORTED", "not_carried", "page headers/footers are never compared"),
    "note": ("NOT_CHECKED", "not_implemented", "footnote/endnote text is not compared against a PDF"),
    "note_ref": ("NOT_CHECKED", "not_implemented", "note reference marks are not compared against a PDF"),
    "image_alt": ("UNSUPPORTED", "absent_in_format", "image alt text exists only in Markdown and DOCX"),
    "list_label": ("NOT_CHECKED", "not_implemented", "list labels"),
    "content": ("NOT_CHECKED", "not_implemented", "content"),
}


def combine(results: list[AxisResult]) -> tuple[str, str | None]:
    statuses = [r.status for r in results]
    if "FAIL" in statuses:
        return "FAIL", None
    for r in results:
        if r.status == "NOT_CHECKED":
            return "NOT_CHECKED", r.reason
    if "PASS" in statuses:
        return "PASS", None
    return "UNSUPPORTED", next((r.reason for r in results if r.reason), "absent_in_format")


def text_axes(a: Canonical, b: Canonical) -> tuple[dict[str, AxisResult], list[Difference], int, frozenset]:
    """The text, numeric and identifier axes, plus the wrong-page count."""
    origins = stream_origins(a.format, b.format)
    A, B = Side(a, origins), Side(b, origins)
    axes: dict[str, AxisResult] = {}
    diffs: list[Difference] = []
    wrong_page = 0
    views = (("text", A.text, B.text, _key_text), ("numeric", A.numeric, B.numeric, _key_core),
             ("identifier", A.identifier, B.identifier, _key_core))
    for axis, av, bv, key in views:
        scope = _SCOPES[axis].format(origins=", ".join(sorted(o.value for o in origins)))
        not_compared = sum(v["segments"] for v in A.excluded.values()) + \
            sum(v["segments"] for v in B.excluded.values()) if axis == "text" else None
        try:
            status, reason, d, details = diff_view(axis, A, B, av, bv, key)
        except TooLarge:
            axes[axis] = AxisResult(
                status="NOT_CHECKED", reason="too_large", scope=scope,
                compared=max(len(av), len(bv)),
                details={"source_tokens": len(av), "target_tokens": len(bv),
                         "note": "the differing region exceeds the alignment limit; no verdict"})
            continue
        except Exception as e:  # a checker that raised has checked nothing (R2)
            axes[axis] = AxisResult(status="NOT_CHECKED", reason="check_failed", scope=scope,
                                    details={"error": f"{type(e).__name__}: {e}"})
            continue
        if axis == "text":
            wrong_page = details.pop("tokens_on_a_different_page", 0)
            details["excluded_from_comparison"] = {"source": A.excluded, "target": B.excluded}
            pdf_limits = [c for c in (a, b) if c.format == "pdf"]
            if pdf_limits:
                details["pdf_scope"] = ("PDF text is the text layer as PyMuPDF decodes it; text inside raster "
                                        "images and text drawn as outlines is never read")
        if axis == "numeric" and not av and not bv:
            details["vacuous"] = True
            details["note"] = "neither document contains a number character"
        if axis == "identifier":
            status, reason, d, details = _with_links(a, b, status, reason, d, details)
        diffs.extend(d)
        axes[axis] = AxisResult(status=status.value if isinstance(status, Status) else status,
                                reason=reason.value if hasattr(reason, "value") else reason,
                                scope=scope, compared=max(len(av), len(bv)), not_compared=not_compared,
                                differences=len(d), details=details)
    return axes, diffs, wrong_page, origins


def _with_links(a: Canonical, b: Canonical, status, reason, diffs, details):
    la, lb = link_targets(a), link_targets(b)
    if _cap(a.format, "links") == "yes" and _cap(b.format, "links") == "yes":
        res, ld = _seq_check("links", "", la, lb, lambda k: k)
        for d in ld:
            d.axis = "identifier.links"
        details["links"] = {"status": res.status, "source_count": len(la), "target_count": len(lb)}
        if res.status == "FAIL":
            status, reason = Status.FAIL, None
        return status, reason, diffs + ld, details
    details["links"] = {"status": "UNSUPPORTED", "reason": "absent_in_format",
                        "source_count": len(la), "target_count": len(lb)}
    return status, reason, diffs, details


def structure_axis(a: Canonical, b: Canonical, origins, wrong_page: int
                   ) -> tuple[dict[str, AxisResult], list[Difference]]:
    checks: dict[str, AxisResult] = {}
    diffs: list[Difference] = []
    for name, fn in (("pages", lambda: check_pages(a, b, wrong_page)),
                     ("headings", lambda: check_headings(a, b, origins)),
                     ("lists", lambda: check_lists(a, b)),
                     ("tables", lambda: check_tables(a, b)),
                     ("emphasis", lambda: check_emphasis(a, b))):
        try:
            res, d = fn()
        except Exception as e:
            res, d = AxisResult(status="NOT_CHECKED", reason="check_failed", scope=name,
                                details={"error": f"{type(e).__name__}: {e}"}), []
        checks[name] = res
        diffs.extend(d)
    return checks, diffs


def default_layout(a: Canonical, b: Canonical) -> AxisResult:
    if "md" in (a.format, b.format) or "txt" in (a.format, b.format):
        return AxisResult(status="UNSUPPORTED", reason="absent_in_format",
                          scope="Markdown and plain text carry no layout")
    return AxisResult(status="NOT_CHECKED", reason="needs_layout_engine",
                      scope="page layout and typography are not compared for document conversions in v0.1")


def default_image(a: Canonical, b: Canonical) -> AxisResult:
    return AxisResult(status="UNSUPPORTED", reason="not_carried",
                      scope="image content is not compared for document conversions in v0.1 "
                            "(Markdown/DOCX alt text is compared by the text check)",
                      details={"source_images": len(a.images) if a.format != "pdf" else None,
                               "target_images": len(b.images) if b.format != "pdf" else None})


def build_report(operation: str, a: Canonical, b: Canonical, *,
                 sources: list[Canonical] | None = None, targets: list[Canonical] | None = None,
                 max_differences: int = 200,
                 axis_overrides: dict[str, AxisResult] | None = None,
                 structure_overrides: dict[str, AxisResult] | None = None,
                 extra_diffs: list[Difference] | None = None,
                 extra_warnings: list[WarningItem] | None = None,
                 text_axes_override: dict[str, AxisResult] | None = None) -> ValidationReport:
    """`text_axes_override` replaces the text/numeric/identifier comparison
    entirely (images have no text; an unreadable output cannot be compared);
    the default structure checks are then skipped too, because they read the
    same canonical forms."""
    sources = sources or [a]
    targets = targets or [b]
    if text_axes_override is not None:
        axes, diffs = dict(text_axes_override), []
        checks = dict(structure_overrides or {})
        if not checks:
            checks = {"all": AxisResult(status="NOT_CHECKED", reason="check_failed",
                                        scope="structure was not compared")}
    else:
        axes, diffs, wrong_page, origins = text_axes(a, b)
        checks, sdiffs = structure_axis(a, b, origins, wrong_page)
        diffs.extend(sdiffs)
        if structure_overrides:
            checks.update(structure_overrides)
    st, st_reason = combine(list(checks.values()))
    axes["structure"] = AxisResult(status=st, reason=st_reason,
                                   scope="the structure checks listed in structure_checks",
                                   differences=sum(c.differences for c in checks.values()),
                                   details={k: v.status for k, v in checks.items()})
    axes["layout"] = default_layout(a, b)
    axes["image"] = default_image(a, b)
    if axis_overrides:
        axes.update(axis_overrides)
    diffs.extend(extra_diffs or [])

    required = list(required_axes(operation))
    statuses = {k: axes[k].status for k in axes}
    failing = [k for k in ("text", "numeric", "identifier", "structure", "layout", "image") if statuses[k] == "FAIL"]
    if failing:
        status = "FAIL"
        basis = f"FAIL on {', '.join(failing)}"
    elif all(statuses[r] == "PASS" for r in required):
        status = "PASS"
        others = [f"{k} {statuses[k]}" for k in _AXIS_ORDER if k not in required and statuses[k] != "PASS"]
        basis = f"every required axis passed ({', '.join(required)})" + \
                (f"; not covered by this PASS: {', '.join(others)}" if others else "")
    elif all(statuses[r] == "UNSUPPORTED" for r in required):
        status = "UNSUPPORTED"
        basis = f"every required axis is UNSUPPORTED ({', '.join(required)})"
    else:
        status = "NOT_CHECKED"
        blocking = [f"{r} {statuses[r]} ({axes[r].reason})" for r in required if statuses[r] != "PASS"]
        basis = "nothing failed, but a required axis was not established: " + ", ".join(blocking)

    warnings = [WarningItem(side="source", code=w.code, message=w.message, location=w.location)
                for c in sources for w in c.warnings]
    warnings += [WarningItem(side="target", code=w.code, message=w.message, location=w.location)
                 for c in targets for w in c.warnings]
    warnings += extra_warnings or []

    unsupported: list[UnsupportedItem] = []
    for name in _AXIS_ORDER:
        ax = axes[name]
        if ax.status in ("NOT_CHECKED", "UNSUPPORTED"):
            unsupported.append(UnsupportedItem(check=name, status=ax.status, reason=ax.reason, detail=ax.scope))
    for name, ch in checks.items():
        if ch.status in ("NOT_CHECKED", "UNSUPPORTED"):
            unsupported.append(UnsupportedItem(check=f"structure.{name}", status=ch.status,
                                               reason=ch.reason, detail=ch.scope))
    for side, cs in (("source", sources), ("target", targets)):
        for c in cs:
            for con in c.unsupported:
                unsupported.append(UnsupportedItem(check=f"{side}:{con.kind}", status="UNSUPPORTED",
                                                   reason="not_carried", detail=f"{con.count} x {con.detail}"))
    text_details = axes["text"].details.get("excluded_from_comparison", {})
    for side in ("source", "target"):
        for origin, counts in text_details.get(side, {}).items():
            st_, rs_, what = _EXCLUDED_ORIGIN_STATUS.get(origin, ("NOT_CHECKED", "not_implemented", origin))
            unsupported.append(UnsupportedItem(
                check=f"{side}:excluded_{origin}", status=st_, reason=rs_,
                detail=f"{counts['segments']} segments ({counts['characters']} characters) not compared: {what}"))

    diffs.sort(key=lambda d: _AXIS_ORDER.index(d.axis.split(".")[0]) if d.axis.split(".")[0] in _AXIS_ORDER else 99)
    total = len(diffs)
    shown = diffs[:max_differences]

    return ValidationReport(
        schema_version=SCHEMA_VERSION, operation=operation,
        source=[c.path for c in sources], target=[c.path for c in targets],
        source_format=a.format, target_format=b.format,
        source_sha256=[c.sha256 for c in sources], target_sha256=[c.sha256 for c in targets],
        status=status, required_axes=required, status_basis=basis,
        text_status=axes["text"].status, numeric_status=axes["numeric"].status,
        identifier_status=axes["identifier"].status, structure_status=axes["structure"].status,
        layout_status=axes["layout"].status, image_status=axes["image"].status,
        axes=axes, structure_checks=checks,
        source_page_count=a.page_count, target_page_count=b.page_count,
        warnings=warnings, differences=shown, differences_total=total,
        differences_truncated=total - len(shown), unsupported_checks=unsupported,
        oracle={"source": a.extractor, "target": b.extractor},
        normalization=N.declared_rules(), tool_versions=tool_versions(),
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        summary=summarize(status, axes, diffs),
    )


def summarize(status: str, axes: dict[str, AxisResult], diffs: list[Difference]) -> str:
    parts = []
    for name in _AXIS_ORDER:
        ax = axes[name]
        s = f"{name} {ax.status}"
        if ax.reason:
            s += f" ({ax.reason})"
        if ax.status == "FAIL":
            s += f" [{ax.differences} difference{'s' if ax.differences != 1 else ''}]"
        parts.append(s)
    lead = [d for d in diffs if d.axis in ("numeric", "identifier") and d.kind != "unverifiable"][:3]
    detail = "; ".join(f"{d.kind} {d.source!r} -> {d.target!r} at {d.target_location or d.source_location}"
                       for d in lead)
    return f"{status}. " + ", ".join(parts) + (f". First number/identifier differences: {detail}" if detail else "")


# --------------------------------------------------------- page utilities ---

def concat_pdf(canons: list[Canonical], label: str) -> Canonical:
    """Several PDFs read as one, pages renumbered consecutively (for merge)."""
    out = Canonical(format="pdf", path=label, sha256="", size_bytes=sum(c.size_bytes for c in canons),
                    extractor=canons[0].extractor if canons else "")
    out.pages = []
    out.outline = []
    offset = 0
    for c in canons:
        for blk in c.blocks:
            nb = copy.copy(blk)
            nb.page = blk.page + offset if blk.page is not None else None
            nb.locator = f"{blk.locator} of {c.path}" if blk.locator else None
            out.blocks.append(nb)
        for p in c.pages or []:
            np_ = copy.copy(p)
            np_.number = p.number + offset
            out.pages.append(np_)
        for ln in c.links:
            ln2 = copy.copy(ln)
            ln2.page = ln.page + offset if ln.page is not None else None
            out.links.append(ln2)
        for e in c.outline or []:
            out.outline.append([e[0], e[1], e[2] + offset if e[2] and e[2] > 0 else e[2]])
        out.warnings.extend(c.warnings)
        out.unsupported.extend(c.unsupported)
        offset += len(c.pages or [])
    return out


def subset_pages(c: Canonical, pages: list[int], label: str) -> Canonical:
    """The pages of `c` listed (1-based, in the given order), renumbered 1..k."""
    out = Canonical(format="pdf", path=label, sha256=c.sha256, size_bytes=c.size_bytes, extractor=c.extractor)
    out.pages = []
    for new, old in enumerate(pages, start=1):
        for blk in c.blocks:
            if blk.page == old:
                nb = copy.copy(blk)
                nb.page = new
                out.blocks.append(nb)
        src = c.pages[old - 1]
        np_ = copy.copy(src)
        np_.number = new
        out.pages.append(np_)
        for ln in c.links:
            if ln.page == old:
                ln2 = copy.copy(ln)
                ln2.page = new
                out.links.append(ln2)
    return out


def origins_note(origins: frozenset[Origin]) -> str:
    return ", ".join(sorted(o.value for o in origins))
