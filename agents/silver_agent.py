import json

import pandas as pd

from core.config import SILVER_DIR
from core.mappings import load_tables, read_mapping
from core.transforms import cast_column, match_sources, require_columns, save_outputs, unique_reference


def transform(frame: pd.DataFrame, rules: pd.DataFrame, base_name: str, references: dict[str, pd.DataFrame]) -> pd.DataFrame:
    output = frame.copy()
    approved = []
    quality = {"unmatched_lookup_rows": 0}
    for rule in rules.to_dict("records"):
        source, target = rule["source_column"], rule["target_column"]
        kind, logic = rule["transformation_type"], rule["transformation_logic"]
        if kind == "surrogate_key":
            continue
        if kind == "deduplicate":
            subset = [column for column in output if not column.startswith(("_", "pk_"))] if logic == "*" else [column.strip() for column in logic.split(",")]
            require_columns(output, subset)
            if not subset:
                raise ValueError("Deduplication needs business columns")
            output = output.drop_duplicates(subset=subset)
            continue
        require_columns(output, [source])
        if source in {"total_amount", "unit_price", "quantity"} and kind not in {"passthrough", "type_cast"}:
            raise ValueError("The approved monetary policy forbids imputing or rewriting amounts and quantities")
        values = output[source]
        if kind == "type_cast":
            values = cast_column(values, logic)
        elif kind == "normalize":
            if logic == "date":
                values = cast_column(values, "datetime").dt.strftime("%Y-%m-%d")
            else:
                method = {"strip": "strip", "lowercase": "lower", "uppercase": "upper", "title case": "title"}[logic]
                values = getattr(values.astype("string").str, method)()
        elif kind == "map_values":
            values = values.replace(json.loads(logic))
        elif kind == "lookup":
            table, key, column = logic.split(":")
            if table not in references:
                raise ValueError("Unknown lookup table")
            reference = references[table]
            require_columns(output, [key])
            require_columns(reference, [column])
            unique_reference(reference, key)
            canonical = output[key].map(reference.set_index(key)[column])
            quality["unmatched_lookup_rows"] += int(canonical.isna().sum())
            values = canonical.combine_first(values)
        elif kind == "fill_null":
            if logic == "0":
                fill = 0
            elif logic == "mode":
                modes = values.mode()
                if modes.empty:
                    raise ValueError("Cannot impute mode from an all-null column")
                fill = modes.iloc[0]
            else:
                fill = getattr(values, logic)()
                if pd.isna(fill):
                    raise ValueError("Cannot impute from an all-null column")
            values = values.fillna(fill)
        elif kind == "drop_null":
            output = output.loc[values.notna()].copy()
            values = values.loc[output.index]
        output[target] = values
        if target not in approved:
            approved.append(target)
    output = output[approved].reset_index(drop=True)
    if not approved:
        raise ValueError("Silver mapping has no output columns")
    output.insert(0, f"pk_{base_name}_silver_id", range(1, len(output) + 1))
    if {"quantity", "unit_price", "total_amount"} <= set(output):
        difference = output.quantity * output.unit_price - output.total_amount
        quality["amount_mismatches"] = int((difference.abs() > 0.011).sum())
        quality["negative_amount_rows"] = int((output.total_amount < 0).sum())
    output.attrs["quality"] = quality
    return output


def run(bronze_paths: list[str], sttm_path: str, run_id: str) -> list[str]:
    tables = load_tables(bronze_paths, "bronze", run_id)
    mapping = match_sources(read_mapping(sttm_path, "silver"), tables, "bronze")
    outputs = {}
    for name, frame in tables.items():
        base = name.removesuffix("_bronze")
        rules = mapping[mapping.source_table == name]
        if set(rules.target_table) != {f"{base}_silver"}:
            raise ValueError("Silver target_table must be <base>_silver")
        outputs[f"{base}_silver"] = transform(frame, rules, base, tables)
    return save_outputs(outputs, SILVER_DIR, "silver", run_id, tables)