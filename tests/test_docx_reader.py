"""The DOCX reader's declared rules, one construct at a time, on hand-built packages."""
import zipfile

import pytest
from builders import HEADING_STYLES, level, minimal_docx, p, r

from docbridge import tools as T
from docbridge.canonical import Origin
from docbridge.extract import extract

ALL = frozenset(Origin) - {Origin.UNKNOWN}


def texts(canon, origins=ALL):
    return [t.display() for t in canon.stream(origins)]


def test_tracked_changes_read_as_accepted(work):
    body = p(runs=r("kept ") + "<w:ins><w:r><w:t>inserted 5</w:t></w:r></w:ins>"
             "<w:del><w:r><w:delText>deleted 6</w:delText></w:r></w:del>")
    c = extract(minimal_docx(work / "t.docx", body))
    assert texts(c) == ["kept", "inserted", "5"]
    assert any(w.code == "docx_tracked_changes" for w in c.warnings)


def test_field_result_read_instruction_skipped_and_hyperlink_field_recorded(work):
    field = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> PAGE </w:instrText></w:r>'
             '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>7</w:t></w:r>'
             '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    link = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText> HYPERLINK "https://bedvibe.studio/f" </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>site</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    c = extract(minimal_docx(work / "f.docx", p(runs=r("page ") + field) + p(runs=link)))
    assert texts(c) == ["page", "7", "site"]
    assert [(l.target, l.text) for l in c.links] == [("https://bedvibe.studio/f", "site")]


def test_hidden_text_is_excluded_and_counted(work):
    c = extract(minimal_docx(work / "h.docx", p(runs=r("shown 1 ") + r("secret 9", vanish=True))))
    assert texts(c) == ["shown", "1"]
    assert any(w.code == "docx_hidden_text" for w in c.warnings)


def test_alternate_content_is_read_once(work):
    box = ('<w:txbxContent><w:p><w:r><w:t>box text 12</w:t></w:r></w:p></w:txbxContent>')
    alt = ('<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:inline>'
           '<wp:docPr id="1" name="tb"/><wps:txbx xmlns:wps="http://schemas.microsoft.com/office/word/2010/'
           f'wordprocessingShape">{box}</wps:txbx></wp:inline></w:drawing></mc:Choice><mc:Fallback>'
           '<w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox>'
           f'{box}</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>')
    c = extract(minimal_docx(work / "a.docx", p(runs=r("anchor ") + alt)))
    assert texts(c).count("12") == 1


NUMBERING = (
    '<w:abstractNum w:abstractNumId="1">' + level(0, "decimal", "%1.") + level(1, "lowerLetter", "%2)")
    + level(2, "lowerRoman", "(%3)") + "</w:abstractNum>"
    '<w:abstractNum w:abstractNumId="4">' + level(0, "decimal", "%1.") + level(1, "decimal", "%1.%2.")
    + "</w:abstractNum>"
    '<w:abstractNum w:abstractNumId="5">' + level(0, "cardinalText", "%1") + "</w:abstractNum>"
    '<w:abstractNum w:abstractNumId="6">' + level(0, "bullet", "") + "</w:abstractNum>"
    '<w:abstractNum w:abstractNumId="7">' + level(0, "upperRoman", "%1.", start=4) + "</w:abstractNum>"
    '<w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num>'
    '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
    '<w:num w:numId="3"><w:abstractNumId w:val="1"/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/>'
    "</w:lvlOverride></w:num>"
    '<w:num w:numId="4"><w:abstractNumId w:val="4"/></w:num>'
    '<w:num w:numId="5"><w:abstractNumId w:val="5"/></w:num>'
    '<w:num w:numId="6"><w:abstractNumId w:val="6"/></w:num>'
    '<w:num w:numId="7"><w:abstractNumId w:val="7"/></w:num>'
)


def labels(c):
    return [b.text(frozenset({Origin.LIST_LABEL})) or None for b in c.list_items()]


def test_word_numbering_semantics(work):
    body = "".join([
        p("a", num=(1, 0)), p("b", num=(1, 1)), p("c", num=(1, 1)), p("d", num=(1, 2)),
        p("e", num=(1, 0)), p("f", num=(1, 1)),
        p("g", num=(2, 0)),          # shares abstractNum 1 without an override: continues
        p("h", num=(3, 0)), p("i", num=(3, 0)),   # startOverride: its own sequence
        p("j", num=(4, 0)), p("k", num=(4, 1)), p("l", num=(4, 1)), p("m", num=(4, 0)), p("n", num=(4, 1)),
        p("o", num=(6, 0)),          # bullet: no label
        p("q", num=(7, 0)), p("s", num=(7, 0)),   # roman from 4
    ])
    c = extract(minimal_docx(work / "n.docx", body, numbering=NUMBERING))
    assert labels(c) == ["1.", "a)", "b)", "(i)", "2.", "a)", "3.", "1.", "2.",
                         "1.", "1.1.", "1.2.", "2.", "2.1.", None, "IV.", "V."]


