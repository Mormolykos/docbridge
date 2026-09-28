"""Lossless reading: outline, search, verbatim page/line/paragraph reads.

THE INVARIANT (DOCUMENT_MCP_CONTRACT.md §0): docbridge may reduce the volume of
its own responses, but it never alters, summarizes, paraphrases, semantically
filters, or silently truncates source evidence. Everything here is exact
retrieval: a read returns a slice of the document's reading text character for
character, with its offsets and its SHA-256; a cut is always stated, with the
number of characters left and a cursor that continues exactly where it stopped.

THE READING TEXT, per format - what "verbatim" means, declared:

    PDF    unit = page. Each page's text layer exactly as PyMuPDF returns it
           (get_text("text") with docbridge's extraction flags). A page with
           visible content and no text layer is an empty unit listed as
           UNREADABLE - never silently empty.
    TXT/MD unit = line. The decoded file; the only change is CRLF/CR -> LF.
           Markdown is returned as source, not rendered.
    DOCX   unit = paragraph, in docbridge's reading order (body, then notes, then
           headers/footers). DOCX has no plain text to quote, so a unit is the
           paragraph as the declared reader extracts it (tracked changes
           accepted, hidden text excluded). Unreadable content appears in place as
           ⟦UNREADABLE: reason⟧, image alt text as ⟦image alt: ...⟧ and ruby
           guide text as ⟦ruby: ...⟧ - marked, never dropped, never passed off
           as body text.

The reading text is the units joined by one "\\n"; every offset indexes it.

SEARCH runs in a separate process (_search_worker.py) that is killed after
DOCBRIDGE_SEARCH_TIMEOUT_S seconds (default 2): a search that runs out of time
returns the error search_timeout, never a partial or empty result.
"""
from __future__ import annotations

import bisect
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from . import normalize as N
from .canonical import Canonical, Origin
from .contract import EVIDENCE_INVARIANT, ErrorCode  # noqa: F401  (re-exported)
from .errors import DocbridgeError
from .extract import detect_format, extract, sha256_of

VERBATIM_MEANS = {
    "pdf": "each page's text layer exactly as PyMuPDF returns it (not the rendered glyphs)",
    "txt": "the decoded file, line endings normalized to LF, nothing else changed",
    "md": "the decoded Markdown source (not rendered), line endings normalized to LF, nothing else changed",
    "docx": "each paragraph as docbridge's declared DOCX reader extracts it; unreadable content, image "
            "alt text and ruby guide text are marked in place with ⟦...⟧",
}
UNIT_KIND = {"pdf": "page", "txt": "line", "md": "line", "docx": "paragraph"}
AROUND_DEFAULT = {"page": 1, "paragraph": 3, "line": 15}
_CACHE_SIZE = 8


