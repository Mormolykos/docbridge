"""Programmatic fixtures. Every test document is generated here, from code, so
the suite reproduces on any machine with the pinned dependencies and Pandoc.
Nothing is read from a checked-in binary."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image

# --------------------------------------------------------------- markdown ---

RICH_MD = """\
# Årsrapport 2025 — Ετήσια αναφορά

Company **Blåbær Consulting AS**, org.nr 923 456 781 MVA, and a *similar* one: 923 456 718.
Long integer 123456789012345, decimal 3.14159, negative −12.5 and -7, percentage 12.5 % and 99.9%.
Currency: €4,999 and €4,599; NOK 1 234 567,89; $1,000,000.00; 2 500 kr.
Dates: 25/03/2025, 2025-03-25, 25.03.2026 and 03/25/2025 at 14:30.
Greek: Το ποσό είναι 1.234,56 € για τον ΑΦΜ 094014201.
Norwegian: Æ Ø Å æ ø å — «sitat» og ‘enkel’.
Accents: café, naïve, résumé, Zoë, São Paulo, Ångström.
Link: [the portfolio](https://bedvibe.studio/portfolio/?id=42) and <https://example.com/x2>; mail <post@example.org>.
Files: report_2025-03.pdf, data.csv and invoice-0042.docx.

## Lists

1. First item 1.1
2. Second item with **bold 22**
   - nested bullet 923456781
   - nested bullet 923456718
     1. deep ordered 7
3. Third item

- Bullet α
- Bullet β with `code 3`

## Table

| Kode | Beløp | Dato | Endring |
|------|------:|------|--------:|
| A-001 | €4,999 | 25/03/2025 | −12.5% |
| Β-002 | €4,599 | 26/03/2025 | +3.0% |
| NO93 8601 1117 947 | 1 234 567,89 | 2025-03-25 | 0% |

## Code

```
total = 4999
iban = "NO93 8601 1117 947"
```

> Quote with three numbers: 1, 2 and 3.

### Last heading 3

Final paragraph with ***bold italic*** and ~~struck 5~~ text.
"""

PLAIN_TXT = (
    "Org.nr 923 456 781 MVA\n"
    "\tIndented with a tab: 3.14159\n"
    "Ελληνικά κείμενο 25/03/2025 και −12.5%\n"
    "Blåbærsyltetøy €4,999 · 2 500 kr\n"
    "\n"
    "After a blank line: 123456789012345\n"
    "\fAfter a form feed: 42\n"
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# -------------------------------------------------------------------- pdf ---

TEXT_RECT = pymupdf.Rect(56, 56, 540, 786)


def text_pdf(path: Path, pages: list[str | None], *, rotations: dict[int, int] | None = None,
             scanned: dict[int, str] | None = None, toc: list | None = None,
             links: dict[int, str] | None = None) -> Path:
    """One page per entry: HTML text, None for a blank page. `scanned` maps a
    0-based page index to text rendered as an image (no text layer)."""
    doc = pymupdf.open()
    for i, html in enumerate(pages):
        page = doc.new_page()
        if scanned and i in scanned:
            scan_into(page, scanned[i])
        elif html is not None:
            page.insert_htmlbox(TEXT_RECT, html)
        if links and i in links:
            page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(56, 760, 300, 780),
                              "uri": links[i]})
    for i, r in (rotations or {}).items():
        doc[i].set_rotation(r)
    if toc:
        doc.set_toc(toc)
    doc.save(str(path))
    doc.close()
    return path


def scan_into(page: pymupdf.Page, text: str) -> None:
    tmp = pymupdf.open()
    tp = tmp.new_page()
    tp.insert_htmlbox(TEXT_RECT, f"<p>{text}</p>")
    pix = tp.get_pixmap(dpi=100)
    page.insert_image(page.rect, stream=pix.tobytes("png"))
    tmp.close()


def ruled_table(page: pymupdf.Page, rows: list[list[str]], x0: float = 60, y0: float = 420,
                col_w: float = 150, row_h: float = 22) -> None:
    for r, row in enumerate(rows):
        for c, txt in enumerate(row):
            rect = pymupdf.Rect(x0 + c * col_w, y0 + r * row_h, x0 + (c + 1) * col_w, y0 + (r + 1) * row_h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_htmlbox(rect + (4, 3, -4, -3), f"<p style='font-size:10px'>{txt}</p>")


REPORT_HTML = """
<h1>Quarterly Report 2025</h1>
<p>Org.nr 923 456 781 MVA and the similar 923 456 718. Amount €4,999 (not €4,599) due 25/03/2025.</p>
<p>Growth −12.5% at Blåbær AS; long id 123456789012345; decimal 3.14159.</p>
<p>Ελληνικά: το ποσό είναι 1.234,56 ευρώ. Contact post@example.org.</p>
<h2>Details</h2>
<p>• First bullet 11</p><p>• Second bullet 22</p>
<p>1. Numbered one</p><p>2. Numbered two</p>
"""

TABLE_ROWS = [["Name", "Amount", "Date"], ["Beløp", "€4,999", "25/03/2025"], ["Ελληνικά", "−12.5%", "2025-03-25"]]


def report_pdf(path: Path, *, with_table: bool = True, scanned_page: bool = False,
               extra_pages: list[str | None] | None = None, html: str = REPORT_HTML) -> Path:
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_htmlbox(pymupdf.Rect(60, 50, 540, 400), html)
    if with_table:
        ruled_table(p, TABLE_ROWS)
    for html in extra_pages or []:
        q = doc.new_page()
        if html is not None:
            q.insert_htmlbox(TEXT_RECT, html)
    if scanned_page:
        scan_into(doc.new_page(), "Scanned page with secret number 555")
    doc.save(str(path))
    doc.close()
    return path


# ------------------------------------------------------------------- docx ---

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="{main}"/>
{extra}
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

MAIN_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def minimal_docx(path: Path, body: str, *, numbering: str | None = None, styles: str | None = None,
                 footnotes: str | None = None, hyperlinks: dict[str, str] | None = None,
                 main_type: str = MAIN_CT, namespace: str = W_NS) -> Path:
    """A hand-built DOCX package around `body` (the inner XML of w:body)."""
    rels = []
    extra_ct = []
    parts = {}
    if numbering:
        rels.append('<Relationship Id="rNum" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/numbering" Target="numbering.xml"/>')
        parts["word/numbering.xml"] = f'<w:numbering xmlns:w="{W_NS}">{numbering}</w:numbering>'
        extra_ct.append('<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-'
                        'officedocument.wordprocessingml.numbering+xml"/>')
    if styles:
        rels.append('<Relationship Id="rSty" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/styles" Target="styles.xml"/>')
        parts["word/styles.xml"] = f'<w:styles xmlns:w="{W_NS}">{styles}</w:styles>'
    if footnotes:
        rels.append('<Relationship Id="rFn" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    'relationships/footnotes" Target="footnotes.xml"/>')
        parts["word/footnotes.xml"] = f'<w:footnotes xmlns:w="{W_NS}">{footnotes}</w:footnotes>'
    for rid, url in (hyperlinks or {}).items():
        rels.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    f'relationships/hyperlink" Target="{url}" TargetMode="External"/>')
    doc = (f'<w:document xmlns:w="{namespace}" xmlns:r="{R_NS}" '
           'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
           'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
           'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
           f'<w:body>{body}</w:body></w:document>')
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
                'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(rels)
                + "</Relationships>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT.format(main=main_type, extra="".join(extra_ct)))
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        for name, xml in parts.items():
            z.writestr(name, xml)
    return path


def p(text: str = "", *, style: str | None = None, num: tuple[int, int] | None = None, runs: str | None = None) -> str:
    ppr = ""
    if style or num:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "")
        if num:
            ppr += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
        ppr += "</w:pPr>"
    content = runs if runs is not None else (f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else "")
    return f"<w:p>{ppr}{content}</w:p>"


def r(text: str, *, bold: bool = False, italic: bool = False, vanish: bool = False) -> str:
    props = ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "") + ("<w:vanish/>" if vanish else "")
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def level(ilvl: int, fmt: str, text: str, start: int = 1) -> str:
    return (f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="{start}"/><w:numFmt w:val="{fmt}"/>'
            f'<w:lvlText w:val="{text}"/></w:lvl>')


HEADING_STYLES = "".join(
    f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/></w:style>'
    for n in range(1, 4))


# ------------------------------------------------------------------ images ---

def two_tone(size=(300, 200), left=(200, 30, 30), right=(30, 30, 200), mode="RGB") -> Image.Image:
    im = Image.new(mode, size, left)
    im.paste(right, (size[0] // 2, 0, size[0], size[1]))
    return im


def jpeg_bytes(im: Image.Image, orientation: int | None = None, dpi: int | None = None) -> bytes:
    b = io.BytesIO()
    kw = {"quality": 92}
    if orientation:
        ex = im.getexif()
        ex[0x0112] = orientation
        kw["exif"] = ex.tobytes()
    if dpi:
        kw["dpi"] = (dpi, dpi)
    im.save(b, "JPEG", **kw)
    return b.getvalue()


def png_bytes(im: Image.Image, **kw) -> bytes:
    b = io.BytesIO()
    im.save(b, "PNG", **kw)
    return b.getvalue()
