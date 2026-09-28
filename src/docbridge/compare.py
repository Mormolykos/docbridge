"""Axis-by-axis comparison of two canonical documents.

Three token views, one alignment rule:

    text        every token, compared exactly after the declared normalization
    numeric     every token containing a Unicode number character, edge
                punctuation stripped. Membership is by character class, never
                by pattern, so no number can fall outside the check because it
                was written in an unexpected way
    identifier  URL, email, date, time, percentage, currency, long numbers,
                space-grouped numbers, codes, file names, plus hyperlink
                targets that are not visible in their own link text

Each view is aligned as a SEQUENCE (difflib, autojunk off): a value that moved
is a difference, because two amounts swapped between table cells are a
corruption even though the set of numbers is unchanged.

UNKNOWN tokens (sentinels) never match. A difference touching one is
`unverifiable`: it can stop an axis from passing, it can never make it fail.
An axis FAILs only on a difference between two readable values.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Callable, Hashable

from . import normalize as N
from .canonical import Canonical, Origin, Sentinel, Tok
from .contract import CAPABILITIES, Reason, Status
from .report import AxisResult, Difference

ALIGN_LIMIT = 250_000
CONTEXT = 6
TEXT_DIFF_CHARS = 300

ALL_ORIGINS = frozenset(Origin) - {Origin.UNKNOWN}


class TooLarge(Exception):
    pass


def stream_origins(a_fmt: str, b_fmt: str) -> frozenset[Origin]:
    """Which classes of text a comparison between these two formats includes.

    Content, list labels and ruby guide text always (audit R4, 2026-09-26:
    visible text is never projected away). Image alt text only between
    Markdown and DOCX, the two formats that store it. Note references and note
    bodies unless a PDF is involved, because a PDF renders notes at page
    bottoms in its own numbering and v0.1 does not model that. Furniture
    (headers and footers) never. What is excluded is counted and reported,
    never dropped silently."""
    origins = {Origin.CONTENT, Origin.LIST_LABEL, Origin.RUBY}
    if a_fmt in ("md", "docx") and b_fmt in ("md", "docx"):
        origins.add(Origin.IMAGE_ALT)
    if "pdf" not in (a_fmt, b_fmt):
        origins |= {Origin.NOTE_REF, Origin.NOTE}
    return frozenset(origins)


def align(a: list[Hashable], b: list[Hashable]) -> list[tuple[str, int, int, int, int]]:
    """difflib opcodes, after trimming the common prefix and suffix so a
    faithful conversion of a long document costs linear time."""
    n = min(len(a), len(b))
    p = 0
    while p < n and a[p] == b[p]:
        p += 1
    s = 0
    while s < n - p and a[len(a) - 1 - s] == b[len(b) - 1 - s]:
        s += 1
    ops: list[tuple[str, int, int, int, int]] = []
    if p:
        ops.append(("equal", 0, p, 0, p))
    mid_a, mid_b = a[p:len(a) - s], b[p:len(b) - s]
    if mid_a or mid_b:
        if max(len(mid_a), len(mid_b)) > ALIGN_LIMIT:
            raise TooLarge()
        sm = SequenceMatcher(None, mid_a, mid_b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            ops.append((tag, i1 + p, i2 + p, j1 + p, j2 + p))
    if s:
        ops.append(("equal", len(a) - s, len(a), len(b) - s, len(b)))
    return ops


class Side:
    """One document's token streams plus the lookups a difference needs."""

    def __init__(self, canon: Canonical, origins: frozenset[Origin]):
        self.canon = canon
        self.text = canon.stream(origins)
        self.numeric = [t for t in self.text if not t.known or N.has_number_char(t.core)]
        self.identifier = identifier_view(self.text)
        # R18: what this projection leaves out is counted from the segments
        # themselves, so a reference glued to a word is still counted.
        excluded: dict[str, dict[str, int]] = {}
        for block in canon.blocks:
            for seg in block.segments:
                if seg.origin is Origin.UNKNOWN or seg.origin in origins or not seg.text.strip():
                    continue
                e = excluded.setdefault(seg.origin.value, {"segments": 0, "characters": 0})
                e["segments"] += 1
                e["characters"] += len(seg.text.strip())
        self.excluded = excluded

    def locate(self, t: Tok) -> str:
        b = self.canon.blocks[t.block] if 0 <= t.block < len(self.canon.blocks) else None
        loc = b.locator if b is not None and b.locator else f"block {t.block + 1}"
        if t.page is not None and "page" not in loc:
            loc = f"page {t.page}, {loc}"
        return loc

    def context(self, t: Tok) -> str:
        i = t.index
        lo, hi = max(0, i - CONTEXT), min(len(self.text), i + CONTEXT + 1)
        return " ".join(x.display() for x in self.text[lo:hi])


