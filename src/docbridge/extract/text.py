"""Plain text: one block per non-empty line.

Decoding never guesses. UTF-8 (with or without BOM) or UTF-16 with a BOM are
read; anything else is refused unless the caller names the encoding. A wrong
guess would corrupt every non-ASCII character silently, which is the failure
this package exists to prevent.
"""
from __future__ import annotations

from pathlib import Path

from ..canonical import Block, BlockKind, Canonical
from ..contract import ErrorCode
from ..errors import DocbridgeError


def decode_text(data: bytes, encoding: str | None = None) -> str:
    if encoding:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError) as e:
            raise DocbridgeError(ErrorCode.UNDECODABLE_TEXT, f"cannot decode as {encoding}: {e}",
                                 "Check the encoding name, or omit it to use UTF-8.") from None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as e:
            raise DocbridgeError(ErrorCode.UNDECODABLE_TEXT, f"invalid UTF-16: {e}",
                                 "The file has a UTF-16 byte-order mark but is not valid UTF-16.") from None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise DocbridgeError(
            ErrorCode.UNDECODABLE_TEXT,
            f"not valid UTF-8 (byte {e.start}: {data[e.start:e.start + 4]!r})",
            "Pass the file's encoding explicitly (e.g. encoding='cp1252'); docbridge does not guess.",
        ) from None


def split_lines(text: str) -> list[str]:
    """CRLF, CR and LF are line ends. Form feed stays inside its line; it is
    whitespace to the tokenizer and a page break to txt_to_docx."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def extract_txt(path: Path, sha256: str, encoding: str | None = None) -> Canonical:
    data = path.read_bytes()
    text = decode_text(data, encoding)
    canon = Canonical(format="txt", path=str(path), sha256=sha256, size_bytes=len(data),
                      extractor="docbridge plain-text reader (lines)")
    for i, line in enumerate(split_lines(text), start=1):
        if line.strip():
            b = Block(BlockKind.PARAGRAPH, locator=f"line {i}")
            b.add(line)
            canon.blocks.append(b)
    return canon
