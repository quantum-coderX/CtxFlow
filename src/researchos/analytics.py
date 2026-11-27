"""Versioned datasets and a fixed, typed analysis catalog.

The model never computes numbers or writes code here. A request names a
dataset version, one operation from :data:`OPERATIONS`, and parameters that
are validated against that operation's schema. Results record the dataset
content hash, parameters, warnings and library versions so that re-running
the same request reproduces identical numbers.
"""

from __future__ import annotations

import csv
import io
import json
import math
import platform
import re
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple, Type, Union

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from researchos.project_store import (
    ProjectStore,
    StoreError,
    canonical_json,
    safe_key,
    sha256_bytes,
    slugify,
    stable_hash,
)

ENGINE_VERSION = "analytics-1.0"
MAX_DATASET_BYTES = 25 * 1024 * 1024
MAX_ROWS = 250_000
MAX_COLUMNS = 512
DTYPES = ("integer", "number", "boolean", "datetime", "string")
NUMERIC_DTYPES = frozenset({"integer", "number", "boolean"})
MISSING_TOKENS = frozenset({"", "na", "n/a", "nan", "null", "none", "#n/a"})
FORMAT_BY_SUFFIX = {
    ".csv": "csv",
    ".tsv": "tsv",
    ".txt": "csv",
    ".json": "json",
    ".jsonl": "jsonl",
    ".xlsx": "xlsx",
    ".parquet": "parquet",
}

