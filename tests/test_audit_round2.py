"""Audit round 2 (2026-09-26, re-audit of 0.2.1): six defects and two
disclosed weaknesses, each tested over its DOMAIN - not only at the audit's
own fixtures.

R1 a link in an HTML table cell changed -> PASS    R4 Word ruby guide text changed -> PASS
R2 a decoy attribute read as the destination       R5 Hangul/Bengali equivalents not found
R3 an HTML table caption deleted -> PASS           R6 split reported carried bookmarks as lost
K1 search had no execution bound                   K2 grayscale orientation certified a wrong rotation

Round 1 repaired each example and the next attack walked around it. So every
generator below states a domain - every structural place an HTML link or text
node can sit, every place Word stores text, every class of canonically
equivalent Unicode, every split of every bookmark layout - and the property
must hold everywhere in it. DOCBRIDGE_FUZZ_N scales them.
"""
import dataclasses
import io
import os
import random
import re
import time
import unicodedata
import zipfile

import pymupdf
import pytest
from builders import minimal_docx, p, r, write
from lxml import etree
from PIL import Image

from docbridge import reading as RD
from docbridge import tools as T
from docbridge.convert import images
from docbridge.extract import extract
from docbridge.normalize import LIGATURES, REMOVED_CHARS

N_CASES = int(os.environ.get("DOCBRIDGE_FUZZ_N", "300"))
SEED = 20260926


# ======================================================== R1-R3: raw HTML ===
# Every structural place an <a href> can sit, in an HTML block and in inline
# HTML (paragraph, list, quote, pipe-table cell).

BLOCK_CONTEXTS = [
    "<div>{x}</div>",
    "<p>Lead {x} tail</p>",
    "<table><tr><td>{x}</td></tr></table>",
    "<table><caption>Cap {x}</caption><tr><td>cell</td></tr></table>",
    "<table><thead><tr><th>{x}</th></tr></thead><tbody><tr><td>v</td></tr></tbody></table>",
    "<table><tfoot><tr><td>{x}</td></tr></tfoot></table>",
    "<table><tr><td><table><tr><td>{x}</td></tr></table></td></tr></table>",
    "<table><tr><td><table><caption>{x}</caption><tr><td>n</td></tr></table></td></tr></table>",
    "<table><tr><td rowspan=\"2\">r</td><td colspan=\"2\">{x}</td></tr><tr><td>z</td></tr></table>",
    "<ul><li>{x}</li></ul>",
    "<ol start=\"3\"><li>a</li><li>{x}</li></ol>",
    "<blockquote><div><span>{x}</span></div></blockquote>",
    "<dl><dt>Term</dt><dd>{x}</dd></dl>",
    "<div>Before<table><tr><td>t</td></tr></table>{x}</div>",
    "<details><summary>{x}</summary>body</details>",
]
INLINE_CONTEXTS = [
    "Please {x} now.",
    "- item {x}",
    "> quoted {x}",
    "| a | b |\n|---|---|\n| {x} | 1 |",
    "**Bold {x}** and *em*",
]
DECOYS = [("data-href", "https://decoy.invalid/same"), ("title", "href='https://decoy.invalid/same'"),
          ("title", 'see href="https://decoy.invalid/"'), ("class", "btn href"), ("id", "pay1"),
          ("rel", "noopener"), ("data-x", "href=https://decoy.invalid/"), ("aria-label", "href")]


def _attr(name: str, value: str, style: int) -> str:
    if style == 0 and re.fullmatch(r"[^\s\"'=<>`]+", value):
        return f"{name}={value}"                 # unquoted, where HTML allows it
    if style == 1 and "'" not in value:
        return f"{name}='{value}'"
    if '"' in value:
        return f"{name}='{value}'"
    return f'{name}="{value}"'


def anchor_layout(rng: random.Random) -> list[tuple[str, str | None, int]]:
    """Attribute order, decoys, quoting and case: everything but the URL."""
    attrs = [(rng.choice(["href", "HREF", "Href"]), None, rng.randrange(3))]
    attrs += [(n, v, rng.randrange(3)) for n, v in rng.sample(DECOYS, rng.randrange(0, 4))]
    rng.shuffle(attrs)
    return attrs


def anchor(layout, url: str, text: str = "Pay invoice") -> str:
    parts = [_attr(n, url if v is None else v, s) for n, v, s in layout]
    return f"<a {' '.join(parts)}>{text}</a>"


def md_pair(rng: random.Random):
    ctx = rng.choice(BLOCK_CONTEXTS + INLINE_CONTEXTS)
    layout = anchor_layout(rng)
    n = rng.randrange(100, 999)
    url_a, url_b = f"https://safe.invalid/pay/{n}", rng.choice(
        [f"https://other.invalid/pay/{n}", f"https://safe.invalid/pay/{n + 1}", f"https://safe.invalid/pay/{n}#x"])
    return ctx, layout, url_a, url_b


