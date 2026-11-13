import json
import math
from pathlib import Path

import pandas as pd

from core.audit import AuditLogger
from core.config import PROFILES_DIR
from core.mappings import identifier


def _number(value):
    return float(value) if pd.notna(value) and math.isfinite(value) else None


def profile_tables(tables: dict[str, pd.DataFrame], run_id: str) -> dict:
    result = {"run_id": run_id, "tables": {}, "candidate_join_keys": {}}
    candidates = {}
    for name, frame in tables.items():
        columns = {}
        for column in frame.columns:
            values = frame[column]
            nonnull = values.dropna()
            text = nonnull.astype(str)
            details = {
                "dtype": str(values.dtype), "null_count": int(values.isna().sum()),
                "null_pct": float(values.isna().mean() * 100) if len(values) else 0,
                "unique_count": int(values.nunique()),
                "sample_values": json.loads(nonnull.drop_duplicates().head(5).to_json(orient="values", date_format="iso")),
                "quality_flags": [],
            }
            if 0 < len(text) and not pd.api.types.is_numeric_dtype(values):
                formats = [r"\d{2}/\d{2}/\d{4}", r"\d{4}-\d{2}-\d{2}", r"\d{2}-\d{2}-\d{4}"]
                if sum(text.str.fullmatch(pattern).mean() > 0.1 for pattern in formats) > 1:
                    details["quality_flags"].append("mixed_date_formats")
                if text.str.len().mean() < 6:
                    details["quality_flags"].append("possible_abbreviations")
                if values.nunique() <= 50:
                    details["distinct_values"] = sorted(text.unique().tolist())
            if pd.api.types.is_numeric_dtype(values):
                details.update(min=_number(values.min()), max=_number(values.max()), mean=_number(values.mean()))
            columns[column] = details
            if column.endswith("_id"):
                candidates.setdefault(column, []).append(name)
        summary = {"row_count": len(frame), "duplicate_rows": int(frame.duplicated().sum()), "columns": columns}
        if {"quantity", "unit_price", "total_amount"} <= set(frame.columns):
            numeric = frame[["quantity", "unit_price", "total_amount"]].apply(pd.to_numeric, errors="coerce")
            difference = numeric["quantity"] * numeric["unit_price"] - numeric["total_amount"]
            summary["amount_mismatches"] = int((difference.abs() > 0.011).sum())
            summary["negative_amount_rows"] = int((numeric["total_amount"] < 0).sum())
        result["tables"][name] = summary
    result["candidate_join_keys"] = {key: names for key, names in candidates.items() if len(names) > 1}
    return result


def profile(file_paths: list[str], run_id: str) -> str:
    tables = {}
    for path in file_paths:
        name = identifier(Path(path).stem)
        if name in tables:
            raise ValueError("Duplicate input table names")
        tables[name] = pd.read_csv(path, low_memory=False)
    result = profile_tables(tables, run_id)
    output = PROFILES_DIR / f"profile_combined_{run_id}.json"
    output.write_text(json.dumps(result, indent=2, allow_nan=False), encoding="utf-8")
    AuditLogger(run_id).log("profiler", "completed", tables=len(tables))
    return str(output)