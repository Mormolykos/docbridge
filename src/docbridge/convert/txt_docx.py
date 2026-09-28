"""Plain text -> DOCX: one paragraph per line, nothing interpreted.

Tabs stay tabs (w:tab), form feeds become page breaks, empty lines stay empty
paragraphs. Characters XML 1.0 cannot store (C0 controls other than tab/line
feed/carriage return/form feed, U+FFFE, U+FFFF) are REFUSED with their
positions - dropping them would be exactly the silent change this tool exists
to prevent.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import docx
from docx.enum.text import WD_BREAK

from ..contract import ErrorCode
from ..errors import DocbridgeError
from ..extract.text import decode_text, split_lines


def _illegal(ch: str) -> bool:
    o = ord(ch)
    if o in (0x9, 0xA, 0xD, 0xC):
        return False
    return o < 0x20 or o in (0xFFFE, 0xFFFF) or 0xD800 <= o <= 0xDFFF


def txt_to_docx(src: Path, out: Path, encoding: str | None = None) -> dict:
    text = decode_text(src.read_bytes(), encoding)
    lines = split_lines(text)
    bad = []
    for ln_no, line in enumerate(lines, start=1):
        for col, ch in enumerate(line, start=1):
            if _illegal(ch):
                bad.append(f"line {ln_no} col {col}: U+{ord(ch):04X}")
    if bad:
        raise DocbridgeError(ErrorCode.UNSUPPORTED_CONTENT,
                             f"{len(bad)} characters cannot be stored in DOCX: " + "; ".join(bad[:10])
                             + (" ..." if len(bad) > 10 else ""),
                             "Remove or replace those control characters; docbridge will not drop them silently.")
    doc = docx.Document()
    props = doc.core_properties
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    props.created = epoch
    props.modified = epoch
    props.last_modified_by = ""
    props.author = ""
    page_breaks = 0
    for line in lines:
        pieces = line.split("\f")
        for k, piece in enumerate(pieces):
            if k > 0:
                doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
                page_breaks += 1
            p = doc.add_paragraph()
            if piece:
                p.add_run(piece)
    doc.save(str(out))
    return {"paragraphs": len(lines), "page_breaks": page_breaks}
