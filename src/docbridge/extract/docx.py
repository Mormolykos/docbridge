"""DOCX, read directly from its OOXML parts with lxml.

python-docx is only used to WRITE (txt_to_docx). Reading needs control over
every construct that can hide or duplicate text, and each one has a declared
rule here - a construct not listed is never silently skipped: if it contains
text, it becomes an UNKNOWN block and a warning.

Declared reading rules (VALIDATION_MODEL.md repeats them):

  tracked changes     insertions (w:ins, w:moveTo) kept, deletions (w:del,
                      w:moveFrom) dropped: the document as if all accepted
  fields              the cached result is read, the instruction is not
  content controls    their content is read (w:sdt/w:sdtContent)
  AlternateContent    only mc:Choice is read; mc:Fallback repeats the same
                      text box for older readers and would double it
  hidden text         runs with w:vanish are excluded and counted
  list labels         computed from numbering.xml (decimal, letters, roman);
                      any other number format is UNKNOWN, never guessed
  footnotes/endnotes  references render "[n]", numbered in reference order
                      across both kinds; bodies follow the main text
  headers/footers     read as FURNITURE; never compared (reported with counts)
  images              wp:docPr/@descr is the alt text; pixels are not compared
  ruby (w:ruby)       the base text, then the guide text Word shows above it,
                      as Origin.RUBY: compared in every projection
  WordArt (VML)       v:textpath/@string is read like a text box
  equations, OLE objects, ActiveX controls, altChunk, subdocuments, ink
  (contentPart), date elements, legacy check-box and drop-down form fields,
  and any text a drawing holds outside its text boxes -> UNKNOWN
  any other vocabulary that holds text, anywhere      -> UNKNOWN
"""
from __future__ import annotations

import posixpath
import re
import zipfile
from pathlib import Path

from lxml import etree

from ..canonical import Block, BlockKind, Canonical, Cell, Emphasis, Link, Origin, Table
from ..contract import ErrorCode, Reason
from ..errors import DocbridgeError

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
V = "urn:schemas-microsoft-com:vml"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
STRICT_W = "http://purl.oclc.org/ooxml/wordprocessingml/main"

REL_OFFICE_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"

MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
REFUSED_TYPES = {
    "application/vnd.ms-word.document.macroEnabled.main+xml": "a macro-enabled document (.docm)",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml": "a template (.dotx)",
    "application/vnd.ms-word.template.macroEnabledTemplate.main+xml": "a macro-enabled template (.dotm)",
}

MAX_UNZIPPED_BYTES = 1024 * 1024 * 1024
MAX_RATIO = 200


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _ln(el) -> tuple[str, str]:
    """(namespace, localname) of an element; ("", "") for anything else."""
    tag = el.tag
    if not isinstance(tag, str):
        return "", ""
    if tag[0] == "{":
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                          huge_tree=False, remove_comments=True, remove_pis=True)

_ON = {"true", "1", "on"}
_OFF = {"false", "0", "off", "none"}


def _toggle(el) -> bool | None:
    """w:b / w:i / w:vanish: absent -> None, present -> True unless val says off."""
    if el is None:
        return None
    v = el.get(w("val"))
    if v is None:
        return True
    return v.lower() not in _OFF


# ------------------------------------------------------------------ package ---

class _Package:
    def __init__(self, path: Path):
        try:
            self.zip = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise DocbridgeError(ErrorCode.MALFORMED_INPUT, "not a valid DOCX (zip) package",
                                 "The file is damaged, or is not a Word 2007+ document.") from None
        infos = self.zip.infolist()
        total = sum(i.file_size for i in infos)
        if total > MAX_UNZIPPED_BYTES:
            raise DocbridgeError(ErrorCode.INPUT_TOO_LARGE,
                                 f"the package unpacks to {total} bytes", "Refused as a possible zip bomb.")
        for i in infos:
            if i.flag_bits & 0x1:
                raise DocbridgeError(ErrorCode.ENCRYPTED_INPUT, "the package has encrypted entries",
                                     "Remove the password in Word and save again.")
            if i.file_size > 50 * 1024 * 1024 and i.file_size > MAX_RATIO * max(i.compress_size, 1):
                raise DocbridgeError(ErrorCode.INPUT_TOO_LARGE,
                                     f"part {i.filename} expands {i.file_size // max(i.compress_size, 1)}x",
                                     "Refused as a possible zip bomb.")
        self.names = {i.filename for i in infos}
        self.main = self._main_part()

    def xml(self, name: str):
        if name not in self.names:
            return None
        try:
            return etree.fromstring(self.zip.read(name), _PARSER)
        except etree.XMLSyntaxError as e:
            raise DocbridgeError(ErrorCode.MALFORMED_INPUT, f"{name} is not well-formed XML: {e}",
                                 "The DOCX is damaged; open and re-save it in Word.") from None

    def rels(self, part: str) -> dict[str, dict]:
        d, base = posixpath.split(part)
        root = self.xml(posixpath.join(d, "_rels", base + ".rels"))
        out: dict[str, dict] = {}
        if root is None:
            return out
        for rel in root.findall(f"{{{PR}}}Relationship"):
            target = rel.get("Target", "")
            external = rel.get("TargetMode") == "External"
            out[rel.get("Id", "")] = {
                "type": rel.get("Type", ""),
                "target": target if external else _resolve(d, target),
                "external": external,
            }
        return out

    def _main_part(self) -> str:
        root_rels = self.rels("")
        main = next((r["target"] for r in root_rels.values() if r["type"] == REL_OFFICE_DOC), None)
        if not main or main not in self.names:
            raise DocbridgeError(ErrorCode.MALFORMED_INPUT, "no main document part",
                                 "The package is not a Word document.")
        ct = self.xml("[Content_Types].xml")
        ctype = None
        if ct is not None:
            for o in ct.findall(f"{{{CT}}}Override"):
                if o.get("PartName", "").lstrip("/") == main:
                    ctype = o.get("ContentType")
        if ctype in REFUSED_TYPES:
            raise DocbridgeError(ErrorCode.UNSUPPORTED_FORMAT, f"the file is {REFUSED_TYPES[ctype]}",
                                 "v0.1 reads plain .docx documents only.")
        if ctype != MAIN_TYPE:
            raise DocbridgeError(ErrorCode.UNSUPPORTED_FORMAT,
                                 f"unexpected main part content type {ctype!r}",
                                 "v0.1 reads plain .docx documents only.")
        return main


