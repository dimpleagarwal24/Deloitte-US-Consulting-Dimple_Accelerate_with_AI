import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from queue import Queue
from threading import Thread
from unittest.mock import Mock

import pandas as pd
import pytest

import cli
from agents import bronze_agent, gold_agent, profiler, reporter, silver_agent, sttm_generator
from core import audit, config
from core.llm import LLMError
from tests.test_mappings import mapping_row


@pytest.fixture
def pipeline_environment(tmp_path, monkeypatch):
    locations = {
        "LANDING_DIR": [cli], "PROFILES_DIR": [profiler], "STTM_DIR": [sttm_generator],
        "BRONZE_DIR": [bronze_agent], "SILVER_DIR": [silver_agent], "GOLD_DIR": [gold_agent],
        "TRACES_DIR": [cli], "REPORTS_DIR": [reporter], "AUDIT_DIR": [audit],
    }
    for name, modules in locations.items():
        path = tmp_path / name.lower()
        monkeypatch.setattr(config, name, path)
        for module in modules:
            monkeypatch.setattr(module, name, path)
    root = Path(__file__).resolve().parents[1]
    files = [root / name for name in ("sales_data.csv", "products.csv", "stores.csv")]
    tables = {path.stem: pd.read_csv(path) for path in files}
    bronze_rows, silver_rows = [], []
    for table, frame in tables.items():
        for column in frame:
            kind = "datetime" if column == "transaction_date" else "float" if pd.api.types.is_numeric_dtype(frame[column]) else "str"
            bronze_rows.append(mapping_row(source_table=table, source_column=column, target_table=f"{table}_bronze", target_column=column, transformation_logic=kind))
            operation, logic = "passthrough", ""
            if table == "sales_data" and column in {"product_name", "category"}:
                operation, logic = "lookup", f"products_bronze:product_id:{column}"
            elif column == "transaction_date":
                operation, logic = "normalize", "date"
            elif column == "customer_segment":
                operation, logic = "map_values", json.dumps({"Prem": "Premium", "Std": "Standard", "Bgt": "Budget"})
            silver_rows.append(mapping_row("silver", source_table=f"{table}_bronze", source_column=column, target_table=f"{table}_silver", target_column=column, transformation_type=operation, transformation_logic=logic))
        for column in ("_load_timestamp", "_source_file"):
            bronze_rows.append(mapping_row(source_table=table, source_column="*", target_table=f"{table}_bronze", target_column=column, transformation_type="metadata_inject", transformation_logic=""))
            silver_rows.append(mapping_row("silver", source_table=f"{table}_bronze", source_column=column, target_table=f"{table}_silver", target_column=column))
        silver_rows.append(mapping_row("silver", source_table=f"{table}_bronze", source_column="*", target_table=f"{table}_silver", target_column="*", transformation_type="deduplicate", transformation_logic="*"))
    monkeypatch.setattr(cli, "display_sttm", lambda path, layer: cli.read_mapping(path, layer))
    return {"files": [str(path) for path in files], "tables": tables,
            "bronze": pd.DataFrame(bronze_rows).to_csv(index=False),
            "silver": pd.DataFrame(silver_rows).to_csv(index=False)}


def mock_responses(environment, dimension="category"):
    rows = []
    if dimension == "region":
        rows.append(mapping_row("gold", source_table="sales_data_silver", source_column="*", target_column="*", target_table="revenue_gold", transformation_type="join", transformation_logic="join_left:sales_data_silver:stores_silver:store_id"))
    rows.extend([
        mapping_row("gold", source_table="sales_data_silver", source_column=dimension, target_column=dimension, target_table="revenue_gold", transformation_type="group_by", transformation_logic=""),
        mapping_row("gold", source_table="sales_data_silver", target_column="total_revenue", target_table="revenue_gold", transformation_type="aggregate", transformation_logic="SUM(total_amount) AS total_revenue"),
    ])
    response = f'SQL:\n```sql\nSELECT {dimension}, total_revenue FROM revenue_gold ORDER BY total_revenue DESC\n```\nNARRATIVE:\nUnverified test draft: 999999999.\nCHARTS:\n```json\n[{{"type":"bar","x":"{dimension}","y":"total_revenue","title":"Net revenue"}}]\n```'
    return [environment["bronze"], environment["silver"], pd.DataFrame(rows).to_csv(index=False), response]