def identifier_view(text: list[Tok]) -> list[Tok]:
    """A pure function of the token sequence. Grouping runs break only at
    UNKNOWN tokens, never at block boundaries: blocks differ between formats
    (a TXT line, a PDF block, a DOCX paragraph), and a view that depended on
    them would turn a line break into an identifier difference. The property
    fuzz found exactly that (P7, 2026-09-24) before any reviewer saw it."""
    out: list[Tok] = []
    i, n = 0, len(text)
    while i < n:
        # a run of known tokens, broken only at sentinels
        j = i
        while j < n and text[j].known:
            j += 1
        if j == i:  # a sentinel: always part of every view
            out.append(text[i])
            i += 1
            continue
        run = text[i:j]
        cores = [t.core for t in run]
        grouped: set[int] = set()
        for s, e in N.grouped_number_spans(cores):
            value = " ".join(cores[s:e])
            digits = N.digit_count(value)
            classes = ("grouped_number",) + (("long_number",) if digits >= N.LONG_NUMBER_DIGITS else ()) \
                + (("currency",) if _beside_currency(cores, s, e) else ())
            head = run[s]
            out.append(Tok(value, value, classes, head.block, head.page, head.index, head.origin))
            grouped.update(range(s, e))
        for k, t in enumerate(run):
            if k in grouped:
                continue
            if N.is_identifier(t.classes):
                out.append(t)
            elif "number_bearing" in t.classes and _beside_currency(cores, k, k + 1):
                out.append(Tok(t.value, t.core, t.classes + ("currency",), t.block, t.page, t.index, t.origin))
        i = j
    out.sort(key=lambda t: t.index)
    return out


def _beside_currency(cores: list[str], s: int, e: int) -> bool:
    """A currency word immediately before or after the span [s, e)."""
    return (s > 0 and cores[s - 1] in N.CURRENCY_WORDS) or (e < len(cores) and cores[e] in N.CURRENCY_WORDS)


def _key_text(t: Tok):
    return t.value


def _key_core(t: Tok):
    return t.core if t.known else t.value


def _joined(toks: list[Tok]) -> str:
    s = " ".join(t.display() for t in toks)
    return s if len(s) <= TEXT_DIFF_CHARS else s[:TEXT_DIFF_CHARS] + " ..."


def diff_view(axis: str, a: Side, b: Side, a_view: list[Tok], b_view: list[Tok],
              key: Callable[[Tok], Hashable]) -> tuple[Status, Reason | None, list[Difference], dict]:
    """Align two views. Returns status, reason, differences and details."""
    ops = align([key(t) for t in a_view], [key(t) for t in b_view])
    diffs: list[Difference] = []
    clean = unverifiable = 0
    wrong_page = 0
    per_token = axis in ("numeric", "identifier")
    for tag, i1, i2, j1, j2 in ops:
        sa, sb = a_view[i1:i2], b_view[j1:j2]
        if tag == "equal":
            if axis == "text":
                wrong_page += sum(1 for x, y in zip(sa, sb, strict=True)
                                  if x.page is not None and y.page is not None and x.page != y.page)
            continue
        if any(not t.known for t in sa) or any(not t.known for t in sb):
            unverifiable += 1
            first_a, first_b = (sa[0] if sa else None), (sb[0] if sb else None)
            diffs.append(Difference(
                axis=axis, kind="unverifiable", source=_joined(sa) or None, target=_joined(sb) or None,
                source_location=a.locate(first_a) if first_a else None,
                target_location=b.locate(first_b) if first_b else None,
                note="an UNKNOWN construct (unreadable content) sits in this span; no verdict is possible here"))
            continue
        clean += 1
        if not per_token:
            first_a, first_b = (sa[0] if sa else None), (sb[0] if sb else None)
            kind = "changed" if sa and sb else ("missing" if sa else "added")
            diffs.append(Difference(
                axis=axis, kind=kind, source=_joined(sa) or None, target=_joined(sb) or None,
                source_location=a.locate(first_a) if first_a else None,
                target_location=b.locate(first_b) if first_b else None,
                source_context=a.context(first_a) if first_a else None,
                target_context=b.context(first_b) if first_b else None))
            continue
        m = min(len(sa), len(sb)) if tag == "replace" else 0
        for x, y in zip(sa[:m], sb[:m], strict=True):
            diffs.append(_token_diff(axis, "changed", a, b, x, y))
        for x in sa[m:]:
            diffs.append(_token_diff(axis, "missing", a, b, x, None))
        for y in sb[m:]:
            diffs.append(_token_diff(axis, "added", a, b, None, y))
    if per_token:
        diffs = _pair_moves(diffs)
    if clean:
        status, reason = Status.FAIL, None
    elif unverifiable:
        status, reason = Status.NOT_CHECKED, Reason.UNRESOLVED_CONTENT
    else:
        status, reason = Status.PASS, None
    details = {"source_tokens": len(a_view), "target_tokens": len(b_view),
               "unverifiable_spans": unverifiable}
    if axis == "text":
        details["tokens_on_a_different_page"] = wrong_page
    return status, reason, diffs, details


