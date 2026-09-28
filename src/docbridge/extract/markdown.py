"""Markdown, read as CommonMark + GFM pipe tables + strikethrough.

That dialect is the declared domain, and it is deliberately the same dialect
docbridge asks Pandoc to read and write (`commonmark+pipe_tables+strikeout`).
Syntax outside it - footnotes, task lists, emoji shortcodes, front matter - is
literal text here, exactly as it is to the converter. A checker and a converter
that disagreed about what a Markdown file says would compare two different
documents and call the result fidelity.

What the reader sees, and therefore what is compared:

* Ordered list labels are rendered the way CommonMark renders them: the first
  item's number sets the start and every later item counts up from it, whatever
  number was typed. A typed number that does not render is reported as a
  warning, because it is a number in the source that no reader will see.
* Image alt text is text (Origin.IMAGE_ALT). v0.1 never embeds images.
* Raw HTML - blocks, table cells, captions and inline tags alike - is read by
  ONE reader over ONE declared domain (extract/html.py): the text a browser
  shows, links from the parsed href attribute, and UNKNOWN for every element
  outside the domain. HTML tables keep their cell structure, including
  rowspan/colspan.
* `<!-- docbridge:page N -->` comments, written by pdf_to_markdown, restore page
  numbers; `<!-- docbridge:unreadable-page N ... -->` marks a page whose
  content was never extracted, and becomes an UNKNOWN block.
"""
from __future__ import annotations

import re
from pathlib import Path

import lxml.etree
import lxml.html
from markdown_it import MarkdownIt

from ..canonical import Block, BlockKind, Canonical, Cell, Emphasis, Link, Origin, Table
from ..contract import Reason
from . import html
from .text import decode_text

DIALECT = "CommonMark + GFM pipe tables + strikethrough (markdown-it-py 'commonmark' preset)"

# matched against a parsed comment's text
_PAGE_MARKER = re.compile(r"\s*docbridge:(page|unreadable-page)\s+(\d+)\b", re.IGNORECASE)


def parser() -> MarkdownIt:
    return MarkdownIt("commonmark").enable("table").enable("strikethrough")


