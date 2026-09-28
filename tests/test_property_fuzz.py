"""Property fuzz (METHODOLOGY R19): properties stated, mutations generated.

Hand-written regression tests pin the failures someone already thought of.
These generate documents from a mixed vocabulary - English, Greek, Norwegian,
accents, and numbers in every shape the brief lists - apply one mutation, and
check a PROPERTY of the verdict:

    P1  a document compared with itself passes text, numeric and identifier
    P2  one digit changed anywhere            -> numeric FAIL, text FAIL, and the
                                                  changed token is reported
    P3  one letter changed                    -> text FAIL; numeric FAIL exactly
                                                  when the token holds a number
    P4  one token deleted                     -> text FAIL; numeric FAIL exactly
                                                  when the token held a number
    P5  a number inserted                     -> numeric FAIL
    P6  two different adjacent numbers swapped -> numeric FAIL
    P7  whitespace rewrapped (newline, tab, NBSP, doubled spaces) -> all PASS
    P8  punctuation added around a number     -> text FAIL, numeric PASS
    P9  every report: statuses in the vocabulary, the report validates against
        its own JSON schema, and a global PASS implies every required axis PASS
    P10 random bytes under any supported extension -> a documented error code or
        a report, never an exception

DOCBRIDGE_FUZZ_N sets the iterations per property (default 300). The seed is
fixed; a failure prints the seed-derived case so it can be replayed.
"""
import os
import random
import zipfile

import jsonschema
import pytest

from docbridge import tools as T
from docbridge.contract import ErrorCode, Status
from docbridge.report import ValidationReport

N_CASES = int(os.environ.get("DOCBRIDGE_FUZZ_N", "300"))
SEED = 20260924

WORDS = ["report", "amount", "total", "due", "Company", "the", "and", "Blåbær", "sjøen", "Ærlig", "Øya",
         "Å", "κείμενο", "ποσό", "Ελληνικά", "ΑΦΜ", "café", "naïve", "résumé", "Zoë", "São", "Ångström",
         "MVA", "invoice", "status", "«sitat»", "‘enkel’", "—", "co-op", "e.g.", "Mr.", "Ltd"]


def number(rng: random.Random) -> list[str]:
    """One number in a random shape; grouped numbers come back as several tokens."""
    k = rng.randrange(16)
    d = lambda n: "".join(rng.choice("0123456789") for _ in range(n))  # noqa: E731
    if k == 0:
        return [d(3), d(3), d(3)]                                # 923 456 781
    if k == 1:
        return [d(9)]                                            # 923456781
    if k == 2:
        return [f"{rng.randrange(1, 99)}.{d(rng.randrange(1, 6))}"]
    if k == 3:
        return [f"{rng.randrange(1, 999)},{d(2)}"]
    if k == 4:
        return [rng.choice("-−") + f"{rng.randrange(1, 999)}.{d(1)}"]
    if k == 5:
        return [f"{rng.randrange(0, 100)}.{d(1)}%"]
    if k == 6:
        return [f"€{rng.randrange(1, 9)},{d(3)}"]
    if k == 7:
        return [f"${rng.randrange(1, 999)},{d(3)}.{d(2)}"]
    if k == 8:
        return [f"{rng.randrange(1, 28):02d}/{rng.randrange(1, 12):02d}/{rng.randrange(1990, 2030)}"]
    if k == 9:
        return [f"{rng.randrange(1990, 2030)}-{rng.randrange(1, 12):02d}-{rng.randrange(1, 28):02d}"]
    if k == 10:
        return [f"{rng.randrange(0, 23):02d}:{d(2)}"]
    if k == 11:
        return ["NO" + d(2), d(4), d(4), d(3)]                  # IBAN-like
    if k == 12:
        return [d(1), d(3), "kr"]
    if k == 13:
        return [f"INV-{d(4)}-{d(4)}"]
    if k == 14:
        return [f"https://bedvibe.studio/p{d(3)}"]
    return [d(rng.randrange(1, 16))]


def document(rng: random.Random) -> list[list[str]]:
    lines = []
    for _ in range(rng.randrange(2, 7)):
        line = []
        for _ in range(rng.randrange(3, 12)):
            line += number(rng) if rng.random() < 0.4 else [rng.choice(WORDS)]
        lines.append(line)
    return lines


def render(lines: list[list[str]]) -> str:
    return "\n".join(" ".join(line) for line in lines) + "\n"


def has_digit(tok: str) -> bool:
    return any(ch.isdigit() for ch in tok)


def positions(lines, pred):
    return [(i, j) for i, line in enumerate(lines) for j, t in enumerate(line) if pred(t)]


