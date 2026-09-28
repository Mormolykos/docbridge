"""images_to_pdf: bytes, pixels (colour and alpha separately) and orientation."""
import io
import random

import pymupdf
import pytest
from builders import jpeg_bytes, png_bytes, two_tone
from PIL import Image

from docbridge import tools as T


def _write(work, name, data):
    p = work / name
    p.write_bytes(data)
    return p


def _rows(rep):
    return rep.axes["image"].details["pages"]


def test_jpeg_embedded_byte_for_byte(work):
    src = _write(work, "a.jpg", jpeg_bytes(two_tone()))
    res = T.images_to_pdf([str(src)], str(work / "o.pdf"))
    assert res.report.status == "PASS", res.report.summary
    doc = pymupdf.open(res.outputs[0])
    xref = doc[0].get_images()[0][0]
    assert doc.extract_image(xref)["image"] == src.read_bytes()
    assert _rows(res.report)[0]["pixels"] == "identical bytes"


@pytest.mark.parametrize("orientation", [3, 6, 8])
def test_exif_orientation_applied_without_reencoding(work, orientation):
    src = _write(work, f"o{orientation}.jpg", jpeg_bytes(two_tone(), orientation=orientation))
    res = T.images_to_pdf([str(src)], str(work / "o.pdf"))
    rep = res.report
    assert rep.status == "PASS", rep.summary
    row = _rows(rep)[0]
    assert row["embedding"] == "original_bytes" and row["pixels"] == "identical bytes"
    assert row["orientation"]["verdict"] == "matches" and row["orientation"]["discriminating"]


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_software_rotation_is_placement_only(work, rotation):
    src = _write(work, "r.jpg", jpeg_bytes(two_tone()))
    rep = T.images_to_pdf([str(src)], str(work / "o.pdf"), rotations=[rotation]).report
    assert rep.status == "PASS", rep.summary
    assert _rows(rep)[0]["pixels"] == "identical bytes"


def test_mirrored_orientation_is_reencoded_losslessly_and_said(work):
    src = _write(work, "m.jpg", jpeg_bytes(two_tone(), orientation=2))
    res = T.images_to_pdf([str(src)], str(work / "o.pdf"))
    assert res.report.status == "PASS", res.report.summary
    assert any("mirrored" in n for n in res.conversion_notes)


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P+transparency", "L", "RGB", "1"])
def test_png_pixels_exact_including_alpha(work, mode):
    random.seed(11)
    w, h = 31, 19
    if mode == "RGBA":
        im = Image.new("RGBA", (w, h))
        im.putdata([tuple(random.randrange(256) for _ in range(4)) for _ in range(w * h)])
    elif mode == "LA":
        im = Image.new("LA", (w, h))
        im.putdata([(random.randrange(256), random.randrange(256)) for _ in range(w * h)])
    elif mode == "P+transparency":
        base = Image.new("RGB", (w, h))
        base.putdata([(random.randrange(256),) * 3 for _ in range(w * h)])
        im = base.convert("P", palette=Image.ADAPTIVE, colors=16)
        im.info["transparency"] = 0
    else:
        im = Image.new(mode if mode != "1" else "L", (w, h))
        im.putdata([random.randrange(256) if mode != "RGB" else tuple(random.randrange(256) for _ in range(3))
                    for _ in range(w * h)])
        if mode == "1":
            im = im.convert("1")
    kw = {"transparency": 0} if mode == "P+transparency" else {}
    src = _write(work, "x.png", png_bytes(im, **kw))
    rep = T.images_to_pdf([str(src)], str(work / "o.pdf")).report
    assert rep.image_status == "PASS", (_rows(rep), rep.summary)
    assert _rows(rep)[0]["pixels"].startswith("identical")


def test_a_replaced_image_is_caught(work):
    src = _write(work, "a.png", png_bytes(two_tone()))
    out = T.images_to_pdf([str(src)], str(work / "o.pdf")).outputs[0]
    # tamper: rebuild the PDF with one pixel changed, then re-verify through the module
    from docbridge.convert import images
    plan = images.plan_image(src, 0, True, "image")
    doc = pymupdf.open(out)
    im = Image.open(io.BytesIO(png_bytes(two_tone())))
    im.putpixel((5, 5), (0, 255, 0))
    doc[0].replace_image(doc[0].get_images()[0][0], stream=png_bytes(im))
    doc.save(str(work / "bad.pdf"))
    checks, axis, diffs, rows = images.verify(work / "bad.pdf", [plan])
    assert axis.status == "FAIL"
    assert rows[0]["pixels"].startswith("DIFFERENT")


def test_paper_page_size_centres_and_keeps_aspect(work):
    src = _write(work, "a.jpg", jpeg_bytes(two_tone((400, 200))))
    rep = T.images_to_pdf([str(src)], str(work / "o.pdf"), page_size="A4").report
    assert rep.status == "PASS", rep.summary
    page = pymupdf.open(str(work / "o.pdf"))[0]
    assert page.rect.width > page.rect.height            # landscape image -> landscape page


@pytest.mark.parametrize("maker,code", [
    (lambda: b"not an image at all", "unsupported_image"),
    (lambda: png_bytes(Image.new("I;16", (8, 8), 300)), "unsupported_image"),
    (lambda: _gif_frames(), "unsupported_image"),
])
def test_refused_images(work, maker, code):
    src = _write(work, "bad.png", maker())
    res = T.images_to_pdf([str(src)], str(work / "o.pdf"))
    assert res.error is not None and res.error.code == code
    assert not (work / "o.pdf").exists()


def _gif_frames() -> bytes:
    # RGB frames: identical-looking P frames are merged by Pillow's GIF writer
    frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    b = io.BytesIO()
    frames[0].save(b, "GIF", save_all=True, append_images=frames[1:])
    return b.getvalue()


def test_sixteen_bit_rgb_png_is_refused_even_though_pillow_reads_it_as_8_bit(work):
    import struct
    import zlib
    w, h = 4, 3
    raw = b"".join(b"\x00" + b"\x12\x34" * 3 * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    src = _write(work, "deep.png", data)
    res = T.images_to_pdf([str(src)], str(work / "o.pdf"))
    assert res.error.code == "unsupported_image" and "16-bit" in res.error.message