_INT_RE = re.compile(r"^[+-]?\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


class AnalyticsError(ValueError):
    """A dataset or analysis request is invalid or cannot be computed."""


# ---------------------------------------------------------------------------
# Typed table
# ---------------------------------------------------------------------------


@dataclass
class Table:
    """Column-oriented table with explicit per-column types; ``None`` is missing."""

    columns: List[str]
    dtypes: Dict[str, str]
    data: Dict[str, List[Any]]

    @property
    def n_rows(self) -> int:
        return len(self.data[self.columns[0]]) if self.columns else 0

    def require(self, name: str) -> List[Any]:
        if name not in self.data:
            raise AnalyticsError(
                f"Unknown column {name!r}. Available: {', '.join(self.columns[:50])}."
            )
        return self.data[name]

    def numeric(self, name: str) -> np.ndarray:
        values = self.require(name)
        dtype = self.dtypes[name]
        if dtype not in NUMERIC_DTYPES:
            raise AnalyticsError(
                f"Column {name!r} has type {dtype}; this operation needs a numeric column."
            )
        return np.array(
            [np.nan if value is None else float(value) for value in values], dtype=float
        )

    def rows(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        count = self.n_rows if limit is None else min(limit, self.n_rows)
        return [{col: self.data[col][index] for col in self.columns} for index in range(count)]

    def take(self, indices: Sequence[int]) -> "Table":
        return Table(
            columns=list(self.columns),
            dtypes=dict(self.dtypes),
            data={col: [self.data[col][i] for i in indices] for col in self.columns},
        )

    def to_payload(self) -> Dict[str, Any]:
        return {
            "columns": [{"name": col, "dtype": self.dtypes[col]} for col in self.columns],
            "data": {col: self.data[col] for col in self.columns},
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "Table":
        columns = [item["name"] for item in payload["columns"]]
        return cls(
            columns=columns,
            dtypes={item["name"]: item["dtype"] for item in payload["columns"]},
            data={col: list(payload["data"][col]) for col in columns},
        )

    def content_hash(self) -> str:
        return stable_hash(self.to_payload(), length=64)


def _is_missing(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, float) and math.isnan(raw):
        return True
    return isinstance(raw, str) and raw.strip().lower() in MISSING_TOKENS


def _to_text(raw: Any) -> Optional[str]:
    if _is_missing(raw):
        return None
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float, str)):
        return str(raw).strip()
    if hasattr(raw, "isoformat"):
        return raw.isoformat()
    return str(raw).strip()


def _parse_number(text: str) -> Optional[float]:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _parse_datetime(text: str) -> Optional[str]:
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat()


def _convert(text: str, dtype: str) -> Any:
    """Convert one non-missing text cell; raises ValueError when impossible."""
    if dtype == "string":
        return text
    if dtype == "integer":
        if _INT_RE.fullmatch(text):
            return int(text)
        number = _parse_number(text)
        if number is not None and number.is_integer():
            return int(number)
        raise ValueError(text)
    if dtype == "number":
        number = _parse_number(text)
        if number is None:
            raise ValueError(text)
        return number
    if dtype == "boolean":
        lowered = text.lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise ValueError(text)
    if dtype == "datetime":
        parsed = _parse_datetime(text)
        if parsed is None:
            raise ValueError(text)
        return parsed
    raise ValueError(f"unknown dtype {dtype}")


def _infer_dtype(texts: List[Optional[str]]) -> Tuple[str, Optional[str]]:
    present = [text for text in texts if text is not None]
    if not present:
        return "string", "column is entirely missing"
    if all(_INT_RE.fullmatch(text) for text in present):
        return "integer", None
    numeric = [_parse_number(text) is not None for text in present]
    if all(numeric):
        return "number", None
    if all(text.lower() in ("true", "false") for text in present):
        return "boolean", None
    if all(_parse_datetime(text) is not None for text in present):
        return "datetime", None
    share = sum(numeric) / len(present)
    if share >= 0.9:
        bad = len(present) - sum(numeric)
        return "string", (
            f"mostly numeric but {bad} non-numeric value(s) present; kept as string "
            "(cast explicitly after cleaning)"
        )
    return "string", None


def build_table(
    header: Sequence[Any],
    raw_rows: Sequence[Sequence[Any]],
    *,
    column_types: Optional[Dict[str, str]] = None,
) -> Tuple[Table, List[str]]:
    """Build a typed table. Types are inferred deterministically unless overridden."""
    warnings: List[str] = []
    names: List[str] = []
    seen: Counter = Counter()
    for index, raw in enumerate(header):
        name = str(raw).strip() if raw is not None else ""
        if not name:
            name = f"column_{index + 1}"
            warnings.append(f"Empty header at position {index + 1} named {name!r}.")
        seen[name] += 1
        if seen[name] > 1:
            renamed = f"{name}_{seen[name]}"
            warnings.append(f"Duplicate header {name!r} renamed to {renamed!r}.")
            name = renamed
        names.append(name)
    if not names:
        raise AnalyticsError("Dataset has no columns.")
    if len(names) > MAX_COLUMNS:
        raise AnalyticsError(f"Dataset exceeds the {MAX_COLUMNS}-column limit.")
    if len(raw_rows) > MAX_ROWS:
        raise AnalyticsError(f"Dataset exceeds the {MAX_ROWS}-row limit.")

    texts: Dict[str, List[Optional[str]]] = {name: [] for name in names}
    for row_number, row in enumerate(raw_rows, start=2):
        if len(row) != len(names):
            raise AnalyticsError(
                f"Row {row_number} has {len(row)} fields; header has {len(names)}."
            )
        for name, cell in zip(names, row):
            texts[name].append(_to_text(cell))

    overrides = dict(column_types or {})
    for name, dtype in overrides.items():
        if name not in texts:
            raise AnalyticsError(f"column_types names unknown column {name!r}.")
        if dtype not in DTYPES:
            raise AnalyticsError(f"column_types[{name!r}] must be one of {DTYPES}.")

    dtypes: Dict[str, str] = {}
    data: Dict[str, List[Any]] = {}
    for name in names:
        if name in overrides:
            dtype = overrides[name]
        else:
            dtype, note = _infer_dtype(texts[name])
            if note:
                warnings.append(f"Column {name!r}: {note}.")
        values: List[Any] = []
        for row_offset, text in enumerate(texts[name]):
            if text is None:
                values.append(None)
                continue
            try:
                values.append(_convert(text, dtype))
            except ValueError:
                raise AnalyticsError(
                    f"Column {name!r} row {row_offset + 2}: {text!r} is not a valid {dtype}."
                ) from None
        dtypes[name] = dtype
        data[name] = values
    return Table(columns=names, dtypes=dtypes, data=data), warnings


def parse_dataset(
    payload: bytes,
    fmt: str,
    *,
    delimiter: Optional[str] = None,
    sheet: Optional[str] = None,
    column_types: Optional[Dict[str, str]] = None,
) -> Tuple[Table, List[str], Dict[str, Any]]:
    """Parse raw bytes into a typed :class:`Table` without changing any values."""
    if fmt in ("csv", "tsv"):
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AnalyticsError(f"File is not valid UTF-8 text ({exc.reason}).") from None
        if fmt == "tsv":
            chosen = "\t"
        elif delimiter:
            if delimiter not in (",", ";", "\t", "|"):
                raise AnalyticsError("delimiter must be one of ',', ';', '\\t', '|'.")
            chosen = delimiter
        else:
            try:
                chosen = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
            except csv.Error:
                chosen = ","
        rows = [row for row in csv.reader(io.StringIO(text), delimiter=chosen) if any(c.strip() for c in row)]
        if not rows:
            raise AnalyticsError("Dataset file is empty.")
        table, warnings = build_table(rows[0], rows[1:], column_types=column_types)
        return table, warnings, {"parser": "python-csv", "delimiter": chosen, "encoding": "utf-8"}

    if fmt in ("json", "jsonl"):
        try:
            text = payload.decode("utf-8-sig")
            if fmt == "jsonl":
                records = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                records = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AnalyticsError(f"Invalid JSON dataset: {exc}.") from None
        if not isinstance(records, list) or not records or not all(
            isinstance(item, dict) for item in records
        ):
            raise AnalyticsError("JSON dataset must be a non-empty array of objects.")
        header: List[str] = []
        for record in records:
            for key, value in record.items():
                if isinstance(value, (dict, list)):
                    raise AnalyticsError("Nested JSON values are not supported; flatten first.")
                if str(key) not in header:
                    header.append(str(key))
        rows = [[record.get(key) for key in header] for record in records]
        table, warnings = build_table(header, rows, column_types=column_types)
        return table, warnings, {"parser": "python-json"}

    if fmt == "xlsx":
        try:
            import openpyxl  # type: ignore
        except ImportError:
            raise AnalyticsError("XLSX support requires openpyxl (pip install openpyxl).") from None
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - parser errors become a visible failure
            raise AnalyticsError(f"Could not read XLSX file: {exc}") from None
        try:
            worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
            rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
            title = worksheet.title
        except KeyError:
            raise AnalyticsError(f"Sheet {sheet!r} not found.") from None
        finally:
            workbook.close()
        rows = [row for row in rows if any(cell is not None for cell in row)]
        if not rows:
            raise AnalyticsError("Worksheet is empty.")
        table, warnings = build_table(rows[0], rows[1:], column_types=column_types)
        return table, warnings, {"parser": "openpyxl", "sheet": title}

    if fmt == "parquet":
        try:
            import pyarrow.parquet as pq  # type: ignore
        except ImportError:
            raise AnalyticsError("Parquet support requires pyarrow (pip install pyarrow).") from None
        try:
            arrow = pq.read_table(io.BytesIO(payload))
        except Exception as exc:  # noqa: BLE001
            raise AnalyticsError(f"Could not read Parquet file: {exc}") from None
        header = list(arrow.column_names)
        columns = [arrow.column(name).to_pylist() for name in header]
        rows = [list(values) for values in zip(*columns)] if columns else []
        table, warnings = build_table(header, rows, column_types=column_types)
        return table, warnings, {"parser": "pyarrow"}

    raise AnalyticsError(f"Unsupported dataset format {fmt!r}.")


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------


def _clean(value: Any) -> Any:
    """Make numpy/NaN values JSON-safe without changing finite numbers."""
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def profile_table(table: Table) -> Dict[str, Any]:
    """Factual schema and quality profile; no values are modified."""
    columns: List[Dict[str, Any]] = []
    warnings: List[str] = []
    n_rows = table.n_rows
    for name in table.columns:
        values = table.data[name]
        present = [value for value in values if value is not None]
        missing = n_rows - len(present)
        info: Dict[str, Any] = {
            "name": name,
            "dtype": table.dtypes[name],
            "count": len(present),
            "missing": missing,
            "missing_pct": (missing / n_rows * 100.0) if n_rows else 0.0,
            "unique": len(set(present)),
        }
        if table.dtypes[name] in NUMERIC_DTYPES and present:
            array = np.array([float(value) for value in present], dtype=float)
            info.update(
                {
                    "mean": float(np.mean(array)),
                    "std": float(np.std(array, ddof=1)) if array.size > 1 else None,
                    "min": float(np.min(array)),
                    "q25": float(np.percentile(array, 25)),
                    "median": float(np.median(array)),
                    "q75": float(np.percentile(array, 75)),
                    "max": float(np.max(array)),
                }
            )
        elif table.dtypes[name] == "datetime" and present:
            info.update({"min": min(present), "max": max(present)})
        elif present:
            info["top_values"] = [
                {"value": value, "count": count}
                for value, count in sorted(
                    Counter(present).items(), key=lambda item: (-item[1], str(item[0]))
                )[:5]
            ]
        if n_rows and missing / n_rows > 0.2:
            warnings.append(f"Column {name!r} is {missing / n_rows:.0%} missing.")
        if present and len(set(present)) == 1 and n_rows > 1:
            warnings.append(f"Column {name!r} is constant.")
        columns.append(info)
    seen_rows: Counter = Counter(
        canonical_json([table.data[col][i] for col in table.columns]) for i in range(n_rows)
    )
    duplicates = sum(count - 1 for count in seen_rows.values() if count > 1)
    if duplicates:
        warnings.append(f"{duplicates} duplicate row(s) present.")
    return _clean(
        {
            "row_count": n_rows,
            "column_count": len(table.columns),
            "duplicate_rows": duplicates,
            "columns": columns,
            "warnings": warnings,
            "quantile_method": "numpy-linear",
        }
    )


# ---------------------------------------------------------------------------
# Transformations (each produces a new immutable dataset version)
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectColumns(_Strict):
    op: Literal["select_columns"]
    columns: List[str] = Field(min_length=1, max_length=MAX_COLUMNS)


class RenameColumns(_Strict):
    op: Literal["rename_columns"]
    mapping: Dict[str, str] = Field(min_length=1)


class FilterRows(_Strict):
    op: Literal["filter_rows"]
    column: str
    operator: Literal["==", "!=", "<", "<=", ">", ">=", "in", "not_in", "is_missing", "not_missing"]
    value: Any = None


class DropMissing(_Strict):
    op: Literal["drop_missing"]
    columns: Optional[List[str]] = None


class FillMissing(_Strict):
    op: Literal["fill_missing"]
    column: str
    strategy: Literal["value", "mean", "median", "mode"]
    value: Any = None


class SortRows(_Strict):
    op: Literal["sort_rows"]
    columns: List[str] = Field(min_length=1, max_length=8)
    descending: bool = False


class Deduplicate(_Strict):
    op: Literal["deduplicate"]
    columns: Optional[List[str]] = None


class CastColumn(_Strict):
    op: Literal["cast_column"]
    column: str
    dtype: Literal["integer", "number", "boolean", "datetime", "string"]


class JoinDataset(_Strict):
    op: Literal["join"]
    dataset_id: str
    version: Optional[int] = Field(default=None, ge=1)
    on: List[str] = Field(min_length=1, max_length=4)
    how: Literal["inner", "left"] = "inner"
    suffix: str = Field(default="_right", pattern=r"^_[A-Za-z0-9_]{1,16}$")


_STEP_MODELS: Dict[str, Type[_Strict]] = {
    "select_columns": SelectColumns,
    "rename_columns": RenameColumns,
    "filter_rows": FilterRows,
    "drop_missing": DropMissing,
    "fill_missing": FillMissing,
    "sort_rows": SortRows,
    "deduplicate": Deduplicate,
    "cast_column": CastColumn,
    "join": JoinDataset,
}
TRANSFORM_OPS = tuple(_STEP_MODELS)
MAX_TRANSFORM_STEPS = 20


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "params"
        parts.append(f"{location}: {error.get('msg')}")
    return "; ".join(parts)


def validate_steps(steps: Sequence[Dict[str, Any]]) -> List[_Strict]:
    if not isinstance(steps, (list, tuple)) or not steps:
        raise AnalyticsError("steps must be a non-empty list.")
    if len(steps) > MAX_TRANSFORM_STEPS:
        raise AnalyticsError(f"At most {MAX_TRANSFORM_STEPS} transform steps are allowed.")
    parsed: List[_Strict] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("op") not in _STEP_MODELS:
            raise AnalyticsError(f"Step {index + 1}: op must be one of {sorted(_STEP_MODELS)}.")
        try:
            parsed.append(_STEP_MODELS[step["op"]].model_validate(step))
        except ValidationError as exc:
            raise AnalyticsError(f"Step {index + 1}: {_validation_message(exc)}") from None
    return parsed


def _sort_key(value: Any) -> Tuple[int, Any]:
    if value is None:
        return (1, 0)
    if isinstance(value, bool):
        return (0, int(value))
    return (0, value)


def _coerce_like(value: Any, dtype: str, column: str) -> Any:
    text = _to_text(value)
    if text is None:
        raise AnalyticsError(f"Value for {column!r} must not be missing.")
    try:
        return _convert(text, dtype)
    except ValueError:
        raise AnalyticsError(f"Value {value!r} is not a valid {dtype} for {column!r}.") from None


def _with_column(table: Table, column: str, values: List[Any], dtype: Optional[str] = None) -> Table:
    data = dict(table.data)
    data[column] = values
    dtypes = dict(table.dtypes)
    if dtype is not None:
        dtypes[column] = dtype
    return Table(list(table.columns), dtypes, data)


def apply_steps(
    table: Table,
    steps: Sequence[_Strict],
    *,
    load_other: Callable[[str, Optional[int]], Tuple[Table, Dict[str, Any]]],
) -> Tuple[Table, List[str], List[Dict[str, Any]]]:
    """Apply validated transform steps; returns (table, warnings, joined inputs)."""
    warnings: List[str] = []
    inputs: List[Dict[str, Any]] = []
    current = table
    for step in steps:
        if isinstance(step, SelectColumns):
            for col in step.columns:
                current.require(col)
            if len(set(step.columns)) != len(step.columns):
                raise AnalyticsError("select_columns lists a column twice.")
            current = Table(
                columns=list(step.columns),
                dtypes={c: current.dtypes[c] for c in step.columns},
                data={c: list(current.data[c]) for c in step.columns},
            )
        elif isinstance(step, RenameColumns):
            for old in step.mapping:
                current.require(old)
            new_names = [step.mapping.get(c, c).strip() for c in current.columns]
            if len(set(new_names)) != len(new_names) or any(not n for n in new_names):
                raise AnalyticsError("rename_columns would create empty or duplicate names.")
            current = Table(
                columns=new_names,
                dtypes={new: current.dtypes[old] for old, new in zip(current.columns, new_names)},
                data={new: current.data[old] for old, new in zip(current.columns, new_names)},
            )
        elif isinstance(step, FilterRows):
            values = current.require(step.column)
            dtype = current.dtypes[step.column]
            if step.operator == "is_missing":
                keep = [i for i, v in enumerate(values) if v is None]
            elif step.operator == "not_missing":
                keep = [i for i, v in enumerate(values) if v is not None]
            elif step.operator in ("in", "not_in"):
                if not isinstance(step.value, list) or not step.value:
                    raise AnalyticsError(f"{step.operator} needs a non-empty list value.")
                allowed = {_coerce_like(v, dtype, step.column) for v in step.value}
                want = step.operator == "in"
                keep = [i for i, v in enumerate(values) if v is not None and (v in allowed) == want]
            else:
                if dtype in ("string", "boolean") and step.operator not in ("==", "!="):
                    raise AnalyticsError(f"Operator {step.operator} is not valid for {dtype} columns.")
                target = _coerce_like(step.value, dtype, step.column)
                if dtype == "datetime":
                    target_key = _time_value(target, dtype)
                    keyed = [None if v is None else _time_value(v, dtype) for v in values]
                else:
                    target_key, keyed = target, list(values)
                compare = {
                    "==": lambda a: a == target_key,
                    "!=": lambda a: a != target_key,
                    "<": lambda a: a < target_key,
                    "<=": lambda a: a <= target_key,
                    ">": lambda a: a > target_key,
                    ">=": lambda a: a >= target_key,
                }[step.operator]
                keep = [i for i, v in enumerate(keyed) if v is not None and compare(v)]
            removed = current.n_rows - len(keep)
            warnings.append(f"filter_rows on {step.column!r} removed {removed} row(s).")
            current = current.take(keep)
        elif isinstance(step, DropMissing):
            cols = step.columns or current.columns
            for col in cols:
                current.require(col)
            keep = [i for i in range(current.n_rows) if all(current.data[c][i] is not None for c in cols)]
            warnings.append(f"drop_missing removed {current.n_rows - len(keep)} row(s).")
            current = current.take(keep)
        elif isinstance(step, FillMissing):
            values = list(current.require(step.column))
            dtype = current.dtypes[step.column]
            present = [v for v in values if v is not None]
            new_dtype = None
            if step.strategy == "value":
                fill = _coerce_like(step.value, dtype, step.column)
            elif step.strategy in ("mean", "median"):
                if dtype not in ("integer", "number"):
                    raise AnalyticsError(f"{step.strategy} fill needs a numeric column.")
                if not present:
                    raise AnalyticsError(f"Column {step.column!r} has no values to compute a fill.")
                array = np.array(present, dtype=float)
                fill = float(np.mean(array) if step.strategy == "mean" else np.median(array))
                if dtype == "integer":
                    new_dtype = "number"
                    values = [None if v is None else float(v) for v in values]
            else:
                if not present:
                    raise AnalyticsError(f"Column {step.column!r} has no values to compute a mode.")
                fill = sorted(Counter(present).items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0]
            filled = sum(1 for v in values if v is None)
            warnings.append(
                f"fill_missing filled {filled} value(s) in {step.column!r} using {step.strategy}={fill!r}."
            )
            current = _with_column(current, step.column, [fill if v is None else v for v in values], new_dtype)
        elif isinstance(step, SortRows):
            for col in step.columns:
                current.require(col)
            order = sorted(
                range(current.n_rows),
                key=lambda i: tuple(_sort_key(current.data[c][i]) for c in step.columns),
                reverse=step.descending,
            )
            current = current.take(order)
        elif isinstance(step, Deduplicate):
            cols = step.columns or current.columns
            for col in cols:
                current.require(col)
            seen: set = set()
            keep = []
            for i in range(current.n_rows):
                key = canonical_json([current.data[c][i] for c in cols])
                if key not in seen:
                    seen.add(key)
                    keep.append(i)
            warnings.append(f"deduplicate removed {current.n_rows - len(keep)} row(s).")
            current = current.take(keep)
        elif isinstance(step, CastColumn):
            values = current.require(step.column)
            converted = []
            for offset, value in enumerate(values):
                text = _to_text(value)
                if text is None:
                    converted.append(None)
                    continue
                try:
                    converted.append(_convert(text, step.dtype))
                except ValueError:
                    raise AnalyticsError(
                        f"cast_column: row {offset + 1} value {value!r} is not a valid {step.dtype}."
                    ) from None
            current = _with_column(current, step.column, converted, step.dtype)
        elif isinstance(step, JoinDataset):
            current = _join(current, step, load_other, inputs)
    return current, warnings, inputs


def _join(
    current: Table,
    step: JoinDataset,
    load_other: Callable[[str, Optional[int]], Tuple[Table, Dict[str, Any]]],
    inputs: List[Dict[str, Any]],
) -> Table:
    other, other_record = load_other(step.dataset_id, step.version)
    inputs.append(
        {
            "dataset_id": other_record["dataset_id"],
            "version": other_record["version"],
            "content_hash": other_record["content_hash"],
        }
    )
    for col in step.on:
        current.require(col)
        other.require(col)
    index: Dict[str, List[int]] = {}
    for j in range(other.n_rows):
        key_values = [other.data[c][j] for c in step.on]
        if any(v is None for v in key_values):
            continue
        index.setdefault(canonical_json(key_values), []).append(j)
    right_cols = [c for c in other.columns if c not in step.on]
    out_names = list(current.columns)
    rename: Dict[str, str] = {}
    for col in right_cols:
        name = col if col not in out_names else f"{col}{step.suffix}"
        if name in out_names:
            raise AnalyticsError(f"join would create duplicate column {name!r}.")
        rename[col] = name
        out_names.append(name)
    data: Dict[str, List[Any]] = {c: [] for c in out_names}
    for i in range(current.n_rows):
        key_values = [current.data[c][i] for c in step.on]
        matches: List[Optional[int]] = []
        if not any(v is None for v in key_values):
            matches = list(index.get(canonical_json(key_values), []))
        if not matches and step.how == "left":
            matches = [None]
        for j in matches:
            for c in current.columns:
                data[c].append(current.data[c][i])
            for c in right_cols:
                data[rename[c]].append(None if j is None else other.data[c][j])
        if len(data[out_names[0]]) > MAX_ROWS:
            raise AnalyticsError(f"join result exceeds the {MAX_ROWS}-row limit.")
    dtypes = dict(current.dtypes)
    for c in right_cols:
        dtypes[rename[c]] = other.dtypes[c]
    return Table(out_names, dtypes, data)


# ---------------------------------------------------------------------------
# Dataset registry
# ---------------------------------------------------------------------------


def detect_format(filename: str, fmt: Optional[str] = None) -> str:
    if fmt:
        value = fmt.lower().strip()
        if value not in set(FORMAT_BY_SUFFIX.values()):
            raise AnalyticsError(f"Unsupported format {fmt!r}.")
        return value
    suffix = Path(filename or "").suffix.lower()
    if suffix not in FORMAT_BY_SUFFIX:
        raise AnalyticsError(
            f"Unsupported file type {suffix or '(none)'}; use one of "
            f"{', '.join(sorted(FORMAT_BY_SUFFIX))}."
        )
    return FORMAT_BY_SUFFIX[suffix]


class DatasetRegistry:
    """Immutable, versioned datasets stored per project."""

    def __init__(self, store: ProjectStore, *, cache_size: int = 16) -> None:
        self.store = store
        self._cache: "OrderedDict[Tuple[str, str, int], Table]" = OrderedDict()
        self._cache_size = cache_size

    def _dir(self, project_id: str, dataset_id: str) -> Path:
        return self.store.kind_dir(project_id, "datasets", safe_key(dataset_id, label="dataset_id"))

    def _manifest_path(self, project_id: str, dataset_id: str) -> Path:
        return self._dir(project_id, dataset_id) / "manifest.json"

    def _version_dir(self, project_id: str, dataset_id: str, version: int) -> Path:
        return self._dir(project_id, dataset_id) / f"v{int(version)}"

    def list(self, project_id: str) -> List[Dict[str, Any]]:
        root = self.store.kind_dir(project_id, "datasets")
        manifests = []
        for directory in self.store.list_subdirs(root):
            manifest = self.store.read_json(directory / "manifest.json")
            if isinstance(manifest, dict):
                manifests.append(manifest)
        manifests.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
        return manifests

    def get(self, project_id: str, dataset_id: str) -> Dict[str, Any]:
        manifest = self.store.read_json(self._manifest_path(project_id, dataset_id))
        if not isinstance(manifest, dict):
            raise AnalyticsError(f"Dataset {dataset_id!r} not found in project {project_id!r}.")
        return manifest

    def latest_version(self, project_id: str, dataset_id: str) -> int:
        return int(self.get(project_id, dataset_id)["latest_version"])

    def version(self, project_id: str, dataset_id: str, version: Optional[int] = None) -> Dict[str, Any]:
        manifest = self.get(project_id, dataset_id)
        number = int(version) if version is not None else int(manifest["latest_version"])
        record = self.store.read_json(self._version_dir(project_id, dataset_id, number) / "version.json")
        if not isinstance(record, dict):
            raise AnalyticsError(f"Dataset {dataset_id!r} has no version {number}.")
        return record

    def load_table(
        self, project_id: str, dataset_id: str, version: Optional[int] = None
    ) -> Tuple[Table, Dict[str, Any]]:
        record = self.version(project_id, dataset_id, version)
        key = (project_id, dataset_id, int(record["version"]))
        with self.store.lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached, record
        payload = self.store.read_json(
            self._version_dir(project_id, dataset_id, record["version"]) / "table.json"
        )
        if not isinstance(payload, dict):
            raise AnalyticsError(f"Dataset {dataset_id!r} v{record['version']} data is missing.")
        table = Table.from_payload(payload)
        if table.content_hash() != record["table_hash"]:
            raise AnalyticsError(f"Dataset {dataset_id!r} v{record['version']} failed its integrity check.")
        with self.store.lock:
            self._cache[key] = table
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return table, record

    def _commit(
        self,
        project_id: str,
        dataset_id: str,
        *,
        name: str,
        description: str,
        table: Table,
        warnings: List[str],
        parser: Dict[str, Any],
        provenance: Dict[str, Any],
        content_hash: str,
        original: Optional[Tuple[str, bytes]] = None,
    ) -> Dict[str, Any]:
        with self.store.lock:
            manifest_path = self._manifest_path(project_id, dataset_id)
            manifest = self.store.read_json(manifest_path)
            now = time.time()
            table_hash = table.content_hash()
            if isinstance(manifest, dict):
                latest = self.version(project_id, dataset_id)
                if latest["content_hash"] == content_hash and latest["table_hash"] == table_hash:
                    return {**latest, "reused": True}
                number = int(manifest["latest_version"]) + 1
            else:
                manifest = {
                    "dataset_id": dataset_id,
                    "project_id": project_id,
                    "name": name,
                    "description": description,
                    "created_at": now,
                    "versions": [],
                }
                number = 1
            version_dir = self._version_dir(project_id, dataset_id, number)
            if original is not None:
                self.store.write_bytes(version_dir / f"original{original[0]}", original[1])
            self.store.write_json(version_dir / "table.json", table.to_payload())
            profile = profile_table(table)
            record = {
                "dataset_id": dataset_id,
                "project_id": project_id,
                "name": manifest["name"],
                "version": number,
                "content_hash": content_hash,
                "table_hash": table_hash,
                "row_count": table.n_rows,
                "column_count": len(table.columns),
                "schema": [{"name": c, "dtype": table.dtypes[c]} for c in table.columns],
                "profile": profile,
                "warnings": list(warnings) + list(profile["warnings"]),
                "parser": parser,
                "provenance": provenance,
                "engine_version": ENGINE_VERSION,
                "created_at": now,
            }
            self.store.write_json(version_dir / "version.json", record)
            manifest["versions"].append(
                {
                    "version": number,
                    "content_hash": content_hash,
                    "row_count": table.n_rows,
                    "created_at": now,
                    "derived_from": provenance.get("derived_from"),
                }
            )
            manifest["latest_version"] = number
            manifest["updated_at"] = now
            if description and not manifest.get("description"):
                manifest["description"] = description
            self.store.write_json(manifest_path, manifest)
            return {**record, "reused": False}

    def ingest(
        self,
        project_id: str,
        *,
        name: str,
        filename: str,
        payload: bytes,
        fmt: Optional[str] = None,
        delimiter: Optional[str] = None,
        sheet: Optional[str] = None,
        column_types: Optional[Dict[str, str]] = None,
        source: str = "upload",
        description: str = "",
        dataset_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Register an upload; identical bytes reuse the latest version."""
        if not payload:
            raise AnalyticsError("Uploaded dataset is empty.")
        if len(payload) > MAX_DATASET_BYTES:
            raise AnalyticsError(f"Dataset exceeds {MAX_DATASET_BYTES // (1024 * 1024)} MB limit.")
        detected = detect_format(filename, fmt)
        table, warnings, parser = parse_dataset(
            payload, detected, delimiter=delimiter, sheet=sheet, column_types=column_types
        )
        display = (name or Path(filename or "").stem).strip()
        try:
            identifier = (
                safe_key(dataset_id, label="dataset_id") if dataset_id else slugify(display, fallback="dataset")
            )
        except StoreError as exc:
            raise AnalyticsError(str(exc)) from None
        return self._commit(
            project_id,
            identifier,
            name=display or identifier,
            description=description,
            table=table,
            warnings=warnings,
            parser={**parser, "format": detected, "column_types": dict(column_types or {})},
            provenance={
                "filename": Path(filename or "").name,
                "source": source,
                "uploaded_at": time.time(),
                "size_bytes": len(payload),
            },
            content_hash=sha256_bytes(payload),
            original=(Path(filename or "").suffix.lower() or f".{detected}", payload),
        )

    def transform(
        self,
        project_id: str,
        dataset_id: str,
        steps: Sequence[Dict[str, Any]],
        *,
        version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Apply validated steps and store the result as a new derived version."""
        parsed = validate_steps(steps)
        table, record = self.load_table(project_id, dataset_id, version)
        result, warnings, joined = apply_steps(
            table, parsed, load_other=lambda oid, over: self.load_table(project_id, oid, over)
        )
        step_payload = [step.model_dump(mode="json") for step in parsed]
        return self._commit(
            project_id,
            dataset_id,
            name=record["name"],
            description="",
            table=result,
            warnings=warnings,
            parser={"parser": "transform"},
            provenance={
                "source": "transform",
                "derived_from": {
                    "version": record["version"],
                    "content_hash": record["content_hash"],
                    "steps": step_payload,
                    "joined": joined,
                },
                "uploaded_at": time.time(),
            },
            content_hash=stable_hash(
                {"parent": record["content_hash"], "steps": step_payload, "joined": joined}, length=64
            ),
        )

    def preview(
        self, project_id: str, dataset_id: str, version: Optional[int] = None, limit: int = 50
    ) -> Dict[str, Any]:
        table, record = self.load_table(project_id, dataset_id, version)
        limit = max(1, min(int(limit), 500))
        return {
            "dataset_id": dataset_id,
            "version": record["version"],
            "columns": record["schema"],
            "rows": table.rows(limit),
            "total_rows": table.n_rows,
        }


# ---------------------------------------------------------------------------
# Operation catalog
# ---------------------------------------------------------------------------


Alternative = Literal["two-sided", "less", "greater"]


class DescribeParams(_Strict):
    columns: Optional[List[str]] = Field(default=None, max_length=MAX_COLUMNS)


class MissingParams(_Strict):
    columns: Optional[List[str]] = Field(default=None, max_length=MAX_COLUMNS)


class ValueCountsParams(_Strict):
    column: str
    limit: int = Field(default=20, ge=1, le=200)
    include_missing: bool = False


class Aggregation(_Strict):
    column: str
    func: Literal["count", "sum", "mean", "median", "min", "max", "std"]


class GroupAggregateParams(_Strict):
    group_by: List[str] = Field(min_length=1, max_length=3)
    aggregations: List[Aggregation] = Field(min_length=1, max_length=20)


class CorrelationParams(_Strict):
    columns: List[str] = Field(min_length=2, max_length=30)
    method: Literal["pearson", "spearman", "kendall"] = "pearson"


class ConfidenceIntervalParams(_Strict):
    column: str
    level: float = Field(default=0.95, gt=0.5, lt=1.0)


class TTestParams(_Strict):
    kind: Literal["one_sample", "independent", "paired"]
    column: str
    mu: float = 0.0
    group_column: Optional[str] = None
    groups: Optional[List[str]] = None
    column_b: Optional[str] = None
    equal_var: bool = False
    alternative: Alternative = "two-sided"
    level: float = Field(default=0.95, gt=0.5, lt=1.0)

    @field_validator("groups")
    @classmethod
    def _two_groups(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if value is not None and (len(value) != 2 or value[0] == value[1]):
            raise ValueError("groups must name exactly two different groups")
        return value


class MannWhitneyParams(_Strict):
    column: str
    group_column: str
    groups: List[str] = Field(min_length=2, max_length=2)
    alternative: Alternative = "two-sided"


class ChiSquareParams(_Strict):
    row_column: str
    col_column: str


class NormalityParams(_Strict):
    column: str


class RegressionParams(_Strict):
    y: str
    x: List[str] = Field(min_length=1, max_length=20)
    add_intercept: bool = True
    level: float = Field(default=0.95, gt=0.5, lt=1.0)


class AnovaParams(_Strict):
    column: str
    group_column: str


class TimeSeriesParams(_Strict):
    time_column: str
    value_column: str
    window: int = Field(default=3, ge=2, le=365)


OpResult = Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    params: Type[_Strict]
    fn: Callable[[Table, Any], OpResult]


def _table(name: str, columns: List[str], rows: List[List[Any]]) -> Dict[str, Any]:
    return {"name": name, "columns": columns, "rows": rows}


def _complete(array: np.ndarray, label: str, warnings: List[str]) -> np.ndarray:
    mask = ~np.isnan(array)
    dropped = int(array.size - mask.sum())
    if dropped:
        warnings.append(f"{dropped} missing value(s) in {label} were excluded.")
    return array[mask]


def _group_values(table: Table, column: str, group_column: str, group: str, warnings: List[str]) -> np.ndarray:
    values = table.numeric(column)
    groups = table.require(group_column)
    picked = np.array(
        [values[i] for i, g in enumerate(groups) if g is not None and _to_text(g) == str(group)],
        dtype=float,
    )
    if picked.size == 0:
        raise AnalyticsError(f"Group {group!r} has no rows in {group_column!r}.")
    return _complete(picked, f"{column} ({group_column}={group})", warnings)


def _op_describe(table: Table, p: DescribeParams) -> OpResult:
    warnings: List[str] = []
    columns = p.columns or [c for c in table.columns if table.dtypes[c] in NUMERIC_DTYPES]
    if not columns:
        raise AnalyticsError("No numeric columns to describe.")
    rows = []
    for col in columns:
        array = _complete(table.numeric(col), col, warnings)
        if array.size == 0:
            rows.append([col, 0] + [None] * 8)
            continue
        sd = float(np.std(array, ddof=1)) if array.size > 1 else None
        rows.append([
            col,
            int(array.size),
            float(np.mean(array)),
            sd,
            float(np.min(array)),
            float(np.percentile(array, 25)),
            float(np.median(array)),
            float(np.percentile(array, 75)),
            float(np.max(array)),
            sd / math.sqrt(array.size) if sd is not None else None,
        ])
    return (
        {"columns": columns, "std_ddof": 1, "quantile_method": "numpy-linear"},
        [_table("summary", ["column", "n", "mean", "std", "min", "q25", "median", "q75", "max", "sem"], rows)],
        warnings,
    )


def _op_missing(table: Table, p: MissingParams) -> OpResult:
    columns = p.columns or table.columns
    rows = []
    for col in columns:
        missing = sum(1 for v in table.require(col) if v is None)
        rows.append([col, table.dtypes[col], missing, (missing / table.n_rows * 100.0) if table.n_rows else 0.0])
    complete_rows = sum(1 for i in range(table.n_rows) if all(table.data[c][i] is not None for c in columns))
    return (
        {"row_count": table.n_rows, "complete_rows": complete_rows},
        [_table("missing", ["column", "dtype", "missing", "missing_pct"], rows)],
        [],
    )


def _op_value_counts(table: Table, p: ValueCountsParams) -> OpResult:
    values = table.require(p.column)
    counts = Counter(v for v in values if v is not None or p.include_missing)
    total = sum(counts.values())
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], _sort_key(kv[0])))
    rows = [[value, count, count / total * 100.0 if total else 0.0] for value, count in ordered[: p.limit]]
    return (
        {"distinct": len(counts), "total": total, "truncated": len(ordered) > p.limit},
        [_table("counts", [p.column, "count", "percent"], rows)],
        [],
    )