def test_r1_r2_a_changed_destination_fails_wherever_the_link_sits(work):
    rng = random.Random(f"{SEED}-html-links")
    for i in range(max(60, N_CASES // 3)):
        ctx, layout, url_a, url_b = md_pair(rng)
        a = write(work / f"a{i}.md", ctx.format(x=anchor(layout, url_a)) + "\n")
        b = write(work / f"b{i}.md", ctx.format(x=anchor(layout, url_b)) + "\n")
        links = extract(a).links
        assert [ln.target for ln in links] == [url_a], (i, ctx, layout, links)   # the real href, never a decoy
        rep = T.document_compare(str(a), str(b)).report
        assert rep.identifier_status == "FAIL" and rep.status == "FAIL", (i, ctx, layout, rep.summary)
        same = T.document_compare(str(a), str(write(work / f"c{i}.md", a.read_text(encoding="utf-8")))).report
        assert same.status == "PASS", (i, ctx, same.summary)


def test_r2_only_a_decoy_attribute_changed_still_passes(work):
    """The destination is the href attribute and nothing else: rewriting every
    decoy leaves the comparison clean (declared: other attributes are not
    rendered text)."""
    rng = random.Random(f"{SEED}-decoys")
    for i in range(max(30, N_CASES // 10)):
        ctx, layout, url_a, _ = md_pair(rng)
        if all(v is None for _, v, _ in layout):
            layout.append(("data-href", "https://decoy.invalid/same", 2))
        moved = [(n, v if v is None else v.replace("decoy", "elsewhere"), s) for n, v, s in layout]
        a = write(work / f"a{i}.md", ctx.format(x=anchor(layout, url_a)) + "\n")
        b = write(work / f"b{i}.md", ctx.format(x=anchor(moved, url_a)) + "\n")
        rep = T.document_compare(str(a), str(b)).report
        assert rep.status == "PASS", (i, ctx, layout, rep.summary)


# Every text node of an HTML table - caption, header, body, footer, spans, nested
# tables, lists in cells, image alt text, link text and destination - carries a
# number; changing one digit in any of them, or deleting any of them, must fail.
TABLE_TEMPLATES = [
    "<table><caption>{0}</caption><tr><th>{1}</th><th>{2}</th></tr><tr><td>{3}</td><td>{4}</td></tr></table>",
    "<table><caption><b>{0}</b> <i>{1}</i></caption><thead><tr><th colspan=\"2\">{2}</th></tr></thead><tbody>"
    "<tr><td rowspan=\"2\">{3}</td><td>{4}</td></tr><tr><td>{5}</td></tr></tbody><tfoot><tr><td>{6}</td>"
    "<td>{7}</td></tr></tfoot></table>",
    "<div>{0}<table><tr><td>{1}<table><caption>{2}</caption><tr><td>{3}</td></tr></table>{4}</td></tr></table>"
    "{5}</div>",
    "<table><tr><td><ul><li>{0}</li><li>{1}</li></ul></td><td><ol><li>{2}</li></ol><p>{3}</p></td></tr></table>",
    "<table><tr><td><span>{0}</span><br>{1}</td><td><img alt=\"{2}\" src=\"x.png\"> {3}</td></tr></table>",
    "<table><tr><td><a href=\"https://pay.invalid/{0}\">{1}</a></td><td><dl><dt>{2}</dt><dd>{3}</dd></dl></td>"
    "</tr></table>",
    "<p>{0}</p><table><caption>{1} <a href=\"https://pay.invalid/c{2}\">terms</a></caption><tr><td>{3}</td></tr>"
    "</table><p>{4}</p>",
]
SLOT_WORDS = ["Debt", "EUR", "Invoice", "Beløp", "ποσό", "total", "kr"]


def _slot(rng: random.Random) -> str:
    return f"{rng.choice(SLOT_WORDS)} {rng.randrange(10, 99999)}"


def test_r1_r3_any_changed_or_deleted_table_text_fails(work):
    rng = random.Random(f"{SEED}-html-tables")
    for i in range(max(60, N_CASES // 3)):
        tpl = rng.choice(TABLE_TEMPLATES)
        slots = [_slot(rng) for _ in range(tpl.count("{"))]
        k = rng.randrange(len(slots))
        changed = list(slots)
        tok = changed[k]
        d = rng.choice([j for j, ch in enumerate(tok) if ch.isdigit()])
        changed[k] = tok[:d] + str((int(tok[d]) + rng.randrange(1, 10)) % 10) + tok[d + 1:]
        deleted = list(slots)
        deleted[k] = ""
        a = write(work / f"a{i}.md", tpl.format(*slots) + "\n")
        for label, other in (("changed", changed), ("deleted", deleted)):
            b = write(work / f"{label}{i}.md", tpl.format(*other) + "\n")
            rep = T.document_compare(str(a), str(b)).report
            assert rep.status == "FAIL", (i, label, tpl, k, slots[k], other[k], rep.summary)
        assert T.document_compare(str(a), str(write(work / f"same{i}.md", tpl.format(*slots) + "\n"))
                                  ).report.status == "PASS", (i, tpl)


# Outside the declared domain: an element docbridge does not model is UNKNOWN
# wherever it appears, with or without text - never read as absent.
UNKNOWN_SNIPPETS = [
    "<svg><text>{n}</text></svg>", "<math><mn>{n}</mn></math>", "<iframe src=\"https://x.invalid/{n}\"></iframe>",
    "<object data=\"x{n}.pdf\"></object>", "<select><option>{n}</option></select>", "<textarea>{n}</textarea>",
    "<my-widget>{n}</my-widget>", "<input value=\"{n}\">", "<form action=\"https://pay.invalid/{n}\">"
    "<button>Pay</button></form>", "<canvas data-v=\"{n}\"></canvas>", "<ol type=\"a\"><li>{n}</li></ol>",
]


@pytest.mark.parametrize("snippet", UNKNOWN_SNIPPETS)
@pytest.mark.parametrize("where", ["block", "inline", "cell"])
def test_r1_r3_an_unmodelled_element_never_passes(work, snippet, where):
    shell = {"block": "<div>Amount {s}</div>\n", "inline": "Amount {s} due.\n",
             "cell": "<table><tr><td>Amount {s}</td></tr></table>\n"}[where]
    a = write(work / "a.md", shell.format(s=snippet.format(n="9000")))
    b = write(work / "b.md", shell.format(s=snippet.format(n="1000")))
    for other in (a, b):
        rep = T.document_compare(str(a), str(other)).report
        assert rep.status != "PASS", (snippet, where, rep.summary)
    assert any(w.code == "md_html_unreadable" for w in T.document_compare(str(a), str(b)).report.warnings)


def test_r2_a_comment_opener_inside_an_attribute_hides_nothing(work):
    tpl = '<div><a title="<!--" href="https://{h}.invalid/pay">Pay</a> total 9000<!-- note --></div>\n'
    a, b = write(work / "a.md", tpl.format(h="safe")), write(work / "b.md", tpl.format(h="other"))
    assert [ln.target for ln in extract(a).links] == ["https://safe.invalid/pay"]
    assert T.document_compare(str(a), str(b)).report.identifier_status == "FAIL"


def test_r3_caption_is_read_in_place_with_its_links(work):
    from docbridge.canonical import Origin
    md = write(work / "c.md", '<p>Lead</p><table><caption>Debt <a href="https://pay.invalid/x">9000</a> EUR'
                              '</caption><tr><td>Invoice</td></tr></table>\n')
    c = extract(md)
    assert [t.display() for t in c.stream(frozenset({Origin.CONTENT}))] == ["Lead", "Debt", "9000", "EUR", "Invoice"]
    assert [(ln.target, ln.text) for ln in c.links] == [("https://pay.invalid/x", "9000")]


# ======================================================== R4: Word text ===
# Every place WordprocessingML stores text a reader sees. The exclusions are
# the DECLARED reading rules and nothing else: deleted text (w:del/w:moveFrom),
# hidden runs (w:vanish), and mc:Fallback copies of an mc:Choice.

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
V_NS = "urn:schemas-microsoft-com:vml"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _t(t: str) -> str:
    return f'<w:t xml:space="preserve">{t}</w:t>'


def _textbox(t: str) -> str:
    return (f'<w:r><w:drawing><wp:anchor><wp:docPr id="1" name="tb"/><a:graphic xmlns:a="{A_NS}"><a:graphicData '
            f'uri="{WPS_NS}"><wps:wsp xmlns:wps="{WPS_NS}"><wps:txbx><w:txbxContent><w:p><w:r>{_t(t)}</w:r></w:p>'
            '</w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>')


# run-level forms: each returns paragraph content holding the text `t`
READABLE_FORMS = {
    "run": lambda t: f"<w:r>{_t(t)}</w:r>",
    "ruby_guide": lambda t: (f"<w:r><w:ruby><w:rubyPr/><w:rt><w:r>{_t(t)}</w:r></w:rt><w:rubyBase><w:r>{_t('Base')}"
                             "</w:r></w:rubyBase></w:ruby></w:r>"),
    "ruby_base": lambda t: (f"<w:r><w:ruby><w:rubyPr/><w:rt><w:r>{_t('guide')}</w:r></w:rt><w:rubyBase><w:r>{_t(t)}"
                            "</w:r></w:rubyBase></w:ruby></w:r>"),
    "field_result": lambda t: ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>DATE</w:instrText>'
                               f'</w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r>{_t(t)}</w:r><w:r>'
                               '<w:fldChar w:fldCharType="end"/></w:r>'),
    "alternate": lambda t: (f'<w:r><mc:AlternateContent><mc:Choice Requires="w14">{_t(t)}</mc:Choice>'
                            f'<mc:Fallback>{_t(t)}</mc:Fallback></mc:AlternateContent></w:r>'),
    "hidden": lambda t: f"<w:r><w:rPr><w:vanish/></w:rPr>{_t(t)}</w:r>",
    "deleted": lambda t: f'<w:del w:id="3" w:author="a"><w:r>{_t(t)}</w:r></w:del>',
    "moved_from": lambda t: f'<w:moveFrom w:id="4" w:author="a"><w:r>{_t(t)}</w:r></w:moveFrom>',
    "textbox": _textbox,
    "vml_textbox": lambda t: (f'<w:r><w:pict><v:shape xmlns:v="{V_NS}"><v:textbox><w:txbxContent><w:p><w:r>{_t(t)}'
                              '</w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r>'),
    "wordart": lambda t: f'<w:r><w:pict><v:shape xmlns:v="{V_NS}"><v:textpath string="{t}"/></v:shape></w:pict></w:r>',
    "footnote": None,          # the text goes into footnotes.xml; built below
}
INLINE_WRAPS = [
    ('<w:hyperlink r:id="rL">', "</w:hyperlink>"), ("<w:sdt><w:sdtContent>", "</w:sdtContent></w:sdt>"),
    ('<w:ins w:id="1" w:author="a">', "</w:ins>"), ('<w:moveTo w:id="2" w:author="a">', "</w:moveTo>"),
    ('<w:smartTag w:uri="u" w:element="e">', "</w:smartTag>"), ('<w:customXml w:element="e">', "</w:customXml>"),
    ('<w:dir w:val="rtl">', "</w:dir>"), ('<w:bdo w:val="rtl">', "</w:bdo>"),
    ('<w:fldSimple w:instr="PAGE">', "</w:fldSimple>"),
]
BLOCK_WRAPS = [
    ("", ""), ("<w:sdt><w:sdtContent>", "</w:sdtContent></w:sdt>"), ('<w:customXml w:element="e">', "</w:customXml>"),
    ("<w:tbl><w:tr><w:tc>", "</w:tc></w:tr></w:tbl>"),
    ("<w:tbl><w:tr><w:tc><w:tbl><w:tr><w:tc>", "</w:tc></w:tr></w:tbl><w:p/></w:tc></w:tr></w:tbl>"),
]


def word_document(rng: random.Random, work, name: str):
    """A random body: paragraphs of random forms, each in random inline and
    block wrappers. Every text holds a unique number."""
    body, notes, n = [], [], [1000]

    def number() -> str:
        n[0] += rng.randrange(1, 50)
        return f"N{n[0]}"

    for _ in range(rng.randrange(2, 6)):
        content = ""
        for _ in range(rng.randrange(1, 4)):
            form = rng.choice(list(READABLE_FORMS))
            if form == "footnote":
                fid = len(notes) + 1
                notes.append(f'<w:footnote w:id="{fid}"><w:p><w:r>{_t(number())}</w:r></w:p></w:footnote>')
                piece = f'<w:r><w:footnoteReference w:id="{fid}"/></w:r>'
            else:
                piece = READABLE_FORMS[form](number())
            for _ in range(rng.randrange(0, 3)):
                o, c = rng.choice(INLINE_WRAPS)
                piece = o + piece + c
            content += piece + r(" ")
        o, c = rng.choice(BLOCK_WRAPS)
        body.append(o + p(runs=content) + c)
    path = minimal_docx(work / name, "".join(body), footnotes="".join(notes) or None,
                        hyperlinks={"rL": "https://pay.invalid/doc"})
    return path


def _excluded(el) -> bool:
    """The declared exclusions, from the XML alone."""
    for a in el.iterancestors():
        tag = etree.QName(a)
        if (tag.namespace, tag.localname) in ((W_NS, "del"), (W_NS, "moveFrom"), (MC_NS, "Fallback")):
            return True
        if (tag.namespace, tag.localname) == (W_NS, "r"):
            v = a.find(f"{{{W_NS}}}rPr/{{{W_NS}}}vanish")
            if v is not None and v.get(f"{{{W_NS}}}val") not in ("0", "false", "off"):
                return True
    return False


def text_positions(path):
    """(part, index, excluded) for every text-bearing node in the body and the notes."""
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    out = []
    for part in ("word/document.xml", "word/footnotes.xml"):
        if part not in parts:
            continue
        root = etree.fromstring(parts[part])
        nodes = [e for e in root.iter() if isinstance(e.tag, str) and (
            etree.QName(e).localname == "t" or (etree.QName(e).localname == "textpath" and e.get("string")))]
        out += [(part, k, _excluded(e)) for k, e in enumerate(nodes)]
    return parts, out


def mutate(parts, part: str, k: int, dst, rng: random.Random):
    root = etree.fromstring(parts[part])
    nodes = [e for e in root.iter() if isinstance(e.tag, str) and (
        etree.QName(e).localname == "t" or (etree.QName(e).localname == "textpath" and e.get("string")))]
    e = nodes[k]
    text = e.get("string") if etree.QName(e).localname == "textpath" else e.text
    digits = [j for j, ch in enumerate(text or "") if ch.isdigit()]
    if not digits:
        return None
    d = rng.choice(digits)
    new = text[:d] + str((int(text[d]) + rng.randrange(1, 10)) % 10) + text[d + 1:]
    if etree.QName(e).localname == "textpath":
        e.set("string", new)
    else:
        e.text = new
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                       if name == part else data)
    return dst


def test_r4_every_visible_word_text_is_compared(work):
    rng = random.Random(f"{SEED}-docx-text")
    checked = excluded_checked = 0
    for i in range(max(15, N_CASES // 12)):
        src = word_document(rng, work, f"d{i}.docx")
        base = T.document_compare(str(src), str(src)).report
        assert base.status == "PASS", (i, base.summary)
        parts, positions = text_positions(src)
        for j, (part, k, excluded) in enumerate(positions):
            dst = mutate(parts, part, k, work / f"d{i}_{j}.docx", rng)
            if dst is None:
                continue
            rep = T.document_compare(str(src), str(dst)).report
            if excluded:
                excluded_checked += 1
                assert rep.status == "PASS", (i, part, k, "declared exclusion", rep.summary)
            else:
                checked += 1
                assert rep.numeric_status == "FAIL" and rep.status == "FAIL", (i, part, k, rep.summary)
    assert checked > 50 and excluded_checked > 5, (checked, excluded_checked)


UNREADABLE_FORMS = {
    "equation": lambda t: f"<m:oMath><m:r><m:t>{t}</m:t></m:r></m:oMath>",
    "foreign_in_paragraph": lambda t: f'<x:thing xmlns:x="urn:x"><w:r>{_t(t)}</w:r></x:thing>',
    "foreign_in_run": lambda t: f'<w:r><x:thing xmlns:x="urn:x">{_t(t)}</x:thing></w:r>',
    "drawing_text_outside_textbox": lambda t: (
        f'<w:r><w:drawing><wp:inline><wp:docPr id="2" name="s"/><a:graphic xmlns:a="{A_NS}"><a:graphicData '
        f'uri="{WPS_NS}"><wps:wsp xmlns:wps="{WPS_NS}"><a:t>{t}</a:t></wps:wsp></a:graphicData></a:graphic>'
        '</wp:inline></w:drawing></w:r>'),
    "chart": lambda t: (f'<w:r><w:drawing><wp:inline><wp:docPr id="3" name="c" descr="{t}"/><a:graphic '
                        f'xmlns:a="{A_NS}"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/'
                        'chart"/></a:graphic></wp:inline></w:drawing></w:r>'),
    "subdocument": lambda t: f'<w:subDoc r:id="rS{t}"/>',
    "ink": lambda t: f'<w:r><w:contentPart r:id="rI{t}"/></w:r>',
    "activex": lambda t: f'<w:r><w:pict><w:control r:id="rC{t}" w:name="c"/></w:pict></w:r>',
    "checkbox_field": lambda t: ('<w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:checkBox><w:default w:val="1"/>'
                                 f'</w:checkBox></w:ffData></w:fldChar></w:r><w:r><w:instrText>FORMCHECKBOX {t}'
                                 '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r>'),
    "ruby_stray_text": lambda t: (f"<w:r><w:ruby><w:rubyPr/><w:rt><w:r>{_t('g')}</w:r></w:rt><w:rubyBase><w:r>"
                                  f"{_t('b')}</w:r></w:rubyBase><x:odd xmlns:x=\"urn:x\">{_t(t)}</x:odd></w:ruby></w:r>"),
}


@pytest.mark.parametrize("form", list(UNREADABLE_FORMS))
def test_r4_text_the_reader_cannot_read_is_never_absent(work, form):
    make = UNREADABLE_FORMS[form]
    a = minimal_docx(work / "a.docx", p(runs=r("Amount ") + make("9000")))
    b = minimal_docx(work / "b.docx", p(runs=r("Amount ") + make("1000")))
    plain = minimal_docx(work / "plain.docx", p("Amount"))
    for x, y in ((a, b), (a, a), (a, plain)):
        rep = T.document_compare(str(x), str(y)).report
        assert rep.status != "PASS", (form, x.name, y.name, rep.summary)
    assert "⟦UNREADABLE" in RD.load(a).full, form


def test_r4_ruby_reads_base_then_guide_and_marks_the_guide(work):
    ruby = (f"<w:r><w:ruby><w:rubyPr/><w:rt><w:r>{_t('90')}</w:r><w:r>{_t('00')}</w:r></w:rt><w:rubyBase><w:r>"
            f"{_t('EUR')}</w:r></w:rubyBase></w:ruby></w:r>")
    doc = minimal_docx(work / "r.docx", p(runs=r("Amount ") + ruby))
    assert RD.load(doc).full == "Amount EUR⟦ruby: 9000⟧"
    assert T.document_search(str(doc), "9000").matches_total == 1


# ======================================================== R5: Unicode ===
# Canonical equivalence across every composition class the fold must handle:
# Hangul syllables and conjoining jamo (L+V, LV+T), Indic two-part vowels,
# Latin/Greek combining marks in any order, and singleton decompositions.

STARTERS = (["a", "e", "o", "A", "c", "n", "α", "ω", "ι", "Ω"] + [chr(c) for c in range(0x1100, 0x1113, 3)]
            + [chr(c) for c in (0xAC00, 0xAC1C, 0xB098, 0xD7A3)]
            + [chr(c) for c in (0x0995, 0x0996, 0x0B95, 0x0DB4, 0x0915, 0x0B92, 0x0D9A)])
FOLLOWERS = ([chr(c) for c in (0x0301, 0x0300, 0x0327, 0x0323, 0x0308, 0x0345, 0x0344, 0x030A)]
             + [chr(c) for c in range(0x1161, 0x1176, 4)] + [chr(c) for c in range(0x11A8, 0x11C3, 5)]
             + [chr(c) for c in (0x09C7, 0x09BE, 0x09D7, 0x09BC, 0x0BC6, 0x0BBE, 0x0BD7, 0x0DD9, 0x0DCF, 0x0DDF,
                                 0x093C, 0x0B57, 0x0D46, 0x0D3E)])
SINGLETONS = [chr(c) for c in (0x2126, 0x212B, 0xF900, 0x0958, 0x1F71, 0x2ADC)]
EXTRAS = [chr(c) for c in (0x200B, 0x00AD, 0xFB01, 0xFEFF)] + [" ", "|", "5"]


def _cluster(rng: random.Random) -> str:
    return rng.choice(STARTERS + SINGLETONS) + "".join(rng.choice(FOLLOWERS) for _ in range(rng.randrange(0, 4)))


def _declared(s: str) -> str:
    return "".join(LIGATURES.get(ch, ch) for ch in s if ch not in REMOVED_CHARS)


def test_r5_the_fold_equals_whole_string_nfc_everywhere():
    rng = random.Random(f"{SEED}-fold")
    for i in range(max(3000, N_CASES * 10)):
        s = "".join(rng.choice(FOLLOWERS + EXTRAS) if rng.random() < 0.3 else _cluster(rng)
                    for _ in range(rng.randrange(1, 8)))
        folded, starts, ends = RD._fold(s)
        assert folded == _declared(unicodedata.normalize("NFC", s)), (i, [hex(ord(c)) for c in s])
        assert len(starts) == len(ends) == len(folded)
        assert all(0 <= a < b <= len(s) for a, b in zip(starts, ends, strict=True))
        assert starts == sorted(starts) and ends == sorted(ends)


@pytest.mark.parametrize("word", ["\u1100\u1161", "\u09c7\u09be", "\u0b92\u0bd7", "\u0dd9\u0dcf", "e\u0301\u0327",
                                  "\u1100\u1161\u11a8", "\u212b", "\uac00\u11a8"])
def test_r5_audit_and_sibling_equivalences_are_found(work, word):
    for doc_form in ("NFC", "NFD", None):
        text = f"x {unicodedata.normalize(doc_form, word) if doc_form else word} y"
        path = write(work / f"u{doc_form}.txt", text)
        for query in {word, unicodedata.normalize("NFC", word), unicodedata.normalize("NFD", word)}:
            found = T.document_search(str(path), query, case_sensitive=True)
            assert found.matches_total == 1, (doc_form, [hex(ord(c)) for c in query])
            m = found.matches[0]
            assert m.text == text[2:-2] and RD.load(path).full[m.char_start:m.char_end] == m.text


def test_r5_search_finds_every_equivalent_form_at_its_offset(work):
    rng = random.Random(f"{SEED}-nfc-search")
    for i in range(max(25, N_CASES // 12)):
        word = "".join(_cluster(rng) for _ in range(rng.randrange(1, 4)))
        doc_form, query_form = rng.choice(["NFC", "NFD"]), rng.choice(["NFC", "NFD"])
        shown = unicodedata.normalize(doc_form, word)
        path = write(work / f"w{i}.txt", f"« {shown} »\n")
        found = T.document_search(str(path), unicodedata.normalize(query_form, word), case_sensitive=True)
        assert found.matches_total >= 1, (i, [hex(ord(c)) for c in word], doc_form, query_form)
        m = found.matches[0]
        assert m.text == shown and m.char_start == 2, (i, [hex(ord(c)) for c in word], m.text)


# ================================================== R6: split bookmarks ===

TITLES = ["Intro", "Pay", "Pay", "Total", "Notes", "Pay"]


def bookmark_pdf(path, rng: random.Random):
    n = rng.randrange(1, 7)
    d = pymupdf.open()
    for i in range(n):
        d.new_page().insert_text((72, 72), f"page {i + 1}")
    toc, level = [], 0
    for _ in range(rng.randrange(0, 9)):
        level = rng.randrange(1, level + 2)
        title = rng.choice(TITLES)
        if rng.random() < 0.2:
            toc.append([level, title, -1, {"kind": pymupdf.LINK_URI, "uri": f"https://pay.invalid/{title}"}])
        else:
            pg = rng.randrange(1, n + 1)
            toc.append([level, title, pg, {"kind": pymupdf.LINK_GOTO, "page": pg - 1,
                                            "to": pymupdf.Point(0, rng.randrange(0, 700))}])
    d.set_toc(toc)
    d.save(str(path))
    d.close()
    with pymupdf.open(str(path)) as d:
        return n, d.get_toc(simple=False)


def test_r6_split_reports_exactly_the_bookmarks_no_output_holds(work):
    rng = random.Random(f"{SEED}-split")
    for i in range(max(30, N_CASES // 10)):
        src = work / f"s{i}.pdf"
        n, toc = bookmark_pdf(src, rng)
        if rng.random() < 0.3:
            spans, kwargs = [(k, k) for k in range(1, n + 1)], {"each_page": True}
        else:
            spans = []
            for _ in range(rng.randrange(1, 4)):
                a = rng.randrange(1, n + 1)
                b = rng.randrange(a, n + 1)
                if (a, b) not in spans:
                    spans.append((a, b))
            kwargs = {"ranges": [f"{a}-{b}" if a != b else str(a) for a, b in spans]}
        res = T.pdf_split(str(src), output_dir=str(work / f"parts{i}"), **kwargs)
        assert res.error is None, res.error
        rep = res.report
        assert rep.status == "PASS", (i, toc, spans, rep.summary)
        for (a, b), out in zip(spans, res.outputs, strict=True):
            want = [e[1] for e in toc if a <= e[2] <= b]
            with pymupdf.open(out) as od:
                assert [e[1] for e in od.get_toc()] == want, (i, a, b)
        lost = sum(1 for e in toc if not any(a <= e[2] <= b for a, b in spans))
        assert rep.structure_checks["bookmarks"].details["not_carried"] == lost, (i, toc, spans)
        assert any(w.code == "bookmarks_not_carried" for w in rep.warnings) == (lost > 0), (i, toc, spans)


# ============================================================ K1: search ===

def test_k1_a_catastrophic_pattern_is_stopped_and_says_so(work, monkeypatch):
    monkeypatch.setenv("DOCBRIDGE_SEARCH_TIMEOUT_S", "0.5")
    path = write(work / "a.txt", "a" * 100 + "!")
    t = time.monotonic()
    res = T.document_search(str(path), "(a+)+$", regex=True)
    elapsed = time.monotonic() - t
    assert res.error is not None and res.error.code == "search_timeout" and not res.matches
    assert "not evidence" in res.error.message
    assert elapsed < 2.5, elapsed
    # the server lives on: the next search answers normally
    assert T.document_search(str(path), "a!").matches_total == 1


def test_k1_the_bounded_worker_answers_exactly_as_the_engine_does(work):
    rng = random.Random(f"{SEED}-worker")
    text = "".join(rng.choice("ab \n5é") for _ in range(4000))
    for pattern, overlapping in [("a", True), ("ab", True), ("aa", True), ("a b", True), (r"\d+", False),
                                 (r"a+b?", False), (r"\s+", False), ("é5", True), (r"(?m)^a", False)]:
        for start, limit in ((0, 500), (3, 7), (10_000, 5)):
            total, spans = RD._run_search(pattern, 0, text, overlapping, start, limit)
            rx = re.compile(pattern)
            if overlapping:
                ref, pos = [], 0
                while (m := rx.search(text, pos)) is not None:
                    ref.append([m.start(), m.end()])
                    pos = m.start() + 1
            else:
                ref = [[m.start(), m.end()] for m in rx.finditer(text)]
            ref = [s for s in ref if s[1] > s[0]]
            assert total == len(ref) and spans == ref[start:start + limit], (pattern, start, limit)


# ======================================================= K2: orientation ===

def _plan(work, im: Image.Image, name: str, cw: int = 0, fmt: str = "PNG"):
    path = work / name
    im.save(path, fmt)
    return images.plan_image(path, cw, True, "image")


def _random_image(rng: random.Random) -> Image.Image:
    w, h = rng.randrange(8, 120), rng.randrange(8, 120)
    im = Image.new("RGB", (w, h), tuple(rng.randrange(256) for _ in range(3)))
    for _ in range(rng.randrange(1, 4)):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        im.paste(tuple(rng.randrange(256) for _ in range(3)),
                 (x0, y0, rng.randrange(x0, w) + 1, rng.randrange(y0, h) + 1))
    return im


def test_k2_audit_isoluminant_rotation_fails(work):
    im = Image.new("RGB", (128, 128), (255, 0, 0))
    im.paste((0, 130, 0), (64, 0, 128, 128))
    plan = _plan(work, im, "iso.png")
    images.build([dataclasses.replace(plan, rotate_cw=180)], work / "wrong.pdf")
    _, axis, diffs, rows = images.verify(work / "wrong.pdf", [plan])
    assert axis.status == "FAIL" and rows[0]["orientation"]["geometry"]["verdict"] == "DIFFERENT"


def test_k2_any_wrong_rotation_fails_and_every_honest_one_passes(work):
    rng = random.Random(f"{SEED}-orient")
    for i in range(max(20, N_CASES // 15)):
        im = _random_image(rng)
        if rng.random() < 0.3:
            im = im.convert(rng.choice(["L", "RGBA", "P"]))
        cw = rng.choice([0, 90, 180, 270])
        plan = _plan(work, im, f"i{i}.png", cw)
        images.build([plan], work / f"ok{i}.pdf")
        _, axis, _, rows = images.verify(work / f"ok{i}.pdf", [plan])
        assert axis.status == "PASS", (i, cw, rows)
        wrong = rng.choice([x for x in (0, 90, 180, 270) if x != plan.rotate_cw])
        images.build([dataclasses.replace(plan, rotate_cw=wrong)], work / f"bad{i}.pdf")
        shown = plan.stored.rotate(-wrong, expand=True)
        _, axis, _, rows = images.verify(work / f"bad{i}.pdf", [plan])
        if shown.tobytes() == plan.expected.tobytes() and shown.size == plan.expected.size:
            assert axis.status == "PASS", (i, "a symmetric image looks the same turned")
        else:
            assert axis.status == "FAIL", (i, cw, wrong, rows)


def test_k2_a_mirrored_placement_fails(work):
    plan = _plan(work, Image.new("RGB", (60, 30), (200, 30, 30)), "m.png")
    images.build([plan], work / "m.pdf")
    doc = pymupdf.open(str(work / "m.pdf"))
    xref = doc[0].get_contents()[0]
    stream = doc.xref_stream(xref).decode("latin-1")
    new, count = re.subn(r"([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+) cm",
                         lambda m: f"-{m[1]} {m[2]} {m[3]} {m[4]} {float(m[5]) + float(m[1])} {m[6]} cm", stream,
                         count=1)
    assert count == 1
    doc.update_stream(xref, new.encode("latin-1"))
    doc.save(str(work / "mirrored.pdf"))
    doc.close()
    _, axis, _, rows = images.verify(work / "mirrored.pdf", [plan])
    assert axis.status == "FAIL" and rows[0]["orientation"]["geometry"]["mirrored"], rows


def test_k2_an_inconclusive_measurement_is_never_a_pass(work, monkeypatch):
    """Geometry unreadable, and an image every rotation of which looks the same:
    nothing can tell right from wrong, so the page is NOT_CHECKED."""
    plan = _plan(work, Image.new("RGB", (40, 40), (10, 200, 30)), "u.png")
    images.build([plan], work / "u.pdf")
    monkeypatch.setattr(images, "_geometry", lambda page, xref, p: {"verdict": "UNAVAILABLE", "note": "test"})
    _, axis, _, rows = images.verify(work / "u.pdf", [plan])
    assert rows[0]["orientation"]["verdict"] == "INCONCLUSIVE" and not rows[0]["orientation"]["discriminating"]
    assert axis.status == "NOT_CHECKED" and axis.reason == "inconclusive"


def test_k2_colour_decides_when_brightness_cannot(work, monkeypatch):
    """With the geometry withheld, the colour measurement alone still sees a
    red/green image turned 180 degrees."""
    im = Image.new("RGB", (64, 64), (255, 0, 0))
    im.paste((0, 130, 0), (32, 0, 64, 64))
    plan = _plan(work, im, "c.png")
    images.build([dataclasses.replace(plan, rotate_cw=180)], work / "c.pdf")
    monkeypatch.setattr(images, "_geometry", lambda page, xref, p: {"verdict": "UNAVAILABLE", "note": "test"})
    _, axis, _, rows = images.verify(work / "c.pdf", [plan])
    assert axis.status == "FAIL" and rows[0]["orientation"]["visual"]["verdict"] == "DIFFERENT"


def test_k2_jpeg_bytes_and_exif_still_verified(work):
    b = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 30, 30)).save(b, "JPEG")
    path = work / "j.jpg"
    path.write_bytes(b.getvalue())
    res = T.images_to_pdf([str(path)], str(work / "j.pdf"), rotations=[90])
    assert res.report.image_status == "PASS", res.report.summary
