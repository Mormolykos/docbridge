"""Over the real MCP protocol: `python -m docbridge.server` as a subprocess,
driven by the SDK's own client. Unit tests prove the functions work; only these
prove the server speaks MCP, rejects what it says it rejects, and survives a
library printing to stdout."""
import asyncio
import json
import os
import sys

from builders import PLAIN_TXT, report_pdf, write

from docbridge.contract import SCHEMA_VERSION

EXPECTED = {"docbridge_contract", "pdf_merge", "pdf_split", "images_to_pdf", "pdf_to_markdown",
            "docx_to_markdown", "markdown_to_docx", "txt_to_docx", "document_extract",
            "document_compare", "validate_conversion", "document_outline", "document_search",
            "document_read", "document_read_around", "get_report"}


def session(work, fn):
    async def go():
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = dict(os.environ, DOCBRIDGE_ROOTS=str(work))
        params = StdioServerParameters(command=sys.executable, args=["-m", "docbridge.server"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)
    return asyncio.run(go())


def test_tools_are_listed_with_strict_versioned_typed_schemas(work):
    async def fn(s):
        return (await s.list_tools()).tools
    tools = session(work, fn)
    assert {t.name for t in tools} == EXPECTED
    for t in tools:
        assert t.input_schema.get("additionalProperties") is False, t.name
        assert f"[docbridge schema {SCHEMA_VERSION}]" in t.description, t.name
        assert t.output_schema and "properties" in t.output_schema, t.name


def test_unknown_and_mistyped_arguments_are_rejected_by_the_transport(work):
    src = write(work / "p.txt", PLAIN_TXT)

    async def fn(s):
        misspelt = await s.call_tool("txt_to_docx", {"input_path": str(src), "overwite": True})
        mistyped = await s.call_tool("txt_to_docx", {"input_path": str(src), "overwrite": "yes"})
        missing = await s.call_tool("txt_to_docx", {})
        return misspelt, mistyped, missing
    for res in session(work, fn):
        assert res.is_error
    assert not (work / "p.docx").exists()


def test_semantic_errors_are_values_not_protocol_errors(work, tmp_path_factory):
    other = tmp_path_factory.mktemp("outside")
    outside = write(other / "o.txt", "1\n")

    async def fn(s):
        return await s.call_tool("txt_to_docx", {"input_path": str(outside)})
    res = session(work, fn)
    assert not res.is_error
    assert res.structured_content["error"]["code"] == "path_outside_roots"
    assert res.structured_content["operation_completed"] is False


def test_a_conversion_round_trips_as_structured_and_text_content(work):
    src = write(work / "p.txt", PLAIN_TXT)

    async def fn(s):
        short = await s.call_tool("txt_to_docx", {"input_path": str(src)})
        full = await s.call_tool("get_report", {"report_id": short.structured_content["report_id"]})
        inline = await s.call_tool("txt_to_docx", {"input_path": str(src), "overwrite": True, "detail": "full"})
        return short, full, inline
    short, full, inline = session(work, fn)
    assert not short.is_error
    sc = short.structured_content
    assert sc["operation_completed"] is True and sc["report"] is None
    assert sc["report_summary"]["status"] == "PASS"
    assert json.loads(short.content[0].text)["report_summary"]["status"] == "PASS"
    assert len(short.content[0].text) < 3000          # the summary is the cheap default
    assert full.structured_content["report"]["status"] == "PASS"
    assert inline.structured_content["report"]["status"] == "PASS"


def test_lossless_reading_over_the_protocol(work):
    pdf = report_pdf(work / "r.pdf")

    async def fn(s):
        found = await s.call_tool("document_search", {"input_path": str(pdf), "query": "923 456 718"})
        m = found.structured_content["matches"][0]
        around = await s.call_tool("document_read_around", {"input_path": str(pdf), "match_id": m["match_id"],
                                                            "max_chars": 300})
        outline = await s.call_tool("document_outline", {"input_path": str(pdf)})
        return m, around.structured_content, outline.structured_content
    m, around, outline = session(work, fn)
    assert m["text"] == "923 456 718" and m["label"] == "page 1"
    assert m["text"] in around["excerpt"]["text"] and around["truncated"] is True and around["next_cursor"]
    assert outline["source"]["unit_kind"] == "page" and outline["outline"]["units_total"] == 1


def test_round1_audit_findings_are_repaired_over_the_protocol(work):
    """The seven attacks of the round-1 adversarial audit, through a real server."""
    import pymupdf
    from builders import minimal_docx, p
    math = ('<m:oMathPara xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:oMath>'
            '<m:r><m:t>9000=1</m:t></m:r></m:oMath></m:oMathPara>')
    minimal_docx(work / "eq.docx", p("Total agreed") + math)
    minimal_docx(work / "no.docx", p("Total agreed"))
    write(work / "la.md", '<a href="https://safe.invalid/pay">Pay invoice</a>')
    write(work / "lb.md", '<a href="https://other.invalid/pay">Pay invoice</a>')
    write(work / "oa.md", "<div>Before<table><tr><td>Amount 9000</td></tr></table>After</div>")
    write(work / "ob.md", "<div><table><tr><td>Amount 9000</td></tr></table>BeforeAfter</div>")
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "Pay invoice")
    d.set_toc([[1, "Payment destination", -1, {"kind": pymupdf.LINK_URI, "uri": "https://safe.invalid/pay"}]])
    d.save(str(work / "bm.pdf"))
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "Readable cover")
    d.new_page().draw_rect(pymupdf.Rect(72, 72, 300, 300), color=(0, 0, 0), fill=(0, 0, 0))
    d.save(str(work / "last.pdf"))
    write(work / "nfd.txt", "Invoice café 12345")
    (work / "enc.txt").write_bytes(("é" * 100 + " NEEDLE " + "z" * 200).encode("utf-8"))

    async def fn(s):
        async def call(name, args):
            res = await s.call_tool(name, args)
            assert not res.is_error, res.content
            return res.structured_content
        out = {}
        out["F1"] = await call("document_compare", {"left_path": str(work / "eq.docx"), "right_path": str(work / "no.docx")})
        out["F2"] = await call("document_compare", {"left_path": str(work / "la.md"), "right_path": str(work / "lb.md")})
        out["F6"] = await call("document_compare", {"left_path": str(work / "oa.md"), "right_path": str(work / "ob.md")})
        out["F7"] = await call("pdf_merge", {"inputs": [str(work / "bm.pdf")], "output_path": str(work / "bm_out.pdf")})
        out["F3"] = await call("document_read", {"input_path": str(work / "last.pdf")})
        out["F4"] = await call("document_search", {"input_path": str(work / "nfd.txt"), "query": "café",
                                                   "case_sensitive": True})
        hit = await call("document_search", {"input_path": str(work / "enc.txt"), "query": "NEEDLE", "encoding": "utf-8"})
        out["F5"] = await call("document_read_around", {"input_path": str(work / "enc.txt"),
                                                        "match_id": hit["matches"][0]["match_id"], "encoding": "latin-1"})
        return out
    out = session(work, fn)
    assert out["F1"]["report_summary"]["status"] != "PASS"
    assert out["F2"]["report_summary"]["axes"]["identifier"] == "FAIL"
    assert out["F6"]["report_summary"]["axes"]["text"] == "FAIL"
    assert out["F7"]["report_summary"]["status"] == "PASS"
    assert pymupdf.open(str(work / "bm_out.pdf")).get_toc(simple=False)[0][3]["uri"] == "https://safe.invalid/pay"
    assert 2 in out["F3"]["unreadable_units"] and out["F3"]["range_unreadable_units"] == [2]
    assert out["F4"]["matches_total"] == 1
    assert out["F5"]["error"]["code"] == "stale_reference"


def test_library_output_does_not_corrupt_the_stream(work):
    # find_tables is where PyMuPDF prints an advisory to stdout; the session
    # must still answer the next call.
    pdf = report_pdf(work / "r.pdf")

    async def fn(s):
        first = await s.call_tool("pdf_to_markdown", {"input_path": str(pdf), "detect_tables": True})
        second = await s.call_tool("docbridge_contract", {})
        return first, second
    first, second = session(work, fn)
    assert not first.is_error and first.structured_content["report_summary"]["status"] == "PASS"
    assert not second.is_error and "statuses" in second.structured_content["contract"]
