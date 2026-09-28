import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))  # makes `builders` importable

from docbridge.convert.pandoc import find_pandoc  # noqa: E402

needs_pandoc = pytest.mark.skipif(find_pandoc() is None, reason="pandoc not found on this machine")


@pytest.fixture
def work(tmp_path, monkeypatch):
    """A scratch directory that is the server's only allowed root."""
    monkeypatch.setenv("DOCBRIDGE_ROOTS", str(tmp_path))
    return tmp_path
