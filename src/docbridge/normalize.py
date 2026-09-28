"""The declared domain: what a token is, what a number is, what an identifier is.

METHODOLOGY R19: code that accepts or rejects evidence declares its domain
before it compares anything. Every comparison in docbridge sees text only after
it has passed through this module, and no other module decides what a token is.

THE RULES, IN ORDER (VALIDATION_MODEL.md states the same list for readers):

1. Unicode NFC.
2. Four invisible characters are removed: soft hyphen, zero-width space, word
   joiner, zero-width no-break space. None of them renders mid-line.
3. The seven typographic ligatures U+FB00..U+FB06 are expanded. PDF text layers
   emit them; no author types them as content.
4. List-bullet glyphs are removed. They carry no text, and the formats disagree
   on whether a bullet is a character (PDF, TXT) or a marker (DOCX, Markdown).
   Checkbox glyphs are NOT bullets: a box changing from empty to ticked is data.
5. A token is a maximal run of characters for which str.isspace() is false.
   Line wrapping, indentation and space width therefore never produce a
   difference, and nothing else is folded: no case folding, no quote folding,
   no dash folding. A straight quote turning curly IS reported.

PROTECTION DOES NOT DEPEND ON CLASSIFICATION. Every token containing a Unicode
number character is compared by the numeric check, whatever it looks like. The
classes below (date, currency, long_number, ...) only label differences for the
reader and select the identifier view; a token no class matches is still
compared, character for character, by the text and numeric checks. That is the
difference between declaring a domain and enumerating failures.
"""
from __future__ import annotations

import re
import unicodedata

REMOVED_CHARS: dict[str, str] = {
    "­": "soft hyphen",
    "​": "zero-width space",
    "⁠": "word joiner",
    "﻿": "zero-width no-break space",
}

LIGATURES: dict[str, str] = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "ſt", "ﬆ": "st",
}

# U+2022 bullet, U+25E6 white bullet, U+25AA small square, U+2023 triangular
# bullet, U+2043 hyphen bullet, U+2219 bullet operator, and the Private Use Area
# code points Word's Symbol/Wingdings bullets decode to (F0B7, F0A7, F0D8, F076).
BULLET_GLYPHS: frozenset[str] = frozenset(
    "•◦▪‣⁃∙"
)

_TRANSLATION = str.maketrans(
    {**{c: None for c in REMOVED_CHARS}, **LIGATURES, **{c: " " for c in BULLET_GLYPHS}}
)


def normalize(text: str) -> str:
    """Rules 1-4. Whitespace is left in place; `tokenize` splits on it."""
    return unicodedata.normalize("NFC", text).translate(_TRANSLATION)


def tokenize(text: str) -> list[str]:
    """Rule 5: maximal runs of non-whitespace, after normalization."""
    return normalize(text).split()


# --- the numeric and identifier views ---------------------------------------

# Punctuation that belongs to the sentence, not to the token it touches. Only
# the numeric and identifier views strip it; the text check compares the full
# token, so "5." against "5" is still a text difference.
_OPENERS = "([{<\"'«“‘„‚‹"
_CLOSERS = ")]}>\"'»”’›,;:.!?…"


def core(token: str) -> str:
    """The token without sentence punctuation on its edges."""
    return token.lstrip(_OPENERS).rstrip(_CLOSERS)


def has_number_char(s: str) -> bool:
    """Unicode general category N*: decimal digits in any script, superscripts,
    fractions, roman-numeral letters. The numeric view's membership test."""
    return any(unicodedata.category(c)[0] == "N" for c in s)


def digit_count(s: str) -> int:
    return sum(1 for c in s if unicodedata.category(c) == "Nd")


def _has_letter(s: str) -> bool:
    return any(unicodedata.category(c)[0] == "L" for c in s)


_URL = re.compile(r"(?i)^(?:[a-z][a-z0-9+.\-]*://\S+|www\.\S+|mailto:\S+)$")
_EMAIL = re.compile(r"^[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+$")
_DATE = re.compile(r"^[0-9]{1,4}[./\-][0-9]{1,2}[./\-][0-9]{1,4}$")
_TIME = re.compile(r"^[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?$")
_PLAIN_NUMBER = re.compile(r"^[+\-−]?[0-9]+(?:[.,'’][0-9]+)*$")
_FILENAME = re.compile(r"^[\w\-.]+\.[A-Za-z][A-Za-z0-9]{1,4}$")

