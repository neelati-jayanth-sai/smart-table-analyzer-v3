"""Shared tool-contract primitives (Architecture.md #11-#13).

Every deterministic query tool is described by a :class:`ToolSpec`:

- ``name``            — the stable model-facing tool name,
- ``query_version``   — identifies the exact reviewed template/implementation
  that produced a result (persisted on every Rxxx record),
- ``parameters``      — the validated Pydantic input contract (strict),
- ``result``          — the backend-independent Pydantic payload contract,
- ``build_payload``   — the single shared rows -> payload constructor used by
  every backend, so local and production results are identical by construction
  (Runtime_Environments_UI.md #12: parity belongs at the tool contract),
- ``snapshot_scoped`` — whether the measurement targets one pinned snapshot.

Tools measure only. Specs carry no diagnosis, no scoring and no SQL.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import NoneType, UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

# Cost classes describe how a tool obtains its measurements; they are tool
# metadata (never diagnosis) and let the Investigator prefer cheap evidence.
COST_METADATA = "metadata"
COST_METADATA_AGGREGATION = "metadata-aggregation"
COST_TARGETED_SCAN = "targeted-scan"


class ToolSpec:
    """Static description of one reviewed query tool."""

    __slots__ = (
        "name",
        "query_version",
        "description",
        "parameters",
        "result",
        "build_payload",
        "snapshot_scoped",
        "cost_class",
        "entry_model",
        "rows_field",
    )

    def __init__(
        self,
        name: str,
        query_version: str,
        description: str,
        parameters: type[BaseModel],
        result: type[BaseModel],
        build_payload: Callable[..., BaseModel],
        *,
        snapshot_scoped: bool = True,
        cost_class: str = COST_METADATA,
        entry_model: type[BaseModel] | None = None,
        rows_field: str | None = None,
    ) -> None:
        self.name = name
        self.query_version = query_version
        self.description = description
        self.parameters = parameters
        self.result = result
        self.build_payload = build_payload
        self.snapshot_scoped = snapshot_scoped
        self.cost_class = cost_class
        # Tabular tools: the payload holds one list of entry rows; the schema
        # and row_count describe those entries. Summary tools describe the
        # payload model itself and have row_count 1.
        self.entry_model = entry_model
        self.rows_field = rows_field

    def payload_schema(self) -> dict[str, str]:
        """Field-name -> type-name map stored with every result. ``int?`` etc.
        mark nullable fields, which pins null semantics into the contract."""
        model = self.entry_model if self.entry_model is not None else self.result
        return payload_field_schemas(model)

    def row_count(self, payload: BaseModel) -> int:
        if self.rows_field is None:
            return 1
        rows = getattr(payload, self.rows_field)
        return len(rows)

    def __repr__(self) -> str:
        return f"ToolSpec(name={self.name!r}, query_version={self.query_version!r})"


def payload_field_schemas(model: type[BaseModel]) -> dict[str, str]:
    """Deterministic field/type map of a payload (or entry) model."""
    return {name: type_name(field.annotation) for name, field in model.model_fields.items()}


def type_name(annotation: Any) -> str:
    """Compact contract type name; ``None``-union members render as ``?``."""
    if annotation is None or annotation is NoneType:
        return "null"
    if annotation in (int, float, str, bool):
        base = {int: "int", float: "float", str: "string", bool: "boolean"}[annotation]
        return base
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        args = [arg for arg in get_args(annotation) if arg is not NoneType]
        if len(args) == 1:
            return type_name(args[0]) + "?"
        return "|".join(type_name(arg) for arg in args)
    if origin is dict:
        return "object"
    if origin is list:
        return "array"
    if origin is tuple or origin is set:
        return "array"
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "object"
    return str(annotation)


def percentile(sorted_values: Sequence[float], p: float) -> float:
    """Linear interpolation between closest ranks, on an ascending sequence.

    This is the single percentile definition shared by every backend and by
    the checked-in production templates (Spark ``percentile``): value =
    v[lo] + (rank - lo) * (v[hi] - v[lo]) with rank = p * (n - 1).
    Callers must pass a non-empty ascending sequence.
    """
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile requires a non-empty sequence")
    if not 0.0 <= p <= 1.0:
        raise ValueError("percentile p must be within [0, 1]")
    if n == 1:
        return float(sorted_values[0])
    rank = (n - 1) * p
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_values[lo])
    frac = rank - lo
    return float(sorted_values[lo]) + frac * (float(sorted_values[hi]) - float(sorted_values[lo]))


def median(sorted_values: Sequence[float]) -> float | None:
    """Contract median: interpolated 50th percentile, None for empty input."""
    if not sorted_values:
        return None
    return percentile(sorted_values, 0.5)


def single_row(rows: Sequence[Mapping[str, Any]], result: type[BaseModel]) -> BaseModel:
    """Validate that an aggregate tool produced exactly one normalized row."""
    if len(rows) != 1:
        raise ValueError(
            f"{result.__name__} expects exactly one aggregate row, got {len(rows)}"
        )
    return result.model_validate(dict(rows[0]))


def sum_optional(values: Sequence[int | None]) -> int | None:
    """Sum over values that report a count; None when nothing reports.

    Contract rule: a missing measurement never masquerades as zero.
    """
    known = [value for value in values if value is not None]
    return sum(known) if known else None