_AGG_FUNCS: Dict[str, Callable[[np.ndarray], Any]] = {
    "sum": np.sum,
    "mean": np.mean,
    "median": np.median,
    "min": np.min,
    "max": np.max,
    "std": lambda a: np.std(a, ddof=1),
}


def _op_group_aggregate(table: Table, p: GroupAggregateParams) -> OpResult:
    warnings: List[str] = []
    for col in p.group_by:
        table.require(col)
    for agg in p.aggregations:
        table.require(agg.column)
    arrays = {agg.column: table.numeric(agg.column) for agg in p.aggregations if agg.func != "count"}
    groups: Dict[str, List[int]] = {}
    keys: Dict[str, List[Any]] = {}
    for i in range(table.n_rows):
        key_values = [table.data[c][i] for c in p.group_by]
        key = canonical_json(key_values)
        groups.setdefault(key, []).append(i)
        keys[key] = key_values
    rows = []
    for key in sorted(groups, key=lambda k: tuple(_sort_key(v) for v in keys[k])):
        indices = groups[key]
        row: List[Any] = list(keys[key]) + [len(indices)]
        for agg in p.aggregations:
            if agg.func == "count":
                row.append(sum(1 for i in indices if table.data[agg.column][i] is not None))
                continue
            sample = arrays[agg.column][indices]
            sample = sample[~np.isnan(sample)]
            if sample.size == 0 or (agg.func == "std" and sample.size < 2):
                row.append(None)
            else:
                row.append(float(_AGG_FUNCS[agg.func](sample)))
        rows.append(row)
    if any(v is None for k in keys.values() for v in k):
        warnings.append("Missing group keys are reported as their own group.")
    columns = list(p.group_by) + ["rows"] + [f"{a.func}_{a.column}" for a in p.aggregations]
    return {"groups": len(rows)}, [_table("groups", columns, rows)], warnings


