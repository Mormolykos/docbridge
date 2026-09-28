"""images_to_pdf: one page per image, pixels preserved and verified.

Embedding, per input:

    JPEG (no mirroring needed)   the original bytes are embedded unchanged
                                 (DCTDecode). Rotation - from EXIF or asked
                                 for - is applied by the page's placement
                                 matrix, never by re-encoding.
    PNG, 8-bit, no transparency  handed to MuPDF, which stores the decoded
                                 pixels losslessly (Flate)
    anything with transparency   split by Pillow into a colour image and an
                                 alpha image, both embedded as lossless PNG
                                 (the alpha one as the soft mask). MuPDF's own
                                 PNG import premultiplies: (10,20,30, alpha 40)
                                 was stored as (12,19,31), and fully transparent
                                 pixels lost their colour (measured 2026-09-24)
    TIFF/BMP/GIF/WEBP, single frame
                                 decoded once by Pillow, embedded as lossless
                                 PNG
    mirrored EXIF orientation    decoded, transposed by Pillow, embedded as
    (2, 4, 5, 7)                 lossless PNG - reported as a transformation

Refused (UNSUPPORTED_IMAGE): 16-bit-per-channel images (MuPDF stores 8 bits, so
precision would be lost silently), multi-frame images, CMYK outside JPEG, and
anything Pillow cannot identify.

Verified, per page (the `image` axis):

    bytes        JPEG: the embedded stream equals the source file, byte for byte
    pixels       otherwise: the stored colour samples equal the source's, and
                 the stored alpha mask equals the source's alpha channel -
                 compared separately, because MuPDF composites them
                 premultiplied (measured 2026-09-24: RGBA (10,20,30,40) reads
                 back as (2,3,5,40) once combined)
    orientation  two measurements (audit K2, 2026-09-26: a grayscale-only
                 comparison certified a red/green image turned 180 degrees,
                 because both halves had the same brightness):
                 GEOMETRY, exact - the image's drawing matrix read back from
                 the PDF is a rotation by a multiple of 90 degrees (page
                 /Rotate included), not mirrored, not skewed, filling its
                 planned rectangle; and the stored pixels turned by that angle
                 equal the expected upright image, pixel for pixel.
                 VISUAL, in colour - the image region rendered near native
                 resolution (<=1024 px) and the expected upright image, both
                 reduced to <=64 px by the same box filter, match within a
                 declared mean-absolute-error bound, averaged over R, G and B,
                 and no other rotation matches better.
                 Either one finding a difference -> FAIL. The geometry
                 matching -> the orientation is verified. Geometry unreadable
                 -> the visual measurement decides only if it DISCRIMINATES
                 (another rotation is measurably worse); if it cannot tell the
                 rotations apart the page is NOT_CHECKED (inconclusive), never
                 PASS.
"""
from __future__ import annotations

import functools
import io
import math
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from PIL import Image, ImageChops, ImageOps, ImageStat, UnidentifiedImageError

from ..contract import ErrorCode
from ..errors import DocbridgeError
from ..report import AxisResult, Difference

DEFAULT_DPI = 96.0
PAPER = {"A4": (595.2756, 841.8898), "Letter": (612.0, 792.0)}
ORIENTATION_MAE_MAX = 16.0
ORIENTATION_TIE = 1.0
THUMB = 64
RENDER_MAX = 1024
ANGLE_TOLERANCE_DEG = 0.01
BBOX_TOLERANCE_PT = 0.01
_EXIF_ROTATION = {1: 0, 3: 180, 6: 90, 8: 270}      # clockwise degrees to show upright
_MIRRORED = {2, 4, 5, 7}
_SUPPORTED = {"JPEG", "PNG", "TIFF", "BMP", "GIF", "WEBP"}


@dataclass
class Plan:
    path: Path
    data: bytes              # what gets embedded
    mode: str                # original_bytes | mupdf_decoded_png | reencoded_png | color_plus_mask_png
    rotate_cw: int           # placement rotation, clockwise
    expected: Image.Image    # the upright image a reader should see
    stored: Image.Image      # the pixels that should be stored (before placement rotation)
    page_w: float
    page_h: float
    rect: tuple
    transformations: list[str]
    mask: bytes | None = None  # alpha channel as a lossless PNG, embedded as the soft mask


