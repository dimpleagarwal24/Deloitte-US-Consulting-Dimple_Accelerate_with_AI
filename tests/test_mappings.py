import json
from unittest.mock import Mock

import pandas as pd
import pytest

from agents import profiler, sttm_generator
from core.mappings import COLUMNS, extract_csv, validate_mapping


def mapping_row(layer="bronze", **overrides):
    source = {"bronze": "landing", "silver": "bronze", "gold": "silver"}[layer]
    return {
        "source_schema": source, "source_table": "sales_data", "source_column": "total_amount",
        "target_schema": layer, "target_table": f"sales_data_{layer}", "target_column": "total_amount",
        "transformation_type": "type_cast" if layer == "bronze" else "passthrough",
        "transformation_logic": "float" if layer == "bronze" else "",
        **overrides,
    }


def test_extract_csv_and_validate():
    expected = pd.DataFrame([mapping_row()])
    text = expected.to_csv(index=False)
    for response in (text, f"Here is your mapping:\n```csv\n{text}```"):
        actual = validate_mapping(extract_csv(response), "bronze")
        assert actual.to_dict("records") == expected.to_dict("records")


@pytest.mark.parametrize("overrides", [
    {"target_table": "../../escape"}, {"transformation_type": "execute"},
    {"transformation_logic": "eval(code)"}, {"target_schema": "silver"},
    {"source_schema": "silver"}, {"source_column": ""},
])
def test_reject_bad_mapping(overrides):
    with pytest.raises(ValueError):
        validate_mapping(pd.DataFrame([mapping_row(**overrides)]), "bronze")


def test_reject_missing_columns_and_empty_mapping():
    for frame in (pd.DataFrame([{"source_table": "sales"}]), pd.DataFrame(columns=COLUMNS)):
        with pytest.raises(ValueError):
            validate_mapping(frame, "bronze")


@pytest.mark.parametrize("kind,logic", [
    ("normalize", "strip"), ("map_values", '{"Prem":"Premium"}'),
    ("lookup", "products:product_id:category"), ("fill_null", "median"),
    ("deduplicate", "*"), ("drop_null", ""), ("surrogate_key", ""),
])
def test_silver_rules(kind, logic):
    frame = pd.DataFrame([mapping_row("silver", transformation_type=kind, transformation_logic=logic)])
    assert len(validate_mapping(frame, "silver")) == 1


@pytest.mark.parametrize("kind,logic", [
    ("map_values", "[]"), ("lookup", "bad"), ("normalize", "eval"),
    ("fill_null", "infinity"), ("deduplicate", "a; DROP TABLE"),
])
def test_reject_bad_silver_logic(kind, logic):
    with pytest.raises(ValueError):
        validate_mapping(pd.DataFrame([mapping_row("silver", transformation_type=kind, transformation_logic=logic)]), "silver")


@pytest.mark.parametrize("kind,logic", [
    ("join", "join_left:sales_data_silver:products_silver:product_id"),
    ("aggregate", "SUM(total_amount) AS total_revenue"), ("group_by", ""),
])
def test_gold_rules(kind, logic):
    assert len(validate_mapping(pd.DataFrame([mapping_row("gold", transformation_type=kind, transformation_logic=logic)]), "gold")) == 1


def test_profile_flags_and_join_keys():
    sales = pd.DataFrame({"product_id": ["P1", "P1", "P2"], "transaction_date": ["01/15/2024", "2024-01-17", "01-31-2024"], "amount": [1.0, None, 3.0]})
    catalog = pd.DataFrame({"product_id": ["P1", "P2"]})
    result = profiler.profile_tables({"sales": sales, "products": catalog}, "test")
    assert result["candidate_join_keys"] == {"product_id": ["sales", "products"]}
    assert result["tables"]["sales"]["column_count"] == 3
    columns = result["tables"]["sales"]["columns"]
    assert "mixed_date_formats" in columns["transaction_date"]["quality_flags"]
    assert columns["amount"]["null_count"] == 1
    assert columns["amount"]["mean"] == 2
    json.dumps(result, allow_nan=False)


