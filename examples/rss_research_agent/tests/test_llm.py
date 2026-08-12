from runner import print_human_result


def test_human_result_contains_all_report_sections(capsys) -> None:
    print_human_result(
        {
            "execution_id": "execution-123",
            "security_findings": "security analysis",
            "technology_findings": "technology analysis",
            "summary": "combined summary",
        },
        "test task",
    )
    output = capsys.readouterr().out
    assert "RSS Research Agent" in output
    assert "Execution ID: execution-123" in output
    assert "Worker Report 1" in output
    assert "Worker Report 2" in output
    assert "Synthesis" in output