def _resolve(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        p = target.lstrip("/")
    else:
        p = posixpath.normpath(posixpath.join(base_dir, target))
    return "" if p.startswith("..") else p


# ------------------------------------------------------------------- styles ---

_HEADING_NAME = re.compile(r"^heading\s*([1-9])$", re.IGNORECASE)


class _Styles:
    def __init__(self, root):
        self.by_id: dict[str, dict] = {}
        self.default_para: str | None = None
        if root is None:
            return
        for st in root.findall(w("style")):
            sid = st.get(w("styleId"))
            if not sid:
                continue
            name_el, based_el = st.find(w("name")), st.find(w("basedOn"))
            ppr, rpr = st.find(w("pPr")), st.find(w("rPr"))
            numpr = ppr.find(w("numPr")) if ppr is not None else None
            outline = ppr.find(w("outlineLvl")) if ppr is not None else None
            self.by_id[sid] = {
                "id": sid,
                "type": st.get(w("type")),
                "name": name_el.get(w("val")) if name_el is not None else "",
                "based": based_el.get(w("val")) if based_el is not None else None,
                "outline": _int(outline.get(w("val"))) if outline is not None else None,
                "numpr": _numpr_fields(numpr),
                "bold": _toggle(rpr.find(w("b"))) if rpr is not None else None,
                "italic": _toggle(rpr.find(w("i"))) if rpr is not None else None,
                "vanish": _toggle(rpr.find(w("vanish"))) if rpr is not None else None,
            }
            if st.get(w("type")) == "paragraph" and st.get(w("default")) in ("1", "true", "on"):
                self.default_para = sid

    def chain(self, sid: str | None) -> list[dict]:
        out, seen = [], set()
        while sid and sid in self.by_id and sid not in seen and len(out) < 32:
            seen.add(sid)
            out.append(self.by_id[sid])
            sid = self.by_id[sid]["based"]
        return out

    def heading_level(self, sid: str | None) -> int | None:
        for st in self.chain(sid):
            m = _HEADING_NAME.match(st["name"] or "")
            if m:
                return int(m.group(1))
            if st["outline"] is not None and 0 <= st["outline"] <= 8:
                return st["outline"] + 1
        return None

    def numpr_fields(self, sid: str | None) -> tuple[int | None, int | None]:
        """(numId, ilvl) a paragraph style supplies, each property from the
        nearest style in the basedOn chain that sets it."""
        num = lvl = None
        for st in self.chain(sid):
            s_num, s_lvl = st["numpr"]
            num = s_num if num is None else num
            lvl = s_lvl if lvl is None else lvl
        return num, lvl

    def run_prop(self, sid: str | None, key: str) -> bool | None:
        for st in self.chain(sid):
            if st[key] is not None:
                return st[key]
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _numpr_fields(el) -> tuple[int | None, int | None]:
    """(numId, ilvl) as a w:numPr sets them; None where it does not."""
    if el is None:
        return None, None
    num, lvl = el.find(w("numId")), el.find(w("ilvl"))
    return (_int(num.get(w("val"))) if num is not None else None,
            _int(lvl.get(w("val"))) if lvl is not None else None)


# ---------------------------------------------------------------- numbering ---

_SUPPORTED_FMT = {"decimal", "decimalZero", "upperRoman", "lowerRoman", "upperLetter",
                  "lowerLetter", "bullet", "none"}


def _roman(n: int) -> str:
    if n <= 0 or n >= 4000:
        return str(n)
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for v, s in vals:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def _format(n: int, fmt: str) -> str | None:
    if fmt == "decimal":
        return str(n)
    if fmt == "decimalZero":
        return f"{n:02d}" if 0 <= n < 10 else str(n)
    if fmt in ("upperRoman", "lowerRoman"):
        r = _roman(n)
        return r if fmt == "upperRoman" else r.lower()
    if fmt in ("upperLetter", "lowerLetter"):
        if n <= 0:
            return str(n)
        letter = chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)
        return letter.upper() if fmt == "upperLetter" else letter
    return None


