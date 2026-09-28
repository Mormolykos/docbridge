"""Hostile and damaged inputs: a documented error or an honest report, never a crash."""
import pymupdf
import pytest
from builders import report_pdf, write

from docbridge import tools as T


def test_pdf_header_then_garbage(work):
    src = work / "g.pdf"
    src.write_bytes(b"%PDF-1.7\n" + b"\x00garbage" * 50)
    res = T.document_extract(str(src))
    assert res.error is not None and res.error.code == "malformed_input"


def test_truncated_pdf_is_an_error_or_a_warned_report(work):
    full = report_pdf(work / "full.pdf").read_bytes()
    cut = work / "cut.pdf"
    cut.write_bytes(full[: len(full) // 2])
    res = T.validate_conversion(str(cut), str(cut))
    if res.error is not None:
        assert res.error.code == "malformed_input"
    else:
        assert any(w.code == "pdf_repaired" for w in res.report.warnings)


def test_password_protected_pdf_is_refused(work):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret 42")
    path = work / "locked.pdf"
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
    assert T.document_extract(str(path)).error.code == "encrypted_input"


def test_owner_password_only_pdf_is_read_with_a_warning(work):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "permissions 7")
    path = work / "perm.pdf"
    doc.save(str(path), encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="",
             permissions=int(pymupdf.PDF_PERM_ACCESSIBILITY))
    res = T.pdf_merge([str(path)], str(work / "m.pdf"))
    assert res.report.status == "PASS"
    assert any(w.code == "pdf_encrypted_permissions" for w in res.report.warnings)


@pytest.mark.parametrize("name,data,code", [
    ("zip.pdf", b"PK\x03\x04" + b"\x00" * 100, "format_mismatch"),
    ("pdf.md", b"%PDF-1.4\n...", "format_mismatch"),
    ("text.docx", b"just text", "format_mismatch"),
    ("x.odt", b"PK\x03\x04", "unsupported_format"),
    ("bad.md", b"\xff\xfe\x00", "undecodable_text"),
])
def test_format_confusion_is_refused(work, name, data, code):
    src = work / name
    src.write_bytes(data)
    assert T.document_extract(str(src)).error.code == code


def test_empty_markdown_is_a_vacuous_pass_and_says_so(work):
    src = write(work / "e.md", "")
    rep = T.validate_conversion(str(src), str(src)).report
    assert rep.status == "PASS"
    assert rep.axes["text"].compared == 0 and rep.axes["numeric"].details.get("vacuous") is True