def _token_diff(axis: str, kind: str, a: Side, b: Side, x: Tok | None, y: Tok | None) -> Difference:
    classes = sorted(set((x.classes if x else ()) + (y.classes if y else ())) - {"number_bearing"})
    return Difference(
        axis=axis, kind=kind,
        source=x.core if x else None, target=y.core if y else None,
        source_location=a.locate(x) if x else None, target_location=b.locate(y) if y else None,
        source_context=a.context(x) if x else None, target_context=b.context(y) if y else None,
        classes=classes)


def _pair_moves(diffs: list[Difference]) -> list[Difference]:
    """A value missing in one place and added in another is one move."""
    missing = [d for d in diffs if d.kind == "missing"]
    added = [d for d in diffs if d.kind == "added"]
    used: set[int] = set()
    out: list[Difference] = []
    moved_ids: set[int] = set()
    for m in missing:
        for k, a in enumerate(added):
            if k not in used and a.target == m.source:
                used.add(k)
                moved_ids.add(id(m))
                moved_ids.add(id(a))
                out.append(Difference(
                    axis=m.axis, kind="moved", source=m.source, target=a.target,
                    source_location=m.source_location, target_location=a.target_location,
                    source_context=m.source_context, target_context=a.target_context,
                    classes=m.classes, note="same value, different position in the sequence"))
                break
    rest = [d for d in diffs if id(d) not in moved_ids]
    return rest + out


def link_targets(canon: Canonical) -> list[str]:
    """Hyperlink targets not visible in their own link text. A link whose
    target is its visible text (an autolink, a pasted URL) is already covered
    by the text check."""
    out = []
    for link in canon.links:
        target = N.normalize_link_target(link.target)
        if not target:
            continue
        shown = " ".join(N.tokenize(link.text))
        if target in shown or target in N.normalize_link_target(shown):
            continue
        out.append(target)
    return out


# ------------------------------------------------------------- structure ---

def _cap(fmt: str, prop: str) -> str:
    return CAPABILITIES.get(fmt, {}).get(prop, "no")


def capability_gate(prop: str, label: str, a: Canonical, b: Canonical,
                    a_count: int, b_count: int) -> AxisResult | None:
    ca, cb = _cap(a.format, prop), _cap(b.format, prop)
    if ca == "yes" and cb == "yes":
        return None
    counts = {"source_count": a_count, "target_count": b_count}
    if ca != "yes" and cb != "yes":
        return AxisResult(status="UNSUPPORTED", reason="absent_in_format",
                          scope=f"neither {a.format} nor {b.format} encodes {label}", details=counts)
    if ca == "yes":
        return AxisResult(status="UNSUPPORTED", reason="absent_in_format",
                          scope=f"{b.format} cannot encode {label}; the source has {a_count}, which the "
                                "target cannot carry", details=counts)
    if b_count == 0:
        return AxisResult(status="UNSUPPORTED", reason="absent_in_format",
                          scope=f"{a.format} does not encode {label} and the target has none", details=counts)
    return AxisResult(status="NOT_CHECKED", reason="inferred_structure",
                      scope=f"{a.format} does not encode {label}; the target's {b_count} were inferred "
                            "and cannot be validated against the source", details=counts)


