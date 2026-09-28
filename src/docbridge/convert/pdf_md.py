"""PDF -> Markdown, from the PDF's text layer.

Everything this module adds beyond the text is INFERENCE, and the validation
report treats it that way: headings (from font size), lists (from bullet glyphs
and sequential numbers) and tables (PyMuPDF's table finder) cannot be validated
against a PDF, which encodes none of them, so structure comes back NOT_CHECKED
with reason `inferred_structure`. What CAN be checked is that every character of
the text layer arrives in the Markdown, in order, on the right page - and that
is what the text escaping below exists to make true.

Pages are marked with `<!-- docbridge:page N -->`. A page with visible content
and no text layer gets `<!-- docbridge:unreadable-page N reason=no_text_layer -->`
and no text: nothing is fabricated, and the checker reads the marker as UNKNOWN.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pymupdf

from .. import normalize as N
from ..extract.pdf import TextBlock, open_pdf, page_is_blank, read_page

HEADING_RATIO = 1.2         # a line this much larger than the body size is a heading candidate
HEADING_MAX_CHARS = 200
MAX_HEADING_LEVELS = 6

_ORDERED = re.compile(r"^(\d{1,9})([.)])(\s+|$)")
_ENTITY = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
_ALWAYS = set("\\`*_[]<~")


def escape_inline(text: str, in_table: bool = False) -> str:
    """Text -> Markdown that CommonMark renders back to exactly `text`
    (modulo whitespace, which the checker never compares)."""
    text = " ".join(text.split())
    out = []
    for ch in text:
        if ch in _ALWAYS or (in_table and ch == "|"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    s = "".join(out)
    s = _ENTITY.sub(lambda m: "\\" + m.group(0), s)
    return s


def escape_line_start(s: str) -> str:
    """Neutralise syntax that only means something at the start of a line."""
    if not s:
        return s
    m = _ORDERED.match(s)
    if m:
        return m.group(1) + "\\" + s[len(m.group(1)):]
    if s[0] in "#>+-=" or (s[0] == "*" and not s.startswith("\\")):
        return "\\" + s
    return s


def _table_md(rows: list[list]) -> list[str]:
    ncols = max((len(r) for r in rows), default=0)
    if ncols == 0:
        return []
    norm = [[escape_inline(c or "", in_table=True) for c in r] + [""] * (ncols - len(r)) for r in rows]
    lines = ["| " + " | ".join(norm[0]) + " |", "|" + "|".join(["---"] * ncols) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return lines


def _inside(bbox, rect) -> bool:
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    return rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1


class _Emitter:
    def __init__(self, heading_levels: dict[float, int], body_size: float):
        self.levels = heading_levels
        self.body = body_size
        self.lines: list[str] = []
        self.prev_ordered: tuple[int, str] | None = None
        self.stats = Counter()

    def para(self, text: str) -> None:
        s = escape_line_start(escape_inline(text))
        if s:
            self.lines += [s, ""]
            self.prev_ordered = None

    def heading(self, text: str, size: float) -> None:
        level = self.levels.get(size, MAX_HEADING_LEVELS)
        # every '#' escaped: a trailing " #" would otherwise be an ATX closing
        # sequence and vanish from the rendered heading
        self.lines += ["#" * level + " " + escape_inline(text).replace("#", "\\#"), ""]
        self.prev_ordered = None
        self.stats["headings"] += 1

    def bullet(self, text: str) -> None:
        self.lines += ["- " + escape_line_start(escape_inline(text)), ""]
        self.prev_ordered = None
        self.stats["bullet_items"] += 1

    def ordered(self, number: int, delim: str, text: str) -> bool:
        """Emit a real list item only when CommonMark will render this exact
        number: first item of a new list, or the next number of the open one."""
        if self.prev_ordered is not None and not (
                self.prev_ordered == (number - 1, delim)):
            return False
        self.lines += [f"{number}{delim} " + escape_line_start(escape_inline(text)), ""]
        self.prev_ordered = (number, delim)
        self.stats["ordered_items"] += 1
        return True

    def raw(self, lines: list[str]) -> None:
        self.lines += lines + [""]
        self.prev_ordered = None

    def block(self, tb: TextBlock) -> None:
        text = tb.text
        sizes = {ln.size for ln in tb.lines}
        if (self.levels and all(s in self.levels for s in sizes)
                and len(text) <= HEADING_MAX_CHARS and text.strip()):
            self.heading(text, max(sizes))
            return
        # line-level list detection
        pending: list[tuple[str, object, list[str]]] = []   # (kind, marker, parts)
        for ln in tb.lines:
            t = ln.text.strip()
            first = t[:1]
            m = _ORDERED.match(t)
            if first and first in N.BULLET_GLYPHS:
                pending.append(("bullet", None, [t[1:].strip()]))
            elif m:
                pending.append(("ordered", (m.group(1), m.group(2)), [t]))
            elif pending:
                pending[-1][2].append(t)
            else:
                pending.append(("para", None, [t]))
        for kind, marker, parts in pending:
            body = " ".join(p for p in parts if p)
            if kind == "bullet":
                if body:
                    self.bullet(body)
            elif kind == "ordered":
                digits, delim = marker
                rest = body[len(digits) + 1:].strip()
                # "01." would render as "1.": only a number CommonMark prints
                # back unchanged may become a list item
                if (str(int(digits)) != digits or not rest
                        or not self.ordered(int(digits), delim, rest)):
                    self.para(body)
            else:
                self.para(body)


def pdf_to_markdown(src: Path, out: Path, *, detect_tables: bool = True,
                    page_markers: bool = True) -> dict:
    """Write Markdown for `src` to `out`. Returns conversion statistics."""
    pymupdf.no_recommend_layout()
    doc = open_pdf(src)
    try:
        reads = [read_page(doc[i]) for i in range(doc.page_count)]
        weight: Counter = Counter()
        for rd in reads:
            for tb in rd.blocks:
                for ln in tb.lines:
                    weight[ln.size] += len(ln.text.strip())
        body = weight.most_common(1)[0][0] if weight else 0.0
        big = sorted({s for s in weight if body and s >= body * HEADING_RATIO}, reverse=True)
        levels = {s: min(i + 1, MAX_HEADING_LEVELS) for i, s in enumerate(big)}
        em = _Emitter(levels, body)
        unreadable: list[int] = []
        tables_found = 0
        for pno, rd in enumerate(reads):
            n = pno + 1
            page = doc[pno]
            if page_markers:
                em.raw([f"<!-- docbridge:page {n} -->"])
            if rd.chars == 0:
                if not page_is_blank(page):
                    unreadable.append(n)
                    em.raw([f"<!-- docbridge:unreadable-page {n} reason=no_text_layer: OCR required, "
                            "content not extracted -->"])
                continue
            tables = []
            if detect_tables:
                try:
                    tables = [t for t in page.find_tables().tables if t.row_count >= 1]
                except Exception:
                    tables = []
            rects = [pymupdf.Rect(t.bbox) for t in tables]
            emitted: set[int] = set()
            for tb in rd.blocks:
                hit = next((k for k, r in enumerate(rects) if _inside(tb.bbox, r)), None)
                if hit is None:
                    em.block(tb)
                    continue
                if hit not in emitted:
                    emitted.add(hit)
                    em.raw(_table_md(tables[hit].extract()))
                    tables_found += 1
            for k, t in enumerate(tables):
                if k not in emitted:
                    em.raw(_table_md(t.extract()))
                    tables_found += 1
        text = "\n".join(em.lines).rstrip() + "\n"
    finally:
        doc.close()
    out.write_text(text, encoding="utf-8", newline="\n")
    return {"pages": len(reads), "unreadable_pages": unreadable, "tables": tables_found,
            "headings": em.stats["headings"], "bullet_items": em.stats["bullet_items"],
            "ordered_items": em.stats["ordered_items"], "body_font_size": body,
            "heading_sizes": big}
