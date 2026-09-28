"""A fresh end-to-end run, outside the unit-test fixtures, over the real MCP protocol.

    python scripts/e2e_fresh.py [--keep]

Creates its own documents (none of tests/builders.py is used), starts
`python -m docbridge.server` as a subprocess, and drives it with the MCP SDK's
client - the same path an agent takes:

  1. create three PDFs (text, a rotated page, a blank page, bookmarks)
  2. merge them                          -> pdf_merge
  3. verify page preservation            -> the merge report, page by page
  4. convert a structured Markdown file  -> markdown_to_docx
  5. extract both                        -> document_extract x2
  6. validate text/numbers/identifiers   -> validate_conversion: expect PASS
  7. corrupt one digit in the DOCX       (outside docbridge, at the XML level)
  8. validate again                      -> expect FAIL, naming the digit
  9. lossless reading: outline, search, read-around and capped reads of the
     merged PDF, each checked against an independent PyMuPDF read

Every tool response is written verbatim to transcript.jsonl in the work
directory (METHODOLOGY R14: preserve the raw output). Exit code 0 only if every
expectation held.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pymupdf

MARKDOWN = """\
# Leverandøravtale 2026 — Σύμβαση προμήθειας

Avtalen gjelder **Nordlys Data AS** (org.nr 921 437 865) og *Φωτεινή ΕΠΕ* (ΑΦΜ 801234567).

## Beløp

| Post | Beløp | Forfall |
|------|------:|---------|
| Lisens | NOK 48 750,00 | 15.01.2026 |
| Support | €7,340 | 2026-04-30 |
| Rabatt | −5.5% | 31/12/2026 |

## Vilkår

1. Betaling innen 30 dager.
2. Forsinkelsesrente 9,75 % p.a.
   - beregnes fra 16.01.2026