def _norm(text: str) -> str:
    return " ".join(N.tokenize(text))


def _seq_check(axis_name: str, scope: str, a_items: list, b_items: list,
               describe: Callable[[Hashable], str]) -> tuple[AxisResult, list[Difference]]:
    ops = align(a_items, b_items)
    diffs: list[Difference] = []
    unknown = clean = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        sa, sb = a_items[i1:i2], b_items[j1:j2]
        if any(isinstance(x, Sentinel) for x in sa + sb):
            unknown += 1
            continue
        clean += 1
        diffs.append(Difference(
            axis=f"structure.{axis_name}", kind="mismatch",
            source="; ".join(describe(x) for x in sa)[:TEXT_DIFF_CHARS] or None,
            target="; ".join(describe(x) for x in sb)[:TEXT_DIFF_CHARS] or None,
            source_location=f"{axis_name} item {i1 + 1}" if sa else None,
            target_location=f"{axis_name} item {j1 + 1}" if sb else None))
    if clean:
        status, reason = "FAIL", None
    elif unknown:
        status, reason = "NOT_CHECKED", "unresolved_content"
    else:
        status, reason = "PASS", None
    return AxisResult(status=status, reason=reason, scope=scope,
                      compared=max(len(a_items), len(b_items)), differences=len(diffs),
                      details={"source_count": len(a_items), "target_count": len(b_items)}), diffs


def _gaps(res: AxisResult, name: str, a: Canonical, b: Canonical) -> AxisResult:
    """Structure a reader saw but could not model keeps its check from passing:
    a comparison over an incomplete inventory is not a verified one. Audit N1
    (2026-09-26): HTML headings and bold were compared as zero items and the
    checks said PASS. A difference between modelled items still FAILs."""
    ga, gb = a.structure_unknown.get(name, []), b.structure_unknown.get(name, [])
    if not (ga or gb):
        return res
    res.details = {**(res.details or {}), "unmodelled_source": len(ga), "unmodelled_target": len(gb),
                   "unmodelled_where": (ga + gb)[:10]}
    if res.status == "PASS":
        res.status, res.reason = "NOT_CHECKED", "unresolved_content"
        res.scope += (f"; NOT established: {len(ga) + len(gb)} place(s) hold {name} that docbridge reads but "
                      "does not model")
    return res


def check_headings(a: Canonical, b: Canonical, origins) -> tuple[AxisResult, list[Difference]]:
    ah, bh = a.headings(), b.headings()
    gate = capability_gate("headings", "headings", a, b, len(ah), len(bh))
    if gate:
        return gate, []

    def key(blk):
        return Sentinel("unresolved_content", blk.locator or "") if blk.has_unknown() \
            else (blk.level, _norm(blk.text(origins)))

    res, diffs = _seq_check("headings", "heading level and text, in document order",
                            [key(h) for h in ah], [key(h) for h in bh],
                            lambda k: f"h{k[0]} {k[1]!r}" if isinstance(k, tuple) else "UNKNOWN")
    return _gaps(res, "headings", a, b), diffs


def check_lists(a: Canonical, b: Canonical) -> tuple[AxisResult, list[Difference]]:
    al, bl = a.list_items(), b.list_items()
    gate = capability_gate("lists", "lists", a, b, len(al), len(bl))
    if gate:
        return gate, []
    res, diffs = _seq_check("lists", "list items wherever they sit - body, table cells (nested tables "
                                     "included), text boxes, notes, and headings that carry numbering: "
                                     "nesting depth and ordered/bulleted, in order (labels and item text are "
                                     "compared by the text check; headers and footers are never compared)",
                            [(x.list_depth or 0, bool(x.ordered)) for x in al],
                            [(x.list_depth or 0, bool(x.ordered)) for x in bl],
                            lambda k: f"depth {k[0]} {'ordered' if k[1] else 'bullet'}")
    return _gaps(res, "lists", a, b), diffs


