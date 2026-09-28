"""pdf_merge / pdf_split: preservation proven per page, and tampering caught."""
import pymupdf
import pytest
from builders import report_pdf, text_pdf

from docbridge import tools as T
from docbridge.convert import pdf_ops


@pytest.fixture
def three(work):
    a = report_pdf(work / "a.pdf", extra_pages=[None])                      # page 2 blank
    b = text_pdf(work / "b.pdf", ["<p>B one 111</p>", "<p>B two rotated 222</p>"], rotations={1: 90},
                 toc=[[1, "B start", 1], [2, "B rotated", 2]], links={0: "https://bedvibe.studio/b"})
    c = text_pdf(work / "c.pdf", ["<p>C only 333</p>"], toc=[[1, "C", 1]])
    return [a, b, c]


def test_merge_preserves_every_page_in_order(work, three):
    res = T.pdf_merge([str(p) for p in three], str(work / "m.pdf"))
    rep = res.report
    assert rep.status == "PASS", rep.summary
    assert rep.source_page_count == rep.target_page_count == 5
    for name in ("pages", "page_identity", "page_rotation", "page_size", "page_links", "bookmarks"):
        assert rep.structure_checks[name].status == "PASS", name
    assert rep.layout_status == "PASS" and rep.axes["layout"].compared == 5
    toc = pymupdf.open(res.outputs[0]).get_toc()
    assert toc == [[1, "B start", 3], [2, "B rotated", 4], [1, "C", 5]]
    assert pymupdf.open(res.outputs[0])[3].rotation == 90


def _tamper(src, dst, fn):
    doc = pymupdf.open(str(src))
    fn(doc)
    doc.save(str(dst))
    doc.close()


@pytest.mark.parametrize("damage,expect", [
    (lambda d: d.delete_page(1), "pages"),
    (lambda d: d.move_page(4, 0), "page_identity"),
    (lambda d: d[3].set_rotation(0), "page_rotation"),
])
def test_page_level_tampering_is_caught(work, three, damage, expect):
    merged = T.pdf_merge([str(p) for p in three], str(work / "m.pdf")).outputs[0]
    bad = work / "bad.pdf"
    _tamper(merged, bad, damage)
    srcs = [pymupdf.open(str(p)) for p in three]
    out = pymupdf.open(str(bad))
    pairs, k = [], 0
    for s, d in zip(three, srcs, strict=True):
        for j in range(d.page_count):
            if k < out.page_count:
                pairs.append((s.name, j + 1, d[j], k + 1, out[k]))
            k += 1
    checks, layout, diffs, rows = pdf_ops.compare_pages(pairs, 72)
    if expect == "pages":
        assert out.page_count == 4 and k == 5
        assert layout.status == "FAIL"     # everything after the gap shifted
    else:
        assert checks[expect].status == "FAIL"
        assert layout.status == "FAIL"


def test_a_changed_digit_on_a_merged_page_fails_validation(work, three):
    # an in-place corruption: the same page regenerated with one digit changed
    from builders import REPORT_HTML
    merged = T.pdf_merge([str(p) for p in three], str(work / "m.pdf")).outputs[0]
    bad_a = report_pdf(work / "a_bad.pdf", extra_pages=[None],
                       html=REPORT_HTML.replace("923 456 781", "923 456 718", 1))
    bad = T.pdf_merge([str(bad_a), str(three[1]), str(three[2])], str(work / "bad.pdf")).outputs[0]
    rep = T.validate_conversion(merged, bad).report
    assert rep.numeric_status == "FAIL" and rep.status == "FAIL"
    numeric = [(d.kind, d.source, d.target) for d in rep.differences if d.axis == "numeric"]
    assert numeric == [("changed", "781", "718")], numeric
    ident = [(d.source, d.target) for d in rep.differences if d.axis == "identifier"]
    assert ("923 456 781", "923 456 718") in ident


def test_split_ranges_and_each_page(work, three):
    merged = T.pdf_merge([str(p) for p in three], str(work / "m.pdf")).outputs[0]
    res = T.pdf_split(merged, ranges=["1-2", "4", "5-"], output_dir=str(work / "parts"))
    rep = res.report
    assert rep.status == "PASS", rep.summary
    assert [p.split("\\")[-1] for p in res.outputs] == ["m_p1-2.pdf", "m_p4.pdf", "m_p5.pdf"]
    assert [pymupdf.open(p).page_count for p in res.outputs] == [2, 1, 1]
    assert pymupdf.open(res.outputs[1])[0].rotation == 90
    assert pymupdf.open(res.outputs[1]).get_toc() == [[1, "B rotated", 1]]   # level clamped from 2
    each = T.pdf_split(merged, each_page=True, output_dir=str(work / "each"))
    assert each.report.status == "PASS" and len(each.outputs) == 5


@pytest.mark.parametrize("kwargs,code", [
    ({"ranges": ["0-2"]}, "invalid_argument"),
    ({"ranges": ["3-1"]}, "invalid_argument"),
    ({"ranges": ["9"]}, "invalid_argument"),
    ({"ranges": ["1-2", "1-2"]}, "invalid_argument"),
    ({"ranges": ["a"]}, "invalid_argument"),
    ({}, "invalid_argument"),
    ({"ranges": ["1"], "each_page": True}, "invalid_argument"),
])
def test_bad_split_requests_are_refused_before_writing(work, three, kwargs, code):
    res = T.pdf_split(str(three[0]), output_dir=str(work / "x"), **kwargs)
    assert res.error is not None and res.error.code == code
    assert not (work / "x").exists() or not any((work / "x").iterdir())


def test_split_refuses_to_overwrite_any_existing_part(work, three):
    first = T.pdf_split(str(three[1]), each_page=True, output_dir=str(work / "p"))
    assert first.report.status == "PASS"
    again = T.pdf_split(str(three[1]), each_page=True, output_dir=str(work / "p"))
    assert again.error.code == "output_exists"
    forced = T.pdf_split(str(three[1]), each_page=True, output_dir=str(work / "p"), overwrite=True)
    assert forced.report.status == "PASS"


def test_scanned_page_merges_with_proof_by_pixels(work):
    scan = report_pdf(work / "scan.pdf", scanned_page=True)
    rep = T.pdf_merge([str(scan)], str(work / "m.pdf")).report
    assert rep.status == "PASS"                     # proven by page identity + raster
    assert rep.text_status == "NOT_CHECKED"         # but never claimed from text
    assert "text NOT_CHECKED" in rep.status_basis
