"""Audit round 4 (2026-09-27, audit of 0.2.3): two findings, each
tested over its DOMAIN - not only at the audit's own fixtures.

W1 a nested Word table changed shape -> tables PASS
W2 a Word list in a table cell or text box removed -> lists PASS

W1's domain: tables nested in DOCX cells, at any depth, on either side, whose
rows, columns, spans or order change while the flattened text stays equal.
Property: the tables check never PASSes over a nested table it does not
model; a definite difference in a modelled table still FAILs.
W2's domain: list paragraphs in every paragraph context the reader walks -
body, table cell, nested cell, VML and DrawingML text boxes, a text box in a
cell, a table in a text box, footnotes, headings - with numbering given
directly, by style, by style with a direct level, and by a style linked to a
list level. Property: removing, re-levelling, re-typing or adding a list FAILs
the lists check; an unchanged document PASSes with every item counted; the
item keeps its cell coordinates, text-box origin, heading role and label.
DOCBRIDGE_FUZZ_N scales the generators.
"""
import copy
import os
import random

import pytest
from builders import HEADING_STYLES, minimal_docx, p, r

from docbridge import tools as T
from docbridge.canonical import BlockKind
from docbridge.extract import extract

N_CASES = int(os.environ.get("DOCBRIDGE_FUZZ_N", "300"))
SEED = 20260927


def compare(a, b):
    return T.document_compare(str(a), str(b)).report


# ================================================================ W1 ===

def tc(inner: str, span: int = 1) -> str:
    props = f'<w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>' if span > 1 else ""
    return f"<w:tc>{props}{inner}</w:tc>"


