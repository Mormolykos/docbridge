"""Path policy: refusals happen before any file is opened or written."""
import os

import pytest
from builders import PLAIN_TXT, write

from docbridge import tools as T


@pytest.fixture
def txt(work):
    return write(work / "p.txt", PLAIN_TXT)


def test_traversal_is_refused_even_when_it_resolves_inside(work, txt):
    sneaky = str(work / "sub" / ".." / "p.txt")
    assert T.txt_to_docx(sneaky).error.code == "path_traversal"


def test_outside_the_allowed_roots(work, txt, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere")
    outside = write(other / "o.txt", "x 1\n")
    assert T.txt_to_docx(str(outside)).error.code == "path_outside_roots"
    assert T.txt_to_docx(str(txt), output_path=str(other / "o.docx")).error.code == "path_outside_roots"


@pytest.mark.parametrize("bad,code", [
    ("\\\\server\\share\\x.txt", "path_forbidden"),
    ("//server/share/x.txt", "path_forbidden"),
    ("p.txt", "path_not_absolute"),
    ("", "invalid_argument"),
])
def test_forbidden_shapes(work, bad, code):
    assert T.txt_to_docx(bad).error.code == code


@pytest.mark.skipif(os.name != "nt", reason="Windows path rules")
def test_windows_specific_refusals(work, txt):
    assert T.txt_to_docx(str(txt) + ":stream").error.code == "path_forbidden"
    assert T.txt_to_docx(str(txt), output_path=str(work / "NUL.docx")).error.code == "path_forbidden"
    assert T.txt_to_docx(str(txt), output_path=str(work / "com1.docx")).error.code == "path_forbidden"


def test_the_source_is_never_overwritten(work, txt):
    md = write(work / "same.md", "# t\n\n1 2 3\n")
    res = T.pdf_merge([str(md)], str(md), overwrite=True)
    assert res.error.code in ("would_overwrite_source", "output_extension_mismatch", "invalid_argument")
    # a real same-path attempt with a matching extension
    res = T.validate_conversion(str(txt), str(txt), report_path=str(txt), overwrite=True)
    assert res.error.code in ("would_overwrite_source", "output_extension_mismatch")
    assert txt.read_text(encoding="utf-8") == PLAIN_TXT


def test_existing_output_needs_overwrite(work, txt):
    assert T.txt_to_docx(str(txt)).report.status == "PASS"
    assert T.txt_to_docx(str(txt)).error.code == "output_exists"
    assert T.txt_to_docx(str(txt), overwrite=True).report.status == "PASS"


def test_extension_must_match(work, txt):
    assert T.txt_to_docx(str(txt), output_path=str(work / "x.pdf")).error.code == "output_extension_mismatch"


def test_a_bad_report_path_is_refused_before_the_conversion_runs(work, txt):
    res = T.txt_to_docx(str(txt), report_path=str(work / "r.txt"))
    assert res.error.code == "output_extension_mismatch"
    assert not (work / "p.docx").exists()


def test_parent_directories_are_created_inside_the_roots(work, txt):
    res = T.txt_to_docx(str(txt), output_path=str(work / "a" / "b" / "c.docx"))
    assert res.report.status == "PASS"


def test_no_temporary_files_are_left_behind(work, txt):
    T.txt_to_docx(str(txt))
    T.txt_to_docx(str(work / "missing.txt"))
    leftovers = [p.name for p in work.rglob("*docbridge-tmp*")]
    assert leftovers == []


def test_input_size_limit(work, txt, monkeypatch):
    monkeypatch.setenv("DOCBRIDGE_MAX_INPUT_MB", "0.00001")
    assert T.txt_to_docx(str(txt)).error.code == "input_too_large"