class _Numbering:
    """Word list numbering, as far as v0.1 declares it:

    * a num with a startOverride keeps its own counters; nums without one that
      share an abstractNum continue the same sequence (Word's behaviour);
    * numbering a level restarts every deeper level;
    * lvlText placeholders %1..%9 take each level's counter in that level's
      format (decimal when the level is isLgl);
    * any other number format makes the label UNKNOWN.
    """

    def __init__(self, root):
        self.abstract: dict[int, dict[int, dict]] = {}
        self.nums: dict[int, dict] = {}
        self.counters: dict[tuple, list] = {}
        self.unsupported_formats: set[str] = set()
        if root is None:
            return
        for a in root.findall(w("abstractNum")):
            aid = _int(a.get(w("abstractNumId")))
            if aid is None:
                continue
            levels = {}
            for lvl in a.findall(w("lvl")):
                ilvl = _int(lvl.get(w("ilvl")))
                if ilvl is not None:
                    levels[ilvl] = _level(lvl)
            if a.find(w("numStyleLink")) is not None:
                levels = {"unresolved": True}
            self.abstract[aid] = levels
        for n in root.findall(w("num")):
            nid = _int(n.get(w("numId")))
            aref = n.find(w("abstractNumId"))
            if nid is None or aref is None:
                continue
            overrides = {}
            for o in n.findall(w("lvlOverride")):
                ilvl = _int(o.get(w("ilvl")))
                so = o.find(w("startOverride"))
                lv = o.find(w("lvl"))
                overrides[ilvl] = {"start": _int(so.get(w("val"))) if so is not None else None,
                                   "level": _level(lv) if lv is not None else None}
            self.nums[nid] = {"abstract": _int(aref.get(w("val"))), "overrides": overrides}

    def level(self, num_id: int, ilvl: int) -> dict | None:
        num = self.nums.get(num_id)
        if num is None:
            return None
        ov = num["overrides"].get(ilvl)
        if ov and ov["level"] is not None:
            return ov["level"]
        levels = self.abstract.get(num["abstract"])
        if not levels or "unresolved" in levels:
            return None
        return levels.get(ilvl)

    def style_level(self, num_id: int, style_ids: list[str]) -> int | None:
        """The level of this list linked to a paragraph style (w:lvl/w:pStyle),
        trying the paragraph's own style first, then the styles it is based on."""
        num = self.nums.get(num_id)
        levels = self.abstract.get(num["abstract"]) if num is not None else None
        if not levels or "unresolved" in levels:
            return None
        for sid in style_ids:
            for ilvl, lv in sorted(levels.items()):
                if lv.get("style") == sid:
                    return ilvl
        return None

    def label(self, num_id: int, ilvl: int) -> tuple[str, str | None]:
        """(kind, text): kind is ordered | bullet | none | unknown."""
        lvl = self.level(num_id, ilvl)
        if lvl is None:
            return "unknown", None
        fmt = lvl["fmt"]
        if fmt == "bullet":
            return "bullet", None
        num = self.nums[num_id]
        has_override = any(o["start"] is not None for o in num["overrides"].values())
        key = ("num", num_id) if has_override else ("abstract", num["abstract"])
        counters = self.counters.setdefault(key, [None] * 9)
        if counters[ilvl] is None:
            ov = num["overrides"].get(ilvl)
            counters[ilvl] = ov["start"] if ov and ov["start"] is not None else lvl["start"]
        else:
            counters[ilvl] += 1
        for j in range(ilvl + 1, 9):
            counters[j] = None
        if fmt == "none":
            return "none", None
        text = lvl["text"]
        if text is None:
            return "none", None

        def sub(m: re.Match) -> str:
            k = int(m.group(1)) - 1
            lv = self.level(num_id, k) or lvl
            value = counters[k] if counters[k] is not None else lv["start"]
            f = "decimal" if lvl["legal"] else lv["fmt"]
            out = _format(value, f)
            if out is None:
                raise _Unformattable(f)
            return out

        try:
            rendered = re.sub(r"%([1-9])", sub, text)
        except _Unformattable as e:
            self.unsupported_formats.add(str(e))
            return "unknown", None
        if fmt not in _SUPPORTED_FMT:
            self.unsupported_formats.add(fmt)
            return "unknown", None
        return "ordered", rendered


class _Unformattable(Exception):
    pass


def _level(lvl) -> dict:
    def val(tag):
        el = lvl.find(w(tag))
        return el.get(w("val")) if el is not None else None
    # ECMA-376 17.9.26: an omitted w:start means zero, not one.
    return {"start": (_int(val("start")) or 0) if val("start") is not None else 0,
            "fmt": val("numFmt") or "decimal",
            "text": val("lvlText"),
            "legal": lvl.find(w("isLgl")) is not None and _toggle(lvl.find(w("isLgl"))) is not False,
            "style": val("pStyle")}


# ------------------------------------------------------------------- walker ---

_BODY_IGNORED = {"sectPr", "bookmarkStart", "bookmarkEnd", "proofErr", "permStart", "permEnd",
                 "commentRangeStart", "commentRangeEnd", "moveFromRangeStart", "moveFromRangeEnd",
                 "moveToRangeStart", "moveToRangeEnd", "customXmlInsRangeStart", "customXmlInsRangeEnd",
                 "customXmlDelRangeStart", "customXmlDelRangeEnd", "tblPr", "tblGrid", "trPr", "tcPr",
                 "pPr", "rPr", "sdtPr", "sdtEndPr", "tblPrEx"}
_RUN_IGNORED = {"rPr", "lastRenderedPageBreak", "commentReference", "annotationRef", "separator",
                "continuationSeparator", "bookmarkStart", "bookmarkEnd", "proofErr"}
_DATE_ELEMENTS = {"dayShort", "dayLong", "monthShort", "monthLong", "yearShort", "yearLong",
                  "pgNum"}


A = "http://schemas.openxmlformats.org/drawingml/2006/main"
# Every element that carries visible text in a WordprocessingML package: Word
# text, Office Math text, DrawingML text. Audit F1 (2026-09-25): checking only
# w:t let a body-level equation (m:t) vanish without a trace and pass.
_TEXT_TAGS = frozenset({w("t"), f"{{{M}}}t", f"{{{A}}}t"})
_TEXTPATH = f"{{{V}}}textpath"      # VML WordArt: the text is an attribute


def _has_text(el) -> bool:
    return any(isinstance(d.tag, str) and (d.tag in _TEXT_TAGS or (d.tag == _TEXTPATH and d.get("string")))
               for d in el.iter())


