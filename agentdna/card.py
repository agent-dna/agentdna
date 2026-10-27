def build_actor_card_payload(
    actor_id: str,
    policy: str,
) -> dict:
    return {
        "type": "actor_card",
        "id": actor_id,
        "policy": policy,
    }


def build_user_card_payload(
    user_id: str,
) -> dict:
    return {
        "type": "user_card",
        "id": user_id,
    }


def build_swarm_delegate_payload(
    parent_agent_id: str, sub_agent_id: str, sub_agent_name: str
) -> dict:
    return {
        "type": "swarm_delegate",
        "parentAgentId": parent_agent_id,
        "subAgentId": sub_agent_id,
        "subAgentName": sub_agent_name,
    }
