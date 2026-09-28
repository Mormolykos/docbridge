"""pdf_merge and pdf_split, and the page-level evidence behind their reports.

A merge or split copies page objects; it should change nothing on any page. So
the checks are about identity, per output page against the source page it must
have come from:

    raster      the page rendered at `raster_dpi` is pixel-identical (SHA-256 of
                the samples). MuPDF renders both sides, so this proves the page
                content survived the copy; it says nothing about MuPDF's own
                rendering being right, which is not the question here.
    text        the page's text layer is identical
    rotation    /Rotate is identical
    size        the page rectangle is identical
    links/annotations/form fields   counted per page, identical
    bookmarks   the output outline equals the sources' outlines, concatenated
                (merge) or cut to the range (split), with page numbers shifted

Checks are positional: output page k is compared with the one source page the
operation says it is, never searched for. Blank pages cannot be confused with
each other that way, and a dropped or reordered page cannot hide.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf

from ..contract import ErrorCode
from ..errors import DocbridgeError
from ..extract.pdf import open_pdf
from ..report import AxisResult, Difference

DEFAULT_RASTER_DPI = 72


def raster_digest(page: pymupdf.Page, dpi: int) -> str:
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    h = hashlib.sha256()
    h.update(f"{pix.width}x{pix.height}x{pix.n}:".encode())
    h.update(pix.samples)
    return h.hexdigest()


def _page_facts(page: pymupdf.Page, dpi: int) -> dict:
    return {
        "raster": raster_digest(page, dpi),
        "text": page.get_text("text"),
        "rotation": page.rotation,
        "size": (round(page.rect.width, 3), round(page.rect.height, 3)),
        "links": len(page.get_links()),
        "annotations": sum(1 for _ in page.annots()),
        "widgets": sum(1 for _ in page.widgets()),
    }


def compare_pages(pairs: list[tuple[str, int, pymupdf.Page, int, pymupdf.Page]], dpi: int
                  ) -> tuple[dict[str, AxisResult], AxisResult, list[Difference], list[dict]]:
    """pairs: (source label, source page no, source page, output page no, output page).
    Returns structure checks, the layout axis, differences and per-page rows."""
    rows: list[dict] = []
    diffs: list[Difference] = []
    bad = {"raster": 0, "text": 0, "rotation": 0, "size": 0, "links": 0, "annotations": 0, "widgets": 0}
    for label, spno, spage, opno, opage in pairs:
        s, o = _page_facts(spage, dpi), _page_facts(opage, dpi)
        row = {"output_page": opno, "source": label, "source_page": spno}
        for k in bad:
            same = s[k] == o[k]
            row[k] = "identical" if same else "DIFFERENT"
            if not same:
                bad[k] += 1
                detail = {"raster": "rendered pixels differ", "text": "text layer differs"}.get(
                    k, f"{s[k]} -> {o[k]}")
                diffs.append(Difference(
                    axis="layout" if k == "raster" else f"structure.page_{k}", kind="mismatch",
                    source=f"{label} page {spno}", target=f"output page {opno}", note=detail,
                    source_location=f"{label} page {spno}", target_location=f"page {opno}"))
        rows.append(row)
    n = len(pairs)

    def res(key: str, scope: str) -> AxisResult:
        return AxisResult(status="FAIL" if bad[key] else "PASS", scope=scope, compared=n,
                          differences=bad[key])

    checks = {
        "page_identity": res("text", "every output page's text layer is identical to the source page it came from"),
        "page_rotation": res("rotation", "every output page keeps its source page's /Rotate"),
        "page_size": res("size", "every output page keeps its source page's rectangle"),
        "page_links": res("links", "every output page keeps its source page's link count"),
        "page_annotations": res("annotations", "every output page keeps its source page's annotation count"),
        "page_form_fields": res("widgets", "every output page keeps its source page's form-field count"),
    }
    layout = AxisResult(status="FAIL" if bad["raster"] else "PASS",
                        scope=f"every output page renders pixel-identical to its source page at {dpi} dpi "
                              "(MuPDF on both sides)",
                        compared=n, differences=bad["raster"], details={"raster_dpi": dpi})
    return checks, layout, diffs, rows


# --- bookmarks ----------------------------------------------------------------
# Audit F7 (2026-09-25): the converter used to rebuild bookmarks from PyMuPDF's
# simplified list (level, title, page), which has no action - a bookmark that
# opened a URL became a dead entry - and the checker compared that same
# simplified list, so the loss passed. Now the converter carries each bookmark's
# complete destination, and the checker derives its expectation from the SOURCE
# files' complete bookmarks, independently of anything the converter produced.

def clamp_levels(levels: list[int]) -> list[int]:
    """The declared hierarchy rule (PyMuPDF refuses anything else): start at 1,
    never jump by more than one level. Applied identically by converter and
    checker - a declared normalization, not a shared representation."""
    out, prev = [], 0
    for lvl in levels:
        new = max(1, min(int(lvl), prev + 1))
        out.append(new)
        prev = new
    return out


def _full_toc(doc: pymupdf.Document) -> list[list]:
    entries = []
    for e in doc.get_toc(simple=False):
        dest = dict(e[3]) if len(e) > 3 and isinstance(e[3], dict) else {}
        dest.pop("xref", None)
        entries.append([int(e[0]), str(e[1]), int(e[2]), dest])
    return entries


def _shift(entry: list, delta: int) -> list:
    lvl, title, page, dest = entry
    dest = dict(dest)
    if dest.get("kind") == pymupdf.LINK_GOTO and isinstance(dest.get("page"), int) and dest["page"] >= 0:
        dest["page"] += delta
    return [lvl, title, page + delta if page > 0 else page, dest]


def _signature(entry: list) -> tuple:
    """What a bookmark IS: level, title, page, and its destination. Visual state
    (collapsed, colour, bold/italic) is not compared - declared."""
    lvl, title, page, d = entry
    kind = d.get("kind", pymupdf.LINK_NONE)
    if kind == pymupdf.LINK_GOTO:
        to = d.get("to")
        point = (round(float(to.x), 1), round(float(to.y), 1)) if to is not None else None
        target = ("goto", d.get("page"), point)
    elif kind == pymupdf.LINK_URI:
        target = ("uri", d.get("uri"))
    elif kind == pymupdf.LINK_GOTOR:
        target = ("gotor", d.get("file"), d.get("page"), d.get("to") and str(d.get("to")))
    elif kind == pymupdf.LINK_LAUNCH:
        target = ("launch", d.get("file"))
    elif kind == pymupdf.LINK_NAMED:
        target = ("named", d.get("name") or d.get("nameddest"))
    elif kind == pymupdf.LINK_NONE:
        target = ("none",)
    else:
        target = ("other", kind, tuple(sorted((k, str(v)) for k, v in d.items()
                                             if k not in ("collapse", "color", "bold", "italic"))))
    return (lvl, title, page, target)


def _describe_sig(s: tuple) -> str:
    return f"level {s[0]} {s[1]!r} page {s[2]} -> {s[3]}"


def bookmark_sigs(doc: pymupdf.Document) -> list[tuple]:
    return [_signature(e) for e in _full_toc(doc)]


def expected_merge_bookmarks(paths: list[Path]) -> list[tuple]:
    """From the source files themselves: every bookmark, destination included,
    page numbers shifted by the pages merged before it."""
    entries, offset = [], 0
    for p in paths:
        doc = open_pdf(p)
        try:
            entries += [_shift(e, offset) for e in _full_toc(doc)]
            offset += doc.page_count
        finally:
            doc.close()
    levels = clamp_levels([e[0] for e in entries])
    return [_signature([lv, *e[1:]]) for lv, e in zip(levels, entries, strict=True)]


def source_bookmarks(path: Path) -> list[tuple]:
    """The source's bookmarks as signatures, in order; a bookmark's identity is
    its INDEX in this list, which no page remapping can change."""
    doc = open_pdf(path)
    try:
        return [_signature(e) for e in _full_toc(doc)]
    finally:
        doc.close()


def expected_split_bookmarks(path: Path, a: int, b: int) -> tuple[list[tuple], set[int]]:
    """(expected signatures in this part, source indices carried by it). A
    bookmark belongs to every part whose range holds its page. Whether a
    bookmark was carried anywhere is decided by its source index - audit R6
    (2026-09-26): comparing shifted signatures against unshifted ones reported
    bookmarks preserved in their outputs as lost."""
    doc = open_pdf(path)
    try:
        entries = _full_toc(doc)
    finally:
        doc.close()
    inside = [(i, e) for i, e in enumerate(entries) if a <= e[2] <= b]
    moved = [_shift(e, -(a - 1)) for _, e in inside]
    levels = clamp_levels([e[0] for e in moved])
    expected = [_signature([lv, *e[1:]]) for lv, e in zip(levels, moved, strict=True)]
    return expected, {i for i, _ in inside}


def bookmark_check(expected: list[tuple], actual: list[tuple]) -> tuple[AxisResult, list[Difference]]:
    diffs = []
    if expected != actual:
        first = next((f"{_describe_sig(x)} vs {_describe_sig(y)}" for x, y in zip(expected, actual, strict=False)
                      if x != y), "(count differs)")
        diffs.append(Difference(axis="structure.bookmarks", kind="mismatch",
                                source=f"{len(expected)} bookmarks expected", target=f"{len(actual)} bookmarks present",
                                note="first differing entry: " + first))
    return AxisResult(status="FAIL" if diffs else "PASS",
                      scope="bookmark level, title, page and destination (the linked page and position, URL, "
                            "file or named action), in order, derived from the source files",
                      compared=max(len(expected), len(actual)), differences=len(diffs),
                      details={"expected": len(expected), "present": len(actual),
                               "vacuous": not expected and not actual}), diffs


def _write_toc(result: pymupdf.Document, entries: list[list]) -> bool:
    levels = clamp_levels([e[0] for e in entries])
    changed = levels != [e[0] for e in entries]
    result.set_toc([[lv, e[1], e[2], e[3]] for lv, e in zip(levels, entries, strict=True)])
    return changed


def merge(inputs: list[Path], out: Path) -> dict:
    result = pymupdf.open()
    toc: list[list] = []
    warnings: list[str] = []
    counts = []
    try:
        for p in inputs:
            src = open_pdf(p)
            try:
                offset = result.page_count
                result.insert_pdf(src, links=True, annots=True, widgets=True)
                toc += [_shift(e, offset) for e in _full_toc(src)]
                counts.append(src.page_count)
            finally:
                src.close()
        if toc and _write_toc(result, toc):
            warnings.append("bookmark hierarchy adjusted to start at level 1")
        result.save(str(out), garbage=3, deflate=True)
    finally:
        result.close()
    return {"page_counts": counts, "warnings": warnings}


_RANGE = re.compile(r"^\s*(\d+)\s*(?:(-)\s*(\d*)\s*)?$")


def parse_ranges(specs: list[str], page_count: int) -> list[tuple[int, int]]:
    out = []
    for spec in specs:
        m = _RANGE.match(spec or "")
        if not m:
            raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, f"bad page range {spec!r}",
                                 "Use 'N', 'N-M' or 'N-' (to the end), 1-based.")
        a = int(m.group(1))
        b = a if not m.group(2) else (int(m.group(3)) if m.group(3) else page_count)
        if not (1 <= a <= b <= page_count):
            raise DocbridgeError(ErrorCode.INVALID_ARGUMENT,
                                 f"page range {spec!r} is outside 1..{page_count} or reversed",
                                 f"The document has {page_count} pages.")
        out.append((a, b))
    return out


def split_names(stem: str, ranges: list[tuple[int, int]], page_count: int) -> list[str]:
    w = len(str(page_count))
    return [f"{stem}_p{a:0{w}d}.pdf" if a == b else f"{stem}_p{a:0{w}d}-{b:0{w}d}.pdf" for a, b in ranges]


def split_one(src_path: Path, a: int, b: int, out: Path) -> dict:
    src = open_pdf(src_path)
    result = pymupdf.open()
    try:
        result.insert_pdf(src, from_page=a - 1, to_page=b - 1, links=True, annots=True, widgets=True)
        toc = [_shift(e, -(a - 1)) for e in _full_toc(src) if a <= e[2] <= b]
        changed = _write_toc(result, toc) if toc else False
        result.save(str(out), garbage=3, deflate=True)
    finally:
        result.close()
        src.close()
    return {"toc_adjusted": changed}