def _outside_textboxes(el) -> bool:
    """True if a drawing holds a text element anywhere but inside one of its
    own w:txbxContent (a WordArt textpath is read, so it does not count)."""
    for d in el.iter(*_TEXT_TAGS):
        a = d.getparent()
        while a is not el and _ln(a) != (W, "txbxContent"):
            a = a.getparent()
        if a is el:
            return True
    return False


# graphicData URIs whose content this reader actually reads (picture alt text,
# text boxes via w:txbxContent, and containers of those)
_READABLE_GRAPHICS = ("/drawingml/2006/picture", "/wordprocessingShape", "/wordprocessingGroup",
                      "/wordprocessingCanvas")


class _Walker:
    def __init__(self, pkg: _Package, canon: Canonical):
        self.pkg, self.c = pkg, canon
        self.styles = _Styles(pkg.xml("word/styles.xml") if "word/styles.xml" in pkg.names
                              else self._part_by_type(pkg.main, "styles"))
        self.numbering = _Numbering(self._part_by_type(pkg.main, "numbering"))
        self.rels = pkg.rels(pkg.main)
        self.part_rels = self.rels
        self.next_note = 1
        self.pending_notes: list[tuple[str, str, int]] = []
        self.origin = Origin.CONTENT
        self.note_number: int | None = None
        self.counts = {"hidden_runs": 0, "field_results": 0, "tracked_insertions": 0,
                       "tracked_deletions": 0, "textboxes": 0, "equations": 0, "ole_objects": 0,
                       "alt_chunks": 0, "date_elements": 0, "caps_runs": 0, "unknown_elements": 0,
                       "embedded_graphics": 0}
        self.deferred: list[Block] = []

    def _part_by_type(self, part: str, kind: str):
        for r in self.pkg.rels(part).values():
            if r["type"] == REL_BASE + kind and not r["external"]:
                return self.pkg.xml(r["target"])
        return None

    # -- block level ------------------------------------------------------------

    def block_level(self, el, ctx: dict) -> None:
        ns, tag = _ln(el)
        if ns == W:
            if tag == "p":
                self.paragraph(el, ctx)
                return
            if tag == "tbl":
                self.table(el, ctx)
                return
            if tag == "sdt":
                content = el.find(w("sdtContent"))
                for child in (content if content is not None else []):
                    self.block_level(child, ctx)
                return
            if tag in ("customXml", "ins", "moveTo"):
                for child in el:
                    self.block_level(child, ctx)
                return
            if tag in ("del", "moveFrom"):
                self.counts["tracked_deletions"] += 1
                return
            if tag == "altChunk":
                self.counts["alt_chunks"] += 1
                self.unknown_block(ctx, "an embedded altChunk (imported HTML/RTF/DOCX)")
                return
            if tag in _BODY_IGNORED:
                return
        if ns == MC and tag == "AlternateContent":
            for child in _choice(el):
                self.block_level(child, ctx)
            return
        if ns == M:
            # m:oMathPara / m:oMath directly in the body, a cell or a content
            # control: an equation, which v0.x cannot read - UNKNOWN, never absent
            self.counts["equations"] += 1
            self.unknown_block(ctx, "an equation (Office Math)")
            return
        if _has_text(el):
            self.counts["unknown_elements"] += 1
            self.unknown_block(ctx, f"an unrecognised element <{tag}> that contains text")

    def _numbering_of(self, ppr, sid: str | None) -> tuple[int, int] | None:
        """(numId, level) of a paragraph, or None (ECMA-376 17.3.1.19, 17.9.23):
        its own numPr overrides its style's property by property - a paragraph
        may give only the level and take the list from its style; numId 0 ends
        numbering; a list that comes from the style takes the level linked to
        that style in the numbering (w:lvl/w:pStyle), else the style's own."""
        d_num, d_lvl = _numpr_fields(ppr.find(w("numPr")) if ppr is not None else None)
        s_num, s_lvl = self.styles.numpr_fields(sid)
        num = d_num if d_num is not None else s_num
        if not num:
            return None
        if d_lvl is not None:
            lvl = d_lvl
        elif d_num is None:
            linked = self.numbering.style_level(num, [st["id"] for st in self.styles.chain(sid)])
            lvl = linked if linked is not None else s_lvl
        else:
            lvl = s_lvl
        return num, min(max(lvl or 0, 0), 8)

    def unknown_block(self, ctx: dict, what: str) -> None:
        b = Block(BlockKind.UNREADABLE, locator=f"body element {len(self.c.blocks) + 1}", **ctx.get("cell", {}))
        b.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
        self.c.blocks.append(b)
        self.c.warn("docx_unreadable", f"{what}: its text is UNKNOWN", b.locator)

    def paragraph(self, p, ctx: dict) -> None:
        ppr = p.find(w("pPr"))
        style_el = ppr.find(w("pStyle")) if ppr is not None else None
        sid = style_el.get(w("val")) if style_el is not None else self.styles.default_para
        level = self.styles.heading_level(sid)
        outline = ppr.find(w("outlineLvl")) if ppr is not None else None
        if level is None and outline is not None:
            ol = _int(outline.get(w("val")))
            level = ol + 1 if ol is not None and 0 <= ol <= 8 else None
        numpr = self._numbering_of(ppr, sid)
        para_vanish = self.styles.run_prop(sid, "vanish")

        kind = ctx.get("kind", BlockKind.PARAGRAPH)
        if level is not None and self.origin is Origin.CONTENT:
            kind = BlockKind.HEADING
        elif level is not None and self.origin is Origin.NOTE:
            # a heading inside a footnote or endnote is read but not modelled
            # (Markdown cannot carry one), so the headings check cannot pass
            # over it. Found 2026-09-27: its level changed and headings passed.
            self.c.structure_gap("headings", f"a heading inside note [{self.note_number}]")
        block = Block(kind, level=level if kind is BlockKind.HEADING else None,
                      locator=f"paragraph {len(self.c.blocks) + 1}", **ctx.get("cell", {}))
        if "cell" in ctx:
            block.locator = f"table {ctx['cell']['table'] + 1} r{ctx['cell']['row'] + 1}c{ctx['cell']['col'] + 1}"
        if numpr is not None:
            label_kind, label = self.numbering.label(*numpr)
            if kind is BlockKind.PARAGRAPH:
                block.kind = BlockKind.LIST_ITEM
            elif self.origin is not Origin.FURNITURE:
                # a list item in a table cell (nested tables included), a text
                # box, a note, or a heading: the role stays, the list role is
                # recorded too. Audit W2 (2026-09-27): only body paragraphs
                # became list items, so a list removed from a cell passed.
                block.list_item = True
            block.list_depth = numpr[1]
            block.ordered = label_kind in ("ordered", "unknown")
            if label_kind == "unknown":
                block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
            elif label:
                block.add(label, Origin.LIST_LABEL if self.origin is Origin.CONTENT else self.origin)
                block.add(" ", self.origin)
        state = {"fields": [], "links": [], "strong": None, "em": None, "vanish": para_vanish,
                 "block_index": len(self.c.blocks)}
        self.c.blocks.append(block)
        self.inline(p, block, state)
        self._close_emphasis(state, "strong")
        self._close_emphasis(state, "em")
        if not block.segments or (not block.text().strip() and not block.has_unknown()):
            if not block.segments or not (block.kind is BlockKind.LIST_ITEM or block.list_item):
                self.c.blocks.pop()
        self.c.blocks.extend(self.deferred)
        self.deferred = []

    # -- inline level -----------------------------------------------------------

    def emit(self, block: Block, st: dict, text: str, origin: Origin | None = None) -> None:
        if not text:
            return
        block.add(text, origin or self.origin)
        for link in st["links"]:
            link["parts"].append(text)

    def inline(self, el, block: Block, st: dict) -> None:
        for child in el:
            ns, tag = _ln(child)
            if ns == W:
                if tag == "r":
                    self.run(child, block, st)
                elif tag == "hyperlink":
                    rid = child.get(f"{{{R}}}id")
                    anchor = child.get(w("anchor"))
                    target = ""
                    if rid and rid in self.part_rels:
                        target = self.part_rels[rid]["target"]
                    elif anchor:
                        target = "#" + anchor
                    st["links"].append({"target": target, "parts": []})
                    self.inline(child, block, st)
                    self._close_link(st, block)
                elif tag == "fldSimple":
                    instr = child.get(w("instr")) or ""
                    self.counts["field_results"] += 1
                    target = _hyperlink_instr(instr)
                    if target is not None:
                        st["links"].append({"target": target, "parts": []})
                        self.inline(child, block, st)
                        self._close_link(st, block)
                    else:
                        self.inline(child, block, st)
                elif tag in ("ins", "moveTo"):
                    self.counts["tracked_insertions"] += 1
                    self.inline(child, block, st)
                elif tag in ("del", "moveFrom"):
                    self.counts["tracked_deletions"] += 1
                elif tag in ("smartTag", "customXml", "bdo", "dir"):
                    self.inline(child, block, st)
                elif tag == "sdt":
                    content = child.find(w("sdtContent"))
                    if content is not None:
                        self.inline(content, block, st)
                elif tag in _BODY_IGNORED or tag in _RUN_IGNORED:
                    continue
                elif tag == "subDoc":
                    # a subdocument: its text lives in another file
                    self._unknown_inline(block, "a subdocument (w:subDoc)")
                elif _has_text(child):
                    self._unknown_inline(block, f"unrecognised inline element <{tag}>")
            elif ns == MC and tag == "AlternateContent":
                self.inline(_ChoiceWrapper(_choice(child)), block, st)
            elif ns == M:
                self.counts["equations"] += 1
                block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
            elif _has_text(child):
                self._unknown_inline(block, f"an element <{tag}> from another vocabulary")

    def _unknown_inline(self, block: Block, what: str) -> None:
        self.counts["unknown_elements"] += 1
        block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
        self.c.warn("docx_unreadable", f"{what}: its text is UNKNOWN", block.locator)

    def _close_link(self, st: dict, block: Block) -> None:
        link = st["links"].pop()
        self.c.links.append(Link(link["target"], "".join(link["parts"]), st["block_index"], None))

    def _in_instruction(self, st: dict) -> bool:
        return any(f["state"] == "instr" for f in st["fields"])

    def run(self, r, block: Block, st: dict) -> None:
        rpr = r.find(w("rPr"))
        rstyle = rpr.find(w("rStyle")).get(w("val")) if rpr is not None and rpr.find(w("rStyle")) is not None else None
        bold = _toggle(rpr.find(w("b"))) if rpr is not None else None
        if bold is None:
            bold = self.styles.run_prop(rstyle, "bold")
        italic = _toggle(rpr.find(w("i"))) if rpr is not None else None
        if italic is None:
            italic = self.styles.run_prop(rstyle, "italic")
        vanish = _toggle(rpr.find(w("vanish"))) if rpr is not None else None
        if vanish is None:
            vanish = self.styles.run_prop(rstyle, "vanish")
        if vanish is None:
            vanish = st["vanish"]
        if rpr is not None and (rpr.find(w("caps")) is not None or rpr.find(w("smallCaps")) is not None):
            self.counts["caps_runs"] += 1

        for child in r:
            ns, tag = _ln(child)
            if ns == MC and tag == "AlternateContent":
                for alt in _choice(child):
                    self._run_child(alt, block, st, bold, italic, vanish)
                continue
            self._run_child(child, block, st, bold, italic, vanish)

    def _run_child(self, child, block, st, bold, italic, vanish) -> None:
        ns, tag = _ln(child)
        if ns != W:
            if ns == M:
                self.counts["equations"] += 1
                block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
            elif _has_text(child):
                self._unknown_inline(block, f"an element <{tag}> from another vocabulary inside a run")
            return
        if tag == "fldChar":
            kind = child.get(w("fldCharType"))
            if kind == "begin":
                st["fields"].append({"state": "instr", "instr": [], "link": False})
                ff = child.find(w("ffData"))
                if ff is not None and (ff.find(w("checkBox")) is not None or ff.find(w("ddList")) is not None):
                    # a legacy check box or drop-down shows a state or an entry
                    # that is stored as settings, never as text
                    self._unknown_inline(block, "a legacy check-box or drop-down form field")
            elif kind == "separate" and st["fields"]:
                f = st["fields"][-1]
                f["state"] = "result"
                self.counts["field_results"] += 1
                target = _hyperlink_instr("".join(f["instr"]))
                if target is not None:
                    st["links"].append({"target": target, "parts": []})
                    f["link"] = True
            elif kind == "end" and st["fields"]:
                f = st["fields"].pop()
                if f["link"]:
                    self._close_link(st, block)
            return
        if tag == "instrText":
            if st["fields"]:
                st["fields"][-1]["instr"].append(child.text or "")
            return
        if self._in_instruction(st) or tag in _RUN_IGNORED or tag == "delText":
            return
        text = None
        origin = None
        if tag == "t":
            text = child.text or ""
        elif tag in ("tab", "ptab"):
            text = "\t"
        elif tag in ("br", "cr"):
            text = "\n"
        elif tag == "noBreakHyphen":
            text = "‑"
        elif tag == "softHyphen":
            text = "­"
        elif tag == "sym":
            code = child.get(w("char"))
            try:
                text = chr(int(code, 16))
            except (TypeError, ValueError):
                text = None
        elif tag in ("footnoteReference", "endnoteReference"):
            n = self.next_note
            self.next_note += 1
            self.pending_notes.append((tag[:-9], child.get(w("id")) or "", n))
            text, origin = f"[{n}]", Origin.NOTE_REF
        elif tag in ("footnoteRef", "endnoteRef"):
            text, origin = (f"[{self.note_number}]" if self.note_number else ""), Origin.NOTE
        elif tag in ("drawing", "pict", "object"):
            self.drawing(child, block, st, tag)
            return
        elif tag in _DATE_ELEMENTS:
            self.counts["date_elements"] += 1
            block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
            return
        elif tag == "ruby":
            self.ruby(child, block, st)
            return
        elif tag in ("contentPart", "control"):
            # ink or an ActiveX control: what it shows is stored in another part
            self._unknown_inline(block, f"a <{tag}> element")
            return
        elif _has_text(child):
            self.counts["unknown_elements"] += 1
            block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
            self.c.warn("docx_unreadable", f"unrecognised run element <{tag}>", block.locator)
            return
        if not text:
            return
        if vanish:
            self.counts["hidden_runs"] += 1
            return
        self.emit(block, st, text, origin)
        # Any run whose bold state differs ends the span, whitespace included:
        # "**a** **b**" is two spans in Markdown, and its DOCX has a plain space
        # run between two bold ones.
        self._emphasis(st, "strong", bool(bold), text)
        self._emphasis(st, "em", bool(italic), text)

    def ruby(self, el, block: Block, st: dict) -> None:
        """Declared order: the base text, then the guide text Word displays
        above it. Audit R4 (2026-09-26): reading only w:rubyBase let a changed
        amount in the guide text (w:rt) pass."""
        base, guide = el.find(w("rubyBase")), el.find(w("rt"))
        if base is not None:
            self.inline(base, block, st)
        if guide is not None:
            saved = self.origin
            if saved is Origin.CONTENT:
                self.origin = Origin.RUBY
            self.inline(guide, block, st)
            self.origin = saved
        if any(_ln(c) not in ((W, "rubyPr"), (W, "rubyBase"), (W, "rt")) and _has_text(c) for c in el):
            self._unknown_inline(block, "text inside a w:ruby outside its base and guide text")

    def _emphasis(self, st: dict, key: str, on: bool, text: str) -> None:
        # Notes included (2026-09-27): bold removed in a footnote passed, and a
        # faithful DOCX->Markdown conversion failed, since Markdown counts it.
        if self.origin not in (Origin.CONTENT, Origin.NOTE):
            return
        if on:
            if st[key] is None:
                st[key] = []
            st[key].append(text)
        else:
            self._close_emphasis(st, key)

    def _close_emphasis(self, st: dict, key: str) -> None:
        parts = st.get(key)
        if parts:
            text = " ".join("".join(parts).split())
            if text:
                self.c.emphasis.append(Emphasis(key, text, st["block_index"]))
        st[key] = None

    def drawing(self, el, block: Block, st: dict, tag: str) -> None:
        if tag == "object":
            self.counts["ole_objects"] += 1
            block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
            self.c.warn("docx_ole_object", "an embedded OLE object: its content is UNKNOWN", block.locator)
            return
        # A drawing is read only if it is a picture, a text-box shape, a group or
        # a canvas. Anything else - a chart, a SmartArt diagram, ink - keeps its
        # text (and its numbers) in parts this reader never opens: UNKNOWN, never
        # absent. Same defect class as audit F1, closed at the domain level.
        unknown = False
        for gd in el.iter(f"{{{A}}}graphicData"):
            uri = gd.get("uri", "")
            if not uri.endswith(_READABLE_GRAPHICS):
                unknown = True
                self.counts["embedded_graphics"] += 1
                block.add("", Origin.UNKNOWN, Reason.UNRESOLVED_CONTENT.value)
                self.c.warn("docx_embedded_graphic",
                            f"an embedded graphic ({uri.rsplit('/', 1)[-1] or 'unknown type'}): its text is UNKNOWN",
                            block.locator)
        # Text is read from text boxes only. Text a readable drawing holds
        # anywhere else, and any ActiveX control, is UNKNOWN (round 2: the
        # visible-text domain, not only the construct the audit named).
        if el.find(f".//{w('control')}") is not None:
            self._unknown_inline(block, "an ActiveX control inside a drawing")
        elif not unknown and _outside_textboxes(el):
            self._unknown_inline(block, "text in a drawing outside its text boxes")
        for doc_pr in el.iter(f"{{{WP}}}docPr"):
            alt = doc_pr.get("descr") or ""
            self.c.images.append({"alt": alt, "name": doc_pr.get("name") or ""})
            if alt:
                block.add(alt, Origin.IMAGE_ALT)
                block.add(" ", self.origin)
        for shape in el.iter(f"{{{V}}}shape"):
            alt = shape.get("alt") or ""
            if alt:
                self.c.images.append({"alt": alt, "name": shape.get("id") or ""})
                block.add(alt, Origin.IMAGE_ALT)
        for tx in el.iter(w("txbxContent")):
            self.counts["textboxes"] += 1
            saved = self.c.blocks
            self.c.blocks = []
            for child in tx:
                self.block_level(child, {"kind": BlockKind.TEXTBOX})
            self.deferred.extend(self.c.blocks)
            self.c.blocks = saved
        for tp in el.iter(_TEXTPATH):
            if tp.get("string"):        # WordArt: read like a text box
                self.counts["textboxes"] += 1
                b = Block(BlockKind.TEXTBOX, locator=f"paragraph {len(self.c.blocks) + len(self.deferred) + 1}")
                b.add(tp.get("string"), self.origin)
                self.deferred.append(b)

    # -- tables -----------------------------------------------------------------

    def table(self, tbl, ctx: dict) -> None:
        if "cell" in ctx:
            # A table inside a cell: its cells' text belongs to the outer cell,
            # its own rows, cells and spans are not modelled - a gap the tables
            # check cannot pass over. Audit W1 (2026-09-27): a warning alone left
            # the check saying PASS while the nested table changed shape.
            where = f"table {ctx['cell']['table'] + 1} r{ctx['cell']['row'] + 1}c{ctx['cell']['col'] + 1}"
            self.c.warn("docx_nested_table", "a nested table was flattened into its outer cell; its own rows, "
                                             "cells and spans are not compared", where)
            self.c.structure_gap("tables", f"a table nested in the DOCX cell {where}")
            for tr in _rows(tbl):
                for tc in _cells(tr):
                    for child in tc:
                        self.block_level(child, ctx)
            return
        index = len(self.c.tables)
        cells: list[Cell] = []
        anchors: dict[int, Cell] = {}
        rows = _rows(tbl)
        width = 0
        for r, tr in enumerate(rows):
            trpr = tr.find(w("trPr"))
            before = trpr.find(w("gridBefore")) if trpr is not None else None
            c = _int(before.get(w("val"))) or 0 if before is not None else 0
            for tc in _cells(tr):
                tcpr = tc.find(w("tcPr"))
                span_el = tcpr.find(w("gridSpan")) if tcpr is not None else None
                span = max(1, _int(span_el.get(w("val"))) or 1) if span_el is not None else 1
                vm = tcpr.find(w("vMerge")) if tcpr is not None else None
                continues = vm is not None and (vm.get(w("val")) or "continue") == "continue"
                start = len(self.c.blocks)
                cell_ctx = {"kind": BlockKind.TABLE_CELL, "cell": {"table": index, "row": r, "col": c}}
                for child in tc:
                    self.block_level(child, cell_ctx)
                text = "\n".join(b.text() for b in self.c.blocks[start:] if b.kind is not BlockKind.TEXTBOX)
                if continues and c in anchors:
                    anchors[c].rowspan += 1
                    if text.strip():
                        anchors[c].text = (anchors[c].text + "\n" + text).strip("\n")
                else:
                    cell = Cell(r, c, text, 1, span)
                    cells.append(cell)
                    anchors[c] = cell
                c += span
            width = max(width, c)
        self.c.tables.append(Table(index, len(rows), width, cells, source="docx"))

    # -- notes and furniture ----------------------------------------------------

    def notes(self) -> None:
        parts = {"footnote": self._part_by_type(self.pkg.main, "footnotes"),
                 "endnote": self._part_by_type(self.pkg.main, "endnotes")}
        rel_parts = {"footnote": self._part_name(self.pkg.main, "footnotes"),
                     "endnote": self._part_name(self.pkg.main, "endnotes")}
        by_id: dict[tuple[str, str], object] = {}
        for kind, root in parts.items():
            if root is None:
                continue
            for note in root.findall(w(kind)):
                by_id[(kind, note.get(w("id")) or "")] = note
        self.origin = Origin.NOTE
        for kind, nid, n in self.pending_notes:
            note = by_id.get((kind, nid))
            if note is None:
                continue
            self.note_number = n
            self.part_rels = self.pkg.rels(rel_parts[kind]) if rel_parts[kind] else {}
            for child in note:
                self.block_level(child, {"kind": BlockKind.NOTE})
        self.note_number = None
        self.origin = Origin.CONTENT
        self.part_rels = self.rels

    def _part_name(self, part: str, kind: str) -> str | None:
        for r in self.pkg.rels(part).values():
            if r["type"] == REL_BASE + kind and not r["external"]:
                return r["target"]
        return None

    def furniture(self) -> int:
        count = 0
        self.origin = Origin.FURNITURE
        for r in self.rels.values():
            if r["type"] in (REL_BASE + "header", REL_BASE + "footer") and not r["external"]:
                root = self.pkg.xml(r["target"])
                if root is None:
                    continue
                self.part_rels = self.pkg.rels(r["target"])
                before = len(self.c.blocks)
                for child in root:
                    self.block_level(child, {"kind": BlockKind.FURNITURE})
                count += len(self.c.blocks) - before
        self.origin = Origin.CONTENT
        self.part_rels = self.rels
        return count


