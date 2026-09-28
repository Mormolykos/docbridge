"""Which paths a tool may read and write, decided before any file is opened.

The rules (DOCUMENT_MCP_CONTRACT.md, "File behaviour"):

* Paths must be absolute. A relative path would resolve against the server's
  working directory, which the calling agent cannot see.
* A path containing a `..` component is refused outright, whatever it would
  resolve to. Traversal is rejected, not normalised away.
* UNC shares (\\\\host\\share), device paths (\\\\?\\, \\\\.\\), NTFS alternate data
  streams (file.txt:stream) and reserved device names (CON, NUL, COM1, ...) are
  refused. Reading a UNC path is a network call.
* After symlinks and junctions are resolved, the path must lie inside one of the
  allowed roots: DOCBRIDGE_ROOTS (os.pathsep-separated), default the user's
  home directory.
* An output never replaces a source, not even with overwrite=True.
* An existing output is replaced only with overwrite=True, and only atomically:
  the new file is written beside it and moved into place, so a failed
  conversion never leaves a half-written file under the requested name.
"""
from __future__ import annotations

import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Iterator

from .contract import ErrorCode
from .errors import DocbridgeError

_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.IGNORECASE)
DEFAULT_MAX_INPUT_BYTES = 200 * 1024 * 1024


def allowed_roots() -> list[Path]:
    raw = os.environ.get("DOCBRIDGE_ROOTS", "").strip()
    parts = [p for p in raw.split(os.pathsep) if p.strip()] if raw else [str(Path.home())]
    roots = []
    for p in parts:
        try:
            roots.append(Path(p).expanduser().resolve())
        except OSError:
            continue
    return roots


def max_input_bytes() -> int:
    raw = os.environ.get("DOCBRIDGE_MAX_INPUT_MB", "").strip()
    if raw:
        try:
            return int(float(raw) * 1024 * 1024)
        except ValueError:
            pass
    return DEFAULT_MAX_INPUT_BYTES


def _refuse(code: ErrorCode, msg: str, hint: str) -> DocbridgeError:
    return DocbridgeError(code, msg, hint)