3. Kontakt: <avtale@nordlys.example> eller [portalen](https://nordlys.example/avtale/2026-117).

```
kontonummer 1506.29.48713
```
"""


def make_pdfs(work: Path) -> list[Path]:
    paths = []
    specs = [
        ("kontrakt.pdf", ["<h1>Kontrakt 2026-117</h1><p>Beløp NOK 48 750,00, forfall 15.01.2026.</p>",
                          "<p>Side to: rente 9,75 % og konto 1506.29.48713.</p>"], {1: 90}, [[1, "Kontrakt", 1]]),
        ("vedlegg.pdf", ["<p>Vedlegg A: 921 437 865 og 921 437 856 er to ulike numre.</p>", None], {}, []),
        ("kvittering.pdf", ["<p>Kvittering €7,340 betalt 2026-04-30 kl. 14:05.</p>"], {}, [[1, "Kvittering", 1]]),
    ]
    for name, pages, rotations, toc in specs:
        doc = pymupdf.open()
        for html in pages:
            page = doc.new_page()
            if html is not None:
                page.insert_htmlbox(pymupdf.Rect(56, 56, 540, 780), html)
        for i, r in rotations.items():
            doc[i].set_rotation(r)
        if toc:
            doc.set_toc(toc)
        p = work / name
        doc.save(str(p))
        paths.append(p)
    return paths


async def run(work: Path) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    transcript = open(work / "transcript.jsonl", "w", encoding="utf-8")
    ok = True

    def expect(cond: bool, what: str) -> None:
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + what)
        ok &= cond

    env = dict(os.environ, DOCBRIDGE_ROOTS=str(work))
    params = StdioServerParameters(command=sys.executable, args=["-m", "docbridge.server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            async def call(name: str, args: dict) -> dict:
                res = await s.call_tool(name, args)
                transcript.write(json.dumps({"tool": name, "args": args, "is_error": res.is_error,
                                             "structured": res.structured_content}, ensure_ascii=False) + "\n")
                if res.is_error:
                    raise SystemExit(f"{name} returned a protocol error: {res.content}")
                return res.structured_content

            print("1-3. create three PDFs, merge, verify pages")
            pdfs = make_pdfs(work)
            m = await call("pdf_merge", {"inputs": [str(p) for p in pdfs], "output_path": str(work / "samlet.pdf"),
                                         "detail": "full"})
            rep = m["report"]
            expect(m["error"] is None and m["operation_completed"], "merge completed")
            expect(rep["target_page_count"] == 5 and rep["source_page_count"] == 5, "5 pages in, 5 pages out")
            expect(rep["structure_checks"]["page_identity"]["status"] == "PASS", "every page's text layer identical")
            expect(rep["layout_status"] == "PASS", "every page pixel-identical at 72 dpi")
            expect(rep["structure_checks"]["page_rotation"]["status"] == "PASS", "rotated page kept its rotation")
            expect(rep["structure_checks"]["bookmarks"]["status"] == "PASS", "bookmarks merged with shifted pages")
            expect(rep["status"] == "PASS", f"merge report: {rep['status_basis']}")

            print("4. Markdown -> DOCX")
            md = work / "avtale.md"
            md.write_text(MARKDOWN, encoding="utf-8")
            conv = await call("markdown_to_docx", {"input_path": str(md)})
            docx = conv["outputs"][0]
            summ = conv["report_summary"]
            expect(conv["report"] is None and summ["status"] == "PASS",
                   f"conversion summary ({len(json.dumps(conv))} chars): {summ['status_basis']}")
            full = (await call("get_report", {"report_id": conv["report_id"]}))["report"]
            expect(full["status"] == summ["status"] and full["status_basis"] == summ["status_basis"],
                   "get_report returns the full report behind the summary, same verdict")

            print("5. extract both")
            kinds = ["numbers", "identifiers"]
            ex_md = (await call("document_extract", {"input_path": str(md), "kinds": kinds, "max_items": 500}))["document"]
            ex_dx = (await call("document_extract", {"input_path": docx, "kinds": kinds, "max_items": 500}))["document"]
            expect([n["value"] for n in ex_md["numbers"]] == [n["value"] for n in ex_dx["numbers"]],
                   f"same {len(ex_md['numbers'])} numbers, same order, in both extractions")
            expect("921 437 865" in [i["value"] for i in ex_dx["identifiers"]], "grouped org number found as an identifier")

            print("6. validate the untouched conversion")
            v1 = (await call("validate_conversion", {"source_path": str(md), "target_path": docx,
                                                     "detail": "full"}))["report"]
            expect(v1["text_status"] == v1["numeric_status"] == v1["identifier_status"] == "PASS",
                   "text, numbers and identifiers PASS")

            print("7. corrupt one digit in the DOCX (48 750,00 -> 48 750,60)")
            bad = work / "avtale_corrupt.docx"
            with zipfile.ZipFile(docx) as zin:
                items = {n: zin.read(n) for n in zin.namelist()}
            xml = items["word/document.xml"].decode("utf-8")
            # in table cells Pandoc keeps "NOK 48 750,00" in one run
            assert "48 750,00<" in xml, "the value to corrupt is not where expected"
            items["word/document.xml"] = xml.replace("48 750,00<", "48 750,60<", 1).encode("utf-8")
            with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zout:
                for n, data in items.items():
                    zout.writestr(n, data)

            print("8. validate again")
            v2 = (await call("validate_conversion", {"source_path": str(md), "target_path": str(bad),
                                                     "detail": "full"}))["report"]
            numeric = [(d["kind"], d["source"], d["target"], d["target_location"]) for d in v2["differences"]
                       if d["axis"] == "numeric"]
            expect(v2["status"] == "FAIL" and v2["numeric_status"] == "FAIL", "status changed PASS -> FAIL")
            expect(v2["identifier_status"] == "FAIL", "the amount is also flagged as a changed identifier (currency)")
            expect(numeric[:1] == [("changed", "750,00", "750,60", numeric[0][3] if numeric else None)],
                   f"the changed digit is named: {numeric}")
            print(f"\n  report summary: {v2['summary']}")

            print("\n9. lossless reading of the merged PDF (checked against an independent PyMuPDF read)")
            merged = work / "samlet.pdf"
            doc = pymupdf.open(str(merged))
            truth = "\n".join(doc[i].get_text("text", flags=pymupdf.TEXT_PRESERVE_WHITESPACE
                                              | pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_MEDIABOX_CLIP)
                              for i in range(doc.page_count))
            doc.close()
            out = await call("document_outline", {"input_path": str(merged)})
            expect(out["outline"]["units_total"] == 5 and [h["title"] for h in out["outline"]["headings"]]
                   == ["Kontrakt", "Kvittering"], f"outline: 5 pages, bookmarks as headings ({len(json.dumps(out))} chars)")
            hit = await call("document_search", {"input_path": str(merged), "query": "921 437 856"})
            expect(hit["matches_total"] == 1 and hit["matches"][0]["label"] == "page 3",
                   "search finds the near-duplicate org number on page 3 only")
            ar = await call("document_read_around", {"input_path": str(merged), "match_id": hit["matches"][0]["match_id"],
                                                     "before": 1, "after": 1})
            ex = ar["excerpt"]
            expect(ex["text"] == truth[ex["char_start"]:ex["char_end"]] and "921 437 856" in ex["text"],
                   f"read-around is verbatim (pages {ex['first_unit']}-{ex['last_unit']}) and contains the match")
            pieces, rd = [], await call("document_read", {"input_path": str(merged), "max_chars": 100})
            pieces.append(rd["excerpt"]["text"])
            while rd["next_cursor"]:
                rd = await call("document_read", {"input_path": str(merged), "cursor": rd["next_cursor"], "max_chars": 100})
                pieces.append(rd["excerpt"]["text"])
            expect("".join(pieces) == truth, f"{len(pieces)} capped reads rebuild the whole text layer exactly")
    transcript.close()
    print(f"\ntranscript: {work / 'transcript.jsonl'}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    ap.add_argument("--work", help="use this (new or empty) directory instead of a temp one")
    args = ap.parse_args()
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="docbridge_e2e_"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"work directory: {work}\n")
    try:
        return asyncio.run(run(work.resolve()))
    finally:
        if not args.keep and not args.work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
