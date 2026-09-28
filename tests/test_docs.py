"""The documents may only assert what the code does (METHODOLOGY R3). These fail
when the vocabulary in code and the vocabulary in the documents drift apart."""
import asyncio
from pathlib import Path

from docbridge import normalize as N
from docbridge.contract import REQUIRED_AXES, SCHEMA_VERSION, ErrorCode, Reason, Status
from docbridge.server import build_server

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def tool_names() -> set[str]:
    return {t.name for t in asyncio.run(build_server().list_tools())}


def test_the_contract_names_every_status_reason_and_error_code():
    doc = read("DOCUMENT_MCP_CONTRACT.md")
    missing = [v for v in [s.value for s in Status] + [r.value for r in Reason] + [e.value for e in ErrorCode]
               if f"`{v}`" not in doc]
    assert missing == []


def test_every_tool_is_documented_in_the_contract_and_the_readme():
    for name in ("DOCUMENT_MCP_CONTRACT.md", "README.md"):
        doc = read(name)
        assert [t for t in tool_names() if f"`{t}`" not in doc] == [], name


def test_required_axes_and_schema_version_match():
    doc = read("DOCUMENT_MCP_CONTRACT.md")
    assert f"**{SCHEMA_VERSION}**" in doc
    for op, axes in REQUIRED_AXES.items():
        assert f"`{op}`" in doc
        assert " | " + ", ".join(f"`{a}`" for a in axes) + " |" in doc, op


def test_the_validation_model_lists_every_normalization_rule():
    doc = read("VALIDATION_MODEL.md")
    for c in list(N.REMOVED_CHARS) + list(N.BULLET_GLYPHS):
        assert f"U+{ord(c):04X}" in doc, c
    assert "U+FB00" in doc and "U+FB06" in doc
    for word in N.CURRENCY_WORDS:
        assert word in doc, word
    assert f"≥ {N.LONG_NUMBER_DIGITS} digits" in doc