IDENTIFIER_CLASSES = frozenset({
    "url", "email", "date", "time", "percentage", "currency",
    "long_number", "grouped_number", "code", "filename",
})

LONG_NUMBER_DIGITS = 6


def classify(token_core: str) -> tuple[str, ...]:
    """Shape labels for a token core. Labels describe shape, never meaning:
    a 9-digit number is `long_number`, not an organisation number."""
    s = token_core
    out: set[str] = set()
    if not s:
        return ()
    if _URL.match(s):
        out.add("url")
    elif _EMAIL.match(s):
        out.add("email")
    if has_number_char(s):
        out.add("number_bearing")
        if _DATE.match(s):
            out.add("date")
        if _TIME.match(s):
            out.add("time")
        if "%" in s or "‰" in s:
            out.add("percentage")
        if any(unicodedata.category(c) == "Sc" for c in s):
            out.add("currency")
        if _PLAIN_NUMBER.match(s) and digit_count(s) >= LONG_NUMBER_DIGITS:
            out.add("long_number")
        if _has_letter(s) and not out & {"url", "email"}:
            out.add("code")
    elif _FILENAME.match(s) and not out:
        out.add("filename")
    return tuple(sorted(out))


def is_identifier(classes: tuple[str, ...]) -> bool:
    return bool(IDENTIFIER_CLASSES.intersection(classes))


_GROUP_HEAD = re.compile(r"^[0-9]{1,3}$")
_GROUP_TAIL = re.compile(r"^[0-9]{3}$")
_GROUP_LAST = re.compile(r"^[0-9]{3}[.,][0-9]{1,3}$")     # a decimal part ends the group: "48 750,00"

# Words that make an adjacent number a currency amount, for LABELLING only
# ("NOK 48 750,00", "2 500 kr", "1.234,56 €"). A number next to none of these
# is still compared by the numeric view; this list can add a label and an
# identifier-view entry, never remove a comparison.
CURRENCY_WORDS = frozenset({
    "NOK", "EUR", "USD", "GBP", "SEK", "DKK", "ISK", "CHF", "JPY", "CNY", "PLN", "CZK", "HUF",
    "CAD", "AUD", "kr", "kr.", "Kr", "Kr.", "€", "$", "£", "¥",
})


def grouped_number_spans(cores: list[str]) -> list[tuple[int, int]]:
    """Runs of tokens that read as one space-grouped number: '923 456 781',
    '4 999', '48 750,00'. Returned as [start, end) index pairs over `cores`.

    Only the identifier view uses this, so that a report can say
    '923 456 781 -> 923 456 718' rather than '781 -> 718'. The numeric view
    compares the individual tokens either way; grouping can add a label, it can
    never remove a comparison."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(cores)
    while i < n:
        if _GROUP_HEAD.match(cores[i]):
            j = i + 1
            while j < n and _GROUP_TAIL.match(cores[j]):
                j += 1
            if j < n and _GROUP_LAST.match(cores[j]):
                j += 1
            if j - i >= 2:
                spans.append((i, j))
                i = j
                continue
        i += 1
    return spans


def normalize_link_target(target: str) -> str:
    t = unicodedata.normalize("NFC", target.strip())
    if t.lower().startswith("mailto:"):
        t = t[7:]
    return t


def declared_rules() -> dict:
    """The rules above as data, for the contract tool and every report."""
    return {
        "unicode_form": "NFC",
        "removed_characters": {f"U+{ord(c):04X}": name for c, name in REMOVED_CHARS.items()},
        "expanded_ligatures": {f"U+{ord(c):04X}": v for c, v in LIGATURES.items()},
        "removed_bullet_glyphs": sorted(f"U+{ord(c):04X}" for c in BULLET_GLYPHS),
        "token": "maximal run of characters for which str.isspace() is false",
        "not_folded": ["case", "quotes", "dashes", "digits of other scripts"],
        "numeric_view": "every token containing a Unicode N* character, sentence punctuation stripped from its edges",
        "identifier_view": sorted(IDENTIFIER_CLASSES),
        "long_number_min_digits": LONG_NUMBER_DIGITS,
    }