SCHEMA = ValidationReport.model_json_schema()
STATUSES = {s.value for s in Status}


def check_invariants(rep: ValidationReport) -> None:
    jsonschema.validate(rep.model_dump(mode="json"), SCHEMA)
    for name in ("text", "numeric", "identifier", "structure", "layout", "image"):
        assert rep.axes[name].status in STATUSES
    if rep.status == "PASS":
        assert all(rep.axes[a].status == "PASS" for a in rep.required_axes)
    if rep.status == "FAIL":
        assert any(rep.axes[a].status == "FAIL" for a in rep.axes)
    for name, ax in rep.axes.items():
        if ax.status in ("NOT_CHECKED", "UNSUPPORTED"):
            assert ax.reason is not None, name


class Pair:
    """Writes a source and a mutated target as .txt and validates them."""

    def __init__(self, root):
        self.root = root
        self.n = 0

    def validate(self, a: str, b: str) -> ValidationReport:
        self.n += 1
        src = self.root / f"s{self.n}.txt"
        tgt = self.root / f"t{self.n}.txt"
        src.write_text(a, encoding="utf-8")
        tgt.write_text(b, encoding="utf-8")
        res = T.validate_conversion(str(src), str(tgt))
        assert res.error is None, res.error
        check_invariants(res.report)
        return res.report


def cases(tag: str):
    rng = random.Random(f"{SEED}-{tag}")
    for i in range(N_CASES):
        yield i, rng, document(rng)


def test_p1_identity(work):
    pair = Pair(work)
    for i, _rng, doc in cases("p1"):
        rep = pair.validate(render(doc), render(doc))
        assert rep.status == "PASS", (i, rep.summary)


def test_p2_one_digit_changed(work):
    pair = Pair(work)
    for i, rng, doc in cases("p2"):
        spots = positions(doc, has_digit)
        if not spots:
            continue
        li, tj = rng.choice(spots)
        tok = doc[li][tj]
        k = rng.choice([n for n, ch in enumerate(tok) if ch.isdigit()])
        new = tok[:k] + rng.choice([c for c in "0123456789" if c != tok[k]]) + tok[k + 1:]
        mutated = [list(line) for line in doc]
        mutated[li][tj] = new
        rep = pair.validate(render(doc), render(mutated))
        assert rep.numeric_status == "FAIL", (i, tok, new, rep.summary)
        assert rep.text_status == "FAIL", (i, tok, new)
        reported = {(d.source, d.target) for d in rep.differences if d.axis == "numeric"} | \
                   {(d.source, None) for d in rep.differences if d.axis == "numeric"} | \
                   {(None, d.target) for d in rep.differences if d.axis == "numeric"}
        from docbridge.normalize import core
        assert (core(tok), None) in reported or (None, core(new)) in reported, (i, tok, new, reported)


def test_p3_one_letter_changed(work):
    pair = Pair(work)
    for i, rng, doc in cases("p3"):
        spots = positions(doc, lambda t: any(ch.isalpha() for ch in t))
        li, tj = rng.choice(spots)
        tok = doc[li][tj]
        k = rng.choice([n for n, ch in enumerate(tok) if ch.isalpha()])
        repl = "x" if tok[k] != "x" else "y"
        new = tok[:k] + repl + tok[k + 1:]
        mutated = [list(line) for line in doc]
        mutated[li][tj] = new
        rep = pair.validate(render(doc), render(mutated))
        assert rep.text_status == "FAIL", (i, tok, new)
        assert rep.numeric_status == ("FAIL" if has_digit(tok) else "PASS"), (i, tok, new, rep.summary)


def test_p4_one_token_deleted(work):
    pair = Pair(work)
    for i, rng, doc in cases("p4"):
        li = rng.randrange(len(doc))
        if len(doc[li]) < 2:
            continue
        tj = rng.randrange(len(doc[li]))
        tok = doc[li][tj]
        mutated = [list(line) for line in doc]
        del mutated[li][tj]
        rep = pair.validate(render(doc), render(mutated))
        # deleting one of two identical neighbours is still a deletion
        assert rep.text_status == "FAIL", (i, tok)
        assert rep.numeric_status == ("FAIL" if has_digit(tok) else "PASS"), (i, tok, rep.summary)


def test_p5_number_inserted(work):
    pair = Pair(work)
    for i, rng, doc in cases("p5"):
        li = rng.randrange(len(doc))
        mutated = [list(line) for line in doc]
        mutated[li].insert(rng.randrange(len(doc[li]) + 1), str(rng.randrange(10, 10**6)))
        rep = pair.validate(render(doc), render(mutated))
        assert rep.numeric_status == "FAIL", (i, rep.summary)


