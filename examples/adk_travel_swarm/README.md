# ADK Travel Swarm

A swarm of four Google ADK agents researches a trip. Gemini is the LLM, and the agents' tools come from a FastMCP server that uses public data needing no API keys. Every agent has its own AgentDNA identity, every MCP tool call is signed as the agent that made it, and every hand-off between agents is a signed hop. The whole run ends as one verifiable chain of custody.

## Architecture

```text
              ┌──────────── hand-offs (any to any) ────────────┐
              ▼                                                 ▼
coordinator ⇄ weather_agent ⇄ destination_agent ⇄ money_agent      Google ADK + Gemini
     │              │                 │                  │
     └──────────────┴───── AgentDNA MCP gateway ─────────┘          one session per agent
                                    │
                         travel MCP server (FastMCP 3.4.7 + AgentDNA middleware)
                                    │
             Open-Meteo · Wikipedia · Frankfurter (ECB) · local time zones
```

**The swarm.** `coordinator` takes the request. Any member can hand control to any other with ADK's `transfer_to_agent`: ADK allows transfers between a parent, its children, and the children as peers. Whoever is active when the work is done gives the final answer.

| Agent | MCP tools | Data source (no API key) |
|---|---|---|
| `coordinator` | `geocode` | Open-Meteo geocoding |
| `weather_agent` | `geocode`, `weather_forecast` | Open-Meteo |
| `destination_agent` | `wikipedia_summary`, `local_time`, `geocode` | Wikipedia REST API, IANA time zones |
| `money_agent` | `convert_currency` | Frankfurter (European Central Bank rates) |

## How AgentDNA fits in

All of it is in `swarm.py`:

- **One gateway, one session per agent.** `MCP_GATEWAY = MCPGateway({"travel": ...})` runs inside the process. Each agent opens its own session with the user's intent, and its ADK `McpToolset`, unpatched, points at that session's URL, so each tool call is signed as that agent.
- **Signed hand-offs.** `HandoffRecorder.before_agent` is every agent's `before_agent_callback`, so it runs whenever an agent takes control. When control passes from A to B, it:
  1. has A sign a hand-off on top of A's chain, plus any of B's own earlier work not already in it;
  2. advances B's session to that hand-off (`session.aadvance(hop)`).

  B's next tool calls then build on the hand-off, and the chain reflects the swarm's actual flow.
- **The final answer** is signed by the agent that gave it, on top of the last hand-off.
- **The user deals only with the coordinator.** Every session starts from the user's request, but only the coordinator's first tool call is signed directly on it. The specialists are reached only through the coordinator's hand-offs. A parent that's already inside another parent is never written as a second, direct link, so the chain records exactly the links that happened. `tests/test_handoffs.py` replays a recorded run and checks this, edge by edge.

```py
async with MCP_GATEWAY.session(IDENTITIES["weather_agent"], incoming) as session:
    toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=session.url("travel"))
    )
```

## Setup

1. Use Python 3.10–3.12, and create and activate a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

2. Install the dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`, then set:
   - `GOOGLE_API_KEY`, a Gemini API key from Google AI Studio;
   - the AgentDNA values: `AGENTDNA_API_KEY`, `PROVENANCE_LAYER_URL`, `ADMIN_SERVER_URL` and `USER_NAME`.
4. Whitelist the four agent identities on the AgentDNA admin server. The MCP server refuses tool calls from agents that aren't whitelisted. Each agent's ID is printed when the agent is created (`agentdna.init.success`).

ADK may log a notice about missing Google Cloud default credentials; a Gemini API key doesn't need them.

## Run

```sh
# Terminal 1: the travel MCP server
python3 mcp_server.py

# Terminal 2: one request
python3 manual.py "I'm flying from New York to Lisbon this weekend. What's the weather, what is Lisbon known for, and how much is 500 USD in euros?"
```

`manual.py` prints:
- the hand-offs the swarm made;
- which agent answered;
- the travel brief.

It then records the run's chain on the provenance layer. Add `--json` to print the structured event instead. `automated.py` runs one of five built-in requests in a loop.

## Tests

`python -m pytest tests -q` needs no network and no keys. It checks:
- the tools' input validation and the local-time tool;
- the provenance chain of a replayed swarm run: every hand-off, every parent link, and that the user deals only with the coordinator.
