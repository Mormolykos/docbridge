"""The evidence invariant, attacked as properties.

docbridge may reduce the volume of its own responses, but it never alters,
summarizes, paraphrases, semantically filters, or silently truncates source
evidence. Stated as checkable properties:

    R1  following next_cursor from any start, with any max_chars, rebuilds the
        requested range exactly - no character lost, added or changed
    R2  every excerpt equals reading_text[char_start:char_end] and hashes to its
        text_sha256; `truncated` is true exactly when something was held back
    R3  "verbatim" means what VALIDATION_MODEL.md says per format, checked
        against an independent read of the file
    R4  a literal search for any span of the document finds it at its offset
    R5  read_around always contains its match; what it leaves out on either side
        is stated and retrievable
    R6  a cursor or match_id from another version of the file is refused
    R7  a report summary never states a different verdict from its full report
"""
import hashlib
import random
import re

import pymupdf
import pytest
from builders import PLAIN_TXT, RICH_MD, minimal_docx, p, r, report_pdf, text_pdf, write
from conftest import needs_pandoc

from docbridge import reading as RD
from docbridge import tools as T
from docbridge.extract.pdf import TEXT_FLAGS

SEED = 20260925


def fuzz_text(rng: random.Random) -> str:
    # decomposed forms are in the vocabulary since audit F4: the generator's
    # blind spot was the defect's blind spot
    words = ["§7(3)", "Annex", "B", "notwithstanding", "except", "Ελληνικά", "Blåbær", "€4,999", "923",
             "456", "781", "25/03/2025", "ﬁnal", "co­operate", "—", "“quoted”", "\t", "a", "the",
             "café", "Ζωή", "Ångström", "café"]
    lines = []
    for _ in range(rng.randrange(1, 40)):
        lines.append(" ".join(rng.choice(words) for _ in range(rng.randrange(0, 14))))
    return rng.choice(["\n", "\r\n"]).join(lines) + rng.choice(["", "\n"])


@pytest.fixture
def corpus(work):
    """Documents of every format, including the awkward ones."""
    docs = [write(work / "plain.txt", PLAIN_TXT), write(work / "rich.md", RICH_MD),
            report_pdf(work / "report.pdf", scanned_page=True),
            text_pdf(work / "blank.pdf", ["<p>one 1</p>", None, "<p>three 3</p>"], rotations={2: 90})]
    docs.append(minimal_docx(work / "eq.docx", p(runs=r("Let ") + "<m:oMath><m:r><m:t>x=5</m:t></m:r></m:oMath>")
                             + p("after 7")))
    txt_docx = T.txt_to_docx(str(docs[0]), output_path=str(work / "plain.docx"))
    docs.append(work / "plain.docx")
    assert txt_docx.error is None
    # a PDF ending in pages with drawn content and no text layer (audit F3)
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "Readable cover 1 " * 20)
    for _ in range(2):
        d.new_page().draw_rect(pymupdf.Rect(72, 72, 300, 300), color=(0, 0, 0), fill=(0, 0, 0))
    d.save(str(work / "trailing.pdf"))
    d.close()
    docs.append(work / "trailing.pdf")
    rng = random.Random(SEED)
    for i in range(12):
        path = work / f"fuzz{i}.txt"
        path.write_bytes(fuzz_text(rng).encode("utf-8"))
        docs.append(path)
    return docs


def chain(path, max_chars, start=1, end=None):
    """Follow next_cursor to the end; return the pieces and the results."""
    first = T.document_read(str(path), start=start, end=end, max_chars=max_chars)
    assert first.error is None, first.error
    results = [first]
    while results[-1].next_cursor:
        nxt = T.document_read(str(path), cursor=results[-1].next_cursor, max_chars=max_chars)
        assert nxt.error is None, nxt.error
        results.append(nxt)
        assert len(results) < 100000
    return results


def check_excerpt(rt, res):
    ex = res.excerpt
    assert ex.text == rt.full[ex.char_start:ex.char_end]
    assert ex.text_sha256 == hashlib.sha256(ex.text.encode("utf-8")).hexdigest()
    assert res.truncated == (res.remaining_chars > 0 or res.omitted_before_chars > 0)
    assert res.invariant.startswith("docbridge may reduce the volume")


