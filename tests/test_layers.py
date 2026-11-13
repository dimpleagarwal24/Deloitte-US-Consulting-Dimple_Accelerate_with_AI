import pandas as pd
import pytest

from agents import bronze_agent, gold_agent, silver_agent
from core.transforms import cast_column
from tests.test_mappings import mapping_row


def rules_for(columns, layer, **overrides):
    return pd.DataFrame([
        mapping_row(layer, source_column=column, target_column=column,
                    transformation_type="passthrough", transformation_logic="", **overrides)
        for column in columns
    ])


def test_bronze_preserves_rows_nulls_and_mixed_dates():
    source = pd.DataFrame({"transaction_date": ["01/15/2024", "2024-01-17", "01-31-2024", None], "product_id": ["P1", "P2", None, "P1"]})
    rules = rules_for(source.columns, "bronze")
    rules.loc[0, ["transformation_type", "transformation_logic"]] = ["type_cast", "datetime"]
    result = bronze_agent.transform(source, rules, "sales.csv", "fixed")
    assert len(result) == len(source)
    assert result.transaction_date.dt.day.tolist()[:3] == [15, 17, 31]
    assert pd.isna(result.product_id.iloc[2])
    assert result._source_file.tolist() == ["sales.csv"] * 4
    assert "_source_file" not in source.columns


@pytest.mark.parametrize("values,kind", [(["not-a-date"], "datetime"), (["2024-02-30"], "datetime"), (["abc"], "float"), (["1.5"], "int"), (["inf"], "float")])
def test_bad_cast_fails_without_silent_coercion(values, kind):
    with pytest.raises(ValueError):
        cast_column(pd.Series(values), kind)


@pytest.mark.parametrize("kind", ["str", "int", "float", "datetime"])
def test_cast_preserves_nulls(kind):
    assert cast_column(pd.Series([None]), kind).isna().all()


def test_bronze_requires_full_column_coverage():
    with pytest.raises(ValueError, match="every source column"):
        bronze_agent.transform(pd.DataFrame({"a": [1], "b": [2]}), rules_for(["a"], "bronze"), "input", "fixed")


def test_silver_lookup_and_dedup_preserve_returns_and_amounts():
    source = pd.DataFrame({"product_id": ["P1", "P1", "P2"], "category": ["Elec.", "Elec.", "Sport"], "total_amount": [100.5, 100.5, -20.0], "_load_timestamp": ["a", "b", "a"]})
    rules = rules_for(source.columns, "silver")
    rules.loc[1, ["transformation_type", "transformation_logic"]] = ["lookup", "products_bronze:product_id:category"]
    rules = pd.concat([rules, pd.DataFrame([mapping_row("silver", source_column="*", target_column="*", transformation_type="deduplicate", transformation_logic="*")])], ignore_index=True)
    catalog = pd.DataFrame({"product_id": ["P1", "P2"], "category": ["Electronics", "Sports"]})
    result = silver_agent.transform(source, rules, "sales_data", {"products_bronze": catalog})
    assert result.category.tolist() == ["Electronics", "Sports"]
    assert result.total_amount.tolist() == [100.5, -20.0]
    assert result.iloc[:, 0].tolist() == [1, 2]
    assert result.columns[0] == "pk_sales_data_silver_id"


@pytest.mark.parametrize("kind,logic,expected", [
    ("normalize", "strip", ["prem", "STD"]),
    ("normalize", "lowercase", [" prem ", "std"]),
    ("normalize", "uppercase", [" PREM ", "STD"]),
    ("normalize", "title case", [" Prem ", "Std"]),
    ("map_values", '{" prem ": "Premium", "STD": "Standard"}', ["Premium", "Standard"]),
])
def test_silver_text_operations(kind, logic, expected):
    source = pd.DataFrame({"segment": [" prem ", "STD"]})
    rules = pd.DataFrame([mapping_row("silver", source_column="segment", target_column="segment", transformation_type=kind, transformation_logic=logic)])
    assert silver_agent.transform(source, rules, "sales", {}).segment.tolist() == expected


@pytest.mark.parametrize("logic,expected", [("mean", 2), ("median", 2), ("mode", 1), ("0", 0)])
def test_silver_fill(logic, expected):
    source = pd.DataFrame({"score": [1.0, None, 3.0]})
    rules = pd.DataFrame([mapping_row("silver", source_column="score", target_column="score", transformation_type="fill_null", transformation_logic=logic)])
    assert silver_agent.transform(source, rules, "sales", {}).score.iloc[1] == expected


def test_silver_does_not_impute_money():
    rules = pd.DataFrame([mapping_row("silver", transformation_type="fill_null", transformation_logic="0")])
    with pytest.raises(ValueError, match="monetary"):
        silver_agent.transform(pd.DataFrame({"total_amount": [None]}), rules, "sales", {})


def test_silver_unmatched_lookup_keeps_original_and_reports_it():
    source = pd.DataFrame({"product_id": ["unknown"], "category": ["Original"]})
    rules = rules_for(source.columns, "silver")
    rules.loc[1, ["transformation_type", "transformation_logic"]] = ["lookup", "products:product_id:category"]
    catalog = pd.DataFrame({"product_id": ["P1"], "category": ["Electronics"]})
    result = silver_agent.transform(source, rules, "sales", {"products": catalog})
    assert result.category.tolist() == ["Original"]
    assert result.attrs["quality"]["unmatched_lookup_rows"] == 1


def gold_rules(with_join=True):
    rows = []
    if with_join:
        rows.append(mapping_row("gold", source_table="sales_silver", source_column="*", target_column="*", transformation_type="join", transformation_logic="join_left:sales_silver:stores_silver:store_id"))
    rows.extend([
        mapping_row("gold", source_table="sales_silver", source_column="region", target_column="region", transformation_type="group_by", transformation_logic=""),
        mapping_row("gold", source_table="sales_silver", target_column="total_revenue", transformation_type="aggregate", transformation_logic="SUM(total_amount) AS total_revenue"),
    ])
    return pd.DataFrame(rows)


def test_gold_joins_without_losing_unknown_regions_or_returns():
    tables = {
        "sales_silver": pd.DataFrame({"store_id": ["S1", "S1", "missing"], "total_amount": [100.0, -20.0, 7.0]}),
        "stores_silver": pd.DataFrame({"store_id": ["S1"], "region": ["West"]}),
    }
    result = gold_agent.transform(tables, gold_rules())
    assert result.total_revenue.sum() == 87
    assert len(result) == 2
    assert result.pk_gold_id.tolist() == [1, 2]
    assert result.attrs["quality"]["unmatched_join_rows"] == 1


def test_gold_rejects_join_fanout():
    tables = {
        "sales_silver": pd.DataFrame({"store_id": ["S1"], "total_amount": [100]}),
        "stores_silver": pd.DataFrame({"store_id": ["S1", "S1"], "region": ["West", "East"]}),
    }
    with pytest.raises(ValueError, match="unique"):
        gold_agent.transform(tables, gold_rules())


@pytest.mark.parametrize("operation,expected", [("SUM", 6), ("AVG", 2), ("COUNT", 3), ("MAX", 3), ("MIN", 1)])
def test_global_aggregation(operation, expected):
    rules = pd.DataFrame([mapping_row("gold", source_table="sales_silver", target_column="metric", transformation_type="aggregate", transformation_logic=f"{operation}(total_amount) AS metric")])
    result = gold_agent.transform({"sales_silver": pd.DataFrame({"total_amount": [1, 2, 3]})}, rules)
    assert result.metric.iloc[0] == expected