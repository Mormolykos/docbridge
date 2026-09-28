"""Format detection and dispatch.

The extension names the format the caller expects; the bytes must agree. A
".pdf" that is really a zip, or a ".docx" that is an OLE container (password-
protected Word, or a legacy .doc renamed), is refused with FORMAT_MISMATCH or
ENCRYPTED_INPUT - never read as whatever it happens to parse as.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..canonical import Canonical
from ..contract import ErrorCode
from ..errors import DocbridgeError

EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "md",
    ".markdown": "md",
    ".txt": "txt",
}

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_format(path: Path) -> str:
    fmt = EXTENSIONS.get(path.suffix.lower())
    if fmt is None:
        raise DocbridgeError(ErrorCode.UNSUPPORTED_FORMAT, f"unsupported file type {path.suffix!r}",
                             "v0.1 reads .pdf, .docx, .md/.markdown and .txt.")
    with open(path, "rb") as f:
        head = f.read(2048)
    if fmt == "pdf" and b"%PDF-" not in head[:1024]:
        raise DocbridgeError(ErrorCode.FORMAT_MISMATCH, "the file has a .pdf name but no PDF header",
                             "Check the file; it is not a PDF.")
    if fmt == "docx":
        if head.startswith(_OLE):
            raise DocbridgeError(ErrorCode.ENCRYPTED_INPUT,
                                 "the file is an OLE container, not a DOCX package: a password-protected "
                                 "Word file or a legacy .doc",
                                 "Remove the password (or save as .docx) in Word first.")
        if not head.startswith(b"PK"):
            raise DocbridgeError(ErrorCode.FORMAT_MISMATCH, "the file has a .docx name but is not a zip package",
                                 "Check the file; it is not a Word 2007+ document.")
    if fmt in ("md", "txt") and (head.startswith(b"%PDF-") or head.startswith(b"PK\x03\x04")):
        raise DocbridgeError(ErrorCode.FORMAT_MISMATCH, f"the file has a {path.suffix} name but binary content",
                             "Check the file type.")
    return fmt


def extract(path: Path, encoding: str | None = None) -> Canonical:
    """Read a validated input path into the canonical form."""
    fmt = detect_format(path)
    digest = sha256_of(path)
    if fmt == "pdf":
        from .pdf import extract_pdf
        return extract_pdf(path, digest)
    if fmt == "docx":
        from .docx import extract_docx
        return extract_docx(path, digest)
    if fmt == "md":
        from .markdown import extract_md
        return extract_md(path, digest, encoding)
    from .text import extract_txt
    return extract_txt(path, digest, encoding)
