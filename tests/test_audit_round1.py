"""Audit round 1 (2026-09-25): the seven findings, at the breadth the audit's
repair gates require - not only its seven original fixtures.

F1 equation lost -> PASS           F5 reference reused under another decoding
F2 HTML link target changed -> PASS F6 HTML table moved past text -> PASS
F3 unreadable final page hidden    F7 bookmark destination lost in merge -> PASS
F4 decomposed text unfindable
"""
import unicodedata

import pymupdf
import pytest
from builders import minimal_docx, p, r, write

from docbridge import reading as RD
from docbridge import tools as T

MATH = ('<m:oMathPara xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:oMath>'
        '<m:r><m:t>9000=1</m:t></m:r></m:oMath></m:oMathPara>')


# ---------------------------------------------------------------------- F1 ---

@pytest.mark.parametrize("placement", ["body", "paragraph", "table", "content_control", "tracked_insertion"])
def test_f1_a_removed_equation_never_passes(work, placement):
    body = {
        "body": p("Total agreed") + MATH,
        "paragraph": p(runs=r("Total agreed ") + MATH.replace("oMathPara", "oMathPara")),
        "table": p("Total agreed") + f"<w:tbl><w:tr><w:tc>{MATH}</w:tc></w:tr></w:tbl>",
        "content_control": p("Total agreed") + f"<w:sdt><w:sdtContent>{MATH}</w:sdtContent></w:sdt>",
        "tracked_insertion": p("Total agreed") + f"<w:customXml>{MATH}</w:customXml>",
    }[placement]
    with_eq = minimal_docx(work / "eq.docx", body)
    without = minimal_docx(work / "no.docx", p("Total agreed"))
    rep = T.document_compare(str(with_eq), str(without)).report
    assert rep.status != "PASS", placement
    assert rep.text_status == "NOT_CHECKED" and rep.axes["text"].reason == "unresolved_content"
    assert "⟦UNREADABLE" in RD.load(with_eq).full


def test_f1_domain_charts_and_diagrams_are_unknown_too(work):
    chart = ('<w:r><w:drawing><wp:inline><wp:docPr id="1" name="c"/><a:graphic '
             'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData '
             'uri="http://schemas.openxmlformats.org/drawingml/2006/chart"/></a:graphic></wp:inline></w:drawing></w:r>')
    doc = minimal_docx(work / "chart.docx", p(runs=r("Revenue ") + chart))
    rep = T.document_compare(str(doc), str(minimal_docx(work / "plain.docx", p("Revenue")))).report
    assert rep.status != "PASS"
    assert any(u.check == "source:embedded_graphic" for u in rep.unsupported_checks)


# ---------------------------------------------------------------------- F2 ---

@pytest.mark.parametrize("a,b", [
    ('<a href="https://safe.invalid/pay">Pay invoice</a>\n', '<a href="https://other.invalid/pay">Pay invoice</a>\n'),
    ("Please <a href='https://safe.invalid/pay'>pay</a> now.\n", "Please <a href='https://other.invalid/pay'>pay</a> now.\n"),
    ('<div><p>See <a href="https://safe.invalid/pay">Pay invoice</a></p></div>\n',
     '<div><p>See <a href="https://other.invalid/pay">Pay invoice</a></p></div>\n'),
    ('<a href=https://safe.invalid/pay>Pay</a>\n', '<a href=https://other.invalid/pay>Pay</a>\n'),
])
def test_f2_changing_only_an_html_link_target_fails(work, a, b):
    rep = T.document_compare(str(write(work / "a.md", a)), str(write(work / "b.md", b))).report
    assert rep.identifier_status == "FAIL" and rep.status == "FAIL"
    assert any(d.axis == "identifier.links" for d in rep.differences)