@dataclass
class ReadingText:
    path: Path
    format: str
    sha256: str
    units: list[str]
    labels: list[str]
    extractor: str
    unreadable: list[int] = field(default_factory=list)     # 1-based units with content docbridge cannot read
    image_units: list[int] = field(default_factory=list)    # PDF pages with raster images (text in them is unread)
    canonical: Canonical | None = None
    full: str = ""
    starts: list[int] = field(default_factory=list)
    reading_sha256: str = ""
    ref_key: str = ""
    _folded: tuple | None = None

    def __post_init__(self) -> None:
        self.full = "\n".join(self.units)
        pos, starts = 0, []
        for u in self.units:
            starts.append(pos)
            pos += len(u) + 1
        self.starts = starts
        self.reading_sha256 = hashlib.sha256(self.full.encode("utf-8")).hexdigest()
        # Audit F5 (2026-09-25): a reference must name the TEXT it indexes, not
        # only the file bytes - the same bytes decoded differently are different
        # text, and an offset into one is meaningless in the other.
        self.ref_key = hashlib.sha256(f"{self.sha256}|{self.reading_sha256}".encode()).hexdigest()[:16]

    def folded(self) -> tuple[str, list[int], list[int]]:
        if self._folded is None:
            self._folded = _fold(self.full)
        return self._folded

    @property
    def unit_kind(self) -> str:
        return UNIT_KIND[self.format]

    def unit_at(self, offset: int) -> int:
        """1-based unit containing `offset` (a joining newline belongs to the unit before it)."""
        return max(1, bisect.bisect_right(self.starts, offset))

    def unit_span(self, first: int, last: int) -> tuple[int, int]:
        return self.starts[first - 1], self.starts[last - 1] + len(self.units[last - 1])

    def source(self) -> dict:
        return {"path": str(self.path), "format": self.format, "sha256": self.sha256,
                "reading_sha256": self.reading_sha256,
                "unit_kind": self.unit_kind, "units_total": len(self.units), "reading_chars": len(self.full),
                "extractor": self.extractor, "verbatim_means": VERBATIM_MEANS[self.format]}

    def covered(self, a: int, end: int, stop: int) -> list[int]:
        """Units the slice [a, end) of the range [.., stop) covers. An empty unit
        counts too: it is covered where it sits inside the slice, or at its end
        when the slice reaches the end of the range. Audit F3 (2026-09-25): an
        empty final page used to fall outside every excerpt, so a whole-document
        read never reported it as unreadable."""
        first = self.unit_at(a)
        out = []
        for u in range(first, len(self.units) + 1):
            s = self.starts[u - 1]
            if s > end:
                break
            if u == first or s < end or (s == end and not self.units[u - 1] and (end == stop or a == end)):
                out.append(u)
        return out


_cache: OrderedDict[tuple, ReadingText] = OrderedDict()


def load(path: Path, encoding: str | None = None) -> ReadingText:
    """The reading text of a validated input path (cached by content hash)."""
    fmt = detect_format(path)
    sha = sha256_of(path)
    key = (sha, fmt, encoding)
    if key in _cache:
        _cache.move_to_end(key)
        rt = _cache[key]
        rt.path = path
        return rt
    if fmt in ("pdf", "docx"):
        key = (sha, fmt, None)          # an encoding means nothing for a binary format
        if key in _cache:
            _cache.move_to_end(key)
            rt = _cache[key]
            rt.path = path
            return rt
    if fmt == "pdf":
        rt = _load_pdf(path, sha)
    elif fmt == "docx":
        rt = _load_docx(path, sha)
    else:
        from .extract.text import decode_text
        text = decode_text(path.read_bytes(), encoding).replace("\r\n", "\n").replace("\r", "\n")
        units = text.split("\n")
        rt = ReadingText(path, fmt, sha, units, [f"line {i}" for i in range(1, len(units) + 1)],
                         extractor="docbridge text decoder (UTF-8/UTF-16-BOM or the named encoding)")
    _cache[key] = rt
    while len(_cache) > _CACHE_SIZE:
        _cache.popitem(last=False)
    return rt


def _load_pdf(path: Path, sha: str) -> ReadingText:
    import pymupdf

    from .extract.pdf import IMAGE_AREA_WARN, TEXT_FLAGS, open_pdf, page_is_blank
    doc = open_pdf(path)
    units, unreadable, image_units = [], [], []
    try:
        for i in range(doc.page_count):
            page = doc[i]
            text = page.get_text("text", flags=TEXT_FLAGS)
            units.append(text)
            if not text.strip() and not page_is_blank(page):
                unreadable.append(i + 1)
            try:
                area = sum(abs(pymupdf.Rect(info["bbox"]) & page.rect) for info in page.get_image_info())
                if abs(page.rect) and area / abs(page.rect) > IMAGE_AREA_WARN and text.strip():
                    image_units.append(i + 1)
            except Exception:
                pass
    finally:
        doc.close()
    return ReadingText(path, "pdf", sha, units, [f"page {i}" for i in range(1, len(units) + 1)],
                       extractor=f"PyMuPDF {pymupdf.VersionBind} page text layer", unreadable=unreadable,
                       image_units=image_units)


