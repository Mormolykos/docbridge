"""Audit round 3 (2026-09-26, audit of 0.2.2): four findings, each
tested over its DOMAIN - not only at the audit's own fixtures.

N1 HTML bold and heading levels changed -> PASS   N3 transparency added -> image PASS
N2 an image's colour lost -> image PASS            N4 every CMYK JPEG -> image FAIL (false)

N1's domain is every structure the structure checks compare (headings,
emphasis, lists, tables) written as HTML, in every context it can sit in. Its
properties: a structural change FAILs; the same structure written as Markdown
or as HTML compares PASS; structure docbridge cannot model is never PASS.
N2-N4's domain is every way a page can show something other than its source:
a fault injected into an honest build - colour samples, opacity, image and
soft-mask dictionaries, colour space, page content, the JPEG stream - must
FAIL, and every honest build must PASS. DOCBRIDGE_FUZZ_N scales the generators.
"""
import dataclasses
import io
import os
import random
import zlib

import pymupdf
import pytest
from builders import write
from PIL import Image, ImageCms

from docbridge import tools as T
from docbridge.convert import images

N_CASES = int(os.environ.get("DOCBRIDGE_FUZZ_N", "300"))
SEED = 20260927


def compare(work, i, a: str, b: str):
    return T.document_compare(str(write(work / f"a{i}.md", a + "\n")), str(write(work / f"b{i}.md", b + "\n"))).report


# ============================================================ N1: structure ===

BLOCK_CONTEXTS = [
    "<div>{x}</div>", "<p>{x}</p>", "<blockquote><div>{x}</div></blockquote>", "<section><article>{x}</article></section>",
    "<table><tr><td>{x}</td></tr></table>", "<table><caption>{x}</caption><tr><td>c</td></tr></table>",
    "<table><tr><th>h</th></tr><tr><td><div>{x}</div></td></tr></table>", "<ul><li>{x}</li></ul>",
    "<ol><li>a</li><li>{x}</li></ol>", "<dl><dt>t</dt><dd>{x}</dd></dl>", "<details><summary>{x}</summary></details>",
]
INLINE_CONTEXTS = ["Please {x} now.", "- item {x}", "> quoted {x}", "| a |\n|---|\n| {x} |", "## Heading {x}",
                   "1. first {x}"]
EMPH_TAGS = {"strong": ["strong", "b"], "em": ["em", "i"]}
MD_EMPH = {"strong": "**", "em": "*"}
WORDS = ["PAY", "Beløp 9000", "ποσό", "due now", "EUR 48 750,00"]