def test_p6_adjacent_numbers_swapped(work):
    pair = Pair(work)
    seen = 0
    for i, rng, doc in cases("p6"):
        flat = [(li, tj) for li, line in enumerate(doc) for tj in range(len(line))]
        nums = [(li, tj) for li, tj in flat if has_digit(doc[li][tj])]
        pairs = [(a, b) for a, b in zip(nums, nums[1:], strict=False) if doc[a[0]][a[1]] != doc[b[0]][b[1]]]
        if not pairs:
            continue
        (la, ta), (lb, tb) = rng.choice(pairs)
        from docbridge.normalize import core
        if core(doc[la][ta]) == core(doc[lb][tb]):
            continue
        mutated = [list(line) for line in doc]
        mutated[la][ta], mutated[lb][tb] = mutated[lb][tb], mutated[la][ta]
        rep = pair.validate(render(doc), render(mutated))
        assert rep.numeric_status == "FAIL", (i, doc[la][ta], doc[lb][tb])
        seen += 1
    assert seen > N_CASES // 3


def test_p7_rewrapping_is_invisible(work):
    pair = Pair(work)
    for i, rng, doc in cases("p7"):
        text = render(doc)
        out = []
        for ch in text:
            if ch == " ":
                out.append(rng.choice([" ", "\n", "\t", "  ", " ", " ", " \r\n "]))
            else:
                out.append(ch)
        rep = pair.validate(text, "".join(out))
        assert rep.status == "PASS", (i, rep.summary)


def test_p8_punctuation_around_a_number(work):
    pair = Pair(work)
    for i, rng, doc in cases("p8"):
        spots = positions(doc, lambda t: has_digit(t) and t[-1].isalnum() and t[0].isalnum())
        if not spots:
            continue
        li, tj = rng.choice(spots)
        mutated = [list(line) for line in doc]
        mutated[li][tj] = "(" + doc[li][tj] + ")."
        rep = pair.validate(render(doc), render(mutated))
        assert rep.text_status == "FAIL", i
        assert rep.numeric_status == "PASS", (i, doc[li][tj], rep.summary)


@pytest.mark.parametrize("suffix", [".pdf", ".docx", ".md", ".txt"])
def test_p10_random_bytes_never_raise(work, suffix):
    rng = random.Random(f"{SEED}-bytes{suffix}")
    codes = {e.value for e in ErrorCode}
    heads = {".pdf": [b"%PDF-1.7\n", b""], ".docx": [b"PK\x03\x04", b""], ".md": [b""], ".txt": [b""]}
    for i in range(max(20, N_CASES // 5)):
        blob = rng.choice(heads[suffix]) + bytes(rng.randrange(256) for _ in range(rng.randrange(0, 4000)))
        src = work / f"r{i}{suffix}"
        src.write_bytes(blob)
        for res in (T.document_extract(str(src)), T.validate_conversion(str(src), str(src))):
            if res.error is not None:
                assert res.error.code in codes, res.error
                assert res.error.code != "internal_error", (i, suffix, res.error)
            else:
                assert res.operation_completed


def test_p2_holds_through_docx_as_well(work):
    """A smaller sample of P2 where the target is a real DOCX edited at the XML level."""
    rng = random.Random(f"{SEED}-docx")
    checked = 0
    for i in range(max(10, N_CASES // 10)):
        doc = document(rng)
        src = work / f"d{i}.txt"
        src.write_text(render(doc), encoding="utf-8")
        res = T.txt_to_docx(str(src))
        assert res.report.status == "PASS", res.report.summary
        with zipfile.ZipFile(res.outputs[0]) as z:
            items = {n: z.read(n) for n in z.namelist()}
        xml = items["word/document.xml"].decode("utf-8")
        digits = [k for k, ch in enumerate(xml) if ch.isdigit() and xml.rfind("<w:t", 0, k) > xml.rfind(">", 0, xml.rfind("<w:t", 0, k))
                  and xml.rfind("</w:t>", 0, k) < xml.rfind("<w:t", 0, k)]
        if not digits:
            continue
        k = rng.choice(digits)
        xml = xml[:k] + rng.choice([c for c in "0123456789" if c != xml[k]]) + xml[k + 1:]
        items["word/document.xml"] = xml.encode("utf-8")
        bad = work / f"d{i}_bad.docx"
        with zipfile.ZipFile(bad, "w") as z:
            for n, data in items.items():
                z.writestr(n, data)
        rep = T.validate_conversion(str(src), str(bad)).report
        assert rep.numeric_status == "FAIL", (i, rep.summary)
        checked += 1
    assert checked >= 5