def test_f2_identical_html_links_still_pass(work):
    text = 'Pay: <a href="https://safe.invalid/pay?a=1&amp;b=2">invoice</a>\n'
    rep = T.document_compare(str(write(work / "a.md", text)), str(write(work / "b.md", text))).report
    assert rep.status == "PASS"
    assert rep.axes["identifier"].details["links"]["source_count"] == 1


# ---------------------------------------------------------------------- F6 ---

@pytest.mark.parametrize("a,b", [
    ("<div>Before<table><tr><td>Amount 9000</td></tr></table>After</div>\n",
     "<div><table><tr><td>Amount 9000</td></tr></table>BeforeAfter</div>\n"),
    ("<div>One<table><tr><td>A 1</td></tr></table>Two<table><tr><td>B 2</td></tr></table>Three</div>\n",
     "<div>One<table><tr><td>B 2</td></tr></table>Two<table><tr><td>A 1</td></tr></table>Three</div>\n"),
    ("<div><p>Lead 5</p><table><tr><td>X 7</td></tr></table><p>Tail 8</p></div>\n",
     "<div><table><tr><td>X 7</td></tr></table><p>Lead 5</p><p>Tail 8</p></div>\n"),
])
def test_f6_moving_an_html_table_fails_the_text_order(work, a, b):
    rep = T.document_compare(str(write(work / "a.md", a)), str(write(work / "b.md", b))).report
    assert rep.text_status == "FAIL" and rep.status == "FAIL"


def test_f6_text_around_a_table_stays_separate_and_in_order(work):
    from docbridge.canonical import Origin
    from docbridge.extract import extract
    md = write(work / "o.md", "<div>Before<table><tr><td>Amount 9000</td></tr></table>After</div>\n")
    stream = [t.display() for t in extract(md).stream(frozenset({Origin.CONTENT}))]
    assert stream == ["Before", "Amount", "9000", "After"]


# ---------------------------------------------------------------------- F7 ---

def _bm_pdf(path, pages, toc):
    d = pymupdf.open()
    for i in range(pages):
        d.new_page().insert_text((72, 72), f"page {i + 1}")
    d.set_toc(toc)
    d.save(str(path))
    d.close()
    return path


URI = {"kind": pymupdf.LINK_URI, "uri": "https://safe.invalid/pay"}


def test_f7_merge_carries_bookmark_actions(work):
    a = _bm_pdf(work / "a.pdf", 1, [[1, "Payment destination", -1, URI]])
    b = _bm_pdf(work / "b.pdf", 2, [[1, "Second", 2, {"kind": pymupdf.LINK_GOTO, "page": 1,
                                                       "to": pymupdf.Point(0, 100)}]])
    res = T.pdf_merge([str(a), str(b)], str(work / "m.pdf"))
    assert res.report.status == "PASS", res.report.summary
    toc = pymupdf.open(res.outputs[0]).get_toc(simple=False)
    assert toc[0][3]["uri"] == "https://safe.invalid/pay"
    assert toc[1][2] == 3 and toc[1][3]["page"] == 2          # shifted by the one page before it


def test_f7_a_lost_bookmark_destination_fails(work):
    from docbridge.convert import pdf_ops
    a = _bm_pdf(work / "a.pdf", 1, [[1, "Payment destination", -1, URI]])
    dead = _bm_pdf(work / "dead.pdf", 1, [[1, "Payment destination", -1, {"kind": pymupdf.LINK_NONE}]])
    with pymupdf.open(str(dead)) as d:
        res, diffs = pdf_ops.bookmark_check(pdf_ops.expected_merge_bookmarks([a]), pdf_ops.bookmark_sigs(d))
    assert res.status == "FAIL" and "safe.invalid" in diffs[0].note