def test_r1_r2_cursor_chains_rebuild_every_document_exactly(corpus):
    rng = random.Random(f"{SEED}-r1")
    for path in corpus:
        rt = RD.load(path)
        for _ in range(4):
            cap = rng.choice([100, 101, 137, 400, 4000])
            results = chain(path, cap)
            for res in results:
                check_excerpt(rt, res)
                assert res.range_unreadable_units == [u for u in rt.unreadable]
            assert "".join(res.excerpt.text for res in results) == rt.full, (path.name, cap)
            assert results[-1].remaining_chars == 0 and not results[-1].truncated
            # every unreadable unit is reported by the excerpt that reaches it (audit F3)
            assert set().union(*(res.unreadable_units for res in results)) == set(rt.unreadable)
            assert results[-1].excerpt.last_unit == len(rt.units)
        # a unit range, too
        n = len(rt.units)
        a = rng.randrange(1, n + 1)
        b = rng.randrange(a, n + 1)
        results = chain(path, 100, a, b)
        lo, hi = rt.unit_span(a, b)
        assert "".join(res.excerpt.text for res in results) == rt.full[lo:hi]


def test_r3_verbatim_means_what_the_model_says(work, corpus):
    for path in corpus:
        rt = RD.load(path)
        if rt.format in ("txt", "md"):
            raw = path.read_bytes().decode("utf-8-sig")
            assert rt.full == raw.replace("\r\n", "\n").replace("\r", "\n")
        elif rt.format == "pdf":
            doc = pymupdf.open(str(path))
            assert rt.units == [doc[i].get_text("text", flags=TEXT_FLAGS) for i in range(doc.page_count)]
    eq = RD.load(work / "eq.docx")
    assert "⟦UNREADABLE: unresolved_content⟧" in eq.full and eq.unreadable == [1]


def test_r4_literal_search_finds_every_span_at_its_offset(corpus):
    rng = random.Random(f"{SEED}-r4")
    for path in corpus:
        rt = RD.load(path)
        tokens = [m for m in re.finditer(r"\S+", rt.full)]
        if not tokens:
            continue
        for _ in range(15):
            i = rng.randrange(len(tokens))
            j = min(len(tokens) - 1, i + rng.randrange(0, 4))
            s, e = tokens[i].start(), tokens[j].end()
            query = rt.full[s:e]
            if any(ch in query for ch in "­​⁠﻿"):
                continue
            found = T.document_search(str(path), query, case_sensitive=True, max_matches=500)
            assert found.error is None, found.error
            starts = {m.char_start for m in found.matches}
            assert s in starts or found.matches_total > 500, (path.name, query)
            for m in found.matches:
                assert m.text == rt.full[m.char_start:m.char_end]


def test_search_folds_only_what_is_declared(work):
    path = write(work / "f.txt", "The ﬁnal co­operation\nOrg.nr\n923 456 781 and CAPS\n")
    assert T.document_search(str(path), "final").matches[0].text == "ﬁnal"          # ligature folded
    assert T.document_search(str(path), "cooperation").matches_total == 1          # soft hyphen folded
    across = T.document_search(str(path), "Org.nr 923 456").matches[0]
    assert across.text == "Org.nr\n923 456" and across.unit == 2 and across.end_unit == 3
    assert T.document_search(str(path), "caps").matches_total == 1
    assert T.document_search(str(path), "caps", case_sensitive=True).matches_total == 0
    assert T.document_search(str(path), "cooperat1on").matches_total == 0          # nothing else folded
    assert T.document_search(str(path), "(", regex=True).error.code == "invalid_argument"


def test_r5_read_around_contains_its_match_and_states_what_it_left_out(corpus):
    rng = random.Random(f"{SEED}-r5")
    for path in corpus:
        rt = RD.load(path)
        tokens = [m for m in re.finditer(r"\S+", rt.full)]
        if not tokens:
            continue
        for _ in range(6):
            tok = rng.choice(tokens)
            found = T.document_search(str(path), tok.group(0), case_sensitive=True, max_matches=1)
            if not found.matches:
                continue
            m = found.matches[0]
            cap = rng.choice([100, 150, 1000, 12000])
            res = T.document_read_around(str(path), m.match_id, before=rng.randrange(0, 3),
                                         after=rng.randrange(0, 3), max_chars=cap)
            assert res.error is None, res.error
            check_excerpt(rt, res)
            ex = res.excerpt
            assert ex.char_start <= m.char_start and m.char_end <= ex.char_end
            lo, hi = rt.unit_span(*res.requested_units)
            assert res.omitted_before_chars == ex.char_start - lo
            assert res.remaining_chars == hi - ex.char_end
            rebuilt = ""
            if res.before_cursor:
                rebuilt += "".join(x.excerpt.text for x in _follow(path, res.before_cursor))
            rebuilt += ex.text
            if res.next_cursor:
                rebuilt += "".join(x.excerpt.text for x in _follow(path, res.next_cursor))
            assert rebuilt == rt.full[lo:hi]


