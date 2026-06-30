from typing import Any, Optional, TypedDict


class GithubAgentState(TypedDict, total=False):
    user_input: str
    task_spec: str

    worker_messages: list[Any]
    final_response: str
    error: Optional[str]

    _agentdna_user_id: str
    _agentdna_workflow: str

    _agentdna_terminal: bool
    _agentdna_terminal_reason: str
    _agentdna_phase: str