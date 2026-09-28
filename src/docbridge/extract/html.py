"""Raw HTML inside Markdown: ONE reader, ONE declared domain.

The round-2 audit (2026-09-26) found three ways raw HTML escaped comparison, and
all three had the same cause: two HTML readers (a block walker and a table
reader) with different coverage, and a regular expression standing in for
attribute parsing. A link in a table cell was never recorded (R1); a decoy
attribute such as data-href="same" was taken for the destination (R2); a
table caption was never read (R3). This module replaces both readers.

THE DECLARED DOMAIN - what a browser shows, as far as docbridge models it:

    read as text      the elements in KNOWN: text nodes in document order;
                      block elements separate lines
    headings          <h1>..<h6>: heading blocks with their level (round 3, N1)
    emphasis          <strong>/<b> bold, <em>/<i> italic: emphasis spans, one
                      per paragraph, as in Markdown and DOCX (round 3, N1)
    lists             <ul>/<ol>/<li>: list items with their nesting depth and
                      ordered/bulleted (round 3, N1); <ol> numbering rendered
                      "1.", "2." ... from `start`; any `type`, `reversed` or
                      <li value> -> UNKNOWN
    links             <a href>, the attribute parsed by lxml (never a pattern)
    image alt text    <img alt>, as Origin.IMAGE_ALT, like a Markdown image
    tables            captions and cells, in place, with rowspan/colspan, and
                      headings, emphasis and lists inside them; content in a
                      table outside every cell and caption -> UNKNOWN; a table
                      inside a cell is flattened into that cell, and its own
                      structure is a gap the tables check cannot pass over
    not rendered      script, style, template, head, title, comments
    UNKNOWN           every element outside KNOWN, with or without text: svg,
                      math, iframe, object, embed, canvas, video, audio, form
                      and its controls, base, custom elements, anything
                      unlisted. Its content is never dropped and never read as
                      absent; it makes PASS impossible, for the text and for
                      every structure check

Not modelled, declared: CSS (generated content, list-style, display:none, the
bold or italic a stylesheet gives <th>, <cite> or a heading), the quotation
marks a <q> adds, tooltips (title attributes), whether a <details> is open,
formatting beyond bold and italic (as in DOCX), definition lists (<dl> reads as
paragraphs), and every attribute except href and alt. Each of these can only
make docbridge read MORE text than a browser shows, never less.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import lxml.html

from ..canonical import Block, BlockKind, Cell, Emphasis, Link, Origin, Segment, Table
from ..contract import Reason

NOT_RENDERED = frozenset({"script", "style", "template", "head", "title"})
BLOCK = frozenset({
    "address", "article", "aside", "blockquote", "body", "center", "details", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hgroup", "hr", "html", "legend", "li", "main", "nav", "noscript", "p", "pre", "section", "summary",
    "ul", "caption", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
})
INLINE = frozenset({
    "a", "abbr", "acronym", "b", "bdi", "bdo", "big", "button", "cite", "code", "data", "del", "dfn", "em",
    "font", "i", "ins", "kbd", "label", "mark", "nobr", "q", "rb", "rp", "rt", "rtc", "ruby", "s", "samp",
    "small", "span", "strike", "strong", "sub", "sup", "time", "tt", "u", "var", "wbr", "picture", "source",
    "colgroup", "col",
})
SPECIAL = frozenset({"img", "br", "ol"})
KNOWN = BLOCK | INLINE | SPECIAL
TABLE_ROW_GROUPS = frozenset({"thead", "tbody", "tfoot"})
TABLE_STRUCTURE = TABLE_ROW_GROUPS | {"tr", "colgroup", "col"}
EMPHASIS = {"strong": "strong", "b": "strong", "em": "em", "i": "em"}
HEADINGS = {f"h{n}": n for n in range(1, 7)}
LISTS = {"ul": False, "ol": True}
# The structure check each structural tag feeds. Inline - one tag in a
# paragraph - a heading, list or table cannot be modelled, so its check
# cannot pass (Markdown reader, inline path).
STRUCTURE_OF = {**{h: "headings" for h in HEADINGS}, "ul": "lists", "ol": "lists", "li": "lists",
                **{t: "tables" for t in ("table", "caption", "thead", "tbody", "tfoot", "tr", "td", "th")}}
STRUCTURE_CHECKS = ("headings", "lists", "tables", "emphasis")

_TAG_NAME = re.compile(r"^<\s*(/)?\s*([A-Za-z][A-Za-z0-9:-]*)")


def tag_name(node) -> str | None:
    return node.tag.lower() if isinstance(node.tag, str) else None


def parse_inline_tag(s: str) -> tuple[str, str | None, dict]:
    """Classify one inline HTML token from markdown-it: ("comment"|"close"|
    "open"|"other", tag name, attributes). Attributes come from lxml's parser,
    so ordering, quoting, custom names and decoy content cannot fool it."""
    s = s.strip()
    if s.startswith("<!--") or s.startswith("<?") or s.startswith("<!"):
        return "comment", None, {}
    m = _TAG_NAME.match(s)
    if not m:
        return "other", None, {}
    name = m.group(2).lower()
    if m.group(1):
        return "close", name, {}
    try:
        root = lxml.html.fragment_fromstring(f"{s}</{name}>", create_parent="div")
        el = next((c for c in root if tag_name(c) == name), None)
        attrs = dict(el.attrib) if el is not None else {}
    except Exception:  # lxml raises several types on hostile input
        return "other", name, {}
    return "open", name, attrs


@dataclass
class _Unit:
    """One future block - a paragraph, a heading or a list item - with the
    emphasis spans that closed inside it."""
    kind: BlockKind
    level: int | None = None
    depth: int | None = None
    ordered: bool | None = None
    segs: list[Segment] = field(default_factory=list)
    emphasis: list[tuple[str, str]] = field(default_factory=list)

    def empty(self) -> bool:
        return not any(s.text.strip() or s.origin is Origin.UNKNOWN for s in self.segs)


class HtmlReader:
    """Reads an lxml element's CONTENT into the canonical form of a Markdown
    reader (`md`: its canonical `c`, `new_block`, `page`, `lists`). Text is
    gathered into units - paragraphs, headings, list items - that become
    blocks: in block mode as they stand; in a table cell or caption with the
    cell's coordinates, a cell's paragraphs as TABLE_CELL blocks, as the DOCX
    reader does. Audit round 3 (N1, 2026-09-26): headings and bold were read
    as plain paragraphs, so the structure checks compared zero items and
    passed; they are structure here, in every context."""

    def __init__(self, md, tok, cell_mode: bool = False, inherit: HtmlReader | None = None):
        self.md, self.c, self.tok, self.cell_mode = md, md.c, tok, cell_mode
        self.units: list[_Unit] = [_Unit(BlockKind.PARAGRAPH)]
        self.links: list[list] = []
        # The open lists (ordered?) and emphasis elements. A table's cells
        # inherit both: a <b> around a table makes every cell bold.
        self.lists: list[bool] = list(inherit.lists) if inherit else []
        self.active: dict[str, int] = dict(inherit.active) if inherit else {"strong": 0, "em": 0}
        self.spans: dict[str, list[str] | None] = {k: ([] if n else None) for k, n in self.active.items()}

    # -- output -----------------------------------------------------------------

    def text(self, s: str, origin: Origin = Origin.CONTENT) -> None:
        if not s:
            return
        for k, part in enumerate(s.split("\n")):
            if k:
                self._newline()
            if part:
                self.units[-1].segs.append(Segment(part, origin))
                if origin is Origin.CONTENT:
                    for parts in self.spans.values():
                        if parts is not None:
                            parts.append(part)
        if origin is Origin.CONTENT:        # a link's own text, as for [text](url)
            for col in self.links:
                col[1].append(s)

    def _newline(self) -> None:
        """A line break: inside a heading or a list item it stays in that unit;
        anywhere else it starts a new paragraph."""
        unit = self.units[-1]
        if unit.kind is BlockKind.PARAGRAPH:
            self._open(BlockKind.PARAGRAPH)
            return
        unit.segs.append(Segment("\n"))
        for parts in self.spans.values():
            if parts is not None:
                parts.append("\n")

    def _open(self, kind: BlockKind, **kw) -> None:
        """End the current unit, with its emphasis spans, and start one of `kind`."""
        self._close_spans()
        cur = self.units[-1]
        if cur.kind is BlockKind.PARAGRAPH and cur.empty() and not cur.emphasis:
            self.units[-1] = _Unit(kind, **kw)
        else:
            self.units.append(_Unit(kind, **kw))
        self.spans = {k: ([] if n else None) for k, n in self.active.items()}

    def _close_spans(self) -> None:
        for kind, parts in self.spans.items():
            if parts is not None:
                text = " ".join("".join(parts).split())
                if text:
                    self.units[-1].emphasis.append((kind, text))
        self.spans = {k: None for k in self.spans}

    def _emphasis(self, kind: str, delta: int) -> None:
        """Bold or italic opens (+1) or closes (-1). A span is the text of one
        unit, so a span crossing a paragraph break is two spans - as in DOCX,
        whose emphasis spans never cross paragraphs."""
        self.active[kind] += delta
        if delta > 0 and self.active[kind] == 1:
            self.spans[kind] = []
        elif delta < 0 and self.active[kind] == 0:
            parts = self.spans[kind]
            text = " ".join("".join(parts or []).split())
            if text:
                self.units[-1].emphasis.append((kind, text))
            self.spans[kind] = None

    def unknown(self, what: str, reason: str = Reason.UNRESOLVED_CONTENT.value, structure: bool = True) -> None:
        """A sentinel in the text; and, for an element docbridge does not model,
        structure it may hold that no structure check can certify."""
        self.units[-1].segs.append(Segment("", Origin.UNKNOWN, reason))
        where = f"line {self.tok.map[0] + 1}" if self.tok is not None and self.tok.map else None
        self.c.warn("md_html_unreadable", f"{what}: its content is UNKNOWN", where)
        if structure:
            for check in STRUCTURE_CHECKS:
                self.c.structure_gap(check, f"{what} ({where or 'raw HTML'})")

    def _tail(self, node) -> None:
        if node.tail:
            self.text(node.tail)

    def materialize(self, paragraph_kind: BlockKind, locator: str | None = None, keep_one: bool = False,
                    **coords) -> list[Block]:
        """Turn the units into blocks, and their emphasis into the canonical
        emphasis list, in document order."""
        self._close_spans()
        blocks: list[Block] = []
        for u in self.units:
            if u.empty() and u.kind is not BlockKind.LIST_ITEM:
                continue
            kind = paragraph_kind if u.kind is BlockKind.PARAGRAPH else u.kind
            b = self.md.new_block(kind, self.tok, level=u.level, list_depth=u.depth, ordered=u.ordered, **coords)
            if locator:
                b.locator = locator
            b.segments.extend(u.segs)
            self.c.emphasis.extend(Emphasis(k, t, len(self.c.blocks) - 1) for k, t in u.emphasis)
            blocks.append(b)
        if not blocks and keep_one:
            b = self.md.new_block(paragraph_kind, self.tok, **coords)
            if locator:
                b.locator = locator
            blocks.append(b)
        self.units = [_Unit(BlockKind.PARAGRAPH)]
        self.spans = {k: ([] if n else None) for k, n in self.active.items()}
        return blocks

    def flush(self) -> None:
        """Block mode: the units read so far become blocks."""
        self.materialize(BlockKind.PARAGRAPH)

    # -- walking ----------------------------------------------------------------

    def run(self, root) -> None:
        """Read the content of `root` (not the element itself)."""
        if root.text:
            self.text(root.text)
        for child in root:
            self.walk(child)

    def walk(self, node) -> None:
        tag = tag_name(node)
        if tag is None or tag in NOT_RENDERED:          # comments, processing instructions
            self._tail(node)
            return
        if tag == "table":
            if self.cell_mode:
                self.c.warn("md_nested_html_table", "a table inside a table cell was flattened into that "
                                                    "cell; its own rows and cells are not compared")
                self.c.structure_gap("tables", "a table inside an HTML table cell")
                self.text("\n")
                self.run(node)
                self.text("\n")
            else:
                self.table(node)
            self._tail(node)
            return
        if tag == "img":        # like a Markdown image: the alt text, in place
            alt = node.get("alt") or ""
            self.text(alt, Origin.IMAGE_ALT)
            self.c.images.append({"src": node.get("src") or "", "alt": alt, "html": True})
            self._tail(node)
            return
        if tag == "br":
            self.text("\n")
            self._tail(node)
            return
        if tag in LISTS:
            self.list_(node, LISTS[tag])
            self._tail(node)
            return
        if tag == "li":         # an item outside any list still renders as a bulleted item
            self.item(node, False, None)
            self._tail(node)
            return
        if tag not in KNOWN:
            self.unknown(f"an unmodelled <{tag}> element")
            self._tail(node)
            return
        if tag in HEADINGS:
            self._open(BlockKind.HEADING, level=HEADINGS[tag])
            self.run(node)
            self._open(BlockKind.PARAGRAPH)
            self._tail(node)
            return
        link = None
        if tag == "a" and node.get("href") is not None:
            link = [node.get("href"), []]
            self.links.append(link)
        emphasis = EMPHASIS.get(tag)
        if emphasis:
            self._emphasis(emphasis, +1)
        block = tag in BLOCK
        if block:
            self.text("\n")
        self.run(node)
        if block:
            self.text("\n")
        if emphasis:
            self._emphasis(emphasis, -1)
        if link is not None:
            self.links = [col for col in self.links if col is not link]    # this one, not an equal one
            self.c.links.append(Link(link[0], " ".join("".join(link[1]).split()), len(self.c.blocks),
                                     self.md.page))
        self._tail(node)

    def list_(self, node, ordered: bool) -> None:
        """<ul> and <ol>. An <ol> is numbered "N." from `start`, like a browser's
        default decimal list; anything that changes the numbering docbridge does
        not model -> UNKNOWN (the items are still read, and still list items)."""
        items = [c for c in node if tag_name(c) == "li"]
        n, supported = 1, True
        if ordered:
            supported = node.get("type") in (None, "1") and node.get("reversed") is None \
                and all(li.get("value") is None for li in items)
            if node.get("start") is not None:
                try:
                    n = int(node.get("start"))
                except ValueError:
                    supported = False
            if not supported:
                self.unknown("an <ol> whose numbering docbridge does not model (type, reversed or li value)",
                             structure=False)
        self._open(BlockKind.PARAGRAPH)
        self.lists.append(ordered)
        if node.text:
            self.text(node.text)
        for child in node:
            if tag_name(child) == "li":
                self.item(child, ordered, f"{n}." if ordered and supported else None)
                self._tail(child)
                n += 1
            else:
                self.walk(child)
        self.lists.pop()
        self._open(BlockKind.PARAGRAPH)

    def item(self, li, ordered: bool, label: str | None) -> None:
        """One list item: depth counts the Markdown lists around the HTML too."""
        depth = len(self.md.lists) + max(len(self.lists), 1) - 1
        self._open(BlockKind.LIST_ITEM, depth=depth, ordered=ordered)
        if label:
            self.text(label, Origin.LIST_LABEL)
            self.text(" ")
        self.run(li)
        self._open(BlockKind.PARAGRAPH)

    # -- tables -----------------------------------------------------------------

    def _read(self, el, paragraph_kind: BlockKind, locator: str, keep_one: bool, **coords) -> list[Block]:
        sub = HtmlReader(self.md, self.tok, cell_mode=True, inherit=self)
        sub.run(el)
        return sub.materialize(paragraph_kind, locator=locator, keep_one=keep_one, **coords)

    def table(self, node) -> None:
        self.flush()
        index = len(self.c.tables)
        captions = [c for c in node if tag_name(c) == "caption"]
        rows = _rows(node)
        read = set(captions) | {el for tr in rows for el in tr if tag_name(el) in ("td", "th")}
        for cap in captions:
            self._read(cap, BlockKind.PARAGRAPH, f"table {index + 1} caption", False)
        occupied: set[tuple[int, int]] = set()
        cells: list[Cell] = []
        for r, tr in enumerate(rows):
            c = 0
            for cell_el in tr:
                if tag_name(cell_el) not in ("td", "th"):
                    continue
                while (r, c) in occupied:
                    c += 1
                rs, cs = _span(cell_el.get("rowspan")), _span(cell_el.get("colspan"))
                for dr in range(rs):
                    for dc in range(cs):
                        occupied.add((r + dr, c + dc))
                blocks = self._read(cell_el, BlockKind.TABLE_CELL, f"table {index + 1} r{r + 1}c{c + 1}", True,
                                    table=index, row=r, col=c)
                cells.append(Cell(r, c, " ".join("\n".join(b.text() for b in blocks).split()), rs, cs))
                c += cs
        if (node.text or "").strip() or _unread(node, read):
            b = self.md.new_block(BlockKind.UNREADABLE, self.tok)
            b.locator = f"table {index + 1}"
            b.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
            self.c.warn("md_html_unreadable", "content inside a <table> that no cell or caption holds: "
                                              "its place in the rendering is UNKNOWN", b.locator)
            self.c.structure_gap("tables", f"content outside every cell of {b.locator}")
        cols = max((cl.col + cl.colspan for cl in cells), default=0)
        self.c.tables.append(Table(index, len(rows), cols, cells, page=self.md.page, source="html"))


def _rows(table) -> list:
    """The table's own rows: direct <tr> children and those in its row groups."""
    out = []
    for child in table:
        tag = tag_name(child)
        if tag == "tr":
            out.append(child)
        elif tag in TABLE_ROW_GROUPS:
            out.extend(g for g in child if tag_name(g) == "tr")
    return out


def _span(v) -> int:
    try:
        n = int(v)
        return n if 1 <= n <= 1000 else 1
    except (TypeError, ValueError):
        return 1


def _unread(el, read: set) -> bool:
    """True if anything under `el` is neither the table's own structure nor
    inside a caption or cell that was read: stray text, a row inside a row, an
    element where only cells belong."""
    for child in el:
        tag = tag_name(child)
        if child in read or tag is None or tag in NOT_RENDERED:
            pass
        elif tag in TABLE_STRUCTURE:
            if (child.text or "").strip() or _unread(child, read):
                return True
        else:
            return True
        if (child.tail or "").strip():
            return True
    return False
