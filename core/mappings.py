import io
import json
import re
from pathlib import Path

import pandas as pd

COLUMNS = [
    "source_schema", "source_table", "source_column", "target_schema",
    "target_table", "target_column", "transformation_type", "transformation_logic",
]
IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
AGGREGATE = re.compile(rf"(SUM|AVG|COUNT|MAX|MIN)\(({IDENTIFIER})\) AS ({IDENTIFIER})", re.IGNORECASE)
OPERATIONS = {
    "bronze": {"type_cast", "passthrough", "metadata_inject"},
    "silver": {"type_cast", "passthrough", "normalize", "map_values", "lookup", "fill_null", "drop_null", "deduplicate", "surrogate_key"},
    "gold": {"join", "group_by", "aggregate"},
}


def identifier(value: str) -> str:
    if not re.fullmatch(IDENTIFIER, value):
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


def extract_csv(text: str) -> pd.DataFrame:
    match = re.search(r"```csv\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return pd.read_csv(io.StringIO(match.group(1) if match else text.strip()), dtype=str, keep_default_na=False)


def validate_mapping(frame: pd.DataFrame, layer: str) -> pd.DataFrame:
    if layer not in OPERATIONS or list(frame.columns) != COLUMNS or frame.empty:
        raise ValueError("STTM must contain nonempty rules and exactly the eight required columns in order.")
    frame = frame.fillna("").astype(str).apply(lambda column: column.str.strip())
    source_schema = {"bronze": "landing", "silver": "bronze", "gold": "silver"}[layer]
    for rule in frame.to_dict("records"):
        if rule["source_schema"] != source_schema or rule["target_schema"] != layer:
            raise ValueError("STTM layer mismatch")
        for field in ("source_table", "target_table", "source_column", "target_column"):
            if field.endswith("column") and rule[field] == "*":
                continue
            identifier(rule[field])
        kind, logic = rule["transformation_type"], rule["transformation_logic"]
        if kind not in OPERATIONS[layer]:
            raise ValueError(f"Unsupported {layer} transformation: {kind}")
        valid = True
        if kind == "type_cast":
            valid = logic in {"datetime", "float", "int", "str"}
        elif kind == "normalize":
            valid = logic in {"strip", "lowercase", "uppercase", "title case", "date"}
        elif kind == "metadata_inject":
            valid = rule["target_column"] in {"_load_timestamp", "_source_file"}
        elif kind == "fill_null":
            valid = logic in {"mean", "median", "mode", "0"}
        elif kind == "map_values":
            values = json.loads(logic)
            valid = isinstance(values, dict) and all(isinstance(key, str) and isinstance(value, str) for key, value in values.items())
        elif kind == "lookup":
            valid = len(logic.split(":")) == 3 and all(re.fullmatch(IDENTIFIER, part) for part in logic.split(":"))
        elif kind == "deduplicate":
            valid = logic == "*" or all(re.fullmatch(IDENTIFIER, part.strip()) for part in logic.split(","))
        elif kind == "join":
            parts = logic.split(":")
            valid = len(parts) == 4 and parts[0] == "join_left" and all(re.fullmatch(IDENTIFIER, part) for part in parts[1:])
        elif kind == "aggregate":
            valid = AGGREGATE.fullmatch(logic) is not None
        elif kind in {"passthrough", "drop_null", "surrogate_key", "group_by"}:
            valid = logic == ""
        if not valid:
            raise ValueError(f"Unsupported logic for {kind}")
    return frame


def read_mapping(path: str, layer: str) -> pd.DataFrame:
    return validate_mapping(pd.read_csv(path, dtype=str, keep_default_na=False), layer)


def load_tables(paths: list[str], layer: str, run_id: str) -> dict[str, pd.DataFrame]:
    tables = {}
    for path in paths:
        name = Path(path).stem.removesuffix(f"_{run_id}")
        identifier(name)
        if not name.endswith(f"_{layer}") or name in tables:
            raise ValueError(f"Unexpected or duplicate {layer} table name")
        tables[name] = pd.read_parquet(path)
    return tables