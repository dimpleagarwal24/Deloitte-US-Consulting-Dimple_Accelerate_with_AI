import json
from unittest.mock import Mock

import pandas as pd
import pytest

import cli
from agents import reporter


@pytest.mark.parametrize("sql", [
    "DROP TABLE sales_gold", "SELECT * FROM read_csv_auto('secret.csv')",
    "SELECT * FROM sales_gold; SELECT 1", "SELECT * FROM unknown",
    "SELECT getenv('GITHUB_TOKEN') FROM sales_gold", "SELECT * FROM sales_gold CROSS JOIN sales_gold AS other",
    "WITH other AS (SELECT * FROM sales_gold) SELECT * FROM other",
    "SELECT * FROM information_schema.tables", "SELECT * FROM sales_gold LIMIT 10000000",
])
def test_reject_unsafe_or_unbounded_sql(sql):
    with pytest.raises(ValueError):
        reporter.validate_sql(sql, {"sales_gold"})


def test_sql_execution_and_grounded_summary():
    tables = {"sales_gold": pd.DataFrame({"category": ["Electronics", "Sports"], "total_revenue": [100, -20]})}
    result, sql, fallback = reporter.execute_query(tables, "SELECT category, total_revenue FROM sales_gold ORDER BY total_revenue DESC")
    assert result.total_revenue.sum() == 80
    assert not fallback
    assert "LIMIT 500" in sql
    summary = reporter.executive_answer(result, fallback)
    assert "Electronics" in summary and "100.00" in summary


def test_failed_sql_is_explicit_preview():
    tables = {"sales_gold": pd.DataFrame({"total_revenue": [100]})}
    result, _, fallback = reporter.execute_query(tables, "SELECT nonexistent FROM sales_gold")
    assert fallback
    assert "not a verified answer" in reporter.executive_answer(result, fallback)


def test_response_parser():
    response = 'SQL:\n```sql\nSELECT * FROM sales_gold\n```\nNARRATIVE:\nDraft only.\nCHARTS:\n```json\n[{"type":"bar","x":"category","y":"total_revenue","title":"Revenue"}]\n```'
    sql, narrative, charts = reporter.parse_response(response)
    assert sql == "SELECT * FROM sales_gold"
    assert narrative == "Draft only."
    assert charts[0]["type"] == "bar"


def test_bad_chart_json_uses_default():
    assert reporter.parse_response("SQL:\nSELECT * FROM sales_gold\nNARRATIVE:\nDraft\nCHARTS:\ninvalid")[2] == []


def test_report_json_retains_reproduction_metadata(monkeypatch):
    frame = pd.DataFrame({"category": ["Electronics"], "revenue": [100]})
    charts = [{"type": "bar", "x": "category", "y": "revenue", "title": "Revenue"}]
    monkeypatch.setattr(reporter, "load_tables", Mock(return_value={"sales_gold": frame}))
    monkeypatch.setattr(reporter, "call_llm", Mock(return_value=
                        'SQL:\nSELECT * FROM sales_gold\nNARRATIVE:\nDraft\nCHARTS:\n' + json.dumps(charts)))
    monkeypatch.setattr(reporter, "render_html", Mock(return_value="<html></html>"))
    monkeypatch.setattr(reporter, "AuditLogger", Mock())
    write = Mock()
    monkeypatch.setattr(type(reporter.REPORTS_DIR), "write_text", write)
    reporter.generate_report(["sales_gold_test.parquet"], "Revenue", "test")
    summary = json.loads(write.call_args_list[1].args[0])
    assert summary["rows_returned"] == 1
    assert summary["gold_tables"] == ["sales_gold"]
    assert summary["chart_specs"] == charts
    assert summary["unverified_model_narrative"] == "Draft"
    assert summary["data"] == frame.to_dict("records")


def test_report_escapes_content_and_handles_negative_pie():
    result = pd.DataFrame({"category": ["<script>alert(1)</script>", "Sports"], "total_revenue": [100, -20]})
    page = reporter.render_html(result, '<img src=x onerror="alert(1)">', "SELECT * FROM sales_gold", "test", False,
                                [{"type": "pie", "x": "category", "y": "total_revenue", "title": "Revenue"}])
    assert '<img src=x onerror="alert(1)">' not in page
    assert "<script>alert(1)</script>" not in page
    assert "plotly" in page.lower()
    assert '"type":"bar"' in page


