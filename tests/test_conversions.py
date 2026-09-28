"""Round trips, structure checks, and the refusals the contract promises."""
import json
import zipfile

from builders import PLAIN_TXT, RICH_MD, report_pdf, text_pdf, write
from conftest import needs_pandoc

from docbridge import tools as T
from docbridge.extract import extract


# ---------------------------------------------------------- markdown <-> docx

@needs_pandoc
def test_markdown_to_docx_rich_document_passes_every_checkable_axis(work):
    res = T.markdown_to_docx(str(write(work / "rich.md", RICH_MD)))
    rep = res.report
    assert res.operation_completed and res.error is None
    assert rep.status == "PASS", rep.summary
    for name in ("headings", "lists", "tables", "emphasis"):
        assert rep.structure_checks[name].status == "PASS", (name, rep.structure_checks[name])
    assert rep.structure_checks["headings"].compared == 5
    assert rep.structure_checks["tables"].details == {"source_count": 1, "target_count": 1}
    assert rep.axes["identifier"].details["links"]["status"] == "PASS"
    # the PASS names what it covers and lists what it does not
    assert rep.required_axes == ["text", "numeric", "identifier"]
    assert "layout UNSUPPORTED" in rep.status_basis
    assert any(u.check == "layout" for u in rep.unsupported_checks)


@needs_pandoc
def test_markdown_docx_markdown_round_trip_preserves_the_canonical_form(work):
    src = write(work / "rich.md", RICH_MD)
    docx = T.markdown_to_docx(str(src)).outputs[0]
    back = T.docx_to_markdown(docx, output_path=str(work / "back.md"))
    assert back.report.status == "PASS", back.report.summary
    rep = T.validate_conversion(str(src), back.outputs[0]).report
    assert rep.status == "PASS", rep.summary
    assert all(rep.structure_checks[n].status == "PASS" for n in ("headings", "lists", "tables", "emphasis"))


@needs_pandoc
def test_raw_html_dropped_by_the_converter_is_reported_missing(work):
    src = write(work / "h.md", "Intro 1.\n\n<div>html block with 42</div>\n\nOutro 2.\n")
    res = T.markdown_to_docx(str(src))
    assert res.operation_completed
    rep = res.report
    assert rep.status == "FAIL"
    assert ("42", None) in [(d.source, d.target) for d in rep.differences if d.axis == "numeric"]
    assert any("raw HTML" in n for n in res.conversion_notes)


@needs_pandoc
def test_images_are_not_carried_and_say_so(work):
    src = write(work / "i.md", "Before.\n\n![Figure 7 alt](chart.png)\n\nAfter.\n")
    res = T.markdown_to_docx(str(src))
    rep = res.report
    assert rep.text_status == "PASS"          # the alt text survives as text
    assert rep.image_status == "UNSUPPORTED"
    assert any("not embedded" in n for n in res.conversion_notes)
    assert any(u.check == "source:image" for u in rep.unsupported_checks)


def test_markdown_list_number_that_never_renders_is_warned(work):
    src = write(work / "l.md", "1. one\n2. two\n7. seven\n")
    canon = extract(src)
    assert any(w.code == "md_list_number_not_rendered" for w in canon.warnings)
    labels = [b.text() for b in canon.list_items()]
    assert labels[2].startswith("3.")          # CommonMark renders 3, whatever was typed


# ----------------------------------------------------------------- txt -> docx

def test_txt_to_docx_passes_and_keeps_tabs_and_page_breaks(work):
    src = write(work / "p.txt", PLAIN_TXT)
    res = T.txt_to_docx(str(src))
    assert res.report.status == "PASS", res.report.summary
    xml = zipfile.ZipFile(res.outputs[0]).read("word/document.xml").decode()
    assert "<w:tab/>" in xml and 'w:type="page"' in xml


def test_txt_with_control_characters_is_refused_with_positions(work):
    src = work / "c.txt"
    src.write_bytes("ok line\nbad\x07bell\n".encode())
    res = T.txt_to_docx(str(src))
    assert not res.operation_completed and res.outputs == []
    assert res.error.code == "unsupported_content"
    assert "line 2 col 4: U+0007" in res.error.message
    assert not (work / "c.docx").exists()


def test_undecodable_text_is_refused_not_guessed(work):
    src = work / "latin.txt"
    src.write_bytes("Blåbær".encode("cp1252"))
    res = T.txt_to_docx(str(src))
    assert res.error.code == "undecodable_text"
    ok = T.txt_to_docx(str(src), encoding="cp1252", output_path=str(work / "ok.docx"))
    assert ok.report.status == "PASS"


# ------------------------------------------------------------------ pdf -> md