def test_n1_emphasis_changes_fail_and_equivalents_pass_in_every_context(work):
    rng = random.Random(f"{SEED}-emphasis")
    for i in range(max(80, N_CASES // 2)):
        kind = rng.choice(["strong", "em"])
        tag = rng.choice(EMPH_TAGS[kind])
        word = rng.choice(WORDS)
        ctx = rng.choice(BLOCK_CONTEXTS + INLINE_CONTEXTS)
        a = ctx.format(x=f"Lead <{tag}>{word}</{tag}> tail")
        mutation = rng.choice(["remove", "other_kind", "extend", "same_kind", "markdown"])
        if mutation == "remove":
            b = ctx.format(x=f"Lead {word} tail")
        elif mutation == "other_kind":
            o = rng.choice(EMPH_TAGS["em" if kind == "strong" else "strong"])
            b = ctx.format(x=f"Lead <{o}>{word}</{o}> tail")
        elif mutation == "extend":
            b = ctx.format(x=f"Lead <{tag}>{word} tail</{tag}>")
        elif mutation == "same_kind":
            o = [t for t in EMPH_TAGS[kind] if t != tag][0]
            b = ctx.format(x=f"Lead <{o}>{word}</{o}> tail")
        else:
            if ctx not in INLINE_CONTEXTS:
                continue
            b = ctx.format(x=f"Lead {MD_EMPH[kind]}{word}{MD_EMPH[kind]} tail")
        rep = compare(work, i, a, b)
        emph = rep.structure_checks["emphasis"]
        if mutation in ("same_kind", "markdown"):
            assert emph.status == "PASS" and rep.status == "PASS", (i, ctx, a, b, rep.summary)
            assert emph.details["source_count"] >= 1, (i, ctx, a)       # compared, not an empty inventory
        else:
            assert emph.status == "FAIL" and rep.status == "FAIL", (i, mutation, ctx, a, b, rep.summary)


HEADING_CONTEXTS = ["{x}", "<div>{x}</div>", "<section>{x}</section>", "<table><tr><td>{x}</td></tr></table>",
                    "<ul><li>{x}</li></ul>", "<blockquote>{x}</blockquote>",
                    "<table><caption>{x}</caption><tr><td>c</td></tr></table>"]


def test_n1_heading_changes_fail_and_equivalents_pass(work):
    rng = random.Random(f"{SEED}-headings")
    for i in range(max(60, N_CASES // 4)):
        lvl = rng.randrange(1, 7)
        text = rng.choice(["Invoice", "Beløp 9000", "Σύμβαση 2026"])
        ctx = rng.choice(HEADING_CONTEXTS)
        a = ctx.format(x=f"<h{lvl}>{text}</h{lvl}>")
        mutation = rng.choice(["level", "paragraph", "div", "markdown"])
        if mutation == "level":
            other = rng.choice([n for n in range(1, 7) if n != lvl])
            b = ctx.format(x=f"<h{other}>{text}</h{other}>")
        elif mutation == "paragraph":
            b = ctx.format(x=f"<p>{text}</p>")
        elif mutation == "div":
            b = ctx.format(x=f"<div>{text}</div>")
        else:
            if ctx != "{x}":
                continue
            b = "#" * lvl + " " + text
        rep = compare(work, i, a, b)
        heads = rep.structure_checks["headings"]
        if mutation == "markdown":
            assert heads.status == "PASS" and rep.status == "PASS", (i, a, b, rep.summary)
            assert heads.details["source_count"] == 1
        else:
            assert heads.status == "FAIL" and rep.status == "FAIL", (i, mutation, ctx, a, b, rep.summary)


def _html_list(items, ordered: bool) -> str:
    tag = "ol" if ordered else "ul"
    out = []
    for text, children in items:
        inner = _html_list(children, ordered) if children else ""
        out.append(f"<li>{text}{inner}</li>")
    return f"<{tag}>{''.join(out)}</{tag}>"


def _md_list(items, ordered: bool, depth: int = 0) -> list[str]:
    lines, n = [], 1
    for text, children in items:
        marker = f"{n}." if ordered else "-"
        lines.append("   " * depth + f"{marker} {text}")
        lines += _md_list(children, ordered, depth + 1) if children else []
        n += 1
    return lines


def _items(rng, depth=0):
    return [(f"item{rng.randrange(100)}", _items(rng, depth + 1) if depth < 2 and rng.random() < 0.3 else [])
            for _ in range(rng.randrange(1, 4))]


def _flatten(items):
    out = []
    for text, children in items:
        out.append((text, []))
        out += _flatten(children)
    return out


def test_n1_list_changes_fail_and_equivalents_pass(work):
    rng = random.Random(f"{SEED}-lists")
    for i in range(max(60, N_CASES // 4)):
        items, ordered = _items(rng), rng.random() < 0.5
        a = _html_list(items, ordered)
        mutation = rng.choice(["kind", "flatten", "merge", "markdown", "in_cell"])
        expect = "FAIL"
        if mutation == "kind":
            b = _html_list(items, not ordered)
        elif mutation == "flatten":
            if _flatten(items) == items:
                continue
            b = _html_list(_flatten(items), ordered)
        elif mutation == "merge":
            if len(items) < 2:
                continue
            merged = [(items[0][0] + " " + items[1][0], items[0][1] + items[1][1])] + items[2:]
            b = _html_list(merged, ordered)
        elif mutation == "markdown":
            b, expect = "\n".join(_md_list(items, ordered)), "PASS"
        else:
            a = f"<table><tr><td>{a}</td></tr></table>"
            b = f"<table><tr><td>{_html_list(items, not ordered)}</td></tr></table>"
        rep = compare(work, i, a, b)
        lists = rep.structure_checks["lists"]
        assert lists.status == expect, (i, mutation, a, b, rep.summary)
        assert lists.details["source_count"] == len(_flatten(items)), (i, a, lists.details)
        if expect == "PASS":
            assert rep.status == "PASS", (i, a, b, rep.summary)


@pytest.mark.parametrize("check,snippet", [
    ("headings", "Text <h2>Invoice</h2> more."), ("lists", "Text <ul><li>a</li></ul> more."),
    ("lists", "Text <li>a</li> more."), ("tables", "Text <table><tr><td>9</td></tr></table> more."),
    ("tables", "<table><tr><td>x<table><tr><td>y</td></tr></table></td></tr></table>"),
])
def test_n1_structure_docbridge_cannot_model_is_never_a_pass(work, check, snippet):
    rep = compare(work, 0, snippet, snippet)
    res = rep.structure_checks[check]
    assert res.status == "NOT_CHECKED" and res.reason == "unresolved_content", (check, snippet, res)
    assert res.details["unmodelled_source"] >= 1


def test_n1_an_unmodelled_element_blocks_every_structure_check(work):
    rep = compare(work, 0, "<div><h1>A</h1><widget>9000</widget></div>", "<div><h1>A</h1><widget>9000</widget></div>")
    for check in ("headings", "lists", "tables", "emphasis"):
        assert rep.structure_checks[check].status == "NOT_CHECKED", check
    assert rep.status != "PASS"


def test_n1_a_structure_fail_is_not_hidden_by_a_gap(work):
    """A gap blocks a PASS; a definite difference among modelled items still FAILs."""
    a = "<h1>A</h1>\n\nText <h2>inline</h2> more."
    b = "<h3>A</h3>\n\nText <h2>inline</h2> more."
    assert compare(work, 0, a, b).structure_checks["headings"].status == "FAIL"


# ================================================= N2-N4: image fault injection ===

def _jpeg(im: Image.Image, **kw) -> bytes:
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90, **kw)
    return b.getvalue()


def _random_image(rng: random.Random) -> Image.Image:
    w, h = rng.randrange(6, 40), rng.randrange(6, 40)
    im = Image.new("RGB", (w, h), tuple(rng.randrange(256) for _ in range(3)))
    for _ in range(rng.randrange(1, 4)):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        im.paste(tuple(rng.randrange(256) for _ in range(3)), (x0, y0, rng.randrange(x0, w) + 1, rng.randrange(y0, h) + 1))
    return im


def honest_source(work, rng: random.Random, i: int):
    """A random source of a random kind; returns its path."""
    im = _random_image(rng)
    kind = rng.choice(["rgb_png", "gray_png", "palette_png", "rgba_png", "la_png", "rgb_jpeg", "gray_jpeg",
                       "cmyk_jpeg", "icc_png"])
    path = work / f"src{i}_{kind}.{'jpg' if 'jpeg' in kind else 'png'}"
    if kind == "rgb_png":
        im.save(path)
    elif kind == "gray_png":
        im.convert("L").save(path)
    elif kind == "palette_png":
        im.convert("P", palette=Image.Palette.ADAPTIVE, colors=8).save(path)
    elif kind == "rgba_png":
        a = im.convert("RGBA")
        a.putalpha(Image.new("L", im.size, rng.randrange(0, 255)))
        a.save(path)
    elif kind == "la_png":
        la = im.convert("LA")
        la.putalpha(Image.new("L", im.size, rng.randrange(0, 255)))
        la.save(path)
    elif kind == "rgb_jpeg":
        path.write_bytes(_jpeg(im))
    elif kind == "gray_jpeg":
        path.write_bytes(_jpeg(im.convert("L")))
    elif kind == "cmyk_jpeg":
        path.write_bytes(_jpeg(im.convert("CMYK")))
    else:
        im.save(path, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    return path, kind


def _png(im: Image.Image) -> bytes:
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def _xrefs(doc):
    page = doc[0]
    xref, smask = page.get_images(full=True)[0][:2]
    return page, xref, smask


def _content(doc, extra_before=b"", extra_after=b""):
    page = doc[0]
    c = page.get_contents()[0]
    doc.update_stream(c, extra_before + doc.xref_stream(c) + extra_after)


# Each fault takes (plan, honest pdf, work path) and returns a corrupted pdf,
# or None where it does not apply to this source.
def _rebuild(plan, out, **changes):
    images.build([dataclasses.replace(plan, **changes)], out)
    return out


def f_gray(plan, pdf, out):
    if plan.mode == "original_bytes" or images._rgb(plan.stored).convert("L").convert("RGB").tobytes() == \
            images._rgb(plan.stored).tobytes():
        return None
    return _rebuild(plan, out, data=_png(images._rgb(plan.stored).convert("L")))


def f_one_pixel(plan, pdf, out):
    if plan.mode == "original_bytes":
        return None
    im = images._rgb(plan.stored).copy()
    r, g, b = im.getpixel((0, 0))
    im.putpixel((0, 0), ((r + 1) % 256, g, b))
    return _rebuild(plan, out, data=_png(im))


def f_swap_channels(plan, pdf, out):
    if plan.mode == "original_bytes":
        return None
    im = images._rgb(plan.stored)
    r, g, b = im.split()
    swapped = Image.merge("RGB", (b, g, r))
    return None if swapped.tobytes() == im.tobytes() else _rebuild(plan, out, data=_png(swapped))


def f_add_transparency(plan, pdf, out):
    if images.has_alpha(plan.stored):
        return None
    return _rebuild(plan, out, mask=_png(Image.new("L", plan.stored.size, 254)))


def f_alpha_one_off(plan, pdf, out):
    if not images.has_alpha(plan.stored):
        return None
    a = images.split_alpha(plan.stored)[1].copy()
    a.putpixel((0, 0), (a.getpixel((0, 0)) + 1) % 256)
    return _rebuild(plan, out, mask=_png(a))


def f_drop_alpha(plan, pdf, out):
    if not images.has_alpha(plan.stored) or images.split_alpha(plan.stored)[1].getextrema() == (255, 255):
        return None
    return _rebuild(plan, out, mask=None)


def _edit(pdf, out, fn):
    doc = pymupdf.open(str(pdf))
    page, xref, smask = _xrefs(doc)
    if fn(doc, page, xref, smask) is False:
        doc.close()
        return None
    doc.save(str(out))
    doc.close()
    return out


def f_decode_invert(plan, pdf, out):
    def fn(doc, page, xref, smask):
        n = pymupdf.Pixmap(doc, xref).n
        inverted = doc.xref_get_key(xref, "Decode")[0] != "null"       # an Adobe CMYK JPEG already is
        doc.xref_set_key(xref, "Decode", "[" + " ".join(["0 1" if inverted else "1 0"] * n) + "]")
    return _edit(pdf, out, fn)


def f_decode_subtle(plan, pdf, out):
    def fn(doc, page, xref, smask):
        n = pymupdf.Pixmap(doc, xref).n
        doc.xref_set_key(xref, "Decode", "[" + " ".join(["0 0.98"] * n) + "]")
    return _edit(pdf, out, fn)


def f_colour_key(plan, pdf, out):
    def fn(doc, page, xref, smask):
        n = pymupdf.Pixmap(doc, xref).n
        doc.xref_set_key(xref, "Mask", "[" + " ".join(["0 255"] * n) + "]")
    return _edit(pdf, out, fn)


def f_dictionary_key(key, value):
    def fault(plan, pdf, out):
        return _edit(pdf, out, lambda doc, page, xref, smask: doc.xref_set_key(xref, key, value))
    return fault


def f_optional_content(plan, pdf, out):
    def fn(doc, page, xref, smask):
        ocg = doc.add_ocg("hidden", on=False)
        doc.xref_set_key(xref, "OC", f"{ocg} 0 R")
    return _edit(pdf, out, fn)


def f_smask_matte(plan, pdf, out):
    def fn(doc, page, xref, smask):
        if not smask:
            return False
        doc.xref_set_key(smask, "Matte", "[0 0 0]")
    return _edit(pdf, out, fn)


def f_profile_swap(plan, pdf, out):
    def fn(doc, page, xref, smask):
        family, profile, n = images._stored_colour_space(doc, xref)
        if family != "/ICCBased" or n != 3:
            return False
        other = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        if other == profile:
            other = images._default_profile(3)
        kind, value = doc.xref_get_key(xref, "ColorSpace")
        cs = int(value.split()[0]) if kind == "xref" else None
        icc = int(doc.xref_object(cs, compressed=True).split("ICCBased")[1].split()[0])
        doc.update_stream(icc, other)
    return _edit(pdf, out, fn)


def f_lab(plan, pdf, out):
    def fn(doc, page, xref, smask):
        if pymupdf.Pixmap(doc, xref).n != 3:
            return False
        doc.xref_set_key(xref, "ColorSpace", "[/Lab <</WhitePoint [0.9505 1 1.089]>>]")
    return _edit(pdf, out, fn)


def f_graphics_state_alpha(plan, pdf, out):
    def fn(doc, page, xref, smask):
        res = int(doc.xref_get_key(page.xref, "Resources")[1].split()[0])
        doc.xref_set_key(res, "ExtGState", "<</GS0 <</ca 0.97 /CA 0.97>>>>")
        _content(doc, extra_before=b"/GS0 gs\n")
    return _edit(pdf, out, fn)


def f_overlay(plan, pdf, out):
    return _edit(pdf, out, lambda doc, page, xref, smask: _content(doc, extra_after=b"q 1 1 1 rg 0 0 1 1 re f Q\n"))


def f_annotation(plan, pdf, out):
    return _edit(pdf, out, lambda doc, page, xref, smask: page.add_rect_annot(pymupdf.Rect(0, 0, 2, 2)))


def f_group(plan, pdf, out):
    return _edit(pdf, out, lambda doc, page, xref, smask: doc.xref_set_key(
        page.xref, "Group", "<</S/Transparency/CS/DeviceRGB>>"))


def f_crop(plan, pdf, out):
    def fn(doc, page, xref, smask):
        r = page.mediabox
        page.set_cropbox(pymupdf.Rect(r.x0, r.y0, r.x1, r.y1 - 0.5))
    return _edit(pdf, out, fn)


def _jpeg_only(fn):
    def fault(plan, pdf, out):
        return fn(plan, pdf, out) if plan.mode == "original_bytes" else None
    return fault


def _raw(doc, xref, data, filters):
    doc.update_stream(xref, data, compress=False)
    doc.xref_set_key(xref, "Filter", filters)


@_jpeg_only
def f_jpeg_byte(plan, pdf, out):
    def fn(doc, page, xref, smask):
        data = bytearray(doc.xref_stream_raw(xref))
        data[len(data) // 2] ^= 0x01
        _raw(doc, xref, bytes(data), "/DCTDecode")
    return _edit(pdf, out, fn)


@_jpeg_only
def f_jpeg_extra_filter(plan, pdf, out):
    return _edit(pdf, out, lambda doc, page, xref, smask: _raw(
        doc, xref, zlib.compress(doc.xref_stream_raw(xref)), "[/FlateDecode /DCTDecode]"))


@_jpeg_only
def f_jpeg_decode_parms(plan, pdf, out):
    return _edit(pdf, out, lambda doc, page, xref, smask: doc.xref_set_key(xref, "DecodeParms", "<</ColorTransform 0>>"))


@_jpeg_only
def f_jpeg_decode_flip(plan, pdf, out):
    def fn(doc, page, xref, smask):
        kind, _ = doc.xref_get_key(xref, "Decode")
        n = {"CMYK": 4, "L": 1}.get(plan.stored.mode, 3)
        doc.xref_set_key(xref, "Decode", "null" if kind != "null" else "[" + " ".join(["1 0"] * n) + "]")
    return _edit(pdf, out, fn)


@_jpeg_only
def f_jpeg_size(plan, pdf, out):
    return _edit(pdf, out, lambda doc, page, xref, smask: doc.xref_set_key(
        xref, "Height", str(plan.stored.size[1] - 1)))


FAULTS = {
    "gray": f_gray, "one_pixel": f_one_pixel, "swap_channels": f_swap_channels,
    "add_transparency": f_add_transparency, "alpha_one_off": f_alpha_one_off, "drop_alpha": f_drop_alpha,
    "decode_invert": f_decode_invert, "decode_subtle": f_decode_subtle, "colour_key_mask": f_colour_key,
    "image_mask": f_dictionary_key("ImageMask", "true"), "intent": f_dictionary_key("Intent", "/Perceptual"),
    "interpolate": f_dictionary_key("Interpolate", "true"), "optional_content": f_optional_content,
    "smask_matte": f_smask_matte, "profile_swap": f_profile_swap, "lab": f_lab,
    "graphics_state_alpha": f_graphics_state_alpha, "overlay": f_overlay, "annotation": f_annotation,
    "transparency_group": f_group, "crop": f_crop,
    "jpeg_byte": f_jpeg_byte, "jpeg_extra_filter": f_jpeg_extra_filter, "jpeg_decode_parms": f_jpeg_decode_parms,
    "jpeg_decode_flip": f_jpeg_decode_flip, "jpeg_size": f_jpeg_size,
}


def test_n2_n4_every_fault_fails_and_every_honest_build_passes(work):
    rng = random.Random(f"{SEED}-faults")
    applied: dict[str, int] = {}
    for i in range(max(25, N_CASES // 12)):
        src, kind = honest_source(work, rng, i)
        plan = images.plan_image(src, rng.choice([0, 90, 180, 270]), True, rng.choice(["image", "A4", "Letter"]))
        honest = work / f"ok{i}.pdf"
        images.build([plan], honest)
        _, axis, diffs, rows = images.verify(honest, [plan])
        assert axis.status == "PASS", (i, kind, rows)
        for name in FAULTS:          # every fault that applies to this source
            bad = FAULTS[name](plan, honest, work / f"bad{i}_{name}.pdf")
            if bad is None:
                continue
            _, axis, diffs, rows = images.verify(bad, [plan])
            assert axis.status == "FAIL", (i, kind, name, rows)
            applied[name] = applied.get(name, 0) + 1
    missing = set(FAULTS) - set(applied)
    assert not missing, f"faults never applied: {sorted(missing)}"


def test_n2_audit_gray_loss_and_n3_added_alpha_fail_through_the_public_tool(work, monkeypatch):
    src = work / "colour.png"
    Image.new("RGB", (40, 30), (100, 105, 110)).save(src)
    plan = images.plan_image(src, 0, True, "image")
    original = images.build
    for name, change in (("gray", {"data": _png(plan.stored.convert("L"))}),
                         ("alpha", {"mask": _png(Image.new("L", plan.stored.size, 240))})):
        monkeypatch.setattr(images, "build", lambda plans, out, change=change: original(
            [dataclasses.replace(plans[0], **change)], out))
        res = T.images_to_pdf([str(src)], output_path=str(work / f"{name}.pdf"), overwrite=True)
        assert res.report.image_status == "FAIL" and res.report.status == "FAIL", (name, res.report.summary)
        monkeypatch.setattr(images, "build", original)


@pytest.mark.parametrize("variant", ["plain", "large", "rotated"])
def test_n4_cmyk_jpeg_is_verified_not_falsely_failed(work, variant):
    im = Image.new("CMYK", (300, 200) if variant == "large" else (60, 40), (20, 80, 130, 5))
    im.paste((200, 10, 0, 40), (0, 0, 20, 20))
    src = work / "c.jpg"
    src.write_bytes(_jpeg(im))
    res = T.images_to_pdf([str(src)], output_path=str(work / "c.pdf"), rotations=[90] if variant == "rotated" else None)
    assert res.report.image_status == "PASS", res.report.summary
    assert images._adobe_marker(src.read_bytes())       # Pillow writes the Adobe marker: the /Decode case


def test_the_default_profiles_are_measured_from_mupdf():
    for n in (1, 3, 4):
        profile = images._default_profile(n)
        assert profile and len(profile) > 128 and profile[36:40] == b"acsp", n     # an ICC profile header
