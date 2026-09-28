"""One exception type, carrying a code from the declared vocabulary and a hint.

Every failure a tool can meet leaves the tool as a value built from this, never
as a stack trace: an agent cannot read a traceback, and "the file is encrypted"
and "the server crashed" need different next moves.
"""
from __future__ import annotations

from .contract import ErrorCode


class DocbridgeError(Exception):
    def __init__(self, code: ErrorCode, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code.value}: {self.message}"
