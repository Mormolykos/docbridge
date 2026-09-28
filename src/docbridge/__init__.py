"""docbridge - document conversion that reports what it did not check.

A conversion that produced a file is not a conversion that preserved the
document. Every operation here returns a validation report whose verdicts are
PASS, FAIL, NOT_CHECKED or UNSUPPORTED per property, and a global PASS only
covers the properties the report names as checked.
"""

__version__ = "0.2.4"
