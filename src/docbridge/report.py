"""The validation report, as typed models.

These models ARE the output contract: the MCP server advertises their JSON
schema as each tool's outputSchema, so a client can check a response against
the declaration instead of trusting it. Every field listed in
DOCUMENT_MCP_CONTRACT.md is here; nothing here is optional without saying why.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StatusT = Literal["PASS", "FAIL", "NOT_CHECKED", "UNSUPPORTED"]
ReasonT = Literal[
    "no_text_layer", "unresolved_content", "inferred_structure", "needs_layout_engine",
    "not_implemented", "too_large", "check_failed", "inconclusive", "absent_in_format", "not_carried",
]
DiffKindT = Literal["missing", "added", "changed", "moved", "mismatch", "unverifiable"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AxisResult(_Model):
    status: StatusT
    reason: ReasonT | None = Field(None, description="Set iff status is NOT_CHECKED or UNSUPPORTED.")
    scope: str = Field(description="What exactly was compared, in one sentence. A PASS covers this and nothing else.")
    compared: int | None = Field(None, description="Items compared on each side (max of the two), if countable.")
    not_compared: int | None = Field(None, description="Items present but deliberately not judged (METHODOLOGY R18).")
    differences: int = 0
    details: dict[str, Any] = Field(default_factory=dict)


class Difference(_Model):
    axis: str
    kind: DiffKindT
    source: str | None = None
    target: str | None = None
    source_location: str | None = None
    target_location: str | None = None
    source_context: str | None = None
    target_context: str | None = None
    classes: list[str] = Field(default_factory=list)
    note: str | None = None


class WarningItem(_Model):
    side: Literal["source", "target", "operation"]
    code: str
    message: str
    location: str | None = None


class UnsupportedItem(_Model):
    check: str
    status: StatusT
    reason: ReasonT | None = None
    detail: str


class ValidationReport(_Model):
    schema_version: str
    operation: str
    source: list[str]
    target: list[str]
    source_format: str
    target_format: str
    source_sha256: list[str]
    target_sha256: list[str]
    status: StatusT = Field(description="Global verdict. PASS only if every required axis is PASS and no axis FAILs.")
    required_axes: list[str] = Field(description="The axes a global PASS covers for this operation.")
    status_basis: str = Field(description="Why the global status is what it is, naming the deciding axes.")
    text_status: StatusT
    numeric_status: StatusT
    identifier_status: StatusT
    structure_status: StatusT
    layout_status: StatusT
    image_status: StatusT
    axes: dict[str, AxisResult]
    structure_checks: dict[str, AxisResult]
    source_page_count: int | None
    target_page_count: int | None
    warnings: list[WarningItem]
    differences: list[Difference]
    differences_total: int
    differences_truncated: int = Field(description="Differences found but not listed (see max_differences).")
    unsupported_checks: list[UnsupportedItem]
    oracle: dict[str, str] = Field(description="What read each side - the instrument each verdict rests on.")
    normalization: dict[str, Any]
    tool_versions: dict[str, str]
    timestamp: str
    summary: str
