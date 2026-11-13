import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

import pytest

from core import audit, config, llm
from core.state import PipelineState


def test_state_lists_are_independent():
    first = PipelineState(run_id="first", business_intent="Revenue")
    second = PipelineState(run_id="second", business_intent="Revenue")
    first.input_files.append("sales.csv")
    assert second.input_files == []
    assert first.report_path is None


def test_ensure_dirs():
    with patch.object(config.Path, "mkdir") as mkdir:
        config.ensure_dirs()
    assert mkdir.call_count == 9
    assert all(call.kwargs == {"parents": True, "exist_ok": True} for call in mkdir.call_args_list)


def test_run_id_has_readable_timestamp_and_collision_protection():
    fixed = datetime(2026, 11, 12, 10, 30, tzinfo=timezone.utc)
    with patch.object(audit, "datetime") as clock:
        clock.now.return_value = fixed
        first, second = audit.new_run_id(), audit.new_run_id()
    assert first.startswith("20261112_103000_")
    assert first != second


def test_audit_appends_json():
    opener = mock_open()
    with patch.object(audit.Path, "open", opener):
        logger = audit.AuditLogger("test_run")
        logger.log("bronze", "completed", rows=3)
    opener.assert_called_once_with("a", encoding="utf-8")
    entry = json.loads(opener().write.call_args.args[0])
    assert entry["rows"] == 3
    assert entry["run_id"] == "test_run"
    assert entry["agent"] == "bronze"
    assert datetime.fromisoformat(entry["timestamp"]).utcoffset().total_seconds() == 0


def test_audit_reads_entries():
    with patch.object(audit.Path, "open", return_value=io.StringIO('{"action":"done"}\n\n')):
        assert audit.AuditLogger("test_run").get_logs() == [{"action": "done"}]


def test_missing_audit_is_empty():
    with patch.object(audit.Path, "open", side_effect=FileNotFoundError):
        assert audit.AuditLogger("test_run").get_logs() == []


@pytest.mark.parametrize("run_id", ["../escape", "", "a/b", "a\\b"])
def test_audit_rejects_unsafe_run_ids(run_id):
    with pytest.raises(ValueError, match="run_id"):
        audit.AuditLogger(run_id)


def test_llm_uses_github_and_no_implicit_retries(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "github")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "test-token")
    client = Mock()
    client.invoke.return_value.content = "answer"
    constructor = Mock(return_value=client)
    with patch.dict("sys.modules", {"langchain_openai": SimpleNamespace(ChatOpenAI=constructor)}):
        assert llm.call_llm("question") == "answer"
    assert constructor.call_args.kwargs["max_retries"] == 0
    assert constructor.call_args.kwargs["model"].startswith("openai/")
    client.invoke.assert_called_once_with("question")


@pytest.mark.parametrize("provider,token", [("anthropic", "test"), ("github", ""), ("github", "your_github_token_here")])
def test_llm_rejects_unconfigured_provider(monkeypatch, provider, token):
    monkeypatch.setattr(config, "LLM_PROVIDER", provider)
    monkeypatch.setattr(config, "GITHUB_TOKEN", token)
    with pytest.raises(llm.LLMError):
        llm.call_llm("question")


def test_llm_errors_do_not_expose_provider_details(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "github")
    monkeypatch.setattr(config, "GITHUB_TOKEN", "test-token")
    constructor = Mock(side_effect=RuntimeError("private credential"))
    with patch.dict("sys.modules", {"langchain_openai": SimpleNamespace(ChatOpenAI=constructor)}):
        with pytest.raises(llm.LLMError) as error:
            llm.call_llm("question")
    assert "private credential" not in str(error.value)


def test_offline_responses_do_not_construct_a_client(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_TOKEN", "")
    constructor = Mock(side_effect=AssertionError("Offline mode attempted network access"))
    with patch.dict("sys.modules", {"langchain_openai": SimpleNamespace(ChatOpenAI=constructor)}):
        with llm.fixture_responses({"bronze": "fixture mapping"}):
            assert llm.response_source() == "review_fixture"
            assert llm.call_llm("prompt", stage="bronze") == "fixture mapping"
        assert llm.response_source() == "github_models"
    constructor.assert_not_called()


def test_missing_offline_response_fails_closed():
    with llm.fixture_responses({"bronze": "fixture mapping"}):
        with pytest.raises(llm.LLMError, match="fixture"):
            llm.call_llm("prompt", stage="gold")


def test_offline_context_resets_after_error():
    with pytest.raises(RuntimeError):
        with llm.fixture_responses({"bronze": "mapping"}):
            raise RuntimeError("test")
    assert llm.response_source() == "github_models"