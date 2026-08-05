from .core import AgentDNA

# `agentdna.login(...)` is the same callable as `AgentDNA.login(...)`:
# it logs the human in and returns an AgentDNA already carrying its run_id.
login = AgentDNA.login

__all__ = ["AgentDNA", "login"]
