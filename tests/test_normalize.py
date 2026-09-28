"""The declared domain (normalize.py) behaves exactly as declared."""
import re
import sys

from docbridge import normalize as N
from docbridge.canonical import Block, BlockKind, Canonical, Origin


def test_regex_whitespace_is_exactly_str_isspace_for_every_code_point():
    # canonical.py tokenizes with \S+ and claims it equals str.split(); prove it.
    pattern = re.compile(r"\s")
    mismatches = [cp for cp in range(sys.maxunicode + 1)
                  if bool(pattern.match(chr(cp))) != chr(cp).isspace()]
    assert mismatches == []


def test_line_wrapping_and_space_width_never_matter():
    a = "Org.nr 923 456 781 MVA"
    for b in ("Org.nr\n923 456\t781   MVA", "Org.nr 923 456 781\r\nMVA"):
        assert N.tokenize(a) == N.tokenize(b)


def test_nothing_else_is_folded():
    assert N.tokenize("“quoted”") != N.tokenize('"quoted"')
    assert N.tokenize("A-B") != N.tokenize("A–B")
    assert N.tokenize("Case") != N.tokenize("case")


def test_invisible_characters_ligatures_and_bullets():
    assert N.tokenize("co­operate") == ["cooperate"]
    assert N.tokenize("ﬁnal ﬂow") == ["final", "flow"]
    assert N.tokenize("• item  other") == ["item", "other"]
    # checkbox glyphs are data, not bullets
    assert N.tokenize("☐ open ☒ done") == ["☐", "open", "☒", "done"]


def test_nfc_composes():
    assert N.tokenize("café") == N.tokenize("café")


def test_core_strips_only_sentence_punctuation():
    assert N.core("(25/03/2025).") == "25/03/2025"
    assert N.core("€4,999,") == "€4,999"
    assert N.core("-7") == "-7"
    assert N.core(".5") == ".5"
    assert N.core("«923456781»") == "923456781"


def test_every_number_character_is_numeric_regardless_of_shape():
    for tok in ("7", "−" + "12.5%", "m²", "½", "٣٤", "NO93", "v0.1", "Ⅷ"):
        assert N.has_number_char(tok), tok
    for tok in ("MVA", "café", "---"):
        assert not N.has_number_char(tok), tok


def test_classes_describe_shape_never_meaning():
    assert "long_number" in N.classify("923456781")
    assert "date" in N.classify("25/03/2025")
    assert "currency" in N.classify("€4,999")
    assert "percentage" in N.classify("12.5%")
    assert "url" in N.classify("https://bedvibe.studio/a1")
    assert "email" in N.classify("post@example.org")
    assert "code" in N.classify("NO93")
    assert "filename" in N.classify("data.csv")
    assert "time" in N.classify("14:30")
    assert N.classify("hello") == ()
    assert not any("org" in c for c in N.classify("923456781"))


def test_grouped_numbers():
    cores = ["Org.nr", "923", "456", "781", "MVA", "4", "999", "kr", "2025", "100", "NOK", "48", "750,00"]
    spans = N.grouped_number_spans(cores)
    assert [" ".join(cores[s:e]) for s, e in spans] == ["923 456 781", "4 999", "48 750,00"]


def test_a_number_beside_a_currency_word_is_a_currency_identifier():
    from docbridge.compare import identifier_view
    c = Canonical(format="txt", path="x", sha256="", size_bytes=0, extractor="t")
    b = Block(BlockKind.PARAGRAPH)
    b.add("Lisens NOK 48 750,00 og 2 500 kr, men 7 epler")
    c.blocks.append(b)
    view = identifier_view(c.stream(frozenset({Origin.CONTENT})))
    labelled = {t.core: t.classes for t in view}
    assert "currency" in labelled["48 750,00"] and "currency" in labelled["2 500"]
    assert "7" not in labelled


def test_stream_keeps_origins_and_never_drops_unknown():
    c = Canonical(format="docx", path="x", sha256="", size_bytes=0, extractor="t")
    b = Block(BlockKind.LIST_ITEM)
    b.add("1.", Origin.LIST_LABEL)
    b.add(" ")
    b.add("footnote")
    b.add("[1]", Origin.NOTE_REF)
    b.add(" and 42")
    c.blocks.append(b)
    u = Block(BlockKind.UNREADABLE)
    u.add("", Origin.UNKNOWN, "no_text_layer")
    c.blocks.append(u)
    all_ = c.stream(frozenset(Origin))
    assert [t.display() for t in all_] == ["1.", "footnote[1]", "and", "42", "<no_text_layer>"]
    assert all_[0].origin is Origin.LIST_LABEL
    no_notes = c.stream(frozenset({Origin.CONTENT, Origin.LIST_LABEL}))
    assert [t.display() for t in no_notes] == ["1.", "footnote", "and", "42", "<no_text_layer>"]
    # a sentinel equals nothing, not even itself in another stream
    assert all_[-1].value != no_notes[-1].value