def check_tables(a: Canonical, b: Canonical) -> tuple[AxisResult, list[Difference]]:
    gate = capability_gate("tables", "tables", a, b, len(a.tables), len(b.tables))
    if gate:
        return gate, []

    def cells(t):
        return [(c.row, c.col, c.rowspan, c.colspan, _norm(c.text)) for c in t.cells]

    diffs: list[Difference] = []
    for i in range(max(len(a.tables), len(b.tables))):
        ta = a.tables[i] if i < len(a.tables) else None
        tb = b.tables[i] if i < len(b.tables) else None
        if ta is None or tb is None:
            diffs.append(Difference(axis="structure.tables", kind="missing" if tb is None else "added",
                                    source=f"table {i + 1} ({ta.rows}x{ta.cols})" if ta else None,
                                    target=f"table {i + 1} ({tb.rows}x{tb.cols})" if tb else None))
            continue
        if (ta.rows, ta.cols) != (tb.rows, tb.cols):
            diffs.append(Difference(axis="structure.tables", kind="mismatch",
                                    source=f"table {i + 1}: {ta.rows} rows x {ta.cols} cols",
                                    target=f"table {i + 1}: {tb.rows} rows x {tb.cols} cols"))
        ca, cb = cells(ta), cells(tb)
        if ca != cb:
            res, cell_diffs = _seq_check("tables", "", ca, cb,
                                         lambda k: f"r{k[0] + 1}c{k[1] + 1} span {k[2]}x{k[3]} {k[4]!r}")
            for d in cell_diffs:
                d.source_location = f"table {i + 1}"
                d.target_location = f"table {i + 1}"
            diffs.extend(cell_diffs)
    status = "FAIL" if diffs else "PASS"
    res = AxisResult(status=status, scope="table count, rows x columns, and every cell's position, span and text",
                     compared=max(len(a.tables), len(b.tables)), differences=len(diffs),
                     details={"source_count": len(a.tables), "target_count": len(b.tables)})
    return _gaps(res, "tables", a, b), diffs


def check_emphasis(a: Canonical, b: Canonical) -> tuple[AxisResult, list[Difference]]:
    gate = capability_gate("emphasis", "bold/italic emphasis", a, b, len(a.emphasis), len(b.emphasis))
    if gate:
        return gate, []
    diffs: list[Difference] = []
    statuses = []
    for kind in ("strong", "em"):
        sa = [_norm(e.text) for e in a.emphasis if e.kind == kind]
        sb = [_norm(e.text) for e in b.emphasis if e.kind == kind]
        res, d = _seq_check("emphasis", "", sa, sb, lambda k, kind=kind: f"{kind} {k!r}")
        statuses.append(res.status)
        diffs.extend(d)
    status = "FAIL" if "FAIL" in statuses else "PASS"
    res = AxisResult(status=status, scope="the text of every bold span and every italic span, in order "
                                          "(direct and character-style formatting; paragraph-style "
                                          "formatting such as a bold heading style is not emphasis)",
                     compared=len(a.emphasis), differences=len(diffs),
                     details={"source_count": len(a.emphasis), "target_count": len(b.emphasis)})
    return _gaps(res, "emphasis", a, b), diffs


def check_pages(a: Canonical, b: Canonical, wrong_page: int) -> tuple[AxisResult, list[Difference]]:
    ca, cb = _cap(a.format, "pages"), _cap(b.format, "pages")
    pa, pb = a.page_count, b.page_count
    counts = {"source_count": pa, "target_count": pb}
    if "layout" in (ca, cb) and (pa is not None or pb is not None):
        return AxisResult(status="NOT_CHECKED", reason="needs_layout_engine",
                          scope="the other side has pages, and DOCX has none until a layout engine "
                                "renders it", details=counts), []
    if pa is None and pb is None:
        return AxisResult(status="UNSUPPORTED", reason="absent_in_format",
                          scope=f"neither {a.format} nor {b.format} has pages here", details=counts), []
    if pa is None or pb is None:
        return AxisResult(status="UNSUPPORTED", reason="absent_in_format",
                          scope="only one side has pages (Markdown carries pages only through "
                                "docbridge page markers)", details=counts), []
    diffs = []
    if pa != pb:
        diffs.append(Difference(axis="structure.pages", kind="mismatch", source=f"{pa} pages", target=f"{pb} pages"))
    if wrong_page:
        diffs.append(Difference(axis="structure.pages", kind="mismatch",
                                source=f"{wrong_page} matching tokens sit on a different page in the target",
                                note="text matched, but not on the same page"))
    return AxisResult(status="FAIL" if diffs else "PASS",
                      scope="page count, and that every matched token sits on the same page number",
                      compared=max(pa, pb), differences=len(diffs), details=counts), diffs
