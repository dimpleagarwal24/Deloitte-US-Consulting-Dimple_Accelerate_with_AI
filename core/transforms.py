import math
from pathlib import Path

import pandas as pd

from core.audit import AuditLogger


def cast_column(values: pd.Series, kind: str) -> pd.Series:
    if kind == "str":
        return values.astype("string")
    if kind == "datetime":
        if pd.api.types.is_datetime64_any_dtype(values):
            return values.copy()
        text = values.astype("string").str.strip()
        result = pd.Series(pd.NaT, index=values.index, dtype="datetime64[ns]")
        for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
            parsed = pd.to_datetime(text, format=date_format, errors="coerce")
            result = result.fillna(parsed)
    elif kind in {"float", "int"}:
        result = pd.to_numeric(values, errors="coerce")
        if not result.dropna().map(math.isfinite).all():
            raise ValueError(f"Non-finite numeric values in {values.name}")
        if kind == "int" and (result.dropna() % 1 != 0).any():
            raise ValueError(f"Fractional values cannot be cast to int: {values.name}")
        result = result.astype("Int64" if kind == "int" else "Float64")
    else:
        raise ValueError(f"Unsupported cast: {kind}")
    if (values.notna() & result.isna()).any():
        raise ValueError(f"Casting {values.name} to {kind} would lose non-null values; revise the mapping.")
    return result


def require_columns(frame: pd.DataFrame, columns: list[str]) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"Mapping references missing columns: {sorted(missing)}")


def unique_reference(frame: pd.DataFrame, key: str) -> None:
    require_columns(frame, [key])
    if frame[key].isna().any() or frame[key].duplicated().any():
        raise ValueError(f"Reference key {key} must be non-null and unique")


def match_sources(mapping: pd.DataFrame, tables: dict[str, pd.DataFrame], layer: str) -> pd.DataFrame:
    aliases = {name.removesuffix(f"_{layer}"): name for name in tables}
    result = mapping.copy()
    result["source_table"] = result.source_table.map(lambda name: name if name in tables else aliases.get(name, name))
    if set(result.source_table) != set(tables):
        raise ValueError("Mapping must cover exactly the input tables")
    return result


def save_outputs(outputs: dict[str, pd.DataFrame], directory: Path, layer: str, run_id: str, inputs: dict[str, pd.DataFrame]) -> list[str]:
    paths = [directory / f"{name}_{run_id}.parquet" for name in outputs]
    if any(path.exists() for path in paths):
        raise ValueError("Refusing to overwrite existing run artifacts")
    audit = AuditLogger(run_id)
    for (name, frame), path in zip(outputs.items(), paths, strict=True):
        frame.to_parquet(path, index=False)
        audit.log(layer, "completed", table=name, output_shape=list(frame.shape),
                  input_shapes={key: list(value.shape) for key, value in inputs.items()},
                  quality=frame.attrs.get("quality", {}))
    return [str(path) for path in paths]