def _load_docx(path: Path, sha: str) -> ReadingText:
    canon = extract(path)
    units, labels, unreadable = [], [], []
    for i, b in enumerate(canon.blocks, start=1):
        parts = []
        for k, s in enumerate(b.segments):
            if s.origin is Origin.UNKNOWN:
                parts.append(f"⟦UNREADABLE: {s.reason or 'unresolved_content'}⟧")
            elif s.origin is Origin.IMAGE_ALT:
                parts.append(f"⟦image alt: {s.text}⟧")
            elif s.origin is Origin.RUBY:
                # one marker per guide text, however many runs it is stored in
                prev = b.segments[k - 1].origin if k else None
                nxt = b.segments[k + 1].origin if k + 1 < len(b.segments) else None
                parts.append(("" if prev is Origin.RUBY else "⟦ruby: ") + s.text
                             + ("" if nxt is Origin.RUBY else "⟧"))
            else:
                parts.append(s.text)
        units.append("".join(parts))
        where = f", {b.locator}" if b.locator and b.locator.startswith("table") else ""
        labels.append(f"paragraph {i} ({b.kind.value}{where})")
        if b.has_unknown():
            unreadable.append(i)
    return ReadingText(path, "docx", sha, units, labels, extractor=canon.extractor, unreadable=unreadable,
                       canonical=canon)


# ------------------------------------------------------------- references ---

def _ref(rt: ReadingText, a: int, b: int) -> str:
    return f"{rt.ref_key}:{a}:{b}"


def parse_ref(rt: ReadingText, ref: str, what: str) -> tuple[int, int]:
    m = re.fullmatch(r"([0-9a-f]{16}):(\d+):(\d+)", (ref or "").strip())
    if not m:
        raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, f"malformed {what}: {ref!r}",
                             f"Pass a {what} exactly as a previous docbridge response returned it.")
    if m.group(1) != rt.ref_key:
        raise DocbridgeError(ErrorCode.STALE_REFERENCE,
                             f"the {what} was issued for a different version or decoding of this file",
                             "The file or its decoding (encoding) changed since; search or read it again "
                             "with the same encoding.")
    a, b = int(m.group(2)), int(m.group(3))
    if not (0 <= a <= b <= len(rt.full)):
        raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, f"{what} is outside the document", "")
    return a, b


# ------------------------------------------------------------------- read ---

def excerpt(rt: ReadingText, a: int, stop: int, max_chars: int, range_start: int | None = None) -> dict:
    """The slice [a, min(stop, a+max_chars)) with everything needed to cite it,
    verify it, and continue it."""
    end = min(stop, a + max_chars)
    text = rt.full[a:end]
    covered = rt.covered(a, end, stop)
    units = [{"unit": u, "label": rt.labels[u - 1], "char_start": max(a, rt.starts[u - 1])} for u in covered]
    whole = rt.covered(range_start if range_start is not None else a, stop, stop)
    out = {
        "excerpt": {"text": text, "char_start": a, "char_end": end,
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "first_unit": covered[0], "last_unit": covered[-1], "units": units},
        "truncated": end < stop or (range_start is not None and range_start < a),
        "remaining_chars": stop - end,
        "next_cursor": _ref(rt, end, stop) if end < stop else None,
        "omitted_before_chars": (a - range_start) if range_start is not None else 0,
        "before_cursor": _ref(rt, range_start, a) if range_start is not None and range_start < a else None,
        "unreadable_units": [u for u in rt.unreadable if u in covered],
        # every unreadable unit of the whole requested range, returned or not
        "range_unreadable_units": [u for u in rt.unreadable if u in whole],
    }
    return out