def _op_correlation(table: Table, p: CorrelationParams) -> OpResult:
    from scipy import stats

    warnings: List[str] = []
    if len(set(p.columns)) != len(p.columns):
        raise AnalyticsError("correlation columns must be distinct.")
    arrays = {col: table.numeric(col) for col in p.columns}
    size = len(p.columns)
    coef: List[List[Optional[float]]] = [[None] * size for _ in range(size)]
    pvals: List[List[Optional[float]]] = [[None] * size for _ in range(size)]
    pairs = []
    for i, a in enumerate(p.columns):
        coef[i][i] = 1.0
        pvals[i][i] = 0.0
        for j in range(i + 1, size):
            b = p.columns[j]
            mask = ~np.isnan(arrays[a]) & ~np.isnan(arrays[b])
            n = int(mask.sum())
            if n < 3:
                warnings.append(f"{a}~{b}: fewer than 3 complete pairs; not computed.")
                continue
            x, y = arrays[a][mask], arrays[b][mask]
            if np.ptp(x) == 0 or np.ptp(y) == 0:
                warnings.append(f"{a}~{b}: a column is constant; correlation undefined.")
                continue
            if p.method == "pearson":
                res = stats.pearsonr(x, y)
            elif p.method == "spearman":
                res = stats.spearmanr(x, y)
            else:
                res = stats.kendalltau(x, y)
            r, pv = float(res.statistic), float(res.pvalue)
            coef[i][j] = coef[j][i] = r
            pvals[i][j] = pvals[j][i] = pv
            pairs.append([a, b, n, r, pv])
    return (
        {"method": p.method, "missing": "pairwise-complete"},
        [
            _table("matrix", ["column"] + list(p.columns), [[p.columns[i]] + coef[i] for i in range(size)]),
            _table("p_values", ["column"] + list(p.columns), [[p.columns[i]] + pvals[i] for i in range(size)]),
            _table("pairs", ["a", "b", "n", "coefficient", "p_value"], pairs),
        ],
        warnings,
    )


