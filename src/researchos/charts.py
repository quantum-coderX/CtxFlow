"""Chart rendering from explicit, validated chart specifications.

The engine (not a model) renders every figure. A chart records the exact
dataset version or analysis it visualizes, the validated spec, the data that
was plotted, and deterministic PNG/SVG outputs.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import numpy as np
from pydantic import Field, ValidationError

from researchos.analytics import (
    NUMERIC_DTYPES,
    AnalysisEngine,
    AnalyticsError,
    DatasetRegistry,
    Table,
    _Strict,
    _clean,
    _sort_key,
    _time_value,
    _validation_message,
)
from researchos.project_store import ProjectStore, StoreError, safe_key, stable_hash

CHART_ENGINE_VERSION = "charts-1.0"
MAX_POINTS = 50_000
MAX_SERIES = 20
MAX_CATEGORIES = 100
CHART_FORMATS = ("png", "svg")


class ChartError(AnalyticsError):
    """A chart spec is invalid or cannot be rendered."""


class ChartSpec(_Strict):
    chart_type: Literal["line", "bar", "scatter", "histogram", "box", "heatmap"]
    dataset_id: Optional[str] = None
    version: Optional[int] = Field(default=None, ge=1)
    analysis_id: Optional[str] = None
    x: Optional[str] = None
    y: Optional[str] = None
    series: Optional[str] = None
    aggregate: Literal["none", "mean", "sum", "median", "count"] = "none"
    bins: int = Field(default=20, ge=1, le=200)
    title: str = Field(default="", max_length=200)
    x_label: str = Field(default="", max_length=120)
    y_label: str = Field(default="", max_length=120)
    log_y: bool = False
    width: float = Field(default=7.0, ge=3.0, le=16.0)
    height: float = Field(default=4.5, ge=2.0, le=12.0)


def validate_chart_spec(spec: Dict[str, Any]) -> ChartSpec:
    if not isinstance(spec, dict):
        raise ChartError("Chart spec must be an object.")
    try:
        parsed = ChartSpec.model_validate(spec)
    except ValidationError as exc:
        raise ChartError(f"Invalid chart spec: {_validation_message(exc)}") from None
    kind = parsed.chart_type
    if kind == "heatmap":
        if not parsed.analysis_id:
            raise ChartError("heatmap charts render a correlation analysis; set analysis_id.")
    else:
        if not parsed.dataset_id:
            raise ChartError(f"{kind} charts need dataset_id.")
        if kind in ("line", "scatter") and (not parsed.x or not parsed.y):
            raise ChartError(f"{kind} charts need x and y.")
        if kind == "bar":
            if not parsed.x:
                raise ChartError("bar charts need x (category column).")
            if parsed.aggregate == "none":
                raise ChartError("bar charts need aggregate (mean, sum, median or count).")
            if parsed.aggregate != "count" and not parsed.y:
                raise ChartError("bar charts need y unless aggregate is count.")
        if kind == "histogram" and not parsed.x:
            raise ChartError("histogram charts need x.")
        if kind == "box" and not parsed.y:
            raise ChartError("box charts need y; x optionally groups the boxes.")
        if kind in ("scatter", "histogram", "box") and parsed.aggregate != "none":
            raise ChartError(f"{kind} charts do not aggregate; set aggregate to none.")
    return parsed


def _axis_values(table: Table, column: str) -> Tuple[List[Any], List[float], str]:
    """Return raw values, numeric plotting positions and axis kind for one column."""
    raw = table.require(column)
    dtype = table.dtypes[column]
    if dtype in NUMERIC_DTYPES:
        return raw, [np.nan if v is None else float(v) for v in raw], "numeric"
    if dtype == "datetime":
        return raw, [np.nan if v is None else _time_value(v, dtype) for v in raw], "datetime"
    raise ChartError(f"Column {column!r} ({dtype}) cannot be used as a continuous axis.")


def _series_groups(table: Table, series: Optional[str]) -> List[Tuple[str, List[int]]]:
    if not series:
        return [("all", list(range(table.n_rows)))]
    values = table.require(series)
    groups: Dict[str, List[int]] = {}
    for index, value in enumerate(values):
        label = "(missing)" if value is None else str(value)
        groups.setdefault(label, []).append(index)
    if len(groups) > MAX_SERIES:
        raise ChartError(f"series column {series!r} has more than {MAX_SERIES} groups.")
    return sorted(groups.items(), key=lambda item: item[0])


def _aggregate(values: np.ndarray, how: str) -> float:
    if how == "count":
        return float(values.size)
    if values.size == 0:
        return float("nan")
    return float({"mean": np.mean, "sum": np.sum, "median": np.median}[how](values))


def _prepare(spec: ChartSpec, table: Optional[Table], analysis: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """Compute exactly the data that will be drawn (no hidden transformations)."""
    warnings: List[str] = []
    kind = spec.chart_type
    if kind == "heatmap":
        assert analysis is not None
        if analysis.get("operation") != "correlation":
            raise ChartError("heatmap analysis_id must reference a correlation analysis.")
        matrix = next((t for t in analysis["tables"] if t["name"] == "matrix"), None)
        if matrix is None:
            raise ChartError("Correlation analysis has no matrix table.")
        labels = matrix["columns"][1:]
        values = [row[1:] for row in matrix["rows"]]
        return {"kind": "matrix", "labels": labels, "values": values, "method": analysis["values"].get("method")}, warnings

    assert table is not None
    if table.n_rows > MAX_POINTS and kind in ("line", "scatter"):
        raise ChartError(f"Dataset has more than {MAX_POINTS} rows; filter or aggregate before plotting.")

    if kind in ("line", "scatter"):
        x_raw, x_num, x_kind = _axis_values(table, spec.x or "")
        y = table.numeric(spec.y or "")
        series_out = []
        for label, indices in _series_groups(table, spec.series):
            points = [(x_num[i], x_raw[i], float(y[i])) for i in indices if not np.isnan(x_num[i]) and not np.isnan(y[i])]
            dropped = len(indices) - len(points)
            if dropped:
                warnings.append(f"{dropped} point(s) with missing x/y omitted from series {label!r}.")
            if kind == "line":
                points.sort(key=lambda p: p[0])
                if spec.aggregate != "none":
                    buckets: Dict[float, List[Tuple[Any, float]]] = {}
                    for xn, xr, yv in points:
                        buckets.setdefault(xn, []).append((xr, yv))
                    points = [
                        (xn, buckets[xn][0][0], _aggregate(np.array([v for _, v in buckets[xn]]), spec.aggregate))
                        for xn in sorted(buckets)
                    ]
                elif len({p[0] for p in points}) < len(points):
                    warnings.append(f"Series {label!r} has repeated x values; consider aggregate.")
            series_out.append({"name": label, "x": [p[1] for p in points], "x_pos": [p[0] for p in points], "y": [p[2] for p in points]})
        return {"kind": "xy", "x_kind": x_kind, "series": series_out}, warnings

    if kind == "bar":
        categories_raw = table.require(spec.x or "")
        y = table.numeric(spec.y) if spec.y else None
        series_out = []
        all_labels: List[Any] = sorted({v for v in categories_raw if v is not None}, key=_sort_key)
        if len(all_labels) > MAX_CATEGORIES:
            raise ChartError(f"x has more than {MAX_CATEGORIES} categories; filter first.")
        if any(v is None for v in categories_raw):
            warnings.append("Rows with a missing category were omitted.")
        for label, indices in _series_groups(table, spec.series):
            heights = []
            for category in all_labels:
                rows = [i for i in indices if categories_raw[i] == category]
                if y is None:
                    heights.append(float(len(rows)))
                else:
                    sample = y[rows]
                    sample = sample[~np.isnan(sample)]
                    heights.append(_aggregate(sample, spec.aggregate))
            series_out.append({"name": label, "categories": [str(c) for c in all_labels], "values": heights})
        return {"kind": "bar", "aggregate": spec.aggregate, "series": series_out}, warnings

    if kind == "histogram":
        values = table.numeric(spec.x or "")
        clean = values[~np.isnan(values)]
        if clean.size == 0:
            raise ChartError("histogram column has no values.")
        if clean.size < values.size:
            warnings.append(f"{values.size - clean.size} missing value(s) omitted.")
        counts, edges = np.histogram(clean, bins=spec.bins)
        return {"kind": "histogram", "edges": edges.tolist(), "counts": counts.tolist(), "n": int(clean.size)}, warnings

    # box
    values = table.numeric(spec.y or "")
    groups = _series_groups(table, spec.x) if spec.x else [("all", list(range(table.n_rows)))]
    if spec.x and len(groups) > MAX_CATEGORIES:
        raise ChartError(f"x has more than {MAX_CATEGORIES} groups.")
    boxes = []
    for label, indices in groups:
        sample = values[indices]
        sample = sample[~np.isnan(sample)]
        if sample.size == 0:
            warnings.append(f"Group {label!r} has no values and is omitted.")
            continue
        boxes.append({
            "name": label,
            "n": int(sample.size),
            "min": float(sample.min()),
            "q1": float(np.percentile(sample, 25)),
            "median": float(np.median(sample)),
            "q3": float(np.percentile(sample, 75)),
            "max": float(sample.max()),
            "values": sample.tolist(),
        })
    if not boxes:
        raise ChartError("No values to plot.")
    return {"kind": "box", "boxes": boxes, "whis": 1.5}, warnings


def _render(spec: ChartSpec, data: Dict[str, Any]) -> Dict[str, bytes]:
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    with matplotlib.rc_context({"svg.hashsalt": "researchos-chart", "font.size": 10}):
        fig = Figure(figsize=(spec.width, spec.height), dpi=100)
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(1, 1, 1)
        kind = data["kind"]
        if kind == "xy":
            for series in data["series"]:
                label = None if series["name"] == "all" else series["name"]
                if spec.chart_type == "line":
                    ax.plot(series["x_pos"], series["y"], marker="o", markersize=3, linewidth=1.5, label=label)
                else:
                    ax.scatter(series["x_pos"], series["y"], s=14, alpha=0.8, label=label)
            if data["x_kind"] == "datetime":
                from datetime import datetime, timezone

                from matplotlib.ticker import FuncFormatter

                # x positions are days since the Unix epoch (see _time_value).
                ax.xaxis.set_major_formatter(
                    FuncFormatter(
                        lambda value, _pos: datetime.fromtimestamp(value * 86400.0, tz=timezone.utc).strftime("%Y-%m-%d")
                    )
                )
                fig.autofmt_xdate()
            if len(data["series"]) > 1:
                ax.legend(fontsize=8)
        elif kind == "bar":
            count = len(data["series"])
            width = 0.8 / count
            positions = np.arange(len(data["series"][0]["categories"]))
            for offset, series in enumerate(data["series"]):
                heights = [0.0 if v is None or np.isnan(v) else v for v in series["values"]]
                ax.bar(positions + offset * width - 0.4 + width / 2, heights, width=width,
                       label=None if series["name"] == "all" else series["name"])
            ax.set_xticks(positions)
            ax.set_xticklabels(data["series"][0]["categories"], rotation=30 if len(positions) > 6 else 0, ha="right" if len(positions) > 6 else "center")
            if not spec.log_y:
                ax.set_ylim(bottom=min(0.0, ax.get_ylim()[0]))
            if count > 1:
                ax.legend(fontsize=8)
        elif kind == "histogram":
            edges = np.array(data["edges"])
            ax.stairs(data["counts"], edges, fill=True, alpha=0.85)
            ax.set_ylabel("count")
        elif kind == "box":
            ax.boxplot([box["values"] for box in data["boxes"]], whis=data["whis"])
            ax.set_xticks(range(1, len(data["boxes"]) + 1))
            ax.set_xticklabels([box["name"] for box in data["boxes"]])
        elif kind == "matrix":
            matrix = np.array([[np.nan if v is None else v for v in row] for row in data["values"]], dtype=float)
            image = ax.imshow(matrix, vmin=-1, vmax=1, cmap="RdBu_r")
            ax.set_xticks(range(len(data["labels"])))
            ax.set_yticks(range(len(data["labels"])))
            ax.set_xticklabels(data["labels"], rotation=45, ha="right")
            ax.set_yticklabels(data["labels"])
            for i in range(matrix.shape[0]):
                for j in range(matrix.shape[1]):
                    if not np.isnan(matrix[i, j]):
                        ax.text(j, i, f"{matrix[i, j]:.2f}", ha="center", va="center", fontsize=8)
            fig.colorbar(image, ax=ax)
        if spec.log_y and kind in ("xy", "bar", "histogram"):
            ax.set_yscale("log")
        ax.set_title(spec.title)
        if spec.x_label or (spec.x and kind != "matrix"):
            ax.set_xlabel(spec.x_label or spec.x or "")
        if spec.y_label or (spec.y and kind != "matrix"):
            ax.set_ylabel(spec.y_label or spec.y or "")
        if kind in ("xy", "bar", "histogram"):
            ax.grid(True, alpha=0.3)
        fig.tight_layout()
        outputs: Dict[str, bytes] = {}
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", dpi=150, metadata={"Software": None})
        outputs["png"] = buffer.getvalue()
        buffer = io.BytesIO()
        fig.savefig(buffer, format="svg", metadata={"Date": None, "Creator": None})
        outputs["svg"] = buffer.getvalue()
    return outputs


def _strip_positions(data: Dict[str, Any]) -> Dict[str, Any]:
    if data.get("kind") != "xy":
        return data
    return {**data, "series": [{k: v for k, v in s.items() if k != "x_pos"} for s in data["series"]]}


class ChartEngine:
    """Validate, render and record charts for a project."""

    def __init__(self, registry: DatasetRegistry, analyses: AnalysisEngine) -> None:
        self.registry = registry
        self.analyses = analyses
        self.store: ProjectStore = registry.store

    def _dir(self, project_id: str, chart_id: Optional[str] = None) -> Path:
        if chart_id is None:
            return self.store.kind_dir(project_id, "charts")
        try:
            return self.store.kind_dir(project_id, "charts", safe_key(chart_id, label="chart_id"))
        except StoreError as exc:
            raise ChartError(str(exc)) from None

    def create(self, project_id: str, spec: Dict[str, Any], *, requested_by: str = "user") -> Dict[str, Any]:
        parsed = validate_chart_spec(spec)
        table: Optional[Table] = None
        analysis: Optional[Dict[str, Any]] = None
        dataset_ref: Dict[str, Any]
        if parsed.analysis_id:
            analysis = self.analyses.get(project_id, parsed.analysis_id)
            dataset_ref = dict(analysis["dataset"])
        if parsed.chart_type != "heatmap":
            table, record = self.registry.load_table(project_id, parsed.dataset_id or "", parsed.version)
            dataset_ref = {
                "dataset_id": record["dataset_id"],
                "version": record["version"],
                "content_hash": record["content_hash"],
            }
        normalized = parsed.model_dump(mode="json")
        normalized["version"] = dataset_ref["version"]
        if parsed.chart_type == "heatmap":
            normalized["dataset_id"] = dataset_ref["dataset_id"]
        data, warnings = _prepare(parsed, table, analysis)
        try:
            files = _render(parsed, data)
        except ChartError:
            raise
        except Exception as exc:  # noqa: BLE001 - renderer failures must be visible
            raise ChartError(f"Chart rendering failed: {type(exc).__name__}: {exc}") from None
        chart_id = "ch-" + stable_hash(
            {"project_id": project_id, "spec": normalized, "dataset": dataset_ref, "engine": CHART_ENGINE_VERSION}
        )
        directory = self._dir(project_id, chart_id)
        for fmt, payload in files.items():
            self.store.write_bytes(directory / f"chart.{fmt}", payload)
        import matplotlib

        record = {
            "chart_id": chart_id,
            "project_id": project_id,
            "spec": normalized,
            "dataset": dataset_ref,
            "analysis_id": parsed.analysis_id,
            "data_used": _clean(_strip_positions(data)),
            "warnings": warnings,
            "formats": sorted(files),
            "status": "ok",
            "requested_by": requested_by,
            "environment": {"engine": CHART_ENGINE_VERSION, "matplotlib": matplotlib.__version__},
            "created_at": time.time(),
        }
        self.store.write_json(directory / "chart.json", record)
        return record

    def get(self, project_id: str, chart_id: str) -> Dict[str, Any]:
        record = self.store.read_json(self._dir(project_id, chart_id) / "chart.json")
        if not isinstance(record, dict):
            raise ChartError(f"Chart {chart_id!r} not found.")
        return record

    def file_path(self, project_id: str, chart_id: str, fmt: str = "png") -> Path:
        if fmt not in CHART_FORMATS:
            raise ChartError(f"format must be one of {CHART_FORMATS}.")
        path = self._dir(project_id, chart_id) / f"chart.{fmt}"
        if not path.exists():
            raise ChartError(f"Chart {chart_id!r} has no {fmt} output.")
        return path

    def list(self, project_id: str, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        records = []
        for directory in self.store.list_subdirs(self._dir(project_id)):
            record = self.store.read_json(directory / "chart.json")
            if isinstance(record, dict) and (
                dataset_id is None or record.get("dataset", {}).get("dataset_id") == dataset_id
            ):
                records.append({k: v for k, v in record.items() if k != "data_used"})
        records.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return records