def read(rt: ReadingText, start: int, end: int | None, max_chars: int, cursor: str | None) -> dict:
    if cursor:
        a, stop = parse_ref(rt, cursor, "cursor")
        return excerpt(rt, a, stop, max_chars)
    n = len(rt.units)
    last = n if end is None else end
    if not (1 <= start <= n) or not (start <= last <= n):
        raise DocbridgeError(ErrorCode.INVALID_ARGUMENT,
                             f"units {start}..{last} are outside 1..{n} {rt.unit_kind}s",
                             f"The document has {n} {rt.unit_kind}s.")
    a, stop = rt.unit_span(start, last)
    return excerpt(rt, a, stop, max_chars)


def around(rt: ReadingText, match_id: str, before: int | None, after: int | None, max_chars: int) -> dict:
    s, e = parse_ref(rt, match_id, "match_id")
    default = AROUND_DEFAULT[rt.unit_kind]
    before = default if before is None else before
    after = default if after is None else after
    u1, u2 = rt.unit_at(s), rt.unit_at(max(s, e - 1))
    first, last = max(1, u1 - before), min(len(rt.units), u2 + after)
    range_start, stop = rt.unit_span(first, last)
    # The match must be inside what is returned. If the range is larger than
    # the cap, the window is placed around the match and both sides that were
    # left out are stated, each with a cursor.
    a = range_start
    if stop - range_start > max_chars:
        room = max(0, max_chars - (e - s))
        a = max(range_start, min(s - room // 2, stop - max_chars))
        a = min(a, s)
    out = excerpt(rt, a, stop, max(max_chars, e - a), range_start=range_start)
    out["match"] = {"match_id": match_id, "char_start": s, "char_end": e, "text": rt.full[s:e],
                    "unit": u1, "label": rt.labels[u1 - 1]}
    out["requested_units"] = [first, last]
    return out


# ----------------------------------------------------------------- search ---

@functools.lru_cache(maxsize=1)
def _combines_backward() -> frozenset[int]:
    """Code points that can compose with the character BEFORE them under NFC:
    the second element of every two-character canonical decomposition in the
    Unicode data, plus the Hangul vowel and final jamo, whose composition is
    algorithmic and not listed. A superset is safe (segments get longer); a
    missing entry would not be, which is what the property test checks."""
    back = set(range(0x1161, 0x1176)) | set(range(0x11A8, 0x11C3))
    for cp in range(sys.maxunicode + 1):
        d = unicodedata.decomposition(chr(cp))
        if d and d[0] != "<":
            parts = d.split()
            if len(parts) == 2:
                back.add(int(parts[1], 16))
    return frozenset(back)


@functools.lru_cache(maxsize=None)
def _boundary_before(ch: str) -> bool:
    """A safe place to cut text for independent NFC normalization: the
    character's canonical decomposition begins with a starter (combining class
    0) that never composes with what precedes it. This is the Unicode
    normalization boundary (UAX #15; ICU's hasCompBoundaryBefore). Judging
    the decomposition, not the character, keeps singletons such as U+2126
    OHM SIGN and composition exclusions such as U+0958 cut points too, so
    their matches are not widened."""
    first = unicodedata.normalize("NFD", ch)[0]
    return unicodedata.combining(first) == 0 and ord(first) not in _combines_backward()


def _segments(text: str):
    """(start, end) of each normalization segment: cut only before a boundary
    character. Audit R5 (2026-09-26): cutting before every class-0 character
    split Hangul L+V jamo and Bengali two-part vowels, which compose across
    class-0 characters, so canonically equivalent text was not found."""
    i, n = 0, len(text)
    while i < n:
        j = i + 1
        while j < n and not _boundary_before(text[j]):
            j += 1
        yield i, j
        i = j


def _fold(text: str) -> tuple[str, list[int], list[int]]:
    """The searchable form, applied IDENTICALLY to the reading text and to the
    query: each normalization segment composed (NFC), the declared ligatures
    expanded, the declared invisible characters removed (normalize.py).
    Every output character maps back to the (start, end) of the source segment
    it came from, so a match returns exact original offsets - widened, at most,
    to whole segments. Nothing else is folded."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for i, j in _segments(text):
        seg = unicodedata.normalize("NFC", text[i:j])
        for ch in seg:
            if ch in N.REMOVED_CHARS:
                continue
            rep = N.LIGATURES.get(ch, ch)
            out.append(rep)
            starts.extend([i] * len(rep))
            ends.extend([j] * len(rep))
    return "".join(out), starts, ends


SEARCH_TIMEOUT_DEFAULT_S = 2.0
_WORKER = Path(__file__).with_name("_search_worker.py")


def search_timeout_s() -> float:
    """The time bound on one search: DOCBRIDGE_SEARCH_TIMEOUT_S, else 2 s."""
    try:
        t = float(os.environ.get("DOCBRIDGE_SEARCH_TIMEOUT_S", SEARCH_TIMEOUT_DEFAULT_S))
    except ValueError:
        return SEARCH_TIMEOUT_DEFAULT_S
    return t if t > 0 else SEARCH_TIMEOUT_DEFAULT_S


def _run_search(pattern: str, flags: int, text: str, overlapping: bool, start: int,
                limit: int) -> tuple[int, list[list[int]]]:
    """Run one search in _search_worker.py under a hard time bound (audit K1).
    Returns (matches_total, spans of matches start .. start+limit-1)."""
    header = json.dumps({"pattern": pattern, "flags": flags, "overlapping": overlapping,
                         "start": start, "limit": limit}).encode("ascii")
    payload = header + b"\n" + text.encode("utf-8", "surrogatepass")
    timeout = search_timeout_s()
    try:
        done = subprocess.run([sys.executable, "-I", "-S", str(_WORKER)], input=payload, capture_output=True,
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise DocbridgeError(
            ErrorCode.SEARCH_TIMEOUT,
            f"the search did not finish within {timeout:g} s and was stopped. No result is returned, and none "
            "is implied: this is not evidence that the text is absent",
            "Search for a shorter or simpler query; for a regular expression, avoid nested repetition such as "
            "(a+)+. The server operator can raise DOCBRIDGE_SEARCH_TIMEOUT_S.") from None
    except OSError as e:
        raise DocbridgeError(ErrorCode.INTERNAL_ERROR, f"the search worker could not start: {e}", None) from None
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no detail"]
        raise DocbridgeError(ErrorCode.INTERNAL_ERROR, f"the search worker failed: {detail[0]}", None)
    out = json.loads(done.stdout)
    return out["total"], out["spans"]


def search(rt: ReadingText, query: str, regex: bool, case_sensitive: bool, max_matches: int,
           start_match: int, context_chars: int) -> dict:
    # the query is folded exactly as the text is, so "ﬁnal" finds "ﬁnal" and a
    # decomposed "café" finds a composed one, and the reverse
    q = _fold(query or "")[0]
    if not q.strip():
        raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, "empty query", "Pass the text to look for.")
    pattern = q if regex else r"\s+".join(re.escape(p) for p in q.split())
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        re.compile(pattern, flags)
    except re.error as e:
        raise DocbridgeError(ErrorCode.INVALID_ARGUMENT, f"invalid regular expression: {e}",
                             "Fix the pattern, or pass regex=false for a literal search.") from None
    folded, f_starts, f_ends = rt.folded()
    # A literal query finds EVERY occurrence, overlapping ones included: "aa"
    # occurs twice in "aaa" (the reading property fuzz, after audit F3/F4). A
    # regex keeps the usual leftmost, non-overlapping semantics.
    total, spans = _run_search(pattern, flags, folded, overlapping=not regex, start=start_match,
                               limit=max_matches)
    matches = []
    for m_start, m_end in spans:
        s, e = f_starts[m_start], f_ends[m_end - 1]
        u1, u2 = rt.unit_at(s), rt.unit_at(max(s, e - 1))
        matches.append({
            "match_id": _ref(rt, s, e), "char_start": s, "char_end": e,
            "unit": u1, "end_unit": u2, "label": rt.labels[u1 - 1],
            "text": rt.full[s:e],
            "context_before": rt.full[max(0, s - context_chars):s],
            "context_after": rt.full[e:e + context_chars],
        })
    returned_until = start_match + len(matches)
    notes = []
    if rt.unreadable:
        notes.append(f"{rt.unit_kind}s {rt.unreadable} have content docbridge cannot read; they were NOT searched, "
                     "so no match there is not evidence of absence")
    if rt.image_units:
        notes.append(f"pages {rt.image_units} carry raster images; text inside images was NOT searched")
    return {"pattern": pattern, "case_sensitive": case_sensitive, "matches": matches, "matches_total": total,
            "matches_returned": len(matches),
            "next_start_match": returned_until if returned_until < total else None,
            "unsearchable_units": list(rt.unreadable), "notes": notes}


# ---------------------------------------------------------------- outline ---

def outline(rt: ReadingText, max_items: int) -> dict:
    """A structural map drawn from what the file itself encodes. Nothing is
    inferred: a PDF's headings are its bookmarks, or none."""
    canon = rt.canonical if rt.canonical is not None else (
        extract(rt.path) if rt.format != "txt" else None)
    truncated: dict[str, int] = {}

    def cap(name: str, items: list) -> list:
        if len(items) > max_items:
            truncated[name] = len(items) - max_items
            return items[:max_items]
        return items

    def unit_of(block_index: int) -> int | None:
        b = canon.blocks[block_index]
        if rt.format == "docx":
            return block_index + 1
        if rt.format == "md":
            return b.line
        return b.page

    out: dict = {"unit_kind": rt.unit_kind, "units_total": len(rt.units), "reading_chars": len(rt.full)}
    notes: list[str] = []
    if canon is None:
        out.update(headings=[], tables=[])
    elif rt.format == "pdf":
        out["headings"] = cap("headings", [{"level": e[0], "title": e[1], "unit": e[2]} for e in (canon.outline or [])])
        out["headings_source"] = "the PDF's bookmarks"
        if not canon.outline:
            notes.append("the PDF has no bookmarks; the outline infers no headings (pdf_to_markdown does, "
                         "and labels them inferred)")
        out["tables"] = []
        notes.append("PDF tables are not listed: a PDF does not encode tables")
        out["pages"] = cap("pages", [{"page": p.number, "chars": p.chars, "text_layer": p.text_layer}
                                     for p in canon.pages or []])
    else:
        blocks = canon.blocks
        heads = [(i, b) for i, b in enumerate(blocks) if b.kind.value == "heading"]
        out["headings"] = cap("headings", [{"level": b.level, "title": b.text(), "unit": unit_of(i)} for i, b in heads])
        out["headings_source"] = "the document's own heading markup/styles"
        tables = []
        for t in canon.tables:
            first = next((i for i, b in enumerate(blocks) if b.table == t.index), None)
            tables.append({"index": t.index + 1, "rows": t.rows, "cols": t.cols,
                           "unit": unit_of(first) if first is not None else None})
        out["tables"] = cap("tables", tables)
    if canon is not None:
        out["counts"] = {"headings": len(out["headings"]) + truncated.get("headings", 0),
                         "tables": len(canon.tables), "links": len(canon.links), "images": len(canon.images),
                         "blocks": len(canon.blocks)}
        out["warnings"] = sorted({w.code for w in canon.warnings})
    out["unreadable_units"] = list(rt.unreadable)
    out["image_units"] = list(rt.image_units)
    out["truncated"] = truncated
    out["notes"] = notes
    return out
