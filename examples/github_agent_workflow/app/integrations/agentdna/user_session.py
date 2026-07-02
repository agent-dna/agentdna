"""
Per-submitter user identity helper.

A UserSession owns a single Human AgentDNA instance and is responsible for:

    User.build()  -> IntentWorkflow
    User.handle() -> verify
    ...
    User.handle(final_workflow)
    User.create_workflow_provenance(final_workflow)

No envelope/context abstractions remain. The IntentWorkflow itself is the
artifact carried throughout the graph.
"""

from __future__ import annotations

import json
import os
import threading

from typing import Any, Dict, Optional

import structlog

from agentdna.core import AgentDNA
from agentdna.types import (
    ACTOR_TYPE_AGENT,
    ACTOR_TYPE_HUMAN,
    HandleResult,
    IntentWorkflow,
)

from .registry import is_agentdna_enabled

logger = structlog.get_logger(__name__)


_USER_CACHE: Dict[str, AgentDNA] = {}
_USER_CACHE_LOCK = threading.Lock()


def _alias_for(
    submitted_by: Optional[str],
    submitter_email: Optional[str],
) -> str:
    return submitter_email or "sample@example.com"



def _get_or_make_user(
    alias: str,
) -> AgentDNA:

    if alias in _USER_CACHE:
        return _USER_CACHE[alias]

    with _USER_CACHE_LOCK:

        if alias in _USER_CACHE:
            return _USER_CACHE[alias]

        user = AgentDNA(
            name=alias,
            type=ACTOR_TYPE_HUMAN,
            api_key=os.environ.get(
                "AGENTDNA_API_KEY",
                "",
            ),
        )

        _USER_CACHE[alias] = user

        logger.info(
            "agentdna_user_ready",
            actor_id=user.get_actor_id(),
            card_id=user.card_id,
        )

        return user


class UserSession:

    def __init__(
        self,
        user: AgentDNA,
        workflow: IntentWorkflow,
        handle_result: HandleResult,
    ):
        self.user = user
        self.workflow = workflow
        self.handle_result = handle_result

    @property
    def user_id(self) -> str:
        return self.user.get_actor_id()

    @classmethod
    async def open(
        cls,
        intent: Dict[str, Any],
        *,
        submitted_by: Optional[str] = None,
        submitter_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        first_agent: Optional[AgentDNA] = None,
    ) -> Optional["UserSession"]:

        if not is_agentdna_enabled():
            return None

        alias = _alias_for(
            submitted_by,
            submitter_email,
        )

        try:
            user = _get_or_make_user(
                alias,
            )

            if first_agent is None:
                raise ValueError(
                    "first_agent must be supplied"
                )

            workflow = user.build(
                recipient_actor_id=first_agent.get_actor_id(),
                recipient_actor_name=first_agent.name,
                recipient_actor_type=ACTOR_TYPE_AGENT,
                payload=json.dumps(intent),
            )

            handle_result = user.handle(
                workflow,
            )

            if not handle_result.verification.valid:

                logger.warning(
                    "user_initial_workflow_invalid",
                    issues=[
                        issue.reason
                        for issue in handle_result.verification.issues
                    ],
                )

                return None

            return cls(
                user=user,
                workflow=workflow,
                handle_result=handle_result,
            )

        except Exception as exc:

            logger.warning(
                "agentdna_user_open_failed",
                error=str(exc),
            )

            return None

    def complete(
        self,
        workflow: IntentWorkflow,
    ) -> bool:

        try:

            handle_result = self.user.handle(
                workflow,
            )

            if not handle_result.verification.valid:

                logger.warning(
                    "user_final_workflow_invalid",
                    issues=[
                        issue.reason
                        for issue in handle_result.verification.issues
                    ],
                )

                return False

            workflow_card_id = (
                self.user.create_workflow_provenance(
                    workflow
                )
            )

            logger.info(
                "workflow_provenance_created",
                workflow_card_id=workflow_card_id,
            )

            return True

        except Exception as exc:

            logger.warning(
                "workflow_completion_failed",
                error=str(exc),
            )

            return False