def _op_confidence_interval(table: Table, p: ConfidenceIntervalParams) -> OpResult:
    from scipy import stats

    warnings: List[str] = []
    array = _complete(table.numeric(p.column), p.column, warnings)
    if array.size < 2:
        raise AnalyticsError("Need at least 2 values for a confidence interval.")
    if array.size < 30:
        warnings.append("n < 30: the t-interval assumes approximately normal data.")
    n = array.size
    mean = float(np.mean(array))
    sem = float(np.std(array, ddof=1) / math.sqrt(n))
    t_crit = float(stats.t.ppf((1 + p.level) / 2, n - 1))
    return (
        {"column": p.column, "n": int(n), "mean": mean, "sem": sem, "level": p.level,
         "lower": mean - t_crit * sem, "upper": mean + t_crit * sem, "method": "student-t"},
        [],
        warnings,
    )


def _op_t_test(table: Table, p: TTestParams) -> OpResult:
    from scipy import stats

    warnings: List[str] = ["t-tests assume approximately normal data; check with normality_test."]
    if p.kind == "one_sample":
        a = _complete(table.numeric(p.column), p.column, warnings)
        if a.size < 2:
            raise AnalyticsError("Need at least 2 values.")
        res = stats.ttest_1samp(a, p.mu, alternative=p.alternative)
        sd = float(np.std(a, ddof=1))
        effect = (float(np.mean(a)) - p.mu) / sd if sd else None
        details: Dict[str, Any] = {"n": int(a.size), "mean": float(np.mean(a)), "mu": p.mu}
    elif p.kind == "paired":
        if not p.column_b:
            raise AnalyticsError("paired t-test needs column_b.")
        a_all, b_all = table.numeric(p.column), table.numeric(p.column_b)
        mask = ~np.isnan(a_all) & ~np.isnan(b_all)
        if int((~mask).sum()):
            warnings.append(f"{int((~mask).sum())} incomplete pair(s) excluded.")
        a, b = a_all[mask], b_all[mask]
        if a.size < 2:
            raise AnalyticsError("Need at least 2 complete pairs.")
        res = stats.ttest_rel(a, b, alternative=p.alternative)
        diff = a - b
        sd = float(np.std(diff, ddof=1))
        effect = float(np.mean(diff)) / sd if sd else None
        details = {"n": int(a.size), "mean_difference": float(np.mean(diff)), "column_b": p.column_b}
    else:
        if not p.group_column or not p.groups:
            raise AnalyticsError("independent t-test needs group_column and two groups.")
        a = _group_values(table, p.column, p.group_column, p.groups[0], warnings)
        b = _group_values(table, p.column, p.group_column, p.groups[1], warnings)
        if a.size < 2 or b.size < 2:
            raise AnalyticsError("Each group needs at least 2 values.")
        res = stats.ttest_ind(a, b, equal_var=p.equal_var, alternative=p.alternative)
        pooled = math.sqrt(
            ((a.size - 1) * np.var(a, ddof=1) + (b.size - 1) * np.var(b, ddof=1)) / (a.size + b.size - 2)
        )
        effect = (float(np.mean(a)) - float(np.mean(b))) / pooled if pooled else None
        details = {
            "groups": list(p.groups),
            "n": [int(a.size), int(b.size)],
            "means": [float(np.mean(a)), float(np.mean(b))],
            "mean_difference": float(np.mean(a) - np.mean(b)),
            "variant": "student" if p.equal_var else "welch",
        }
    ci = res.confidence_interval(confidence_level=p.level)
    return (
        {
            "kind": p.kind,
            "statistic": float(res.statistic),
            "p_value": float(res.pvalue),
            "df": float(res.df),
            "alternative": p.alternative,
            "level": p.level,
            "ci_lower": float(ci.low),
            "ci_upper": float(ci.high),
            "cohens_d": effect,
            **details,
        },
        [],
        warnings,
    )