def test_empty_profile_is_json_serializable():
    result = profiler.profile_tables({"empty": pd.DataFrame({"value": pd.Series(dtype=float)})}, "empty")
    json.dumps(result, allow_nan=False)
    assert result["tables"]["empty"]["columns"]["value"]["null_pct"] == 0


@pytest.mark.parametrize("values,mixed", [
    (["1/5/2024", "2024-1-6", None], True),
    (["1-5-2024", "2024/1/6"], True),
    (["1/5/2024", "01/06/2024"], False),
    (["not a date", None], False),
    ([None, None], False),
])
def test_profile_date_format_variants(values, mixed):
    result = profiler.profile_tables({"sales": pd.DataFrame({"date": values})}, "test")
    flags = result["tables"]["sales"]["columns"]["date"]["quality_flags"]
    assert ("mixed_date_formats" in flags) is mixed


def test_profile_detects_uppercase_id_suffix():
    table = pd.DataFrame({"PRODUCT_ID": ["P1"]})
    result = profiler.profile_tables({"sales": table, "products": table}, "test")
    assert result["candidate_join_keys"] == {"PRODUCT_ID": ["sales", "products"]}


def test_profile_records_source_and_output_lineage(monkeypatch):
    monkeypatch.setattr(profiler.pd, "read_csv", Mock(return_value=pd.DataFrame({"amount": [1]})))
    write = Mock()
    monkeypatch.setattr(profiler.Path, "write_text", write)
    audit = Mock()
    monkeypatch.setattr(profiler, "AuditLogger", Mock(return_value=audit))
    output = profiler.profile(["sales.csv"], "test")
    saved = json.loads(write.call_args.args[0])
    assert saved["tables"]["sales"]["source_path"] == "sales.csv"
    assert saved["tables"]["sales"]["column_count"] == 1
    audit.log.assert_called_once_with("profiler", "completed", tables=1,
                                      input_files=["sales.csv"], output=output)


def test_generator_validates_before_saving(monkeypatch):
    response = pd.DataFrame([mapping_row()]).to_csv(index=False)
    call = Mock(return_value=response)
    monkeypatch.setattr(sttm_generator, "call_llm", call)
    monkeypatch.setattr(sttm_generator, "AuditLogger", Mock())
    save = Mock()
    monkeypatch.setattr(pd.DataFrame, "to_csv", save)
    path = sttm_generator.generate_mapping("bronze", {"tables": {}}, "Revenue", "test")
    assert str(path).endswith("sttm_bronze_test.csv")
    call.assert_called_once()
    save.assert_called_once()
    call.return_value = "not a mapping"
    with pytest.raises(ValueError):
        sttm_generator.generate_mapping("bronze", {}, "Revenue", "test")
    assert save.call_count == 1


@pytest.mark.parametrize("include_source_path", [True, False])
def test_bronze_prompt_keeps_profile_paths_local(monkeypatch, include_source_path):
    metadata = {"row_count": 1, "column_count": 1, "columns": {"amount": {"dtype": "float64"}}}
    if include_source_path:
        metadata["source_path"] = "private/input/sales.csv"
    profile = {"tables": {"sales": metadata}}
    monkeypatch.setattr(sttm_generator.Path, "read_text", Mock(return_value=json.dumps(profile)))
    generate = Mock(return_value="mapping.csv")
    monkeypatch.setattr(sttm_generator, "generate_mapping", generate)
    assert sttm_generator.generate_bronze_sttm("profile.json", "Revenue", "test") == "mapping.csv"
    sent = generate.call_args.args[1]["tables"]["sales"]
    assert "source_path" not in sent
    assert sent["column_count"] == 1
    assert sent["columns"] == metadata["columns"]