"""Markdown <-> DOCX through Pandoc, run as a sandboxed subprocess.

Why Pandoc, and why the checker is not Pandoc: the converter being scored must
not be the thing scoring it (METHODOLOGY R7). Pandoc converts; docbridge's own
readers (markdown-it-py for Markdown, lxml for DOCX) judge the result. A Pandoc
bug and a docbridge-reader bug would have to coincide to pass silently.

Fixed invocation, all of it deliberate:

  --sandbox                 no file reads beyond the named input, no network.
                            Images referenced from Markdown are therefore never
                            fetched; Pandoc writes their alt text instead, and
                            the report says so.
  -f/-t commonmark+pipe_tables+strikeout
                            the same dialect docbridge's Markdown reader uses.
                            Pandoc's `gfm` reader rewrites content the reader
                            would keep literal (":smile:" -> an emoji, "- [ ]" ->
                            a checkbox glyph), measured 2026-09-24.
  --wrap=none --eol=lf      no hard wrapping, platform-independent line ends
  --no-highlight            code blocks stay plain (see markdown_to_docx)
  SOURCE_DATE_EPOCH=0       deterministic document metadata dates
  Markdown goes in on stdin, already decoded by docbridge's strict decoder, so
  Pandoc never guesses an encoding.
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..contract import ErrorCode
from ..errors import DocbridgeError

MD_DIALECT = "commonmark+pipe_tables+strikeout"
TIMEOUT_S = 180
MIN_VERSION = (2, 15)   # --sandbox


def find_pandoc() -> str | None:
    env = os.environ.get("DOCBRIDGE_PANDOC", "").strip()
    if env:
        return env if Path(env).is_file() else None
    found = shutil.which("pandoc")
    if found:
        return found
    home = Path.home()
    for cand in (home / "miniconda3" / "Library" / "bin" / "pandoc.exe",
                 home / "anaconda3" / "Library" / "bin" / "pandoc.exe",
                 Path(os.environ.get("LOCALAPPDATA", str(home))) / "Pandoc" / "pandoc.exe",
                 Path("C:/Program Files/Pandoc/pandoc.exe")):
        if cand.is_file():
            return str(cand)
    return None


def _flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


@functools.lru_cache(maxsize=1)
def pandoc_version() -> str | None:
    exe = find_pandoc()
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL, creationflags=_flags())
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"pandoc(?:\.exe)?\s+([0-9][0-9.]*)", (r.stdout or "").strip())
    return m.group(1) if m else None


def _require() -> str:
    exe = find_pandoc()
    version = pandoc_version()
    if not exe or not version:
        raise DocbridgeError(ErrorCode.DEPENDENCY_MISSING, "pandoc was not found",
                             "Install Pandoc (>= 2.15) or set DOCBRIDGE_PANDOC to pandoc.exe.")
    parts = tuple(int(x) for x in version.split(".")[:2] if x.isdigit())
    if parts < MIN_VERSION:
        raise DocbridgeError(ErrorCode.DEPENDENCY_MISSING, f"pandoc {version} has no --sandbox",
                             "Upgrade Pandoc to 2.15 or later.")
    return exe


def _run(args: list[str], stdin_text: str | None = None) -> str:
    exe = _require()
    env = dict(os.environ, SOURCE_DATE_EPOCH="0")
    try:
        r = subprocess.run([exe, "--sandbox", *args], input=stdin_text,
                           stdin=None if stdin_text is not None else subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT_S, env=env, creationflags=_flags())
    except subprocess.TimeoutExpired:
        raise DocbridgeError(ErrorCode.CONVERSION_FAILED, f"pandoc exceeded {TIMEOUT_S}s",
                             "The document may be pathological; try a smaller part of it.") from None
    except OSError as e:
        raise DocbridgeError(ErrorCode.DEPENDENCY_MISSING, f"pandoc could not start: {e}",
                             "Check the Pandoc installation.") from None
    if r.returncode != 0:
        raise DocbridgeError(ErrorCode.CONVERSION_FAILED,
                             f"pandoc exited {r.returncode}: {(r.stderr or '').strip()[:500]}",
                             "The input may be malformed for its format.")
    return (r.stderr or "").strip()


def markdown_to_docx(markdown_text: str, out: Path) -> list[str]:
    """Returns Pandoc's warnings, one per line.

    --no-highlight: with highlighting on, Pandoc styles code tokens with
    character styles - and its Python lexer marks "03" in "25/03/2025" as an
    ErrorTok, which is BOLD (measured 2026-09-24). That is formatting no author
    wrote; code blocks stay plain text."""
    err = _run(["-f", MD_DIALECT, "-t", "docx", "--no-highlight", "-o", str(out)], stdin_text=markdown_text)
    return [ln for ln in err.splitlines() if ln.strip()]


def docx_to_markdown(src: Path, out: Path) -> list[str]:
    err = _run(["-f", "docx", "-t", MD_DIALECT, "--wrap=none", "--eol=lf",
                "--track-changes=accept", "-o", str(out), str(src)])
    return [ln for ln in err.splitlines() if ln.strip()]