def _op_mann_whitney(table: Table, p: MannWhitneyParams) -> OpResult:
    from scipy import stats

    warnings: List[str] = []
    a = _group_values(table, p.column, p.group_column, p.groups[0], warnings)
    b = _group_values(table, p.column, p.group_column, p.groups[1], warnings)
    res = stats.mannwhitneyu(a, b, alternative=p.alternative)
    return (
        {"statistic": float(res.statistic), "p_value": float(res.pvalue), "n": [int(a.size), int(b.size)],
         "groups": list(p.groups), "alternative": p.alternative,
         "medians": [float(np.median(a)), float(np.median(b))]},
        [],
        warnings,
    )


def _op_chi_square(table: Table, p: ChiSquareParams) -> OpResult:
    from scipy import stats

    warnings: List[str] = []
    rows_raw = table.require(p.row_column)
    cols_raw = table.require(p.col_column)
    pairs = [(r, c) for r, c in zip(rows_raw, cols_raw) if r is not None and c is not None]
    if len(pairs) < len(rows_raw):
        warnings.append(f"{len(rows_raw) - len(pairs)} row(s) with missing categories excluded.")
    row_levels = sorted({r for r, _ in pairs}, key=_sort_key)
    col_levels = sorted({c for _, c in pairs}, key=_sort_key)
    if len(row_levels) < 2 or len(col_levels) < 2:
        raise AnalyticsError("Both columns need at least 2 categories.")
    if len(row_levels) * len(col_levels) > 2500:
        raise AnalyticsError("Contingency table is too large (max 2500 cells).")
    counts = Counter(pairs)
    observed = np.array([[counts[(r, c)] for c in col_levels] for r in row_levels], dtype=float)
    res = stats.chi2_contingency(observed)
    if (res.expected_freq < 5).any():
        warnings.append("Some expected counts are < 5; the chi-square approximation may be unreliable.")
    n = observed.sum()
    cramers_v = math.sqrt(res.statistic / (n * (min(observed.shape) - 1))) if n else None
    return (
        {"statistic": float(res.statistic), "p_value": float(res.pvalue), "df": int(res.dof),
         "n": int(n), "cramers_v": cramers_v, "yates_correction": bool(res.dof == 1)},
        [_table("observed", [p.row_column] + [str(c) for c in col_levels],
                [[r] + [int(v) for v in observed[i]] for i, r in enumerate(row_levels)])],
        warnings,
    )