@pytest.mark.integration
@pytest.mark.parametrize("dimension", ["category", "region"])
def test_actual_inputs_end_to_end(pipeline_environment, monkeypatch, dimension):
    environment = pipeline_environment
    model = Mock(side_effect=mock_responses(environment, dimension))
    monkeypatch.setattr(sttm_generator, "call_llm", model)
    monkeypatch.setattr(reporter, "call_llm", model)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    before = {path: Path(path).read_bytes() for path in environment["files"]}
    state = cli.run_pipeline(environment["files"], f"Which {dimension} has the highest net sales revenue?")
    assert state.status == "completed"
    assert model.call_count == 4
    assert len(state.bronze_paths) == len(state.silver_paths) == 3
    bronze = pd.read_parquet(state.bronze_paths[0])
    silver = pd.read_parquet(state.silver_paths[0])
    gold = pd.read_parquet(state.gold_paths[0])
    assert len(bronze) == 186
    assert bronze.transaction_date.notna().sum() == environment["tables"]["sales_data"].transaction_date.notna().sum()
    assert (silver.total_amount < 0).sum() > 0
    expected = environment["tables"]["sales_data"].copy()
    catalog = environment["tables"]["products"].set_index("product_id")
    for column in ("category", "product_name"):
        expected[column] = expected.product_id.map(catalog[column]).combine_first(expected[column])
    expected["customer_segment"] = expected.customer_segment.replace({"Prem": "Premium", "Std": "Standard", "Bgt": "Budget"})
    expected["transaction_date"] = pd.to_datetime(expected.transaction_date, format="mixed", dayfirst=False).dt.strftime("%Y-%m-%d")
    expected = expected.drop_duplicates()
    if dimension == "region":
        expected = expected.merge(environment["tables"]["stores"], on="store_id", validate="many_to_one", how="left")
    totals = expected.groupby(dimension, dropna=False).total_amount.sum().sort_index()
    actual = gold.set_index(dimension).total_revenue.sort_index()
    totals.index = totals.index.astype("string").fillna("Unknown")
    actual.index = actual.index.astype("string").fillna("Unknown")
    pd.testing.assert_series_equal(actual, totals, check_dtype=False, check_names=False, check_index_type=False)
    assert float(actual.sum()) == pytest.approx(float(silver.total_amount.sum()))
    summary = json.loads(Path(state.report_path).with_suffix(".json").read_text())
    page = Path(state.report_path).read_text(encoding="utf-8")
    assert "Unverified test draft: 999999999." not in page
    assert summary["status"] == "query_executed"
    assert "plotly" in page.lower()
    assert all(Path(path).read_bytes() == content for path, content in before.items())
    logs = audit.AuditLogger(state.run_id).get_logs()
    assert sum(entry["agent"] == "approval" and entry["action"] == "approved" for entry in logs) == 3
    assert {"profiler", "sttm_generator", "bronze", "silver", "gold", "reporter", "pipeline"} <= {entry["agent"] for entry in logs}
    trace = json.loads((cli.TRACES_DIR / f"state_{state.run_id}.json").read_text())
    assert trace["status"] == "completed"
    print(f"VALIDATED_REPORT={state.report_path}")
    print(f"VALIDATED_NET_REVENUE={actual.sum():.2f}; SILVER_ROWS={len(silver)}; DIMENSION={dimension}")


@pytest.mark.integration
@pytest.mark.parametrize("abort_at", [1, 2, 3])
def test_abort_stops_before_unapproved_layer(pipeline_environment, monkeypatch, abort_at):
    model = Mock(side_effect=mock_responses(pipeline_environment))
    monkeypatch.setattr(sttm_generator, "call_llm", model)
    choices = iter(["y"] * (abort_at - 1) + ["n"])
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    state = cli.run_pipeline(pipeline_environment["files"], "Net revenue")
    assert state.status == "aborted"
    assert model.call_count == abort_at
    assert not state.gold_paths
    if abort_at == 1:
        assert not state.bronze_paths
    if abort_at <= 2:
        assert not state.silver_paths


@pytest.mark.integration
def test_model_failure_has_trace_without_secret(pipeline_environment, monkeypatch):
    monkeypatch.setattr(sttm_generator, "call_llm", Mock(side_effect=LLMError("private provider detail")))
    with pytest.raises(LLMError):
        cli.run_pipeline(pipeline_environment["files"], "Net revenue")
    trace = json.loads(next(cli.TRACES_DIR.glob("*.json")).read_text())
    assert trace["status"] == "failed"
    log = next(audit.AUDIT_DIR.glob("*.jsonl")).read_text()
    assert "private provider detail" not in log
    assert "LLMError" in log