def test_pdf_to_markdown_validates_text_and_marks_structure_as_inferred(work):
    src = report_pdf(work / "r.pdf")
    res = T.pdf_to_markdown(str(src))
    rep = res.report
    assert rep.status == "PASS", rep.summary
    assert rep.structure_checks["pages"].status == "PASS"
    for name in ("headings", "lists", "tables"):
        assert rep.structure_checks[name].status == "NOT_CHECKED"
        assert rep.structure_checks[name].reason == "inferred_structure"
    md = open(res.outputs[0], encoding="utf-8").read()
    assert "<!-- docbridge:page 1 -->" in md
    assert "| Name | Amount | Date |" in md


def test_scanned_page_is_never_a_pass_and_nothing_is_fabricated(work):
    src = report_pdf(work / "s.pdf", scanned_page=True)
    res = T.pdf_to_markdown(str(src))
    rep = res.report
    assert rep.status == "NOT_CHECKED"
    assert rep.text_status == "NOT_CHECKED" and rep.axes["text"].reason == "unresolved_content"
    assert rep.numeric_status != "PASS"
    md = open(res.outputs[0], encoding="utf-8").read()
    assert "555" not in md                      # the number exists only as pixels
    assert "unreadable-page 2" in md
    assert any(w.code == "pdf_no_text_layer" for w in rep.warnings)


def test_pdf_blank_and_rotated_pages(work):
    src = text_pdf(work / "b.pdf", ["<p>Page one 1</p>", None, "<p>Rotated three 3</p>"], rotations={2: 90})
    rep = T.pdf_to_markdown(str(src)).report
    assert rep.status == "PASS", rep.summary
    assert rep.source_page_count == rep.target_page_count == 3


def test_text_on_the_wrong_page_is_a_structure_failure(work):
    src = text_pdf(work / "w.pdf", ["<p>alpha 1</p>", "<p>beta 2</p>"])
    moved = write(work / "w.md", "<!-- docbridge:page 1 -->\n\nalpha 1\n\nbeta 2\n\n<!-- docbridge:page 2 -->\n")
    rep = T.validate_conversion(str(src), str(moved)).report
    assert rep.text_status == "PASS"
    assert rep.structure_checks["pages"].status == "FAIL"


# --------------------------------------------------------- extract / compare

def test_document_extract_lists_every_number_and_identifier(work):
    src = write(work / "rich.md", RICH_MD)
    doc = T.document_extract(str(src), kinds=["numbers", "identifiers", "dates", "urls"],
                             output_path=str(work / "full.json")).document
    values = [n["value"] for n in doc["numbers"]]
    for v in ("123456789012345", "3.14159", "−12.5", "-7", "€4,999", "25/03/2025", "094014201"):
        assert v in values, v
    assert "923 456 781" in [i["value"] for i in doc["identifiers"]]
    assert any(d["value"] == "25.03.2026" for d in doc["dates"])
    assert any(u["value"].startswith("https://example.com/x2") for u in doc["urls"])
    assert doc["semantic_interpretation"].startswith("none")
    full = json.loads((work / "full.json").read_text(encoding="utf-8"))
    assert full["counts"] == doc["counts"]


def test_document_extract_caps_lists_and_states_what_it_left_out(work):
    src = write(work / "many.txt", " ".join(str(i) for i in range(50)) + "\n")
    doc = T.document_extract(str(src), kinds=["numbers"], max_items=10).document
    assert len(doc["numbers"]) == 10 and doc["truncated"]["numbers"] == 40


def test_document_compare_is_symmetric_in_what_it_finds(work):
    a = write(work / "a.txt", "one 1 two 2\n")
    b = write(work / "b.txt", "one 1 two 3\n")
    ab = T.document_compare(str(a), str(b)).report
    ba = T.document_compare(str(b), str(a)).report
    assert ab.status == ba.status == "FAIL"
    assert [(d.source, d.target) for d in ab.differences if d.axis == "numeric"] == [("2", "3")]
    assert [(d.source, d.target) for d in ba.differences if d.axis == "numeric"] == [("3", "2")]


def test_identity_passes_for_every_format(work):
    pdf = report_pdf(work / "r.pdf")
    md = write(work / "r.md", RICH_MD)
    txt = write(work / "r.txt", PLAIN_TXT)
    for f in (pdf, md, txt):
        rep = T.validate_conversion(str(f), str(f)).report
        assert rep.status == "PASS", (f, rep.summary)


def test_report_written_to_disk_matches_the_returned_one(work):
    src = write(work / "p.txt", PLAIN_TXT)
    res = T.txt_to_docx(str(src), report_path=str(work / "report.json"))
    on_disk = json.loads((work / "report.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == res.report.status and on_disk["source_sha256"] == res.report.source_sha256