def _op_normality(table: Table, p: NormalityParams) -> OpResult:
    from scipy import stats

    warnings: List[str] = []
    a = _complete(table.numeric(p.column), p.column, warnings)
    if a.size < 3:
        raise AnalyticsError("Shapiro-Wilk needs at least 3 values.")
    if a.size > 5000:
        warnings.append("n > 5000: Shapiro-Wilk p-values may be inaccurate.")
    res = stats.shapiro(a)
    return (
        {"test": "shapiro-wilk", "statistic": float(res.statistic), "p_value": float(res.pvalue),
         "n": int(a.size), "skewness": float(stats.skew(a)), "excess_kurtosis": float(stats.kurtosis(a))},
        [],
        warnings,
    )


def _op_regression(table: Table, p: RegressionParams) -> OpResult:
    from scipy import stats

    warnings: List[str] = []
    if p.y in p.x:
        raise AnalyticsError("y must not also be a predictor.")
    if len(set(p.x)) != len(p.x):
        raise AnalyticsError("Predictors must be distinct.")
    matrix = np.column_stack([table.numeric(p.y)] + [table.numeric(col) for col in p.x])
    mask = ~np.isnan(matrix).any(axis=1)
    dropped = int((~mask).sum())
    if dropped:
        warnings.append(f"{dropped} row(s) with missing values excluded (listwise).")
    data = matrix[mask]
    y = data[:, 0]
    X = data[:, 1:]
    names = list(p.x)
    if p.add_intercept:
        X = np.column_stack([np.ones(len(y)), X])
        names = ["intercept"] + names
    n, k = X.shape
    if n <= k:
        raise AnalyticsError(f"Need more rows ({n}) than parameters ({k}).")
    if np.linalg.matrix_rank(X) < k:
        raise AnalyticsError("Predictors are perfectly collinear; remove redundant columns.")
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    df_resid = n - k
    ssr = float(resid @ resid)
    sigma2 = ssr / df_resid
    se = np.sqrt(np.diag(sigma2 * np.linalg.inv(X.T @ X)))
    with np.errstate(divide="ignore", invalid="ignore"):
        t_vals = beta / se
    p_vals = 2 * stats.t.sf(np.abs(t_vals), df_resid)
    t_crit = stats.t.ppf((1 + p.level) / 2, df_resid)
    sst = float(((y - y.mean()) ** 2).sum()) if p.add_intercept else float((y ** 2).sum())
    r2 = 1 - ssr / sst if sst else None
    df_model = k - 1 if p.add_intercept else k
    adj_r2 = (
        1 - (1 - r2) * (n - (1 if p.add_intercept else 0)) / df_resid if r2 is not None else None
    )
    if df_model > 0 and r2 is not None and r2 < 1:
        f_stat: Optional[float] = (r2 / df_model) / ((1 - r2) / df_resid)
        f_p: Optional[float] = float(stats.f.sf(f_stat, df_model, df_resid))
    else:
        f_stat, f_p = None, None
    if n < 10 * k:
        warnings.append("Fewer than 10 observations per parameter; estimates may be unstable.")
    rows = [
        [names[i], float(beta[i]), float(se[i]), float(t_vals[i]), float(p_vals[i]),
         float(beta[i] - t_crit * se[i]), float(beta[i] + t_crit * se[i])]
        for i in range(k)
    ]
    return (
        {"y": p.y, "x": list(p.x), "n": int(n), "df_resid": int(df_resid), "r_squared": r2,
         "adj_r_squared": adj_r2, "f_statistic": f_stat, "f_p_value": f_p,
         "residual_std_error": math.sqrt(sigma2), "level": p.level, "method": "ols"},
        [_table("coefficients", ["term", "estimate", "std_error", "t", "p_value", "ci_lower", "ci_upper"], rows)],
        warnings,
    )


def _op_anova(table: Table, p: AnovaParams) -> OpResult:
    from scipy import stats

    warnings: List[str] = ["One-way ANOVA assumes normal residuals and equal variances."]
    values = table.numeric(p.column)
    groups_raw = table.require(p.group_column)
    buckets: Dict[str, List[float]] = {}
    skipped = 0
    for value, group in zip(values, groups_raw):
        if group is None or np.isnan(value):
            skipped += 1
            continue
        buckets.setdefault(_to_text(group) or "", []).append(float(value))
    if skipped:
        warnings.append(f"{skipped} row(s) with missing values excluded.")
    labels = sorted(buckets)
    if len(labels) < 2:
        raise AnalyticsError("ANOVA needs at least 2 groups.")
    if len(labels) > 100:
        raise AnalyticsError("ANOVA supports at most 100 groups.")
    if any(len(buckets[label]) < 2 for label in labels):
        raise AnalyticsError("Each group needs at least 2 values.")
    arrays = [np.array(buckets[label]) for label in labels]
    res = stats.f_oneway(*arrays)
    everything = np.concatenate(arrays)
    grand = everything.mean()
    ss_between = float(sum(a.size * (a.mean() - grand) ** 2 for a in arrays))
    ss_total = float(((everything - grand) ** 2).sum())
    levene = stats.levene(*arrays)
    if levene.pvalue < 0.05:
        warnings.append("Levene's test suggests unequal variances (p < 0.05).")
    return (
        {"statistic": float(res.statistic), "p_value": float(res.pvalue),
         "df_between": len(labels) - 1, "df_within": int(everything.size - len(labels)),
         "eta_squared": ss_between / ss_total if ss_total else None,
         "levene_p_value": float(levene.pvalue)},
        [_table("groups", [p.group_column, "n", "mean", "std"],
                [[label, len(buckets[label]), float(np.mean(arrays[i])), float(np.std(arrays[i], ddof=1))]
                 for i, label in enumerate(labels)])],
        warnings,
    )


