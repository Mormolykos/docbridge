"""The corruptions the brief names, injected after a clean conversion, in every
format pair docbridge converts. Each test first proves the clean conversion
passes (the control, METHODOLOGY R7: check the fixture too), then changes one
digit and requires FAIL with the exact before/after values located.

The source also contains the near-duplicate 923 456 718, so a transposition
that produces a value existing elsewhere in the document must still be caught:
the numeric check is a sequence comparison, not a set membership test."""
import zipfile

import pytest
from builders import PLAIN_TXT, RICH_MD, report_pdf, write
from conftest import needs_pandoc

from docbridge import tools as T

CORRUPTIONS = [
    # (text in the source, corrupted text, digit-bearing token before, after)
    ("923 456 781", "923 456 718", "781", "718"),
    ("€4,999", "€4,599", "€4,999", "€4,599"),
    ("25/03/2025", "25/03/2026", "25/03/2025", "25/03/2026"),
]


def assert_clean(res):
    assert res.error is None, res.error
    rep = res.report
    assert rep.text_status == "PASS", rep.summary
    assert rep.numeric_status == "PASS", rep.summary
    assert rep.identifier_status == "PASS", rep.summary


def assert_caught(rep, before, after):
    assert rep.status == "FAIL", rep.summary
    assert rep.numeric_status == "FAIL", rep.summary
    assert rep.text_status == "FAIL", rep.summary
    pairs = [(d.source, d.target) for d in rep.differences if d.axis == "numeric"]
    assert (before, after) in pairs, pairs
    d = next(d for d in rep.differences if d.axis == "numeric" and (d.source, d.target) == (before, after))
    assert d.kind == "changed" and d.source_location and d.target_location and d.source_context


def corrupt_docx(src, dst, old, new):
    """Replace the first occurrence of `old` in word/document.xml. Pandoc
    splits runs at spaces, so a grouped number is corrupted in its last run."""
    with zipfile.ZipFile(src) as zin:
        items = {n: zin.read(n) for n in zin.namelist()}
    xml = items["word/document.xml"].decode("utf-8")
    for candidate in (old, old.split()[-1]):
        if candidate != old:
            new_part = new.split()[-1]
            target, repl = f">{candidate}<", f">{new_part}<"
        else:
            target, repl = old, new
        if target in xml:
            xml = xml.replace(target, repl, 1)
            break
    else:
        raise AssertionError(f"{old!r} not in document.xml")
    items["word/document.xml"] = xml.encode("utf-8")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, data in items.items():
            zout.writestr(n, data)


@needs_pandoc
@pytest.mark.parametrize("old,new,before,after", CORRUPTIONS)
def test_markdown_to_docx_catches_one_digit(work, old, new, before, after):
    src = write(work / "rich.md", RICH_MD)
    res = T.markdown_to_docx(str(src))
    assert_clean(res)
    bad = work / "corrupt.docx"
    corrupt_docx(res.outputs[0], bad, old, new)
    rep = T.validate_conversion(str(src), str(bad)).report
    assert_caught(rep, before, after)


@pytest.mark.parametrize("old,new,before,after", CORRUPTIONS)
def test_txt_to_docx_catches_one_digit(work, old, new, before, after):
    text = PLAIN_TXT + "Amount €4,999 due 25/03/2025\n"
    src = write(work / "plain.txt", text)
    res = T.txt_to_docx(str(src))
    assert_clean(res)
    bad = work / "corrupt.docx"
    with zipfile.ZipFile(res.outputs[0]) as zin:
        items = {n: zin.read(n) for n in zin.namelist()}
    xml = items["word/document.xml"].decode("utf-8")
    assert old in xml
    items["word/document.xml"] = xml.replace(old, new, 1).encode("utf-8")
    with zipfile.ZipFile(bad, "w") as zout:
        for n, data in items.items():
            zout.writestr(n, data)
    assert_caught(T.validate_conversion(str(src), str(bad)).report, before, after)


@pytest.mark.parametrize("old,new,before,after", CORRUPTIONS)
def test_pdf_to_markdown_catches_one_digit(work, old, new, before, after):
    src = report_pdf(work / "report.pdf")
    res = T.pdf_to_markdown(str(src))
    assert_clean(res)
    md = res.outputs[0]
    text = open(md, encoding="utf-8").read()
    assert old in text
    bad = write(work / "corrupt.md", text.replace(old, new, 1))
    assert_caught(T.validate_conversion(str(src), str(bad)).report, before, after)


@needs_pandoc
@pytest.mark.parametrize("old,new,before,after", CORRUPTIONS)
def test_docx_to_markdown_catches_one_digit(work, old, new, before, after):
    docx = T.markdown_to_docx(str(write(work / "rich.md", RICH_MD))).outputs[0]
    res = T.docx_to_markdown(docx, output_path=str(work / "back.md"))
    assert_clean(res)
    text = open(res.outputs[0], encoding="utf-8").read()
    assert old in text
    bad = write(work / "corrupt.md", text.replace(old, new, 1))
    assert_caught(T.validate_conversion(docx, str(bad)).report, before, after)


def test_swapping_two_near_duplicate_numbers_is_caught(work):
    # 923 456 781 and 923 456 718 both exist; swapping them keeps the SET of
    # numbers identical. Only an order-aware comparison sees it.
    src = write(work / "a.txt", "first 923456781 then 923456718\n")
    swapped = write(work / "b.txt", "first 923456718 then 923456781\n")
    rep = T.validate_conversion(str(src), str(swapped)).report
    assert rep.numeric_status == "FAIL"
    kinds = {d.kind for d in rep.differences if d.axis == "numeric"}
    assert kinds & {"changed", "moved"}


def test_deleting_or_adding_a_number_is_caught(work):
    src = write(work / "a.txt", "totals: 10 20 30\n")
    for other in ("totals: 10 30\n", "totals: 10 20 30 40\n"):
        rep = T.validate_conversion(str(src), str(write(work / "b.txt", other))).report
        assert rep.numeric_status == "FAIL", other