@pytest.mark.integration
def test_offline_review_creates_labelled_artifacts_without_keys(pipeline_environment, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    monkeypatch.setattr(config, "LLM_PROVIDER", "not-configured")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert cli.main(["--offline-review"]) == 0
    trace = json.loads(next(cli.TRACES_DIR.glob("*.json")).read_text())
    assert trace["execution_mode"] == "offline_review"
    assert trace["status"] == "completed"
    assert len(trace["bronze_paths"]) == len(trace["silver_paths"]) == 3
    assert len(trace["gold_paths"]) == 1
    summary = json.loads(Path(trace["report_path"]).with_suffix(".json").read_text())
    assert summary["execution_mode"] == "offline_review"
    assert "OFFLINE REVIEW" in Path(trace["report_path"]).read_text(encoding="utf-8")
    logs = audit.AuditLogger(trace["run_id"]).get_logs()
    assert sum(entry.get("llm_calls", 0) for entry in logs) == 0
    assert sum(entry.get("response_source") == "review_fixture" for entry in logs) == 4
    approvals = [entry for entry in logs if entry["agent"] == "approval"]
    assert len(approvals) == 3
    assert all(len(entry["mapping_sha256"]) == 64 for entry in approvals)


@pytest.mark.parametrize("arguments", [["--files", "other.csv"], ["--intent", "Another question"]])
def test_offline_review_cannot_silently_answer_a_different_scenario(arguments):
    with pytest.raises(SystemExit) as result:
        cli.main(["--offline-review", *arguments])
    assert result.value.code == 2


@pytest.mark.integration
@pytest.mark.parametrize("abort_at", [None, "bronze", "silver", "gold"])
def test_real_cli_blocks_at_each_gate(tmp_path, request, abort_at):
    evidence_path = request.config.getoption("--review-evidence")
    evidence_root = Path(evidence_path).resolve() if evidence_path else tmp_path
    output_root = evidence_root / (f"abort_{abort_at}" if abort_at else "completed")
    output_root.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PIPELINE_OUTPUT_DIR": str(output_root), "GITHUB_TOKEN": "", "LLM_PROVIDER": "github", "PYTHONIOENCODING": "utf-8"}
    configured = subprocess.run([sys.executable, "-c", "from core.config import LANDING_DIR; print(LANDING_DIR)"],
                                cwd=project, env=environment, capture_output=True, text=True, check=True)
    assert Path(configured.stdout.strip()) == output_root / "data" / "landing"
    process = subprocess.Popen([sys.executable, "-u", "cli.py", "--offline-review"], cwd=project, env=environment,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", bufsize=1)
    characters = Queue()
    transcript = []

    def collect_output():
        for character in iter(lambda: process.stdout.read(1), ""):
            transcript.append(character)
            characters.put(character)

    reader = Thread(target=collect_output, daemon=True)
    reader.start()
    gates = []
    try:
        for layer in ("bronze", "silver", "gold"):
            output = ""
            while not output.endswith("[n]o abort > "):
                output += characters.get(timeout=60)
            trace = json.loads(next((output_root / "data" / "traces").glob("*.json")).read_text())
            assert trace["status"] == f"awaiting_{layer}_approval"
            assert process.poll() is None
            assert not list((output_root / "data" / f"{layer}_layer").glob("*.parquet"))
            choice = "n" if layer == abort_at else "y"
            gates.append({"layer": layer, "blocked_before_input": True, "downstream_absent_before_input": True, "test_input": choice})
            process.stdin.write(choice + "\n")
            process.stdin.flush()
            if choice == "n":
                break
        process.stdin.close()
        assert process.wait(timeout=60) == (1 if abort_at else 0)
        reader.join(timeout=5)
        assert not reader.is_alive()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    trace = json.loads(next((output_root / "data" / "traces").glob("*.json")).read_text())
    assert trace["status"] == ("aborted" if abort_at else "completed")
    logs = [json.loads(line) for line in next((output_root / "audit_logs").glob("*.jsonl")).read_text().splitlines()]
    assert sum(entry.get("llm_calls", 0) for entry in logs) == 0
    if not abort_at:
        assert {"profiler", "sttm_generator", "bronze", "silver", "gold", "reporter", "approval", "pipeline"} <= {entry["agent"] for entry in logs}
        for layer, expected_count in (("bronze", 3), ("silver", 3), ("gold", 1)):
            assert len(list((output_root / "data" / f"{layer}_layer").glob("*.parquet"))) == expected_count
            mapping = next((output_root / "data" / "sttm").glob(f"sttm_{layer}_*.csv"))
            approval = next(entry for entry in logs if entry["agent"] == "approval" and entry["layer"] == layer)
            assert approval["mapping_sha256"] == hashlib.sha256(mapping.read_bytes()).hexdigest()
    artifacts = []
    for path in sorted(output_root.rglob("*")):
        if path.is_file():
            artifact = {"path": path.relative_to(output_root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            if path.suffix == ".parquet":
                frame = pd.read_parquet(path)
                artifact.update(rows=len(frame), columns=frame.columns.tolist())
            artifacts.append(artifact)
    manifest = {"evidence_type": "automated_offline_review", "run_id": trace["run_id"], "status": trace["status"],
                "approval_method": "Scripted stdin after blocking assertions; NOT human sign-off or a live LLM run",
                "api_calls": 0, "gates": gates, "artifacts": artifacts}
    (output_root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output_root / "terminal.txt").write_text("AUTOMATED OFFLINE REVIEW; INPUTS PROVIDED BY TEST HARNESS, NOT A HUMAN\n" + "".join(transcript), encoding="utf-8")