class _Reader:
    def __init__(self, canon: Canonical):
        self.c = canon
        self.page: int | None = None
        self.lists: list[dict] = []
        self.item_block: Block | None = None
        self.quote_depth = 0
        self.cur: Block | None = None
        self.table: dict | None = None

    # -- blocks ---------------------------------------------------------------

    def new_block(self, kind: BlockKind, tok=None, **kw) -> Block:
        line = tok.map[0] + 1 if tok is not None and tok.map else None
        b = Block(kind, page=self.page, locator=f"line {line}" if line else None, line=line, **kw)
        self.c.blocks.append(b)
        return b

    def run(self, src: str) -> None:
        tokens = parser().parse(src)
        for tok in tokens:
            t = tok.type
            if t == "heading_open":
                self.item_block = None
                self.cur = self.new_block(BlockKind.HEADING, tok, level=int(tok.tag[1:]))
            elif t == "paragraph_open":
                if self.item_block is not None:
                    self.cur, self.item_block = self.item_block, None
                else:
                    kind = BlockKind.QUOTE if self.quote_depth else BlockKind.PARAGRAPH
                    depth = len(self.lists) - 1 if self.lists else None
                    self.cur = self.new_block(kind, tok, list_depth=depth)
            elif t in ("paragraph_close", "heading_close"):
                self.cur = None
            elif t == "inline":
                if self.cur is None:  # defensive: inline outside a known container
                    self.cur = self.new_block(BlockKind.PARAGRAPH, tok)
                self.inline(tok.children or [], self.cur, len(self.c.blocks) - 1)
            elif t == "bullet_list_open":
                self.lists.append({"ordered": False, "start": 1, "n": 0})
            elif t == "ordered_list_open":
                start = int(tok.attrs.get("start", 1)) if tok.attrs else 1
                self.lists.append({"ordered": True, "start": start, "n": 0})
            elif t in ("bullet_list_close", "ordered_list_close"):
                self.lists.pop()
                self.item_block = None
            elif t == "list_item_open":
                self.list_item(tok)
            elif t == "list_item_close":
                self.item_block = None
            elif t == "blockquote_open":
                self.quote_depth += 1
            elif t == "blockquote_close":
                self.quote_depth -= 1
            elif t in ("fence", "code_block"):
                self.item_block = None
                b = self.new_block(BlockKind.CODE, tok)
                content = tok.content[:-1] if tok.content.endswith("\n") else tok.content
                b.add(content)
            elif t == "html_block":
                self.item_block = None
                self.html_block(tok)
            elif t.startswith(("table_", "thead_", "tbody_", "tr_", "th_", "td_")):
                self.table_token(tok)
            # hr: no text. Anything else is a structural wrapper with no content.

    def list_item(self, tok) -> None:
        L = self.lists[-1]
        L["n"] += 1
        b = self.new_block(BlockKind.LIST_ITEM, tok, list_depth=len(self.lists) - 1,
                           ordered=L["ordered"])
        if L["ordered"]:
            rendered = L["start"] + L["n"] - 1
            b.add(f"{rendered}{tok.markup or '.'}", Origin.LIST_LABEL)
            b.add(" ")
            typed = tok.info
            if typed and typed.isdigit() and int(typed) != rendered:
                self.c.warn("md_list_number_not_rendered",
                            f"list item typed as {typed}{tok.markup} renders as {rendered}{tok.markup} "
                            "under CommonMark; the typed number appears in no rendering",
                            b.locator)
        self.item_block = b

    # -- tables -----------------------------------------------------------------

    def table_token(self, tok) -> None:
        t = tok.type
        if t == "table_open":
            self.item_block = None
            self.table = {"index": len(self.c.tables), "row": -1, "col": 0, "cells": [],
                          "line": tok.map[0] + 1 if tok.map else None}
        elif t == "tr_open":
            self.table["row"] += 1
            self.table["col"] = 0
            if tok.map:
                self.table["line"] = tok.map[0] + 1
        elif t in ("th_open", "td_open"):
            tb = self.table
            self.cur = self.new_block(BlockKind.TABLE_CELL, tok, table=tb["index"], row=tb["row"], col=tb["col"])
            self.cur.locator = f"table {tb['index'] + 1} r{tb['row'] + 1}c{tb['col'] + 1}"
            self.cur.line = tb["line"]      # markdown-it gives cells no map; the row's line is theirs
            tb["col"] += 1
        elif t in ("th_close", "td_close"):
            tb = self.table
            tb["cells"].append(Cell(self.cur.row, self.cur.col, self.cur.text()))
            self.cur = None
        elif t == "table_close":
            tb = self.table
            rows = tb["row"] + 1
            cols = max((c.col + 1 for c in tb["cells"]), default=0)
            self.c.tables.append(Table(tb["index"], rows, cols, tb["cells"], page=self.page,
                                       source="markdown_pipe"))
            self.table = None

    # -- raw HTML ---------------------------------------------------------------

    def html_block(self, tok) -> None:
        # Parsed whole, comments included: stripping comments with a pattern
        # first would let "<!--" inside an attribute value swallow real markup.
        try:
            root = lxml.html.fragment_fromstring(tok.content, create_parent="div")
        except Exception:  # lxml raises several types on hostile input
            b = self.new_block(BlockKind.UNREADABLE, tok)
            b.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
            self.c.warn("md_html_unparsed", "an HTML block could not be parsed; its text is UNKNOWN", b.locator)
            return
        for node in root.iter(lxml.etree.Comment):
            m = _PAGE_MARKER.match(node.text or "")
            if m is None:
                continue
            if m.group(1).lower() == "page":
                self.page = int(m.group(2))
                self.c.page_markers = (self.c.page_markers or 0) + 1
            else:
                b = self.new_block(BlockKind.UNREADABLE, tok)
                b.page = int(m.group(2))
                b.locator = f"page {b.page}"
                b.add("", Origin.UNKNOWN, Reason.NO_TEXT_LAYER.value)
        if not (root.text or "").strip() and all(html.tag_name(ch) is None and not (ch.tail or "").strip()
                                                 for ch in root):
            return      # comments only
        self.c.warn("md_raw_html", "raw HTML block read as the text a browser would render",
                    f"line {tok.map[0] + 1}" if tok.map else None)
        # One reader for blocks, tables, cells and captions, in document order.
        # Audit F6 (2026-09-25): tables pulled out first let a moved table
        # compare equal. R1-R3 (2026-09-26): a second, narrower table reader
        # lost cell links and captions.
        reader = html.HtmlReader(self, tok)
        reader.run(root)
        reader.flush()

    # -- inline -----------------------------------------------------------------

    def inline(self, children, block: Block, block_index: int) -> None:
        collectors: list[list] = []   # [kind, payload, parts]

        def emit(text: str, origin: Origin = Origin.CONTENT) -> None:
            block.add(text, origin)
            for col in collectors:
                col[2].append(text)

        for ch in children:
            t = ch.type
            if t in ("text", "code_inline"):
                emit(ch.content)
            elif t == "softbreak":
                emit("\n")
            elif t == "hardbreak":
                emit("\n")
            elif t == "html_inline":
                # The same declared domain as an HTML block (extract/html.py),
                # one tag at a time; attributes come from lxml, never a pattern
                # (audit R2: a decoy data-href was read as the destination).
                kind, name, attrs = html.parse_inline_tag(ch.content)
                emphasis = html.EMPHASIS.get(name or "")
                if kind == "open" and name == "br":
                    emit("\n")
                elif kind == "open" and name == "a":
                    if "href" in attrs:
                        collectors.append(["html_link", attrs["href"], []])
                elif kind == "open" and name == "img":
                    alt = attrs.get("alt") or ""
                    block.add(alt, Origin.IMAGE_ALT)
                    self.c.images.append({"src": attrs.get("src") or "", "alt": alt, "block": block_index,
                                          "html": True})
                elif kind == "open" and emphasis:
                    # Audit N1: <strong>/<b> and <em>/<i> are emphasis, like ** and *
                    collectors.append(["html_" + emphasis, None, []])
                elif kind == "close" and emphasis:
                    col = _pop(collectors, "html_" + emphasis)
                    if col is not None:
                        self._emphasis(emphasis, col[2], block_index)
                elif kind == "close" and name == "a":
                    col = _pop(collectors, "html_link")
                    if col is not None:
                        self.c.links.append(Link(col[1], "".join(col[2]), block_index, block.page))
                elif kind == "other" or (kind == "open" and (name not in html.KNOWN or name == "ol"
                                                             or name in html.NOT_RENDERED)):
                    # outside the inline domain: a hidden, generated or
                    # embedded rendering docbridge cannot see from one tag
                    block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
                    self.c.warn("md_html_unreadable", f"inline <{name or '?'}>: what it renders is UNKNOWN",
                                block.locator)
                    if name != "ol":        # an element docbridge does not model may hold any structure
                        for check in html.STRUCTURE_CHECKS:
                            self.c.structure_gap(check, f"inline <{name or '?'}> ({block.locator})")
                if kind == "open" and name in html.STRUCTURE_OF:
                    # a heading, list or table opened inside a paragraph: one
                    # tag cannot say what it contains, so its check cannot pass
                    self.c.structure_gap(html.STRUCTURE_OF[name], f"inline <{name}> ({block.locator})")
                # comments, closing tags and the known inline elements render no text of their own
            elif t == "image":
                alt = ch.content or ""
                block.add(alt, Origin.IMAGE_ALT)
                self.c.images.append({"src": ch.attrs.get("src", "") if ch.attrs else "", "alt": alt,
                                      "block": block_index})
            elif t == "link_open":
                collectors.append(["link", ch.attrs.get("href", "") if ch.attrs else "", []])
            elif t == "link_close":
                col = _pop(collectors, "link")
                if col is not None:
                    self.c.links.append(Link(col[1], "".join(col[2]), block_index, block.page))
            elif t in ("strong_open", "em_open"):
                collectors.append([t[:-5], None, []])
            elif t in ("strong_close", "em_close"):
                col = _pop(collectors, t[:-6])
                if col is not None:
                    text = " ".join("".join(col[2]).split())
                    if text:
                        self.c.emphasis.append(Emphasis(col[0], text, block_index))
            elif t in ("s_open", "s_close"):
                pass
            else:
                if ch.content:
                    emit(ch.content)
                    self.c.warn("md_unknown_inline", f"inline token {t!r} read as text", block.locator)
        # an <a href>, <strong> or <em> never closed in this paragraph still
        # ends with it: a link, an emphasis span
        for col in collectors:
            if col[0] == "html_link":
                self.c.links.append(Link(col[1], "".join(col[2]), block_index, block.page))
            elif col[0] in ("html_strong", "html_em"):
                self._emphasis(col[0][5:], col[2], block_index)

    def _emphasis(self, kind: str, parts: list[str], block_index: int) -> None:
        text = " ".join("".join(parts).split())
        if text:
            self.c.emphasis.append(Emphasis(kind, text, block_index))


def _pop(collectors: list[list], kind: str):
    for i in range(len(collectors) - 1, -1, -1):
        if collectors[i][0] == kind:
            return collectors.pop(i)
    return None


def extract_md(path: Path, sha256: str, encoding: str | None = None) -> Canonical:
    data = path.read_bytes()
    src = decode_text(data, encoding)
    canon = Canonical(format="md", path=str(path), sha256=sha256, size_bytes=len(data),
                      extractor=f"docbridge Markdown reader, dialect: {DIALECT}")
    _Reader(canon).run(src)
    canon.note_unsupported("image", len(canon.images),
                           "images are compared by alt text only; v0.1 never embeds or compares pixels")
    return canon