def _follow(path, cursor):
    out = [T.document_read(str(path), cursor=cursor, max_chars=100000)]
    while out[-1].next_cursor:
        out.append(T.document_read(str(path), cursor=out[-1].next_cursor, max_chars=100000))
    return out


def test_r6_references_from_another_version_are_refused(work):
    path = write(work / "v.txt", "alpha 1\nbeta 2\ngamma 3\n" * 50)
    res = T.document_read(str(path), max_chars=100)
    hit = T.document_search(str(path), "beta 2").matches[0]
    path.write_text("alpha 1\nbeta 9\n", encoding="utf-8")
    assert T.document_read(str(path), cursor=res.next_cursor).error.code == "stale_reference"
    assert T.document_read_around(str(path), hit.match_id).error.code == "stale_reference"
    assert T.document_read(str(path), cursor="garbage").error.code == "invalid_argument"
    assert T.document_read(str(path), start=99).error.code == "invalid_argument"


def test_unreadable_units_are_never_silent(work, corpus):
    pdf = work / "report.pdf"
    found = T.document_search(str(pdf), "555")
    assert found.matches_total == 0 and found.unsearchable_units == [2]
    assert any("not evidence of absence" in n for n in found.notes)
    read = T.document_read(str(pdf), start=2, end=2)
    assert read.unreadable_units == [2] and read.excerpt.text == ""


def test_outline_is_drawn_from_the_file_not_inferred(work, corpus):
    md = T.document_outline(str(work / "rich.md")).outline
    assert [h["title"] for h in md["headings"]][:2] == ["Årsrapport 2025 — Ετήσια αναφορά", "Lists"]
    assert md["headings"][0]["unit"] == 1 and md["tables"][0]["unit"] is not None
    pdf = T.document_outline(str(work / "report.pdf")).outline
    assert pdf["headings"] == [] and any("no bookmarks" in n for n in pdf["notes"])
    assert pdf["unreadable_units"] == [2]
    marked = text_pdf(work / "bm.pdf", ["<p>a</p>", "<p>b</p>"], toc=[[1, "Start", 1], [2, "Two", 2]])
    assert T.document_outline(str(marked)).outline["headings"] == [
        {"level": 1, "title": "Start", "unit": 1}, {"level": 2, "title": "Two", "unit": 2}]


@needs_pandoc
def test_r7_summary_never_differs_from_the_full_report(work):
    src = write(work / "s.md", RICH_MD)
    clean = T.markdown_to_docx(str(src))
    bad = write(work / "bad.md", RICH_MD.replace("923 456 781", "923 456 718", 1))
    for res in (clean, T.validate_conversion(str(src), str(bad))):
        s, full = res.report_summary, res.report
        assert s.status == full.status and s.status_basis == full.status_basis
        assert s.axes == {k: v.status + (f" ({v.reason})" if v.reason else "") for k, v in full.axes.items()}
        assert s.differences_total == full.differences_total
        assert s.differences == full.differences[:len(s.differences)]
        got = T.get_report(res.report_id)
        assert got.report.model_dump() == full.model_dump()
        assert T.get_report(res.report_id, "differences").section["differences"] == \
            [d.model_dump() for d in full.differences]
    assert T.get_report("0000000000000000").error.code == "report_not_found"


def test_lean_extract_pages_lists_and_states_the_rest(work):
    src = write(work / "n.txt", " ".join(str(i) for i in range(1, 51)) + "\n")
    lean = T.document_extract(str(src)).document
    assert "numbers" not in lean and lean["counts"]["numbers"] == 50
    got, offset = [], 0
    while True:
        page = T.document_extract(str(src), kinds=["numbers"], max_items=7, offset=offset).document
        got += [n["value"] for n in page["numbers"]]
        if "numbers" not in page["next_offset"]:
            break
        assert page["truncated"]["numbers"] == 50 - offset - 7
        offset = page["next_offset"]["numbers"]
    assert got == [str(i) for i in range(1, 51)]
    assert T.document_extract(str(src), kinds=["nonsense"]).error.code == "invalid_argument"