def _png_bytes(im: Image.Image) -> bytes:
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def has_alpha(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info


def split_alpha(im: Image.Image) -> tuple[Image.Image, Image.Image]:
    """(colour, alpha) with no compositing and no premultiplication."""
    if im.mode == "LA":
        return im.convert("L"), im.getchannel("A")
    rgba = im.convert("RGBA")
    color = im.convert("L") if im.mode in ("L", "1") else rgba.convert("RGB")
    return color, rgba.getchannel("A")


def _png_bit_depth(data: bytes) -> int | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return data[24]
    return None


def plan_image(path: Path, user_cw: int, apply_exif: bool, page_size: str) -> Plan:
    data = path.read_bytes()
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as e:
        raise DocbridgeError(ErrorCode.UNSUPPORTED_IMAGE, f"{path.name}: cannot be read as an image ({e})",
                             "Pass a JPEG, PNG, TIFF, BMP, GIF or WEBP file.") from None
    fmt = im.format or ""
    if fmt not in _SUPPORTED:
        raise DocbridgeError(ErrorCode.UNSUPPORTED_IMAGE, f"{path.name}: {fmt or 'unknown'} images are not supported",
                             "Convert it to PNG or JPEG first.")
    if getattr(im, "n_frames", 1) > 1:
        raise DocbridgeError(ErrorCode.UNSUPPORTED_IMAGE, f"{path.name}: has {im.n_frames} frames",
                             "Extract the frame you want first; docbridge will not pick one silently.")
    if im.mode in ("I", "I;16", "I;16B", "I;16L", "I;16N", "F") or _png_bit_depth(data) == 16:
        raise DocbridgeError(ErrorCode.UNSUPPORTED_IMAGE, f"{path.name}: 16-bit or float samples",
                             "PDF embedding here stores 8 bits per channel; convert to 8-bit first "
                             "so the precision loss is your decision, not a silent one.")
    if im.mode == "CMYK" and fmt != "JPEG":
        raise DocbridgeError(ErrorCode.UNSUPPORTED_IMAGE, f"{path.name}: CMYK {fmt}",
                             "Only CMYK JPEG is embedded unchanged; convert other CMYK images yourself.")
    orientation = 1
    if apply_exif:
        try:
            orientation = int(im.getexif().get(0x0112, 1) or 1)
        except Exception:
            orientation = 1
        if orientation not in range(1, 9):
            orientation = 1
    transformations: list[str] = []
    upright = ImageOps.exif_transpose(im) if orientation != 1 else im.copy()
    expected = upright.rotate(-user_cw, expand=True) if user_cw else upright

    mask = None
    if orientation in _MIRRORED:
        stored = upright.rotate(-user_cw, expand=True) if user_cw else upright
        embed, mode, rotate_cw = _png_bytes(stored), "reencoded_png", 0
        transformations.append(f"EXIF orientation {orientation} is mirrored: decoded, transposed and "
                               "re-embedded as lossless PNG")
    else:
        rotate_cw = (_EXIF_ROTATION.get(orientation, 0) + user_cw) % 360
        stored = im
        if fmt == "JPEG":
            embed, mode = data, "original_bytes"
        elif fmt == "PNG" and not has_alpha(im):
            embed, mode = data, "mupdf_decoded_png"
        else:
            if im.mode not in ("1", "L", "LA", "RGB", "RGBA", "P", "PA"):
                stored = im.convert("RGBA" if "A" in im.mode else "RGB")
                transformations.append(f"{im.mode} converted to {stored.mode}")
            embed, mode = _png_bytes(stored), "reencoded_png"
            if fmt != "PNG":
                transformations.append(f"{fmt} decoded and embedded as lossless PNG")
        if orientation != 1:
            transformations.append(f"EXIF orientation {orientation} applied by page rotation (no re-encoding)")
    if mode != "original_bytes" and has_alpha(stored):
        color, alpha = split_alpha(stored)
        embed, mask, mode = _png_bytes(color), _png_bytes(alpha), "color_plus_mask_png"
        transformations.append("transparency embedded as a separate lossless alpha mask")
    if user_cw:
        transformations.append(f"rotated {user_cw} degrees clockwise by page placement")

    ew, eh = expected.size
    dpi = DEFAULT_DPI
    info_dpi = im.info.get("dpi")
    if isinstance(info_dpi, tuple) and info_dpi and 10 <= float(info_dpi[0]) <= 10000:
        dpi = float(info_dpi[0])
    if page_size == "image":
        pw, ph = ew / dpi * 72.0, eh / dpi * 72.0
        rect = (0.0, 0.0, pw, ph)
    else:
        paper = PAPER[page_size]
        pw, ph = (paper[1], paper[0]) if ew > eh else paper
        scale = min(pw / ew, ph / eh)
        w, h = ew * scale, eh * scale
        rect = ((pw - w) / 2, (ph - h) / 2, (pw + w) / 2, (ph + h) / 2)
    return Plan(path, embed, mode, rotate_cw, expected, stored, pw, ph, rect, transformations, mask)


def build(plans: list[Plan], out: Path) -> list[int]:
    doc = pymupdf.open()
    xrefs = []
    try:
        for p in plans:
            page = doc.new_page(width=p.page_w, height=p.page_h)
            # PyMuPDF rotates anti-clockwise (measured 2026-09-24): clockwise r -> (360 - r) % 360
            xrefs.append(page.insert_image(pymupdf.Rect(p.rect), stream=p.data, mask=p.mask,
                                           rotate=(360 - p.rotate_cw) % 360, keep_proportion=False))
        doc.save(str(out), garbage=3, deflate=True)
    finally:
        doc.close()
    return xrefs


def _mae(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute error over every band (R, G and B for colour images)."""
    means = ImageStat.Stat(ImageChops.difference(a, b)).mean
    return float(sum(means) / len(means))


def verify(out: Path, plans: list[Plan]) -> tuple[dict[str, AxisResult], AxisResult, list[Difference], list[dict]]:
    """Structure checks, the image axis, differences and per-page rows."""
    doc = pymupdf.open(str(out))
    diffs: list[Difference] = []
    rows: list[dict] = []
    bad_pixels = bad_orient = bad_construction = unverifiable = inconclusive = bad_size = 0
    try:
        page_count = doc.page_count
        count_ok = page_count == len(plans)
        for k, p in enumerate(plans):
            row = {"page": k + 1, "source": str(p.path), "embedding": p.mode,
                   "transformations": p.transformations}
            if k >= doc.page_count:
                rows.append(row)
                continue
            page = doc[k]
            if abs(page.rect.width - p.page_w) > 0.01 or abs(page.rect.height - p.page_h) > 0.01:
                bad_size += 1
                row["page_size"] = "DIFFERENT"
            imgs = page.get_images(full=True)
            if len(imgs) != 1:
                bad_pixels += 1
                row["pixels"] = f"DIFFERENT: {len(imgs)} images on the page"
                diffs.append(Difference(axis="image", kind="mismatch", source=p.path.name,
                                        target=f"page {k + 1}", note=f"{len(imgs)} images on the page, expected 1"))
                rows.append(row)
                continue
            xref, smask = imgs[0][0], imgs[0][1]
            verdict = _pixels(doc, xref, smask, p)
            row["pixels"] = verdict
            if verdict.startswith("DIFFERENT"):
                bad_pixels += 1
                diffs.append(Difference(axis="image", kind="mismatch", source=p.path.name,
                                        target=f"page {k + 1}", note=verdict))
            elif verdict.startswith("UNVERIFIABLE"):
                unverifiable += 1
            built = _construction(doc, page, xref, smask)
            row["construction"] = built
            if built.startswith("DIFFERENT"):
                bad_construction += 1
                diffs.append(Difference(axis="image", kind="mismatch", source=p.path.name,
                                        target=f"page {k + 1}", note=built))
            elif built.startswith("UNVERIFIABLE"):
                unverifiable += 1
            ov = _orientation(page, xref, p)
            row["orientation"] = ov
            if ov["verdict"] == "DIFFERENT":
                bad_orient += 1
                geo, vis = ov["geometry"], ov["visual"]
                why = geo.get("note") if geo["verdict"] == "DIFFERENT" else (
                    f"the rendered page does not match the expected upright image (colour MAE "
                    f"{vis['mae_expected']:.2f}, best other rotation {vis['mae_best_other']:.2f})")
                diffs.append(Difference(axis="image", kind="mismatch", source=p.path.name,
                                        target=f"page {k + 1}", note=why))
            elif ov["verdict"] == "INCONCLUSIVE":
                inconclusive += 1
            elif ov["verdict"] == "UNVERIFIABLE":
                unverifiable += 1
            rows.append(row)
    finally:
        doc.close()
    checks = {
        "page_count": AxisResult(status="PASS" if count_ok else "FAIL",
                                 scope="one page per input image", compared=len(plans),
                                 differences=0 if count_ok else 1,
                                 details={"expected": len(plans), "actual": page_count}),
        "page_size": AxisResult(status="FAIL" if bad_size else "PASS",
                                scope="every page has the size computed from the image's pixels and DPI "
                                      f"(default {DEFAULT_DPI:g} when the file declares none) or the chosen paper",
                                compared=len(plans), differences=bad_size),
    }
    if not count_ok:
        diffs.append(Difference(axis="structure.page_count", kind="mismatch", source=f"{len(plans)} images",
                                target=f"{page_count} pages"))
    if bad_pixels or bad_orient or bad_construction:
        status, reason = "FAIL", None
    elif unverifiable:
        status, reason = "NOT_CHECKED", "check_failed"
    elif inconclusive:
        status, reason = "NOT_CHECKED", "inconclusive"
    else:
        status, reason = "PASS", None
    image_axis = AxisResult(
        status=status, reason=reason,
        scope="per page: the page is exactly the planned construction (one image drawn once, nothing else on "
              "the page, the planned image and soft-mask dictionary entries only); the JPEG stream identical "
              "to the source file under exactly the DCTDecode filter, or the stored samples, decoded, equal to "
              "the source's compared as RGB; read in the source's colour space (its own ICC profile, or "
              "MuPDF's default); the effective opacity equal to the source's alpha, a source without alpha "
              "being fully opaque; and the orientation verified - exactly, from the image's drawing matrix (a "
              "multiple of 90 degrees, not mirrored, not skewed, filling its rectangle, the stored pixels so "
              "turned equal to the expected upright image), and visually, in colour (mean absolute error <= "
              f"{ORIENTATION_MAE_MAX:g}/255 over R, G and B after both sides are reduced to <= {THUMB}px by the "
              "same box filter, no other rotation matching better). A visual measurement that cannot tell the "
              "rotations apart is never a pass on its own",
        compared=len(plans), differences=bad_pixels + bad_orient + bad_construction,
        details={"pixel_or_byte_mismatches": bad_pixels, "orientation_mismatches": bad_orient,
                 "construction_mismatches": bad_construction,
                 "unverifiable_pages": unverifiable, "inconclusive_pages": inconclusive})
    return checks, image_axis, diffs, rows


def _pixels(doc: pymupdf.Document, xref: int, smask: int, p: Plan) -> str:
    """What the page's image IS, compared exactly with the source: the stored
    bytes (JPEG) or colour samples, the colour space they are read in, and
    the effective opacity. Audit round 3 (2026-09-26): the reference used to
    be converted to whatever the OUTPUT held, so a grayscale output redefined
    the reference and erased the colour loss (N2); opacity was compared only
    when the source had alpha, so added transparency passed (N3); and JPEG
    bytes were read through extract_image, which re-encodes CMYK (N4)."""
    try:
        verdict = _jpeg_bytes(doc, xref, p) if p.mode == "original_bytes" else _samples(doc, xref, p)
        if not verdict.startswith("identical"):
            return verdict
        wrong = _colour_space(doc, xref, p) or _opacity(doc, smask, p)
        return wrong or verdict
    except Exception as e:
        return f"UNVERIFIABLE: {type(e).__name__}: {e}"


def _jpeg_bytes(doc: pymupdf.Document, xref: int, p: Plan) -> str:
    """The encoded stream itself, read raw, under exactly the DCTDecode filter
    and no decode parameters - never a re-encoding of it - and the dictionary
    entries that decide how those bytes are read, exactly as the JPEG needs
    them: its size, 8 bits, and a /Decode inversion only for an Adobe CMYK
    JPEG (the one case MuPDF writes it; measured 2026-09-26)."""
    filters = doc.xref_get_key(xref, "Filter")[1].replace(" ", "")
    if filters not in ("/DCTDecode", "[/DCTDecode]"):
        return f"DIFFERENT: the image is stored through {filters}, not as its JPEG bytes"
    if doc.xref_get_key(xref, "DecodeParms")[0] != "null":
        return "DIFFERENT: the JPEG stream carries decode parameters the plan does not"
    if doc.xref_stream_raw(xref) != p.data:
        return "DIFFERENT: the stored JPEG stream differs from the source file"
    size = (_int_key(doc, xref, "Width"), _int_key(doc, xref, "Height"))
    if size != p.stored.size or _int_key(doc, xref, "BitsPerComponent") != 8:
        return f"DIFFERENT: the dictionary declares {size[0]}x{size[1]} at "\
               f"{_int_key(doc, xref, 'BitsPerComponent')} bits; the JPEG is {p.stored.size[0]}x{p.stored.size[1]} at 8"
    kind, value = doc.xref_get_key(xref, "Decode")
    got = [float(x) for x in re.findall(_NUMBER.decode(), value)] if kind != "null" else None
    want = [1.0, 0.0] * 4 if p.stored.mode == "CMYK" and _adobe_marker(p.data) else None
    if got != want:
        return (f"DIFFERENT: /Decode is {value if got is not None else 'absent'}; this JPEG needs "
                f"{'[1 0 1 0 1 0 1 0]' if want else 'none'}")
    return "identical bytes"


def _int_key(doc: pymupdf.Document, xref: int, key: str) -> int | None:
    kind, value = doc.xref_get_key(xref, key)
    try:
        return int(value) if kind == "int" else None
    except ValueError:
        return None


def _adobe_marker(data: bytes) -> bool:
    """An APP14 "Adobe" segment before the image data: the CMYK samples are
    stored inverted, and a reader needs /Decode [1 0 1 0 ...] to show them."""
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker, length = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xEE and data[i + 4:i + 9] == b"Adobe":
            return True
        if marker in (0xDA, 0xD9):      # start of scan / end of image
            return False
        i += 2 + length
    return False


def _rgb(im: Image.Image) -> Image.Image:
    """The colour part of an image as RGB: lossless for the modes docbridge
    stores (1, L, P and RGB, with or without alpha), so no colour is lost."""
    colour = split_alpha(im)[0] if has_alpha(im) else im
    return colour if colour.mode == "RGB" else colour.convert("RGB")


def _samples(doc: pymupdf.Document, xref: int, p: Plan) -> str:
    """Stored samples, decoded by MuPDF (a /Decode array applied), against the
    source's colour in ONE representation that keeps it: RGB. A gray output
    is widened to RGB, never the source narrowed to gray."""
    base = pymupdf.Pixmap(doc, xref)
    if base.alpha:      # a colour-key or stencil mask, or transparency in the image data
        return "DIFFERENT: the stored image carries transparency of its own, which the plan does not"
    if base.n not in (1, 3):
        return f"DIFFERENT: the stored image has {base.n} colour components; the plan stores RGB or gray"
    want = _rgb(p.stored)
    if (base.width, base.height) != want.size:
        return f"DIFFERENT: stored {base.width}x{base.height}, source {want.size[0]}x{want.size[1]}"
    got = Image.frombytes("L" if base.n == 1 else "RGB", (base.width, base.height), bytes(base.samples))
    if (got if got.mode == "RGB" else got.convert("RGB")).tobytes() != want.tobytes():
        return "DIFFERENT: stored colour samples differ from the source's (compared as RGB)"
    return "identical pixels"


_ICC_SIGNATURE = {1: b"GRAY", 3: b"RGB ", 4: b"CMYK"}
_DEVICE = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}


def _stored_colour_space(doc: pymupdf.Document, xref: int) -> tuple[str, bytes | None, int | None]:
    """(the colour space, its ICC profile bytes if ICCBased, its channel count)."""
    kind, value = doc.xref_get_key(xref, "ColorSpace")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    m = re.fullmatch(r"\[\s*/ICCBased\s+(\d+)\s+0\s+R\s*\]", value.strip())
    if m:
        icc = int(m.group(1))
        return "/ICCBased", doc.xref_stream(icc), _int_key(doc, icc, "N")
    family = value.strip()
    return family, None, {v: k for k, v in _DEVICE.items()}.get(family)


@functools.lru_cache(maxsize=None)
def _default_profile(n: int) -> bytes | None:
    """The ICC profile MuPDF writes for an image that carries none, read from
    MuPDF itself: a constant of the library, per channel count."""
    mode, fmt = {1: ("L", "PNG"), 3: ("RGB", "PNG"), 4: ("CMYK", "JPEG")}[n]
    b = io.BytesIO()
    Image.new(mode, (1, 1)).save(b, fmt)
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=1, height=1)
        return _stored_colour_space(doc, page.insert_image(page.rect, stream=b.getvalue()))[1]
    finally:
        doc.close()


def _colour_space(doc: pymupdf.Document, xref: int, p: Plan) -> str:
    """The samples are read in the source's colour space: the source's own ICC
    profile when it carries one for its channel count, otherwise MuPDF's
    default (or the device space it stands for). Identical samples in another
    colour space are different colours. Returns "" when it holds."""
    family, profile, n = _stored_colour_space(doc, xref)
    if n not in _DEVICE:
        return f"DIFFERENT: the samples are stored in {family}, not in an RGB, gray or CMYK space"
    own = p.stored.info.get("icc_profile")
    if own and own[16:20] == _ICC_SIGNATURE.get(n):
        ok = family == "/ICCBased" and profile == own
        want = "the source's own ICC profile"
    else:
        ok = (family == "/ICCBased" and profile == _default_profile(n)) or family == _DEVICE.get(n)
        want = "MuPDF's default profile (the source carries none)"
    return "" if ok else f"DIFFERENT: the samples are read in {family}, not in {want}"


def _opacity(doc: pymupdf.Document, smask: int, p: Plan) -> str:
    """Effective opacity on both sides: the soft mask (fully opaque when there
    is none) against the source's alpha, a source without alpha being fully
    opaque. Returns "" when they are equal."""
    size = p.stored.size
    expected = split_alpha(p.stored)[1] if has_alpha(p.stored) else Image.new("L", size, 255)
    if smask:
        m = pymupdf.Pixmap(doc, smask)
        if m.alpha or m.n != 1:
            return "DIFFERENT: the soft mask is not a plain gray mask"
        actual = Image.frombytes("L", (m.width, m.height), bytes(m.samples))
    else:
        actual = Image.new("L", size, 255)
    if actual.size != expected.size:
        return f"DIFFERENT: the soft mask is {actual.size[0]}x{actual.size[1]}, the image {size[0]}x{size[1]}"
    if actual.tobytes() == expected.tobytes():
        return ""
    if not has_alpha(p.stored):
        return "DIFFERENT: the PDF adds transparency the source does not have"
    if not smask:
        return "DIFFERENT: the source has transparency and the PDF image has no alpha mask"
    return "DIFFERENT: the alpha mask differs from the source's alpha channel"


# The planned construction (measured 2026-09-26 on every input kind): the page
# dictionary below, resources holding exactly one image, and content that draws
# it once. Anything else on the page can change what it shows.
_PAGE_KEYS = frozenset({"Type", "MediaBox", "Rotate", "Resources", "Parent", "Contents"})
_IMAGE_KEYS = frozenset({"Type", "Subtype", "Filter", "DecodeParms", "Width", "Height", "BitsPerComponent",
                         "ColorSpace", "Length", "SMask", "Decode"})
_MASK_KEYS = _IMAGE_KEYS - {"SMask", "Decode"}
_NUMBER = rb"[-+]?(?:\d+\.?\d*|\.\d+)"
_DRAW_ONCE = re.compile(rb"\s*q\s+(?:" + _NUMBER + rb"\s+){6}cm\s+/([^\s/\[\]<>(){}%]+)\s+Do\s+Q\s*")


def _construction(doc: pymupdf.Document, page: pymupdf.Page, xref: int, smask: int) -> str:
    """The page is exactly the planned construction, so nothing outside the
    compared image can change what the page shows: no other drawing,
    graphics-state transparency, blend mode or clipping (the content stream
    is `q <matrix> cm /<image> Do Q` and nothing more), no annotations,
    transparency group or crop (the page dictionary holds the planned keys
    only), and no mask, stencil, optional content or alternate in the image
    and soft-mask dictionaries (the planned keys only). Audit N3 asked for the
    transparency representations beyond the soft mask; this closes the page
    to all of them at once."""
    try:
        keys = set(doc.xref_get_keys(page.xref))
        if keys != _PAGE_KEYS:
            return f"DIFFERENT: the page dictionary holds {sorted(keys)}, not the planned {sorted(_PAGE_KEYS)}"
        if page.cropbox != page.mediabox:
            return "DIFFERENT: the page's visible area (CropBox) is not the whole page"
        kind, res = doc.xref_get_key(page.xref, "Resources")
        if kind != "xref":
            return "DIFFERENT: the page's resources are not the planned object"
        res_xref = int(res.split()[0])
        if set(doc.xref_get_keys(res_xref)) != {"XObject"}:
            return f"DIFFERENT: the page's resources hold {sorted(doc.xref_get_keys(res_xref))}, not only the image"
        entry = re.fullmatch(r"<<\s*/(\S+?)\s+(\d+)\s+0\s+R\s*>>", doc.xref_get_key(res_xref, "XObject")[1].strip())
        if entry is None or int(entry.group(2)) != xref:
            return "DIFFERENT: the page's resources hold more than the planned image"
        drawn = _DRAW_ONCE.fullmatch(b"\n".join(doc.xref_stream(c) for c in page.get_contents()))
        if drawn is None or drawn.group(1).decode("latin-1") != entry.group(1):
            return "DIFFERENT: the page draws more than the planned image, or draws it another way"
        extra = set(doc.xref_get_keys(xref)) - _IMAGE_KEYS
        if extra:
            return f"DIFFERENT: the image dictionary adds {sorted(extra)}"
        if "Decode" in doc.xref_get_keys(xref) and \
                doc.xref_get_key(xref, "Filter")[1].replace(" ", "") not in ("/DCTDecode", "[/DCTDecode]"):
            return "DIFFERENT: a /Decode array on a decoded image, which the plan never writes"
        if smask:
            extra = set(doc.xref_get_keys(smask)) - _MASK_KEYS
            if extra:
                return f"DIFFERENT: the soft-mask dictionary adds {sorted(extra)}"
        return "the planned construction"
    except Exception as e:
        return f"UNVERIFIABLE: {type(e).__name__}: {e}"


def _orientation(page: pymupdf.Page, xref: int, p: Plan) -> dict:
    """Both measurements and the verdict they support together (module
    docstring): matches | DIFFERENT | INCONCLUSIVE | UNVERIFIABLE."""
    geo, vis = _geometry(page, xref, p), _visual(page, p)
    if "DIFFERENT" in (geo["verdict"], vis["verdict"]):
        verdict = "DIFFERENT"
    elif geo["verdict"] == "matches":
        verdict = "matches"
    elif vis["verdict"] == "matches" and vis["discriminating"]:
        verdict = "matches"
    elif vis["verdict"] == "matches":
        verdict = "INCONCLUSIVE"     # nothing measured can tell the rotations apart: never a pass
    else:
        verdict = "UNVERIFIABLE"
    # The row keeps its 0.2.1 keys. `discriminating` now means: the verdict
    # rests on a measurement able to tell the rotations apart - the exact
    # geometry whenever it was read, else the visual one.
    return {"verdict": verdict,
            "discriminating": geo["verdict"] in ("matches", "DIFFERENT") or vis["discriminating"],
            "mae_expected": vis["mae_expected"], "mae_best_other": vis["mae_best_other"],
            "geometry": geo, "visual": vis}


def _geometry(page: pymupdf.Page, xref: int, p: Plan) -> dict:
    """The exact placement, read back from the PDF: the drawing matrix of the
    image, and what the stored pixels look like after it."""
    try:
        placements = [i for i in page.get_image_info(xrefs=True) if i.get("xref") == xref]
        if not placements:
            return {"verdict": "UNAVAILABLE", "note": "the image's drawing matrix could not be read"}
        if len(placements) > 1:
            return {"verdict": "DIFFERENT", "note": f"the image is drawn {len(placements)} times; planned once"}
        a, b, c, d = placements[0]["transform"][:4]
        bbox = pymupdf.Rect(placements[0]["bbox"])
        det = a * d - b * c
        # PyMuPDF's page space has y pointing down, so atan2(b, a) is the
        # clockwise angle of the image's top edge (measured 2026-09-26 on
        # 0/90/180/270: the top-left corner lands TL, TR, BR, BL)
        angle = (math.degrees(math.atan2(b, a)) + page.rotation) % 360
        quarter = round(angle / 90) % 4 * 90
        skew = abs(a * c + b * d) / max(math.hypot(a, b) * math.hypot(c, d), 1e-12)
        row = {"clockwise_degrees": round(angle, 4), "mirrored": det <= 0, "page_rotation": page.rotation,
               "bbox": [round(v, 3) for v in bbox], "planned_rect": [round(v, 3) for v in p.rect]}
        if det <= 0:
            return {"verdict": "DIFFERENT", "note": "the image is drawn mirrored", **row}
        if skew > 1e-6 or abs((angle - quarter + 180) % 360 - 180) > ANGLE_TOLERANCE_DEG:
            return {"verdict": "DIFFERENT", "note": "the image is drawn skewed or at an angle that is not a "
                                                    "multiple of 90 degrees", **row}
        if max(abs(u - v) for u, v in zip(bbox, p.rect, strict=True)) > BBOX_TOLERANCE_PT:
            return {"verdict": "DIFFERENT", "note": "the image does not fill its planned rectangle", **row}
        shown = p.stored.rotate(-quarter, expand=True) if quarter else p.stored
        if shown.mode != p.expected.mode or shown.size != p.expected.size or shown.tobytes() != p.expected.tobytes():
            return {"verdict": "DIFFERENT", "note": f"the stored image turned {quarter} degrees clockwise, as the "
                                                    "PDF draws it, is not the expected upright image", **row}
        return {"verdict": "matches", **row}
    except Exception as ex:
        return {"verdict": "UNAVAILABLE", "note": f"{type(ex).__name__}: {ex}"}


def _visual(page: pymupdf.Page, p: Plan) -> dict:
    """Render the image region near its native resolution, then shrink BOTH
    sides with the same Pillow box filter. Comparing MuPDF's downsampling
    with Pillow's disagreed badly on high-frequency images (random noise read
    as a wrong orientation); one shared filter removes that instrument error.
    Compared in colour: gray alone could not tell red from green (K2)."""
    try:
        rect = pymupdf.Rect(p.rect)
        long_px = min(max(p.expected.size), RENDER_MAX)
        scale = long_px / max(rect.width, rect.height)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=rect, alpha=False)
        region = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if p.expected.mode == "CMYK":
            # the renderer converts CMYK to RGB its own way (measured: colour
            # MAE 33.7 against Pillow's conversion of the same pixels), so the
            # expected side goes through MuPDF's converter too - one conversion
            # on both sides, as with the shared box filter
            e = p.expected
            cmyk = pymupdf.Pixmap(pymupdf.csCMYK, e.width, e.height, e.tobytes(), False)
            rgb = pymupdf.Pixmap(pymupdf.csRGB, cmyk)
            exp = Image.frombytes("RGB", (rgb.width, rgb.height), rgb.samples)
        else:
            # the page shows transparency over white; compare against the same
            rgba = p.expected.convert("RGBA")
            canvas = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            canvas.alpha_composite(rgba)
            exp = canvas.convert("RGB")
        w, h = region.size
        k = min(1.0, THUMB / max(w, h))
        size = (max(1, round(w * k)), max(1, round(h * k)))
        small = region.resize(size, Image.BOX)

        def fit(im: Image.Image) -> Image.Image:
            return im.resize(size, Image.BOX)

        maes = {rot: _mae(small, fit(exp.rotate(-rot, expand=True))) for rot in (0, 90, 180, 270)}
        e = maes[0]
        best_other = min(v for k, v in maes.items() if k != 0)
        if e > ORIENTATION_MAE_MAX or best_other + ORIENTATION_TIE < e:
            verdict = "DIFFERENT"
        else:
            verdict = "matches"
        # discriminating: the expected rotation and the best other one are told
        # apart, in either direction (a clearly better OTHER rotation counts)
        return {"verdict": verdict, "mae_expected": round(e, 3), "mae_best_other": round(best_other, 3),
                "discriminating": abs(best_other - e) > ORIENTATION_TIE}
    except Exception as ex:
        return {"verdict": "UNVERIFIABLE", "error": f"{type(ex).__name__}: {ex}",
                "mae_expected": -1.0, "mae_best_other": -1.0, "discriminating": False}
