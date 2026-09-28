"""PDF, read through PyMuPDF.

What a PDF "says" here is its TEXT LAYER as PyMuPDF decodes it, in content-
stream order - not the glyphs on the page. The two can disagree (a wrong
ToUnicode map, text drawn as vector outlines, text inside a scanned image), and
nothing in v0.1 can see that disagreement. Every report carries this sentence
in its oracle field, because a PASS against a PDF means "the text layer
matches", never "the page matches".

Per page, the text layer is classified:

    present   extractable text exists
    absent    no extractable text, but something is drawn: a scan, an image,
              outlined text. The page becomes an UNKNOWN block, so no check can
              pass across it (OCR required; v0.1 has none)
    blank     nothing is drawn at all: an empty page legitimately has no text

Pages are also flagged when they carry raster images (text inside an image is
never read) or invisible text (render mode 3, typically someone's OCR layer,
whose agreement with the image is unknown).

The block/line reading below is shared with pdf_to_markdown on purpose
(METHODOLOGY R6: the identity function is shared code, not a convention
repeated on both sides).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from ..canonical import Block, BlockKind, Canonical, Link, Origin, PageInfo
from ..contract import ErrorCode, Reason
from ..errors import DocbridgeError

# PyMuPDF prints advisories and MuPDF warnings to stdout by default. On an MCP
# stdio server stdout is the protocol stream; send both to Python logging
# (stderr) instead. The SDK also diverts fd 1 while serving - this is the
# second guard, not the only one.
pymupdf.no_recommend_layout()
try:
    pymupdf.set_messages(pylogging=True)
except Exception:  # pragma: no cover - older PyMuPDF
    pass

# TEXT_PRESERVE_WHITESPACE | TEXT_PRESERVE_LIGATURES | TEXT_MEDIABOX_CLIP, no
# dehyphenation: a hyphen at a line end stays a hyphen, on both sides.
TEXT_FLAGS = (pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES
              | pymupdf.TEXT_MEDIABOX_CLIP)
BLANK_RENDER_DPI = 24
BLANK_THRESHOLD = 250           # any pixel darker than this (0-255 gray) is drawn content
IMAGE_AREA_WARN = 0.05          # raster images covering >5% of a page are flagged


@dataclass
class Line:
    text: str
    size: float                 # dominant font size (by characters)
    bold: bool
    bbox: tuple


@dataclass
class TextBlock:
    lines: list[Line] = field(default_factory=list)
    bbox: tuple = (0, 0, 0, 0)

    @property
    def text(self) -> str:
        return " ".join(ln.text for ln in self.lines)


@dataclass
class PageRead:
    blocks: list[TextBlock]
    chars: int
    invisible: bool
    image_area: float


def open_pdf(path: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(str(path), filetype="pdf")
    except Exception as e:  # PyMuPDF raises FileDataError, RuntimeError, ...
        raise DocbridgeError(ErrorCode.MALFORMED_INPUT, f"PyMuPDF cannot open the PDF: {e}",
                             "The file is damaged or is not a PDF.") from None
    if doc.needs_pass:
        doc.close()
        raise DocbridgeError(ErrorCode.ENCRYPTED_INPUT, "the PDF requires a password",
                             "Remove the password first; docbridge never guesses or strips passwords.")
    if doc.page_count == 0:
        # A damaged file MuPDF opens as empty would otherwise compare as a
        # clean, vacuous PASS against another empty reading (R1).
        doc.close()
        raise DocbridgeError(ErrorCode.MALFORMED_INPUT, "the PDF has no readable pages",
                             "The file is damaged or empty.")
    return doc


def read_page(page: pymupdf.Page) -> PageRead:
    """Text blocks of one page in content-stream order, with per-line font data."""
    d = page.get_text("dict", flags=TEXT_FLAGS)
    blocks: list[TextBlock] = []
    chars = 0
    invisible = False
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        tb = TextBlock(bbox=tuple(b.get("bbox", (0, 0, 0, 0))))
        for ln in b.get("lines", []):
            spans = ln.get("spans", [])
            text = "".join(s.get("text", "") for s in spans)
            if not text.strip():
                continue
            weight: dict[float, int] = {}
            bold_chars = 0
            for s in spans:
                n = len(s.get("text", "").strip())
                weight[round(s.get("size", 0), 1)] = weight.get(round(s.get("size", 0), 1), 0) + n
                if s.get("flags", 0) & 16:
                    bold_chars += n
                if s.get("alpha", 255) == 0:
                    invisible = True
            n_chars = sum(weight.values())
            chars += n_chars
            size = max(weight.items(), key=lambda kv: kv[1])[0] if weight else 0.0
            tb.lines.append(Line(text, size, n_chars > 0 and bold_chars * 2 >= n_chars,
                                 tuple(ln.get("bbox", (0, 0, 0, 0)))))
        if tb.lines:
            blocks.append(tb)
    area = abs(page.rect)
    image_area = 0.0
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"]) & page.rect
            image_area += abs(r)
    except Exception:
        pass
    return PageRead(blocks, chars, invisible, image_area / area if area else 0.0)


def page_is_blank(page: pymupdf.Page) -> bool:
    """True if nothing visible is drawn. Rendering is the only honest test:
    a page can hold images, vector paths or outlined text with no text layer."""
    pix = page.get_pixmap(dpi=BLANK_RENDER_DPI, colorspace=pymupdf.csGRAY, alpha=False)
    samples = pix.samples
    return min(samples) >= BLANK_THRESHOLD if samples else True


def extract_pdf(path: Path, sha256: str) -> Canonical:
    doc = open_pdf(path)
    canon = Canonical(format="pdf", path=str(path), sha256=sha256, size_bytes=path.stat().st_size,
                      extractor=f"PyMuPDF {pymupdf.VersionBind} text layer, content-stream order "
                                "(the text layer, not the rendered glyphs)")
    canon.pages = []
    image_pages: list[int] = []
    invisible_pages: list[int] = []
    if getattr(doc, "is_repaired", False):
        canon.warn("pdf_repaired", "MuPDF had to repair this file to open it; content lost to the damage "
                                   "is missing from the text layer and from every comparison")
    encryption = (doc.metadata or {}).get("encryption")
    if encryption:
        canon.warn("pdf_encrypted_permissions",
                   f"the file is encrypted ({encryption}) and opened without a password; files docbridge "
                   "writes from it carry no encryption or permission restrictions")
    try:
        for pno in range(doc.page_count):
            page = doc[pno]
            n = pno + 1
            rd = read_page(page)
            info = PageInfo(number=n, rotation=page.rotation, width=round(page.rect.width, 2),
                            height=round(page.rect.height, 2), chars=rd.chars,
                            invisible_text=rd.invisible)
            try:
                info.images = len(page.get_images(full=False))
            except Exception:
                info.images = 0
            if rd.chars > 0:
                info.text_layer = "present"
                for bi, tb in enumerate(rd.blocks):
                    blk = Block(BlockKind.PARAGRAPH, page=n, locator=f"page {n} block {bi + 1}")
                    blk.add(tb.text)
                    canon.blocks.append(blk)
                if rd.image_area > IMAGE_AREA_WARN:
                    image_pages.append(n)
                if rd.invisible:
                    invisible_pages.append(n)
            elif page_is_blank(page):
                info.text_layer = "blank"
            else:
                info.text_layer = "absent"
                blk = Block(BlockKind.UNREADABLE, page=n, locator=f"page {n}")
                blk.add("", Origin.UNKNOWN, Reason.NO_TEXT_LAYER.value)
                canon.blocks.append(blk)
            canon.pages.append(info)
            for ln in page.get_links():
                if ln.get("kind") == pymupdf.LINK_URI and ln.get("uri"):
                    try:
                        text = page.get_textbox(ln["from"]).strip()
                    except Exception:
                        text = ""
                    canon.links.append(Link(ln["uri"], text, None, n))
            annots = sum(1 for _ in page.annots())
            widgets = sum(1 for _ in page.widgets())
            canon.annotations["annotations"] = canon.annotations.get("annotations", 0) + annots
            canon.annotations["widgets"] = canon.annotations.get("widgets", 0) + widgets
        canon.outline = [list(e[:3]) for e in doc.get_toc(simple=True)]
    finally:
        doc.close()

    absent = [p.number for p in canon.pages if p.text_layer == "absent"]
    if absent:
        canon.warn("pdf_no_text_layer",
                   f"pages {absent} show content but have no text layer; OCR would be required, "
                   "v0.1 has none, and their text is UNKNOWN")
    if image_pages:
        canon.warn("pdf_raster_images",
                   f"pages {image_pages} carry raster images over >{int(IMAGE_AREA_WARN * 100)}% of the page; "
                   "text inside images is never read")
    if invisible_pages:
        canon.warn("pdf_invisible_text",
                   f"pages {invisible_pages} carry invisible text (usually an OCR layer); its agreement "
                   "with what the page shows is NOT_CHECKED")
    canon.note_unsupported("pdf_text_in_images", len(image_pages),
                           "pages whose raster images may contain text that is never read")
    return canon
