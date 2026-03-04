from __future__ import annotations
from typing import Any, Dict, Optional

from .trust import RubixTrustService
from .handler import RubixMessageHandler


class AgentDNA:
    """
    Single entry point for agent developers.

    They only call:
        dna.build(...)          # outbound message (request or response)
        await dna.handle(...)   # inbound message (request or response)

    Behavior is decided purely by `role`:

      role="host":
        - build  -> host_request      (host → remote)
        - handle -> host              (remote → host, verify + NFT)

      role="remote":
        - build  -> agent_response    (remote → host)
        - handle -> remote            (host → remote, verify host)
    """

    def __init__(
        self,
        alias: str,
        api_key: str,
        role: str = "remote",
        chain_url: Optional[str] = None,
        token_filename: str = "agent_info.json",
    ) -> None:
        if role not in ("host", "remote"):
            raise ValueError("AgentDNA.role must be 'host' or 'remote'")

        self.role = role
        self.trust = RubixTrustService(alias=alias,api_key=api_key, chain_url=chain_url)

        self.handler = RubixMessageHandler(
            alias=alias,
            api_key=api_key,
            token_filename=token_filename,
            trust_service=self.trust,
            enable_nft=(role == "host"),
        )

    # ------------------------------------------------------------------
    # BUILD: outbound messages
    # ------------------------------------------------------------------
    def build(self, **kwargs) -> Dict[str, Any]:
        """
        Host:
            dna.build(
                original_message=task,
                state=tool_context.state or {},
            )

        Remote:
            dna.build(
                original_message=original_message,
                response=agent_reply_text,
                host_block=host_block,
                extra={...},
            )
        """
        if self.role == "host":
            if "original_message" not in kwargs:
                raise ValueError("Host.build() requires original_message")
            return self.handler.build(
                kind="host_request",
                **kwargs,
            )

        # remote
        if "original_message" not in kwargs or "response" not in kwargs:
            raise ValueError("Remote.build() requires original_message and response")
        return self.handler.build(
            kind="agent_response",
            **kwargs,
        )

    # ------------------------------------------------------------------
    # HANDLE: inbound messages
    # ------------------------------------------------------------------
    async def handle(self, **kwargs) -> Dict[str, Any]:
        """
        Host:
            result = await dna.handle(
                resp_parts=resp_parts,
                original_task=task,
                remote_name=agent_name,
                execute_nft=True,
            )

        Remote:
            verify_info = await dna.handle(
                raw_text=raw,
                verify_mode="light",
            )
        """
        if self.role == "remote":
            # Remote handling inbound from host
            raw_text = kwargs.get("raw_text")
            if raw_text is None:
                raise ValueError("Remote.handle() requires raw_text")

            mode = kwargs.get("verify_mode", "light")
            # trust.verify_message_payload is sync, but it's fine to call
            return self.trust.verify_message_payload(
                raw_text=raw_text,
                mode=mode,
            )

        # Host handling inbound from remotes
        for required in ("resp_parts", "original_task", "remote_name"):
            if required not in kwargs:
                raise ValueError(f"Host.handle() requires {required}")
        return await self.handler.handle(
            kind="host",
            **kwargs,
        )