def _time_value(value: Any, dtype: str) -> float:
    if dtype == "datetime":
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp() / 86400.0
    return float(value)


def _op_time_series(table: Table, p: TimeSeriesParams) -> OpResult:
    warnings: List[str] = []
    table.require(p.time_column)
    time_dtype = table.dtypes[p.time_column]
    if time_dtype not in ("datetime", "integer", "number"):
        raise AnalyticsError(f"time_column {p.time_column!r} must be datetime or numeric.")
    values = table.numeric(p.value_column)
    points = [
        (_time_value(t, time_dtype), t, float(v))
        for t, v in zip(table.data[p.time_column], values)
        if t is not None and not np.isnan(v)
    ]
    if len(points) < table.n_rows:
        warnings.append(f"{table.n_rows - len(points)} row(s) with missing values excluded.")
    points.sort(key=lambda item: item[0])
    if len(points) < 3:
        raise AnalyticsError("Need at least 3 observations.")
    times = np.array([pt[0] for pt in points])
    series = np.array([pt[2] for pt in points])
    if len(set(times.tolist())) < len(times):
        warnings.append("Duplicate time stamps present; values are not aggregated.")
    if np.ptp(times) == 0:
        raise AnalyticsError("All observations share one time stamp.")
    slope, _intercept = np.polyfit(times, series, 1)
    centered = series - series.mean()
    denom = float((centered ** 2).sum())
    lag1 = float((centered[:-1] * centered[1:]).sum() / denom) if denom else None
    rolling: List[Optional[float]] = []
    for i in range(len(series)):
        window = series[max(0, i - p.window + 1): i + 1]
        rolling.append(float(window.mean()) if window.size == p.window else None)
    first, last = float(series[0]), float(series[-1])
    return (
        {"n": len(points), "start": points[0][1], "end": points[-1][1], "first": first, "last": last,
         "change": last - first, "pct_change": (last - first) / abs(first) * 100.0 if first else None,
         "mean": float(series.mean()), "trend_slope": float(slope),
         "trend_unit": "per day" if time_dtype == "datetime" else f"per unit of {p.time_column}",
         "lag1_autocorrelation": lag1, "window": p.window},
        [_table("series", [p.time_column, p.value_column, f"rolling_mean_{p.window}"],
                [[pt[1], pt[2], rolling[i]] for i, pt in enumerate(points)])],
        warnings,
    )


OPERATIONS: Dict[str, Operation] = {
    op.name: op
    for op in (
        Operation("describe", "Count, mean, std (ddof=1), quartiles and SEM for numeric columns.", DescribeParams, _op_describe),
        Operation("missing_summary", "Missing-value counts per column.", MissingParams, _op_missing),
        Operation("value_counts", "Frequency table for one column.", ValueCountsParams, _op_value_counts),
        Operation("group_aggregate", "Group rows and aggregate numeric columns.", GroupAggregateParams, _op_group_aggregate),
        Operation("correlation", "Pairwise Pearson/Spearman/Kendall correlations with p-values.", CorrelationParams, _op_correlation),
        Operation("confidence_interval", "Student-t confidence interval for a mean.", ConfidenceIntervalParams, _op_confidence_interval),
        Operation("t_test", "One-sample, independent (Welch/Student) or paired t-test.", TTestParams, _op_t_test),
        Operation("mann_whitney", "Mann-Whitney U test between two groups.", MannWhitneyParams, _op_mann_whitney),
        Operation("chi_square", "Chi-square test of independence for two categorical columns.", ChiSquareParams, _op_chi_square),
        Operation("normality_test", "Shapiro-Wilk normality test with skewness/kurtosis.", NormalityParams, _op_normality),
        Operation("linear_regression", "Ordinary least squares with SEs, t, p and CIs.", RegressionParams, _op_regression),
        Operation("anova_oneway", "One-way ANOVA with eta-squared and Levene's test.", AnovaParams, _op_anova),
        Operation("time_series_summary", "Trend, change, lag-1 autocorrelation and rolling mean.", TimeSeriesParams, _op_time_series),
    )
}


def operation_catalog() -> List[Dict[str, Any]]:
    return [
        {"name": op.name, "description": op.description, "params_schema": op.params.model_json_schema()}
        for op in OPERATIONS.values()
    ]


def validate_request(operation: str, params: Optional[Dict[str, Any]]) -> Tuple[Operation, _Strict]:
    """Validate a (model- or user-) proposed operation before anything executes."""
    if operation not in OPERATIONS:
        raise AnalyticsError(f"Unknown operation {operation!r}. Allowed: {', '.join(OPERATIONS)}.")
    if params is not None and not isinstance(params, dict):
        raise AnalyticsError("params must be an object.")
    op = OPERATIONS[operation]
    try:
        return op, op.params.model_validate(params or {})
    except ValidationError as exc:
        raise AnalyticsError(f"Invalid params for {operation}: {_validation_message(exc)}") from None


def environment_info() -> Dict[str, str]:
    import scipy

    return {
        "engine": ENGINE_VERSION,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
    }


class AnalysisEngine:
    """Run catalog operations on stored dataset versions and record the results."""

    def __init__(self, registry: DatasetRegistry) -> None:
        self.registry = registry
        self.store = registry.store

    def _dir(self, project_id: str) -> Path:
        return self.store.kind_dir(project_id, "analyses")

    def run(
        self,
        project_id: str,
        dataset_id: str,
        operation: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        version: Optional[int] = None,
        requested_by: str = "user",
    ) -> Dict[str, Any]:
        op, validated = validate_request(operation, params)
        table, record = self.registry.load_table(project_id, dataset_id, version)
        normalized = validated.model_dump(mode="json")
        identity = {
            "project_id": project_id,
            "dataset_id": dataset_id,
            "version": record["version"],
            "content_hash": record["content_hash"],
            "operation": operation,
            "params": normalized,
            "engine": ENGINE_VERSION,
        }
        analysis_id = "an-" + stable_hash(identity)
        try:
            values, tables, warnings = op.fn(table, validated)
        except AnalyticsError:
            raise
        except (ValueError, ArithmeticError, np.linalg.LinAlgError) as exc:
            raise AnalyticsError(f"{operation} could not be computed: {exc}") from None
        result = {
            "analysis_id": analysis_id,
            "project_id": project_id,
            "dataset": {
                "dataset_id": dataset_id,
                "version": record["version"],
                "content_hash": record["content_hash"],
            },
            "operation": operation,
            "params": normalized,
            "values": _clean(values),
            "tables": _clean(tables),
            "warnings": warnings,
            "status": "ok",
            "requested_by": requested_by,
            "environment": environment_info(),
            "created_at": time.time(),
        }
        self.store.write_json(self._dir(project_id) / f"{analysis_id}.json", result)
        return result

    def get(self, project_id: str, analysis_id: str) -> Dict[str, Any]:
        try:
            key = safe_key(analysis_id, label="analysis_id")
        except StoreError as exc:
            raise AnalyticsError(str(exc)) from None
        record = self.store.read_json(self._dir(project_id) / f"{key}.json")
        if not isinstance(record, dict):
            raise AnalyticsError(f"Analysis {analysis_id!r} not found.")
        return record

    def list(self, project_id: str, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        records = [
            record for record in self.store.iter_json(self._dir(project_id))
            if dataset_id is None or record.get("dataset", {}).get("dataset_id") == dataset_id
        ]
        records.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return records


__all__ = [
    "AnalysisEngine",
    "AnalyticsError",
    "DatasetRegistry",
    "ENGINE_VERSION",
    "OPERATIONS",
    "TRANSFORM_OPS",
    "Table",
    "operation_catalog",
    "parse_dataset",
    "profile_table",
    "validate_request",
    "validate_steps",
]