def test_gate_reprompts_and_revalidates_after_edit(monkeypatch, capsys):
    actions = iter(["invalid", "e", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(actions))
    display = Mock()
    editor = Mock()
    monkeypatch.setattr(cli, "display_sttm", display)
    monkeypatch.setattr(cli, "edit_sttm", editor)
    assert cli.hitl_gate("bronze", "mapping.csv")
    assert display.call_count == 3
    editor.assert_called_once_with("mapping.csv")
    assert "Please type y, e, or n" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["n", "no", "abort", " ABORT "])
def test_gate_abort(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", Mock(side_effect=[answer]))
    monkeypatch.setattr(cli, "display_sttm", Mock())
    assert not cli.hitl_gate("silver", "mapping.csv")


def test_gate_invalid_mapping_cannot_be_approved(monkeypatch):
    monkeypatch.setattr(cli, "display_sttm", Mock(side_effect=ValueError("bad mapping")))
    actions = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda _: next(actions))
    assert not cli.hitl_gate("bronze", "mapping.csv")


def test_gate_eof_aborts(monkeypatch):
    monkeypatch.setattr(cli, "display_sttm", Mock())
    monkeypatch.setattr("builtins.input", Mock(side_effect=EOFError))
    assert not cli.hitl_gate("gold", "mapping.csv")


def test_malformed_chart_fields_use_default():
    frame = pd.DataFrame({"category": ["Electronics"], "revenue": [100]})
    page = reporter.render_html(frame, "Revenue", "SELECT * FROM sales_gold", "test", False,
                                [{"type": [], "x": "category", "y": "revenue"}])
    assert '"type":"bar"' in page


@pytest.mark.parametrize("status,exit_code", [("completed", 0), ("aborted", 1)])
def test_cli_main_success_and_abort(monkeypatch, status, exit_code):
    run = Mock(return_value=cli.PipelineState(run_id="test", business_intent="Revenue", status=status))
    monkeypatch.setattr(cli, "run_pipeline", run)
    assert cli.main(["--files", "sales.csv", "--intent", "Revenue"]) == exit_code
    run.assert_called_once_with(["sales.csv"], "Revenue")


@pytest.mark.parametrize("error", [ValueError("Invalid mapping"), OSError("Missing file"), EOFError(), KeyboardInterrupt()])
def test_cli_main_expected_failures(monkeypatch, error):
    monkeypatch.setattr(cli, "run_pipeline", Mock(side_effect=error))
    assert cli.main(["--files", "sales.csv", "--intent", "Revenue"]) == 1


def test_cli_main_unexpected_failure_does_not_leak_details(monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_pipeline", Mock(side_effect=RuntimeError("sensitive internal details")))
    assert cli.main(["--files", "sales.csv", "--intent", "Revenue"]) == 1
    assert "sensitive internal details" not in capsys.readouterr().out


def test_cli_prompts_for_missing_arguments(monkeypatch):
    responses = iter(['"sales data.csv" products.csv', "Revenue"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    run = Mock(return_value=cli.PipelineState(run_id="test", business_intent="Revenue", status="completed"))
    monkeypatch.setattr(cli, "run_pipeline", run)
    assert cli.main([]) == 0
    run.assert_called_once_with(["sales data.csv", "products.csv"], "Revenue")


def test_editor_uses_argument_list_not_shell(monkeypatch):
    monkeypatch.setenv("EDITOR", '"custom-editor" --wait')
    launch = Mock()
    monkeypatch.setattr(cli.subprocess, "run", launch)
    cli.edit_sttm("mapping.csv")
    assert launch.call_args.args[0][:2] == ["custom-editor", "--wait"]
    assert launch.call_args.kwargs["shell"] is False


@pytest.mark.parametrize("intent", ["", " " * 10, "a" * 2001])
def test_invalid_intent_fails_before_io(intent):
    with pytest.raises(ValueError, match="Business intent"):
        cli.run_pipeline([], intent)