def test_f7_split_carries_actions_in_range_and_reports_the_rest(work):
    src = _bm_pdf(work / "s.pdf", 3, [[1, "Pay", -1, URI], [1, "Two", 2, {"kind": pymupdf.LINK_GOTO, "page": 1}],
                                      [1, "Three", 3, {"kind": pymupdf.LINK_GOTO, "page": 2}]])
    res = T.pdf_split(str(src), ranges=["2-3"], output_dir=str(work / "parts"))
    assert res.report.status == "PASS", res.report.summary
    toc = pymupdf.open(res.outputs[0]).get_toc(simple=False)
    assert [(e[1], e[2], e[3]["page"]) for e in toc] == [("Two", 1, 0), ("Three", 2, 1)]
    assert any(w.code == "bookmarks_not_carried" for w in res.report.warnings)
    assert any("not carried" in n and "safe.invalid" in n for n in res.conversion_notes)


# ---------------------------------------------------------------------- F3 ---

def _trailing(path, trailing: int):
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "Readable cover " * 40)
    for _ in range(trailing):
        d.new_page().draw_rect(pymupdf.Rect(72, 72, 300, 300), color=(0, 0, 0), fill=(0, 0, 0))
    d.save(str(path))
    d.close()
    return path


@pytest.mark.parametrize("cap", [100, 12000])
def test_f3_unreadable_trailing_pages_are_reported_by_whole_and_continued_reads(work, cap):
    pdf = _trailing(work / "t.pdf", 2)
    first = T.document_read(str(pdf), max_chars=cap)
    assert first.range_unreadable_units == [2, 3]
    seen = set(first.unreadable_units)
    res = first
    while res.next_cursor:
        res = T.document_read(str(pdf), cursor=res.next_cursor, max_chars=cap)
        assert res.range_unreadable_units == [2, 3]
        seen |= set(res.unreadable_units)
    assert seen == {2, 3}
    assert res.excerpt.last_unit == 3


# ---------------------------------------------------------------------- F4 ---

@pytest.mark.parametrize("doc_form,query_form", [("NFD", "NFD"), ("NFD", "NFC"), ("NFC", "NFD"), ("NFC", "NFC")])
def test_f4_search_finds_composed_and_decomposed_text(work, doc_form, query_form):
    text = unicodedata.normalize(doc_form, "Invoice café Ζωή Ångström 12345")
    path = write(work / "u.txt", text)
    for word in ("café", "Ζωή", "Ångström"):
        found = T.document_search(str(path), unicodedata.normalize(query_form, word), case_sensitive=True)
        assert found.matches_total == 1, (doc_form, query_form, word)
        m = found.matches[0]
        assert m.text == unicodedata.normalize(doc_form, word)       # original text, original offsets
        assert RD.load(path).full[m.char_start:m.char_end] == m.text
    # composition is the only thing folded: an unaccented query never matches the accented word
    assert T.document_search(str(path), "cafe", case_sensitive=True).matches_total == 0


# ---------------------------------------------------------------------- F5 ---

def test_f5_references_are_bound_to_the_decoding(work):
    path = work / "e.txt"
    path.write_bytes(("é" * 100 + " NEEDLE " + "z" * 200).encode("utf-8"))
    hit = T.document_search(str(path), "NEEDLE", encoding="utf-8").matches[0]
    first = T.document_read(str(path), encoding="utf-8", max_chars=100)
    assert T.document_read_around(str(path), hit.match_id, encoding="latin-1").error.code == "stale_reference"
    assert T.document_read(str(path), cursor=first.next_cursor, encoding="latin-1").error.code == "stale_reference"
    same = T.document_read_around(str(path), hit.match_id, encoding="utf-8")
    assert same.error is None and same.match["text"] == "NEEDLE"


def test_f5_an_ascii_file_reads_identically_under_both_decodings(work):
    path = write(work / "a.txt", "plain ascii NEEDLE here\n")
    hit = T.document_search(str(path), "NEEDLE", encoding="utf-8").matches[0]
    again = T.document_read_around(str(path), hit.match_id, encoding="latin-1")
    assert again.error is None and again.match["text"] == "NEEDLE"   # same text, same offsets: still valid
