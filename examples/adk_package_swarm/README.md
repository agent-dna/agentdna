# ADK Package Swarm

A swarm of four Google ADK agents does due diligence on Python packages: should we adopt this package, is this pinned version safe, what should we upgrade to? Gemini is the LLM, and the agents' tools come from a FastMCP server that uses public data needing no API keys. Every member has its own AgentDNA identity, every MCP tool call is signed as the member that made it, and every hand-off is a signed hop: the run ends as one verifiable chain of custody.

## Architecture

```text
              ┌──────────── hand-offs (any to any) ────────────┐
              ▼                                                 ▼
coordinator ⇄ package_agent ⇄ security_agent ⇄ ecosystem_agent      Google ADK + Gemini
                    │               │                 │
                    └──────── AgentDNA Provenance Relay ────┘       one session per turn
                                    │
                    package-intelligence MCP server                 FastMCP 3.4.7 + AgentDNA middleware + CBAC
                                    │
                    PyPI · pypistats · OSV.dev · deps.dev (OpenSSF Scorecard)
```

**The swarm.** `coordinator` takes the request. Any member can hand control to any other with ADK's `transfer_to_agent`: ADK allows transfers between a parent, its children, and the children as peers. Each specialist does its part with its own tools, then hands off to whichever specialist's part is still missing, or back to `coordinator`, which writes the final report with a verdict: adopt, adopt with care, or avoid.

| Agent | Does | MCP tools | Data source (no API key) |
|---|---|---|---|
| `coordinator` | Routes the work, writes the report | none | none |
| `package_agent` | Versions, licence, Python support, release activity, downloads | `package_info`, `release_history`, `download_stats` | PyPI JSON API, pypistats.org |
| `security_agent` | Known vulnerabilities and the versions that fix them | `find_vulnerabilities`, `vulnerability_details` | OSV.dev |
| `ecosystem_agent` | Dependency tree, repository health | `dependencies`, `project_health` | deps.dev, OpenSSF Scorecard |

Each specialist's MCP tools are filtered to its own (`swarm.py`).

## How AgentDNA fits in

All of the agent side is in `swarm.py`. The server side is `AgentDNAMCPMiddleware` in `mcp_server.py`: it verifies each tool call's chain, checks that the calling agent is whitelisted, has the CBAC service (the `cbac` package's `authorize`) decide whether the call fits the user's intent, and signs the answer.

- **An identity per member.** `identities()` creates the four members' `AgentDNA` instances, each registered with its policy from `policies/`. `manual.py` has the user sign the request, and records the finished chain.
- **Each turn is a relay session.** `Custody.before_agent` is every member's `before_agent_callback`, so it runs whenever a member takes control. It opens a `ProvenanceRelay` session for that member on the workflow it just received: the user's request for the coordinator's first turn, a hand-off after that. Opening the session verifies the workflow, and the member's tool calls through it are signed as that member, on top of it.
- **Hand-offs are signed.** When control passes from A to B, A's session closes and A signs the hand-off on top of what its turn produced, addressed to B. B's session then starts from that hand-off.
- **The answer** is signed by the member that gave it, on top of its last turn.

The chain is one line, from the user's request through every hand-off, tool call and answer, exactly as the swarm ran; `tests/test_swarm.py` checks it envelope by envelope.

### Why the extra `Custody` and `SessionToolset` classes?

In the simplest AgentDNA setup, your own code calls each agent, so it can hand each one the workflow it should build on:

```py
async with PROVENANCE_RELAY.session(AGENT, workflow_it_received) as session:
    ...  # the agent's MCP client uses session.url(...)
```

A swarm doesn't work that way. ADK decides when control passes from one agent to another (`transfer_to_agent`), inside its own runner. There's no line of your code between "A hands off" and "B starts" where you could give B a workflow. That causes two problems, and each class solves one:

1. **Nobody signs the hand-off.** `Custody` hooks into ADK's `before_agent_callback`, which ADK calls every time an agent takes control. There it does what your own code would do at a hand-off: it closes A's session, has A sign the hand-off, and opens B's session on it.
2. **B's MCP client is stuck on an old URL.** Each relay session has its own URL, and B gets a new session on every turn. But ADK creates each agent's `McpToolset`, URL included, once, when the agents are built, before anything has been handed off. `SessionToolset` stands in for it: on each turn, `Custody` points it at the new session's URL, and it gives ADK the tools from there. It also turns off ADK's caching of tools for the whole run, which would otherwise keep B on its first, closed session.

