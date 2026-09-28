"""Versions of every instrument a report's verdicts rest on."""
from __future__ import annotations

import functools
import importlib.metadata as md
import sys

from . import __version__


@functools.lru_cache(maxsize=1)
def tool_versions() -> dict[str, str]:
    v = {"docbridge": __version__, "python": sys.version.split()[0]}
    try:
        import pymupdf
        v["pymupdf"] = pymupdf.VersionBind
        v["mupdf"] = pymupdf.VersionFitz
    except Exception:  # pragma: no cover - dependency missing
        v["pymupdf"] = "not importable"
    for dist in ("python-docx", "markdown-it-py", "pillow", "lxml", "mcp"):
        try:
            v[dist] = md.version(dist)
        except md.PackageNotFoundError:
            v[dist] = "not installed"
    from .convert.pandoc import pandoc_version
    v["pandoc"] = pandoc_version() or "not found"
    return v
