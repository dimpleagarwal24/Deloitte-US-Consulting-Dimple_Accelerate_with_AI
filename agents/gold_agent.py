import pandas as pd

from core.config import GOLD_DIR
from core.mappings import AGGREGATE, load_tables, read_mapping
from core.transforms import require_columns, save_outputs, unique_reference


def _sum(values: pd.Series):
    return values.sum(min_count=1)


def transform(tables: dict[str, pd.DataFrame], rules: pd.DataFrame) -> pd.DataFrame:
    joins = rules[rules.transformation_type == "join"]
    quality = {"unmatched_join_rows": 0}
    frame = None
    joined = set()
    for rule in joins.to_dict("records"):
        _, left, right, key = rule["transformation_logic"].split(":")
        if left not in tables or right not in tables or right in joined:
            raise ValueError("Unknown or repeated table in join")
        if frame is None:
            frame = tables[left].copy()
            joined.add(left)
        if left not in joined:
            raise ValueError("Join rules must form a connected chain")
        require_columns(frame, [key])
        reference = tables[right]
        unique_reference(reference, key)
        quality["unmatched_join_rows"] += int((~frame[key].isin(reference[key])).sum())
        frame = frame.merge(reference, on=key, how="left", validate="many_to_one", suffixes=("", f"__{right}"))
        joined.add(right)
    if frame is None:
        names = rules.source_table.unique().tolist()
        if len(names) != 1 or names[0] not in tables:
            raise ValueError("Gold without joins needs one known source table")
        frame = tables[names[0]].copy()
        joined.add(names[0])
    if not set(rules.source_table) <= joined:
        raise ValueError("Gold references a table outside the join chain")
    groups = []
    for rule in rules[rules.transformation_type == "group_by"].to_dict("records"):
        source, target = rule["source_column"], rule["target_column"]
        require_columns(frame, [source])
        if target in groups or (target != source and target in frame):
            raise ValueError("Conflicting group aliases")
        frame[target] = frame[source]
        groups.append(target)
    aggregates = {}
    functions = {"SUM": _sum, "AVG": "mean", "COUNT": "count", "MAX": "max", "MIN": "min"}
    for rule in rules[rules.transformation_type == "aggregate"].to_dict("records"):
        match = AGGREGATE.fullmatch(rule["transformation_logic"])
        if match is None:
            raise ValueError("Unsupported aggregate expression")
        operation, column, alias = match.groups()
        require_columns(frame, [column])
        if alias != rule["target_column"] or alias in aggregates or alias in groups or alias == "pk_gold_id":
            raise ValueError("Duplicate or inconsistent aggregate alias")
        if operation.upper() in {"SUM", "AVG"} and not pd.api.types.is_numeric_dtype(frame[column]):
            raise ValueError("SUM and AVG require numeric input columns")
        aggregates[alias] = pd.NamedAgg(column=column, aggfunc=functions[operation.upper()])
    if not aggregates:
        raise ValueError("Gold requires at least one aggregate")
    if groups:
        result = frame.groupby(groups, dropna=False, sort=True).agg(**aggregates).reset_index()
    else:
        result = pd.DataFrame([{alias: frame[spec.column].agg(spec.aggfunc) for alias, spec in aggregates.items()}])
    result.insert(0, "pk_gold_id", range(1, len(result) + 1))
    result.attrs["quality"] = quality
    return result


def run(silver_paths: list[str], sttm_path: str, business_intent: str, run_id: str) -> list[str]:
    tables = load_tables(silver_paths, "silver", run_id)
    mapping = read_mapping(sttm_path, "gold")
    outputs = {}
    for name, rules in mapping.groupby("target_table", sort=False):
        if not name.endswith("_gold"):
            raise ValueError("Gold target names must end in _gold")
        outputs[name] = transform(tables, rules)
    return save_outputs(outputs, GOLD_DIR, "gold", run_id, tables)