One hand-off, step by step:

```text
ADK: package_agent transfers to security_agent
 └─ Custody.before_agent("security_agent")
      1. package_agent's MCP client disconnects, and its session closes: the chain of its turn
      2. package_agent signs the hand-off on top of that chain, addressed to security_agent
      3. a session opens for security_agent on the hand-off, which verifies it
      4. security_agent's SessionToolset switches to that session's URL
ADK: security_agent runs; each of its tool calls is signed on top of the hand-off
```

**Would another framework need them?** It depends on two things: who moves control between agents, and when the MCP client is created.

| If... | then... |
|---|---|
| your code moves control (an orchestrator calling workers, explicit graph nodes) | you need neither class: open a session per agent on the workflow you pass it |
| the framework moves control (ADK, OpenAI Agents SDK hand-offs, LangGraph swarm, AutoGen group chats, CrewAI delegation) | you need a `Custody`-like hook at each hand-off, in whatever callback the framework offers |
| the MCP client is created once, when the agent is built (as ADK's `McpToolset` is) | you need a `SessionToolset`-like stand-in that follows each turn's session URL |
| the MCP client is created inside each turn | you don't: build it on the session's URL |

## Files

| File | What it is |
|---|---|
| `package_tools.py` | The tools, as plain async functions; no MCP or ADK code |
| `mcp_server.py` | Serves the tools over MCP (Streamable HTTP), behind AgentDNA's middleware and CBAC |
| `swarm.py` | The agents, their instructions and tools, their AgentDNA identities and chain of custody, and `run_swarm()` |
| `policies/` | Each member's AgentDNA policy, registered with its identity |
| `manual.py` | Runs one request from the command line |
| `automated.py` | Runs built-in requests in a loop |

## Setup

1. Use Python 3.10–3.12, and create and activate a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
```

2. Install the dependencies: `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env`, then set:
   - `GOOGLE_API_KEY`, a Gemini API key from Google AI Studio (`GEMINI_MODEL` picks the model);
   - the AgentDNA values: `AGENTDNA_API_KEY`, `PROVENANCE_LAYER_URL`, `ADMIN_SERVER_URL` and `USER_NAME`.
4. Whitelist `package_agent`, `security_agent` and `ecosystem_agent` on the AgentDNA admin server: the MCP server refuses tool calls from agents that aren't whitelisted. The coordinator calls no tools, so it doesn't need to be. Each agent's ID is printed when it is created (`agentdna.init.success`).

## Run

```sh
# Terminal 1: the package-intelligence MCP server
python3 mcp_server.py

# Terminal 2: one request
python3 manual.py "We're about to pin requests 2.31.0 in production. Is that safe, and should we upgrade?"
```

`manual.py` prints:
- the hand-offs the swarm made, and the tool calls each agent made;
- which agent answered;
- the report.

Add `--json` to print a structured result instead. `automated.py` runs one of five built-in requests in a loop.

### Example prompts

```sh
# An old pinned version with known vulnerabilities: what to upgrade to
python3 manual.py "Is pyyaml 5.3 still OK to use? What would we need to upgrade to?"

# No version given: the swarm checks the latest release
python3 manual.py "Due diligence on httpx: is the latest version safe to adopt, and how healthy is the project?"

# Two packages compared side by side
python3 manual.py "Compare flask and fastapi for a new internal service: maintenance, vulnerabilities and dependencies."

# A narrower question, mostly for one specialist
python3 manual.py "Our team wants to add pydantic. How many dependencies does it pull in, and is the project well maintained?"
```

A run makes a dozen or so Gemini calls. The agents retry Gemini's transient 429 and 503 errors with backoff, but a free-tier key can still run out of quota; a different `GEMINI_MODEL` has its own.

`config.py` silences three kinds of ADK notice that don't affect the run: its announcements of the experimental features it uses internally, its suggestion to configure context caching, and its failed probe for Google Cloud mTLS on each MCP connection. Every other ADK warning still shows.

## Tests

`python -m pytest tests -q` needs no network and no keys. It checks:
- the tools' input validation, and how they parse each API's responses;
- the swarm's wiring: every member can reach every other, and a request driven by a scripted model flows through ADK's real runner, transfers and tool calls, from the coordinator through each specialist and back;
- the chain of custody of that run, with stand-in identities and relay: each turn is a session on the workflow that member received, every hand-off is signed by the sender and addressed to the receiver, and the chain records exactly the flow that happened.