def _check_shape(raw: str, what: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise _refuse(ErrorCode.INVALID_ARGUMENT, f"{what} is empty", "Pass an absolute file path.")
    if "\x00" in raw:
        raise _refuse(ErrorCode.PATH_FORBIDDEN, f"{what} contains a NUL character", "Pass a real file path.")
    s = raw.strip()
    if s.startswith(("\\\\", "//")):
        raise _refuse(ErrorCode.PATH_FORBIDDEN, f"{what} is a UNC or device path: {s!r}",
                      "docbridge works on local files only; copy the file to a local folder first.")
    win = PureWindowsPath(s)
    if os.name == "nt":
        tail = s[2:] if len(s) >= 2 and s[1] == ":" else s
        if ":" in tail:
            raise _refuse(ErrorCode.PATH_FORBIDDEN, f"{what} names an alternate data stream: {s!r}",
                          "Remove the ':stream' suffix.")
    parts = win.parts if os.name == "nt" else Path(s).parts
    if any(p == ".." for p in parts):
        raise _refuse(ErrorCode.PATH_TRAVERSAL, f"{what} contains a '..' component: {s!r}",
                      "Pass the final absolute path; '..' is refused, not resolved.")
    for p in parts:
        if _RESERVED.match(p.rstrip(" .")):
            raise _refuse(ErrorCode.PATH_FORBIDDEN, f"{what} uses a reserved device name: {p!r}",
                          "Rename the file.")
    path = Path(s)
    if not path.is_absolute():
        raise _refuse(ErrorCode.PATH_NOT_ABSOLUTE, f"{what} is not absolute: {s!r}",
                      "Pass an absolute path; relative paths resolve against a directory you cannot see.")
    return path


def _inside_roots(resolved: Path, what: str) -> None:
    roots = allowed_roots()
    for root in roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    raise _refuse(ErrorCode.PATH_OUTSIDE_ROOTS,
                  f"{what} resolves outside the allowed roots: {resolved}",
                  "Allowed roots: " + "; ".join(str(r) for r in roots)
                  + ". Set DOCBRIDGE_ROOTS in the server's environment to change them.")


def resolve_input(raw: str, what: str = "input path") -> Path:
    path = _check_shape(raw, what)
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise _refuse(ErrorCode.INPUT_NOT_FOUND, f"{what} does not exist: {raw}",
                      "Check the path; it must name an existing file.") from None
    _inside_roots(resolved, what)
    if not resolved.is_file():
        raise _refuse(ErrorCode.INPUT_NOT_FOUND, f"{what} is not a regular file: {resolved}",
                      "Pass a file, not a directory.")
    size = resolved.stat().st_size
    if size > max_input_bytes():
        raise _refuse(ErrorCode.INPUT_TOO_LARGE,
                      f"{what} is {size} bytes, above the {max_input_bytes()} byte limit",
                      "Raise DOCBRIDGE_MAX_INPUT_MB for this server if the file is legitimate.")
    return resolved


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def resolve_output(raw: str, *, extensions: tuple[str, ...], overwrite: bool,
                   sources: list[Path], what: str = "output path") -> Path:
    path = _check_shape(raw, what)
    if path.suffix.lower() not in extensions:
        raise _refuse(ErrorCode.OUTPUT_EXTENSION_MISMATCH,
                      f"{what} must end in {' or '.join(extensions)}: {raw}",
                      f"Use a {extensions[0]} file name.")
    # Resolve the deepest existing ancestor, so a symlinked parent is followed
    # before the roots check, then re-attach the parts that do not exist yet.
    existing = path
    missing: list[str] = []
    while not existing.exists():
        missing.append(existing.name)
        if existing.parent == existing:
            break
        existing = existing.parent
    resolved = existing.resolve()
    for name in reversed(missing):
        resolved = resolved / name
    _inside_roots(resolved, what)
    for src in sources:
        if _same(resolved, src):
            raise _refuse(ErrorCode.WOULD_OVERWRITE_SOURCE, f"{what} is the source file: {resolved}",
                          "Choose a different output name; docbridge never replaces a source.")
    if resolved.exists():
        if resolved.is_dir():
            raise _refuse(ErrorCode.OUTPUT_EXISTS, f"{what} is a directory: {resolved}", "Name a file.")
        if not overwrite:
            raise _refuse(ErrorCode.OUTPUT_EXISTS, f"{what} already exists: {resolved}",
                          "Pass overwrite=true to replace it, or choose another name.")
    return resolved


def resolve_output_dir(raw: str, *, what: str = "output directory") -> Path:
    path = _check_shape(raw, what)
    existing, missing = path, []
    while not existing.exists():
        missing.append(existing.name)
        if existing.parent == existing:
            break
        existing = existing.parent
    resolved = existing.resolve()
    for name in reversed(missing):
        resolved = resolved / name
    _inside_roots(resolved, what)
    if resolved.exists() and not resolved.is_dir():
        raise _refuse(ErrorCode.OUTPUT_EXISTS, f"{what} is a file: {resolved}", "Name a directory.")
    return resolved


def default_output(source: Path, suffix: str) -> Path:
    """Deterministic default name: the source's stem, beside the source."""
    return source.with_suffix(suffix)


@contextmanager
def atomic_target(final: Path) -> Iterator[Path]:
    """Yield a temporary path beside `final`; move it into place on success,
    delete it on failure. Parent directories are created inside the roots
    (the caller has already checked `final` is inside them)."""
    final.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{final.stem}.", suffix=f".docbridge-tmp{final.suffix}",
                               dir=str(final.parent))
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        yield tmp_path
        os.replace(tmp_path, final)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
