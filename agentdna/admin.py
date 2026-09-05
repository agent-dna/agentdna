from urllib.parse import urljoin

def request_agent_whitelist_check(agentdna_admin_url: str, agent_id: str) -> bool:
    """
    Request the AgentDNA Admin server to check if the agent is whitelisted.
    """
    import requests

    url = urljoin(agentdna_admin_url, f"/agent-admin/v1/whitelist/{agent_id}")
    response = requests.get(url)

    if response.status_code == 200:
        data = response.json()
        return data.get("data", False)
    else:
        raise Exception(
            f"Failed to check whitelist status for agent {agent_id}. "
            f"Status code: {response.status_code}, Response: {response.text}"
        )