def test_unsupported_number_format_is_unknown_not_guessed(work):
    c = extract(minimal_docx(work / "u.docx", p("item", num=(5, 0)), numbering=NUMBERING))
    assert c.list_items()[0].has_unknown()
    assert any(w.code == "docx_numbering_unsupported" for w in c.warnings)
    md = work / "u.md"
    md.write_text("1. item\n", encoding="utf-8")
    rep = T.validate_conversion(str(work / "u.docx"), str(md)).report
    assert rep.text_status == "NOT_CHECKED" and rep.axes["text"].reason == "unresolved_content"


def test_footnotes_render_in_reference_order(work):
    notes = ('<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
             '<w:footnote w:id="2"><w:p><w:r><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> Source 1984.'
             '</w:t></w:r></w:p></w:footnote>')
    body = p(runs=r("Claim") + '<w:r><w:footnoteReference w:id="2"/></w:r>' + r(" ends."))
    c = extract(minimal_docx(work / "fn.docx", body, footnotes=notes))
    assert texts(c) == ["Claim[1]", "ends.", "[1]", "Source", "1984."]
    no_notes = frozenset({Origin.CONTENT, Origin.LIST_LABEL})
    assert texts(c, no_notes) == ["Claim", "ends."]


def test_equation_is_unknown(work):
    body = p(runs=r("Let ") + "<m:oMath><m:r><m:t>x=5</m:t></m:r></m:oMath>")
    c = extract(minimal_docx(work / "m.docx", body))
    assert any(not t.known for t in c.stream(ALL))
    assert any(u.kind == "equation" for u in c.unsupported)


def test_unrecognised_element_with_text_becomes_unknown(work):
    c = extract(minimal_docx(work / "x.docx", '<w:mystery><w:r><w:t>odd 3</w:t></w:r></w:mystery>'))
    assert any(not t.known for t in c.stream(ALL))
    assert any(w.code == "docx_unreadable" for w in c.warnings)


def test_headings_tables_emphasis_links(work):
    body = (p("Title 1", style="Heading1") + p("Sub 2", style="Heading2")
            + p(runs=r("a", bold=True) + r(" ") + r("b", bold=True) + r(" plain ") + r("it", italic=True))
            + p(runs='<w:hyperlink r:id="rL"><w:r><w:t>portfolio</w:t></w:r></w:hyperlink>')
            + "<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val=\"2\"/></w:tcPr>" + p("wide") + "</w:tc></w:tr>"
            "<w:tr><w:tc><w:tcPr><w:vMerge w:val=\"restart\"/></w:tcPr>" + p("tall") + "</w:tc><w:tc>" + p("x 1")
            + "</w:tc></w:tr><w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr>" + p("") + "</w:tc><w:tc>" + p("y 2")
            + "</w:tc></w:tr></w:tbl>")
    c = extract(minimal_docx(work / "s.docx", body, styles=HEADING_STYLES,
                             hyperlinks={"rL": "https://bedvibe.studio/portfolio/"}))
    assert [(b.level, b.text()) for b in c.headings()] == [(1, "Title 1"), (2, "Sub 2")]
    assert [(e.kind, e.text) for e in c.emphasis] == [("strong", "a"), ("strong", "b"), ("em", "it")]
    assert [(l.target, l.text) for l in c.links] == [("https://bedvibe.studio/portfolio/", "portfolio")]
    t = c.tables[0]
    assert (t.rows, t.cols) == (3, 2)
    assert [(x.row, x.col, x.rowspan, x.colspan, x.text) for x in t.cells] == [
        (0, 0, 1, 2, "wide"), (1, 0, 2, 1, "tall"), (1, 1, 1, 1, "x 1"), (2, 1, 1, 1, "y 2")]


@pytest.mark.parametrize("kind,code", [
    ("strict", "unsupported_format"),
    ("docm", "unsupported_format"),
    ("no_main", "malformed_input"),
    ("bad_xml", "malformed_input"),
    ("ole", "encrypted_input"),
])
def test_refused_packages(work, kind, code):
    path = work / "bad.docx"
    if kind == "strict":
        minimal_docx(path, p("x"), namespace="http://purl.oclc.org/ooxml/wordprocessingml/main")
    elif kind == "docm":
        minimal_docx(path, p("x"), main_type="application/vnd.ms-word.document.macroEnabled.main+xml")
    elif kind == "no_main":
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
    elif kind == "bad_xml":
        minimal_docx(path, "<w:p><w:r><w:t>unclosed")
    else:
        path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    res = T.document_extract(str(path))
    assert res.error is not None and res.error.code == code, res.error


def test_zip_bomb_is_refused(work):
    path = work / "bomb.docx"
    minimal_docx(path, p("x"))
    with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/padding.bin", b"\x00" * (60 * 1024 * 1024))
    assert T.document_extract(str(path)).error.code == "input_too_large"
