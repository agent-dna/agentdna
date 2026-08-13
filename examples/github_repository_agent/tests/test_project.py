from config import settings
from agents.github_agent import GitHubRepositoryAgent
from runner import print_human_result


def test_agent_has_stable_identity() -> None:
    assert GitHubRepositoryAgent.agent_id == "github-repository-agent"
    assert settings.agent_id == GitHubRepositoryAgent.agent_id


def test_print_human_result_displays_readable_report(capsys) -> None:
    print_human_result(
        {
            "execution_id": "execution-123",
            "repository": "octo-org/example-repository",
            "result": "The repository has two pull requests awaiting review.",
        },
        "Review open pull requests.",
    )

    output = capsys.readouterr().out
    assert "GitHub Repository Agent" in output
    assert "Execution ID: execution-123" in output
    assert "Repository: octo-org/example-repository" in output
    assert "Task: Review open pull requests." in output
    assert "Analysis" in output
    assert "two pull requests awaiting review" in output