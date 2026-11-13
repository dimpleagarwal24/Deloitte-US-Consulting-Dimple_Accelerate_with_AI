from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core.config import BRONZE_DIR
from core.mappings import identifier, read_mapping
from core.transforms import cast_column, match_sources, save_outputs


def transform(frame: pd.DataFrame, rules: pd.DataFrame, source_file: str, timestamp: str) -> pd.DataFrame:
    column_rules = rules[rules.transformation_type != "metadata_inject"]
    if set(column_rules.source_column) != set(frame.columns) or column_rules.source_column.duplicated().any():
        raise ValueError("Bronze must map every source column exactly once")
    if column_rules.target_column.duplicated().any() or set(column_rules.target_column) & {"_load_timestamp", "_source_file", "*"}:
        raise ValueError("Duplicate or reserved Bronze target columns")
    output = pd.DataFrame(index=frame.index)
    for rule in column_rules.to_dict("records"):
        values = frame[rule["source_column"]]
        if rule["transformation_type"] == "type_cast":
            values = cast_column(values, rule["transformation_logic"])
        output[rule["target_column"]] = values
    output["_load_timestamp"] = timestamp
    output["_source_file"] = source_file
    return output


def run(input_files: list[str], sttm_path: str, run_id: str) -> list[str]:
    tables = {identifier(Path(path).stem): pd.read_csv(path, dtype="string") for path in input_files}
    if len(tables) != len(input_files):
        raise ValueError("Duplicate input names")
    mapping = match_sources(read_mapping(sttm_path, "bronze"), tables, "landing")
    outputs = {}
    timestamp = datetime.now(timezone.utc).isoformat()
    for path in input_files:
        name = Path(path).stem
        rules = mapping[mapping.source_table == name]
        if set(rules.target_table) != {f"{name}_bronze"}:
            raise ValueError("Bronze target_table must be <source_table>_bronze")
        metadata = rules[rules.transformation_type == "metadata_inject"]
        if len(metadata) != 2 or set(metadata.target_column) != {"_load_timestamp", "_source_file"}:
            raise ValueError("Bronze requires both metadata mapping rows")
        outputs[f"{name}_bronze"] = transform(tables[name], rules, str(path), timestamp)
    return save_outputs(outputs, BRONZE_DIR, "bronze", run_id, tables)