def tbl(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join("<w:tr>" + "".join(row) + "</w:tr>" for row in rows) + "</w:tbl>"


SHAPES = ["grid", "one_cell", "one_row", "one_column", "span"]


def cells_of(words: list[str], shape: str) -> list[list[str]]:
    """Table rows holding `words` in reading order, in one of several shapes -
    rows, columns and spans change, the flattened text does not."""
    if shape == "one_cell":
        return [[tc("".join(p(w) for w in words))]]
    if shape == "one_row":
        return [[tc(p(w)) for w in words]]
    if shape == "one_column":
        return [[tc(p(w))] for w in words]
    if shape == "span":
        return [[tc(p(words[0]), span=2)], [tc(p(w)) for w in words[1:]]]
    half = (len(words) + 1) // 2
    return [[tc(p(w)) for w in words[:half]], [tc(p(w)) for w in words[half:]]]


def in_first_cell(rows: list[list[str]], xml: str) -> list[list[str]]:
    """`xml` appended to the first cell, after its own paragraphs."""
    rows = [list(r) for r in rows]
    rows[0][0] = rows[0][0][:-len("</w:tc>")] + xml + p("") + "</w:tc>"
    return rows


def outer(inner_xml: str, extra_rows: int = 0) -> str:
    rows = [[tc(p("outer") + inner_xml + p(""))]] + [[tc(p(f"row{i}"))] for i in range(extra_rows)]
    return tbl(rows)


def test_w1_a_nested_table_is_never_passed_over(work):
    rng = random.Random(f"{SEED}-nested")
    for i in range(max(30, N_CASES // 10)):
        words = [f"w{rng.randrange(1000)}" for _ in range(rng.randrange(2, 6))]
        deep = [f"deep{rng.randrange(1000)}" for _ in range(rng.randrange(2, 4))] if rng.random() < 0.5 else None
        shape, deep_shape = rng.choice(SHAPES), rng.choice(SHAPES)

        def nested(sh, dsh, flat_deep=False, words=words, deep=deep):
            rows = cells_of(words, sh)
            if deep:
                rows = in_first_cell(rows, "".join(p(w) for w in deep) if flat_deep else tbl(cells_of(deep, dsh)))
            return tbl(rows)

        a_xml = outer(nested(shape, deep_shape))
        mutation = rng.choice(["reshape", "flatten_one_side", "nested_one_side", "identity"])
        if mutation == "reshape":          # the deepest nested table changes shape, the text does not
            b_xml = outer(nested(shape, rng.choice([s for s in SHAPES if s != deep_shape]))) if deep \
                else outer(nested(rng.choice([s for s in SHAPES if s != shape]), None))
        elif mutation in ("flatten_one_side", "nested_one_side"):
            flat = outer(nested(shape, deep_shape, flat_deep=True)) if deep else outer("".join(p(w) for w in words))
            a_xml, b_xml = (a_xml, flat) if mutation == "flatten_one_side" else (flat, a_xml)
        else:
            b_xml = a_xml
        a = minimal_docx(work / f"a{i}.docx", a_xml)
        b = minimal_docx(work / f"b{i}.docx", b_xml)
        rep = compare(a, b)
        tables = rep.structure_checks["tables"]
        assert rep.text_status == "PASS", (i, mutation, "the flattened text must stay equal")
        assert tables.status == "NOT_CHECKED" and tables.reason == "unresolved_content", (i, mutation, tables)
        assert tables.details["unmodelled_source"] + tables.details["unmodelled_target"] >= 1
        # a definite difference in the modelled outer table still FAILs beside the gap
        c = minimal_docx(work / f"c{i}.docx", outer(nested(shape, deep_shape), extra_rows=1))
        assert compare(minimal_docx(work / f"d{i}.docx", outer(nested(shape, deep_shape))), c) \
            .structure_checks["tables"].status == "FAIL", (i, "outer change hidden by a nested gap")


def test_w1_every_nesting_level_is_a_gap_and_top_level_tables_still_pass(work):
    two = outer(tbl([[tc(p("a") + tbl([[tc(p("b")), tc(p("c"))]]) + p(""))]]))
    c = extract(minimal_docx(work / "two.docx", two))
    assert len(c.structure_unknown["tables"]) == 2                     # one gap per nested table
    plain = tbl([[tc(p("x")), tc(p("y"))]])
    rep = compare(minimal_docx(work / "p1.docx", plain), minimal_docx(work / "p2.docx", plain))
    assert rep.structure_checks["tables"].status == "PASS"              # no nesting: nothing withheld


# ================================================================ W2 ===

BULLET, DECIMAL, LINKED = 1, 2, 3


def _levels(fmt: str, linked: bool = False) -> str:
    out = []
    for ilvl in range(3):
        text = "\u2022" if fmt == "bullet" else ".".join(f"%{k + 1}" for k in range(ilvl + 1)) + "."
        style = f'<w:pStyle w:val="{("HeadA", "HeadB", "HeadC")[ilvl]}"/>' if linked else ""
        out.append(f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>{style}'
                   f'<w:lvlText w:val="{text}"/></w:lvl>')
    return "".join(out)


NUMBERING = (f'<w:abstractNum w:abstractNumId="0">{_levels("bullet")}</w:abstractNum>'
             f'<w:abstractNum w:abstractNumId="1">{_levels("decimal")}</w:abstractNum>'
             f'<w:abstractNum w:abstractNumId="2">{_levels("decimal", linked=True)}</w:abstractNum>'
             '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
             '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
             '<w:num w:numId="3"><w:abstractNumId w:val="2"/></w:num>')


def styles(bullet_id: str, number_id: str) -> str:
    """List styles under arbitrary ids - nothing may depend on a style's name."""
    def st(sid, name, extra):
        return f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>{extra}</w:style>'
    return (st(bullet_id, f"x {bullet_id}", f'<w:pPr><w:numPr><w:numId w:val="{BULLET}"/></w:numPr></w:pPr>')
            + st(number_id, f"x {number_id}", f'<w:pPr><w:numPr><w:numId w:val="{DECIMAL}"/></w:numPr></w:pPr>')
            + st("HeadA", "heading 1", f'<w:pPr><w:numPr><w:numId w:val="{LINKED}"/></w:numPr></w:pPr>')
            + st("HeadB", "heading 2", '<w:basedOn w:val="HeadA"/>')
            + st("HeadC", "heading 3", '<w:basedOn w:val="HeadA"/>'))


def para(text: str, style: str | None = None, num: int | None = None, lvl: int | None = None) -> str:
    ppr = f'<w:pStyle w:val="{style}"/>' if style else ""
    if num is not None or lvl is not None:
        ppr += ("<w:numPr>" + (f'<w:ilvl w:val="{lvl}"/>' if lvl is not None else "")
                + (f'<w:numId w:val="{num}"/>' if num is not None else "") + "</w:numPr>")
    return f"<w:p>{'<w:pPr>' + ppr + '</w:pPr>' if ppr else ''}<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"


V = "urn:schemas-microsoft-com:vml"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"


def vml(inner: str) -> str:
    return (f'<w:p><w:r><w:pict><v:shape xmlns:v="{V}"><v:textbox><w:txbxContent>{inner}</w:txbxContent>'
            '</v:textbox></v:shape></w:pict></w:r></w:p>')


def dml(inner: str) -> str:
    return (f'<w:p><w:r><w:drawing><wp:anchor><wp:docPr id="1" name="tb"/><a:graphic xmlns:a="{A}"><a:graphicData '
            f'uri="{WPS}"><wps:wsp xmlns:wps="{WPS}"><wps:txbx><w:txbxContent>{inner}</w:txbxContent></wps:txbx>'
            '</wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r></w:p>')


CONTEXTS = {
    "body": lambda x: x,
    "cell": lambda x: tbl([[tc(x)]]),
    "nested_cell": lambda x: tbl([[tc(p("outer") + tbl([[tc(x)]]) + p(""))]]),
    "vml_textbox": vml,
    "dml_textbox": dml,
    "textbox_in_cell": lambda x: tbl([[tc(vml(x))]]),
    "table_in_textbox": lambda x: vml(tbl([[tc(x)]])),
    "footnote": None,               # the paragraphs go into footnotes.xml
}
SOURCES = ["direct", "style", "style_with_level", "linked_style"]


def item(source: str, kind: str, depth: int, text: str, ids) -> str:
    """One list paragraph: `kind` is bullet or number; depth 0..2."""
    bullet_id, number_id = ids
    if source == "direct":
        return para(text, num=BULLET if kind == "bullet" else DECIMAL, lvl=depth)
    if source == "style":
        return para(text, style=bullet_id if kind == "bullet" else number_id)       # the style's own level: 0
    if source == "style_with_level":
        return para(text, style=bullet_id if kind == "bullet" else number_id, lvl=depth)
    return para(text, style=("HeadA", "HeadB", "HeadC")[depth])                       # level linked to the style


def w2_document(work, name, ctx, paras, ids):
    xml = "".join(paras)
    if ctx == "footnote":
        return minimal_docx(work / name, p(runs='<w:r><w:t xml:space="preserve">Body </w:t></w:r>'
                                                '<w:r><w:footnoteReference w:id="1"/></w:r>'),
                            footnotes=f'<w:footnote w:id="1">{xml}</w:footnote>', numbering=NUMBERING,
                            styles=styles(*ids))
    return minimal_docx(work / name, CONTEXTS[ctx](xml), numbering=NUMBERING, styles=styles(*ids))


def test_w2_list_changes_fail_in_every_context_and_numbering_source(work):
    rng = random.Random(f"{SEED}-lists")
    seen = set()
    for i in range(max(60, N_CASES // 4)):
        ctx, source = rng.choice(list(CONTEXTS)), rng.choice(SOURCES)
        ids = (f"B{rng.randrange(10**6)}", f"N{rng.randrange(10**6)}")
        kind = "number" if source == "linked_style" else rng.choice(["bullet", "number"])
        depths = [0 if source == "style" else rng.randrange(0, 3) for _ in range(rng.randrange(1, 4))]
        texts = [f"item{rng.randrange(1000)}" for _ in depths]
        paras = [item(source, kind, d, t, ids) for d, t in zip(depths, texts, strict=True)] + [para("plain tail")]
        mutated = list(paras)
        k = rng.randrange(len(depths))
        mutation = rng.choice(["remove", "depth", "type", "add"])
        if mutation == "remove":
            mutated[k] = para(texts[k])
        elif mutation == "depth":
            d = depths[k]
            if source == "style":
                mutated[k] = para(texts[k], style=ids[0] if kind == "bullet" else ids[1], lvl=1)
            else:
                mutated[k] = item(source, kind, (d + 1) % 3, texts[k], ids)
        elif mutation == "type":
            if source == "linked_style":
                mutated[k] = para(texts[k], style=("HeadA", "HeadB", "HeadC")[depths[k]], num=BULLET,
                                  lvl=depths[k])
            else:
                mutated[k] = item(source, "number" if kind == "bullet" else "bullet", depths[k], texts[k], ids)
        else:
            mutated[-1] = item(source, kind, 0, "plain tail", ids)
        a = w2_document(work, f"a{i}.docx", ctx, paras, ids)
        b = w2_document(work, f"b{i}.docx", ctx, mutated, ids)
        same = w2_document(work, f"s{i}.docx", ctx, paras, ids)
        lists = compare(a, b).structure_checks["lists"]
        assert lists.status == "FAIL", (i, ctx, source, kind, mutation, depths, lists)
        control = compare(a, same).structure_checks["lists"]
        assert control.status in ("PASS", "NOT_CHECKED"), (i, ctx, source, control)
        assert control.details["source_count"] == len(depths), (i, ctx, source, control.details)
        seen.add((ctx, source))
    assert {c for c, _ in seen} == set(CONTEXTS) and {s for _, s in seen} == set(SOURCES)


@pytest.mark.parametrize("ctx", list(CONTEXTS))
def test_w2_a_list_item_keeps_its_other_role_and_its_label(work, ctx):
    ids = ("Bx", "Nx")
    doc = w2_document(work, "r.docx", ctx, [item("direct", "number", 0, "first", ids),
                                            item("direct", "number", 1, "second", ids)], ids)
    items = extract(doc).list_items()
    assert [(b.list_depth, b.ordered) for b in items] == [(0, True), (1, True)], ctx
    assert [b.text().split()[0] for b in items] == ["1.", "1.1."], ctx                  # readable labels
    for b in items:
        if ctx in ("cell", "nested_cell", "table_in_textbox"):
            assert b.kind is BlockKind.TABLE_CELL and (b.table, b.row, b.col) != (None, None, None), ctx
        elif ctx in ("vml_textbox", "dml_textbox", "textbox_in_cell"):
            assert b.kind is BlockKind.TEXTBOX, ctx
        elif ctx == "footnote":
            assert b.kind is BlockKind.NOTE, ctx
        else:
            assert b.kind is BlockKind.LIST_ITEM, ctx


def test_w2_a_numbered_heading_is_a_heading_and_a_list_item(work):
    ids = ("Bx", "Nx")
    doc = w2_document(work, "h.docx", "body", [para("Scope", style="HeadA"), para("Terms", style="HeadB")], ids)
    c = extract(doc)
    assert [(b.level, b.text()) for b in c.headings()] == [(1, "1. Scope"), (2, "1.1. Terms")]
    assert [(b.list_depth, b.ordered) for b in c.list_items()] == [(0, True), (1, True)]


def test_w2_numbering_resolution_follows_the_spec(work):
    """A paragraph's own numPr overrides its style's property by property, and
    numId 0 ends numbering."""
    ids = ("Bx", "Nx")
    doc = w2_document(work, "n.docx", "body", [
        para("a", style="Nx"),                  # the style's list, the style's level 0
        para("b", style="Nx", lvl=2),           # the style's list, the paragraph's level
        para("c", style="Nx", num=0),           # numbering switched off by the paragraph
        para("d", style="HeadC"),               # a list linked to the style: its level, 2
    ], ids)
    items = extract(doc).list_items()
    assert [(b.text().split()[-1], b.list_depth) for b in items] == [("a", 0), ("b", 2), ("d", 2)]


@pytest.mark.parametrize("where", ["cell", "textbox", "cell_level_2"])
def test_w2_word_template_list_styles_like_audit_fixtures(work, where):
    """Documents made the way Word's own template makes them (python-docx):
    "List Bullet" / "List Bullet 2" / "List Number" styles."""
    from docx import Document
    from docx.oxml import parse_xml

    def make(name, styled: bool):
        d = Document()
        style = ("List Bullet 2" if where == "cell_level_2" else "List Number") if styled else None
        if where == "textbox":
            template = d.add_paragraph("Alpha", style=style)
            inner = copy.deepcopy(template._p)
            template._p.getparent().remove(template._p)
            run = d.add_paragraph("Box:").add_run()
            pict = parse_xml(f'<w:pict xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                             f'xmlns:v="{V}"><v:shape><v:textbox><w:txbxContent/></v:textbox></v:shape></w:pict>')
            pict.find(".//{http://schemas.openxmlformats.org/wordprocessingml/2006/main}txbxContent").append(inner)
            run._r.append(pict)
        else:
            cell = d.add_table(rows=1, cols=1).cell(0, 0)
            cell.add_paragraph("Alpha", style=style)
            cell.add_paragraph("Beta", style=style)
        d.save(work / name)
        return work / name

    a, b = make("a.docx", True), make("b.docx", False)
    assert compare(a, b).structure_checks["lists"].status == "FAIL", where
    control = compare(a, make("c.docx", True)).structure_checks["lists"]
    assert control.status == "PASS" and control.details["source_count"] >= 1, where


# ========================================================= footnotes ===
# Found 2026-09-27 in the round-4 coverage review: emphasis and headings were
# never read inside footnotes, so bold removed there, or a note heading's level
# changed, passed.

def fn_doc(path, note_xml: str, *, body_bold: bool = True, styles: str | None = None):
    body = p(runs=r("Loud", bold=body_bold) + '<w:r><w:footnoteReference w:id="1"/></w:r>')
    return minimal_docx(path, body, footnotes=f'<w:footnote w:id="1">{note_xml}</w:footnote>', styles=styles)


FORMATS = ["", "b", "i", "bi"]


def test_every_emphasis_change_inside_a_footnote_fails(work):
    rng = random.Random(20260927)
    for case in range(40):
        fmts = [rng.choice(FORMATS) for _ in range(4)]
        k = rng.randrange(4)
        changed = list(fmts)
        changed[k] = rng.choice([f for f in FORMATS if f != fmts[k]])

        def note(fs):
            return p(runs="".join(r(f"w{i} ", bold="b" in f, italic="i" in f) for i, f in enumerate(fs)))

        a = fn_doc(work / f"a{case}.docx", note(fmts))
        res = compare(a, fn_doc(work / f"b{case}.docx", note(changed))).structure_checks["emphasis"]
        assert res.status == "FAIL", (fmts, changed)
        assert compare(a, fn_doc(work / f"c{case}.docx", note(fmts))).structure_checks["emphasis"].status == "PASS"


@pytest.mark.parametrize("source", ["outline_level", "heading_style"])
def test_a_heading_inside_a_footnote_never_passes(work, source):
    def heading(level):
        if level is None:
            return p("Note Title")
        if source == "heading_style":
            return p("Note Title", style=f"Heading{level}")
        return f'<w:p><w:pPr><w:outlineLvl w:val="{level - 1}"/></w:pPr>{r("Note Title")}</w:p>'

    a = fn_doc(work / "a.docx", heading(1), styles=HEADING_STYLES)
    for name, after in {"level": heading(2), "removed": heading(None), "same": heading(1)}.items():
        res = compare(a, fn_doc(work / f"{name}.docx", after, styles=HEADING_STYLES)).structure_checks["headings"]
        assert res.status == "NOT_CHECKED" and res.reason == "unresolved_content", name
    # control: a body heading's level change still FAILs beside a note heading
    def with_body(name, style):
        body = p("Top", style=style) + p(runs=r("x") + '<w:r><w:footnoteReference w:id="1"/></w:r>')
        return minimal_docx(work / name, body, footnotes=f'<w:footnote w:id="1">{heading(1)}</w:footnote>',
                            styles=HEADING_STYLES)

    res = compare(with_body("ba.docx", "Heading1"), with_body("bb.docx", "Heading2")).structure_checks["headings"]
    assert res.status == "FAIL"


def test_a_faithful_docx_to_markdown_with_footnote_bold_passes_emphasis(work):
    """Before: Markdown counted the note's bold and DOCX did not - a false FAIL."""
    a = fn_doc(work / "a.docx", p(runs=r("plain ") + r("Important", bold=True) + r(" tail")))
    res = T.docx_to_markdown(str(a), str(work / "a.md")).report.structure_checks["emphasis"]
    assert res.status == "PASS" and res.details["source_count"] == 2
