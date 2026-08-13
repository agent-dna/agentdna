from dataclasses import replace
import json
import sqlite3

import mcp_server as seed_mcp_server
import sqlite_analytics_agent.mcp_server as mcp_server
import sqlite_analytics_agent.seed_data as seed_data
from mcp_server import _validate_query, initialize_database, sqlite_list_tables
from runner import log_result, print_human_result
from agents.sqlite_agent.agent import build_llm


def test_database_is_initialized_and_discoverable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "settings", replace(mcp_server.settings, database_path=tmp_path / "analytics.db"))
    initialize_database()
    assert sqlite_list_tables()["tables"] == ["customers", "order_items", "orders", "products"]


def test_writes_are_rejected() -> None:
    try:
        _validate_query("DELETE FROM orders")
    except ValueError as error:
        assert "Only SELECT" in str(error)
    else:
        raise AssertionError("destructive query was not rejected")


def test_seed_script_initializes_a_schema_less_database_idempotently(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "analytics.db"
    sqlite3.connect(database_path).close()
    seeded_settings = replace(mcp_server.settings, database_path=database_path)
    monkeypatch.setattr(mcp_server, "settings", seeded_settings)
    monkeypatch.setattr(seed_mcp_server, "settings", seeded_settings)
    monkeypatch.setattr(seed_data, "settings", seeded_settings)
    first_seed = seed_data.seed_database()
    second_seed = seed_data.seed_database()
    assert first_seed == second_seed == {"customers": 3, "products": 3, "orders": 4, "order_items": 5}


def test_runner_prints_completed_execution_as_json(capsys) -> None:
    log_result({"agent_id": "sqlite-analytics-agent", "result": "analysis complete"}, "Analyse orders")
    output = json.loads(capsys.readouterr().out)
    assert output["event_type"] == "agent.execution.completed"
    assert output["task"] == "Analyse orders"
    assert output["result"] == "analysis complete"


def test_runner_prints_human_readable_result(capsys) -> None:
    print_human_result(
        {"execution_id": "execution-123", "database": "data/analytics.db", "result": "analysis complete"},
        "Analyse orders",
    )
    output = capsys.readouterr().out
    assert "SQLite Analytics Agent" in output
    assert "Execution ID: execution-123" in output
    assert "analysis complete" in output


def test_openai_gateway_url_is_normalized_to_v1(monkeypatch) -> None:
    openai_settings = replace(
        mcp_server.settings,
        llm_backend="openai",
        openai_api_key="test-key",
        openai_model="qwen3-coder:30b",
        openai_base_url="https://llm.agentdna.io",
    )
    monkeypatch.setattr("agents.sqlite_agent.agent.settings", openai_settings)
    llm = build_llm()
    assert llm.model == "qwen3-coder:30b"
    assert llm.base_url == "https://llm.agentdna.io/v1"