class _ChoiceWrapper(list):
    """Lets `inline` iterate the children of an mc:Choice like an element."""


def _choice(alt) -> list:
    choice = alt.find(f"{{{MC}}}Choice")
    if choice is None:
        choice = alt.find(f"{{{MC}}}Fallback")
    return list(choice) if choice is not None else []


def _rows(tbl) -> list:
    out = []
    for child in tbl:
        ns, tag = _ln(child)
        if ns == W and tag == "tr":
            out.append(child)
        elif ns == W and tag in ("sdt", "customXml"):
            content = child.find(w("sdtContent")) if tag == "sdt" else child
            if content is not None:
                out.extend(c for c in content if _ln(c) == (W, "tr"))
    return out


def _cells(tr) -> list:
    out = []
    for child in tr:
        ns, tag = _ln(child)
        if ns == W and tag == "tc":
            out.append(child)
        elif ns == W and tag in ("sdt", "customXml"):
            content = child.find(w("sdtContent")) if tag == "sdt" else child
            if content is not None:
                out.extend(c for c in content if _ln(c) == (W, "tc"))
    return out


_HYPERLINK = re.compile(r'^\s*HYPERLINK\s+(?:\\[a-z]\s+)*"?([^"\s]+)"?', re.IGNORECASE)


def _hyperlink_instr(instr: str) -> str | None:
    m = _HYPERLINK.match(instr or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------- entry ---

def extract_docx(path: Path, sha256: str) -> Canonical:
    pkg = _Package(path)
    root = pkg.xml(pkg.main)
    if root is None:
        raise DocbridgeError(ErrorCode.MALFORMED_INPUT, "the main document part is missing", "")
    if _ln(root)[0] == STRICT_W:
        raise DocbridgeError(ErrorCode.UNSUPPORTED_FORMAT, "Strict Open XML documents are not read by v0.1",
                             "Save the document as a standard Word Document (.docx).")
    if _ln(root) != (W, "document"):
        raise DocbridgeError(ErrorCode.MALFORMED_INPUT, "the main part is not a WordprocessingML document", "")
    body = root.find(w("body"))
    canon = Canonical(format="docx", path=str(path), sha256=sha256, size_bytes=path.stat().st_size,
                      extractor="docbridge OOXML reader (lxml over word/document.xml and its parts)")
    walker = _Walker(pkg, canon)
    if body is not None:
        for child in body:
            walker.block_level(child, {})
    walker.notes()
    furniture = walker.furniture()

    k = walker.counts
    fmts = walker.numbering.unsupported_formats
    if fmts:
        canon.warn("docx_numbering_unsupported",
                   f"list number formats {sorted(fmts)} are not rendered by v0.1; those labels are UNKNOWN")
    if k["tracked_insertions"] or k["tracked_deletions"]:
        canon.warn("docx_tracked_changes",
                   f"tracked changes present ({k['tracked_insertions']} insertions, {k['tracked_deletions']} "
                   "deletions): read as if all were accepted")
    if k["hidden_runs"]:
        canon.warn("docx_hidden_text", f"{k['hidden_runs']} hidden runs (w:vanish) excluded from the text")
    if k["field_results"]:
        canon.warn("docx_field_results",
                   f"{k['field_results']} field results compared as cached in the file; Word may recompute them")
    if k["caps_runs"]:
        canon.warn("docx_caps_formatting",
                   f"{k['caps_runs']} runs use caps/small-caps formatting; the stored letter case is compared")
    if k["textboxes"]:
        canon.warn("docx_textboxes", f"{k['textboxes']} text boxes read after their anchor paragraph; "
                                     "their position in a rendering may differ")
    canon.note_unsupported("equation", k["equations"], "OMML equations are UNKNOWN in v0.1")
    canon.note_unsupported("ole_object", k["ole_objects"], "embedded OLE objects are UNKNOWN in v0.1")
    canon.note_unsupported("embedded_graphic", k["embedded_graphics"],
                           "charts, diagrams and other non-picture graphics are UNKNOWN")
    canon.note_unsupported("alt_chunk", k["alt_chunks"], "imported altChunk content is UNKNOWN in v0.1")
    canon.note_unsupported("date_element", k["date_elements"], "date/page elements render at view time; UNKNOWN")
    canon.note_unsupported("header_footer", furniture,
                           "header/footer blocks are extracted as furniture and never compared")
    canon.note_unsupported("image", len(canon.images),
                           "images are compared by alt text only; v0.1 never compares pixels")
    comments = walker._part_by_type(pkg.main, "comments")
    if comments is not None:
        canon.note_unsupported("comment", len(comments.findall(w("comment"))),
                               "review comments are not part of the compared text")
    return canon
