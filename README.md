# AgentDNA

Security, Governance and Audit framework for AI Agents.

## The Problem with AI Agents

AI Agents are rapidly moving from experimental prototypes into production systems capable of making decisions autonomously. Unlike traditional automation, their behaviour depends on the instructions and context they receive. As Multi-Agent Systems (MAS) become more common, a single user request may pass through multiple autonomous Agents and external applications before reaching its final destination.

This introduces a new set of security concerns:

* Who created the initial intent of the workflow?
* Can every step in the workflow be traced back to the original requester?
* Was an Agent authorized to perform an action according to its policy?
* Has an Agent's policy been modified since it was deployed?
* How much an Agent has drifted from the initial intent?

AgentDNA was built to ensure that every autonomous decision can be identified, verified, authorized and audited.

## Pillars of AgentDNA

AgentDNA is built on the following pillars:

### 1. Chain of Custody Authentication (CoCA)

Chain of Custody Authentication (CoCA) captures every interaction between participants as a cryptographically signed Envelope.

Rather than treating a workflow as a collection of independent events, every new Envelope wraps the previous one, forming a nested chain that preserves the complete journey of an intent.

Every participant signs only the action they performed, creating a verifiable chain of custody from the original requester to the final outcome.

### 2. Context Based Access Control (CBAC)

CBAC evaluates the complete intent, verifies every signed participant in the chain and validates that the Agent's current policy permits the requested action. It does so by using a local Inference Engine that examines Agent's policy and with its intent, the initial user intent and calculates a Trust score for the Agent. Authorization changes from *are you allowed?* to *are you allowed, and what is the intent?*

### 3. Immutable Provenance

Every completed workflow can be committed to the Provenance Layer as an immutable provenance record.

This provides:

* Complete workflow history
* Cryptographic proof of every participant
* Auditability of every autonomous decision
* Tamper-evident workflow storage

## Core Data Structures

AgentDNA revolves around the following core Data Structures.

### Envelope

An `Envelope` captures a single interaction between two Actors.

Every Envelope is digitally signed by the sender and references its parent Envelope, allowing workflows to be represented as a cryptographically verifiable chain.

```json
{
  "from": "ID of the actor building the envelope",
  "to": "(Optional) ID of the actor who is recieving the envelope",
  "payload": "{\"action\":\"produce_task_spec\"}",
  "epoch": "Unix timestamp of Envelope formation",
  "status_code": "Represents the status code for errors occured while the envelope was formed",
  "signature": "Hex encoded signature by the actor building the envelope",
  "parent_envelope": "List of Envelopes upon which the current envelope is being built upon",
  "hash": "Hash of the Envelope's content which is signed and verified"
}
```

Following status codes are set:

| Codes      | Description                                             |
| ---------- | ------------------------------------------------------- |
| 1000       | No issues found                                         |
| 1001       | Agent not whitelisted                                   |
| 1002       | Error while performing Agent whitelist verification     |
| 2001       | Envelope verification failed under `light` mode         |
| 2002       | Envelope verification failed under `heavy` mode         |
| 2003       | Envelope verification failed under `boundary` mode      |
| 2999       | CoCA verification failure for unknown reason            |
| 4001       | MCP Tool Execution error. A special case where the workflow isn't interrupted |
| 4002       | Generic Middleware Execution error                      |
---

### IntentWorkflow

An `IntentWorkflow` represents the complete lifecycle of an intent.

Rather than storing a sequence of events, AgentDNA stores the latest `Envelope`. Every Envelope recursively references its parent, allowing the entire chain of custody to be reconstructed from a single object.

IntentWorkflow is a DTO (Data Transfer Object) that is passed between Actors of the Agentic Workflow.

For example:

```json
{
  "type": "intent_workflow",
  "version": "1.0",
  "remarks": "",
  "info": {},
  "envelope": {
    "from": "worker_actor_id",
    "to": "coordinator_actor_id",
    "payload": "{\"status\":\"completed\"}",
    "epoch": 1782668370,
    "status_code": 1000,
    "hash": "<Hash of the Envelope's content>",
    "signature": "...",
    "parent_envelope": [{
      "from": "coordinator_actor_id",
      "to": "worker_actor_id",
      "payload": "{\"action\":\"produce_task_spec\"}",
      "epoch": 1782668362,
      "hash": "<Hash of the Envelope's content>",
      "status_code": 1000,
      "signature": "...",
      "parent_envelope": [{
        "... previous envelope ..."
      }]
    }
  }
}
```

Each `parent_envelope` links to the previous interaction, forming a nested chain that captures the complete journey of an intent from its origin to its final outcome.


### Cards

Cards are immutable records stored on the Provenance Layer. They represent persistent identities and completed workflows that can be independently retrieved and verified. These can thought of as immutable append-log files, where the only way to edit information is to append new information. This allows us to version check on the changes made on a card. One such instance is Agent Card, where every entry reflects the policy change of the Agent.


#### UserCard

A `UserCard` represents a Human identity.

```python
@dataclass
class UserCard:
    type: str
    id: str
    metadata: dict[str, Any] = field(default_factory=dict)
```

| Field      | Description                                             |
| ---------- | ------------------------------------------------------- |
| `type`     | Card type. Supported values: `human`, `agent` and `app` |
| `id`       | Unique identifier of the User Card.                     |
| `metadata` | Optional metadata associated with the user identity.    |

#### AgentCard

An `AgentCard` represents a deployed AI Agent.

```python
@dataclass
class AgentCard:
    type: str
    id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    policy: str = ""
```

| Field      | Description                                                                                                                                    |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `type`     | Card type. For example, `agent`.                                                                                                               |
| `id`       | Unique identifier of the Agent Card.                                                                                                           |
| `metadata` | Optional metadata describing the deployed Agent.                                                                                               |
| `policy`   | The Agent's policy document captured at deployment time. |

#### Workflow Provenance Card

A Workflow Provenance Card represents a completed `IntentWorkflow`.

Unlike User and Agent Cards, which represent identities, a Workflow Provenance Card captures a complete execution of an intent.

It stores the final `IntentWorkflow`, including the nested Envelope chain and all associated signatures. Since each Envelope references its parent, the stored workflow preserves the complete chain of custody from the initiating Human through every participating Agent and application to the final response.

This immutable record allows the entire workflow to be independently verified and audited at any point in the future.

## Getting Started

To install agentdna, run the following:

```bash
pip install agent-dna
```

Let's start with a simple example involving a human and a single AI agent.

Suppose Alice wants an AI assistant to summarize a document.

```text
Alice (Human)
      │
      │ "Summarize this document."
      ▼
Assistant Agent
      │
      │ Generates summary
      ▼
Alice (Human)
```

Although this is a simple interaction, several questions arise:

- How does the assistant verify that the request actually originated from Alice?
- How can the assistant determine whether the request was modified in transit?
- How can Alice later establish what was requested and what response was produced?

AgentDNA addresses these questions through three core operations: `build()`, `handle()` and `record()`.

### Initialize the participants

Each participant in a workflow is represented by an `AgentDNA` instance.

For this example, we'll create one Human and one AI Agent.

```python
from agentdna.core import AgentDNA

user = AgentDNA(
    name="Alice",
    type="user",
    api_key="<Optional, only required for Beta (Explained later)>",
    provenance_layer_url = "<Optional, Provenance Layer URL>"
)

assistant = AgentDNA(
    name="Assistant",
    type="agent",
    api_key="<Optional, only required for Beta (Explained later)>",
    provenance_layer_url = "<Optional, Provenance Layer URL>",
    admin_server_url="<Optional, Admin Server URL. Used for agent whitelist verification>"
)
```

Supported Actor types are:

* `user`
* `agent`
* `tool`

### Securing Agent-User Interaction

**Step 1. Build the initial workflow**

Alice creates the first signed `Envelope` containing the user's request.

```python
workflow = user.build(
    payload='{"request":"Summarize this document."}'
)
```

The `workflow` now contains the initial signed Envelope.

```text
Alice ─────────▶ Assistant
```

**Step 2. Verify before acting**

Before processing the request, the Assistant verifies the workflow.

```python
from agentdna.error import RESULT_OK

verification_code = assistant.verify(workflow)

if verification_code != RESULT_OK:
    invalid_workflow_record = assistant.build(
        payload="invalid workflow received",
        verification_code=verification_code
    )

    # Record the details on the immutable Provenance Layer 
    assistant.record(invalid_workflow_record)
    
    raise RuntimeError("Invalid AgentDNA workflow")
```

This verifies the chain of custody and evaluates whether the request should be accepted according to the Assistant's policy.

Only after successful verification should the Assistant perform its work. When verification fails, the failure should be recorded before the request is rejected.

**Step 3. Build the response**

After generating the summary, the Assistant appends a new signed `Envelope` to the existing workflow.

```python
workflow = assistant.build(
    payload='{"summary":"..."}',
    previous_workflows=workflow_from_last_step,
)
```

The final workflow has the following structure:

```text
Alice ─────────▶ Assistant ─────────▶ Alice
```

Notice that the original request is preserved. The Assistant simply appends a new signed Envelope, extending the chain of custody.

**Step 4. Store the completed workflow**

After the interaction is complete, the workflow can be committed to the Provenance Layer.

```python
workflow_card_id = user.record(
    workflow
)
```

This creates an immutable Workflow Provenance Card containing the complete interaction between Alice and the Assistant.

The same pattern scales naturally to Multi-Agent Systems. Every participant follows the same sequence:

```text
Receive workflow
        │
        ▼
    verify()
        │
Perform work
        │
        ▼
    build()
        │
Forward workflow
```

As the workflow propagates between actors, each actor appends a signed `Envelope`. The resulting `IntentWorkflow` provides a verifiable record of the interactions and decisions that occurred along the execution path.

Workflow provenance is created for both successful and failed interactions between actors.

The same mechanism used to secure human-agent communication can also be applied to agent-agent communication.

### Securing Agent-Resource Interaction

Agentic applications frequently allow an agent to access external resources on behalf of a user. This introduces additional security risks: an agent may be manipulated by an untrusted actor or induced to access resources beyond the authority granted by the user.

This is where CoCA and CBAC become relevant.

We have already seen CoCA in action. CBAC provides an authorization decision for resource access based on the request context and configured authorization policies.

Consider the previous example with an MCP server added as the resource provider:

```text
Alice ─────────▶ Assistant ─────────▶ MCP Server
```

To secure this interaction, AgentDNA is added on both sides of the MCP connection:

- on the agent's side, the **Provenance Relay** signs every MCP tool call the agent makes (it handles only agent-to-MCP-server traffic, not communication between agents);
- on the MCP server's side, the **MCP Server Middleware** verifies each call before the tool runs, and signs its answer.

```text
Alice ──▶ Assistant ──▶ MCP client ──▶ Provenance Relay ──▶ MCP Server + AgentDNA Middleware
                        (unchanged)    signs each call       verifies the call, runs the tool,
                                       checks each answer    signs the answer
```

**1. Agent side: Provenance Relay**

Your agent keeps using the MCP client it already uses: `langchain-mcp-adapters`, CrewAI's `MCPServerAdapter`, `fastmcp.Client`, or any other. The only change is the URL you give that client: instead of the MCP server's URL, you give it a URL from a relay session. The relay runs inside your process, so there's no extra service to start. For every tool call, it:

1. signs the request as your agent and attaches the current `IntentWorkflow` to it;
2. forwards the request to the real MCP server;
3. checks the server's signed answer before your agent sees the result.

The relay is only for an agent's calls to MCP servers. Communication between agents, such as one agent handing a task or its result to another, doesn't go through it: secure it with `build()` and `verify()`, as shown in [Securing Agent-User Interaction](#securing-agent-user-interaction).

Using it takes three steps. Assume the AI Assistant uses LangGraph with `langchain-mcp-adapters`:

```py
from langchain_mcp_adapters.client import MultiServerMCPClient

from agentdna.core import AgentDNA
from agentdna.mcp.relay import ProvenanceRelay

AGENT = AgentDNA(...)

# 1. Once per process: give each MCP server a name, and its URL.
PROVENANCE_RELAY = ProvenanceRelay({"rss": "http://127.0.0.1:8013/mcp"})

# 2. For each task: open a session with the IntentWorkflow the Assistant received.
#    The session verifies that workflow before anything else happens.
async with PROVENANCE_RELAY.session(AGENT, existing_intent_workflow) as session:
    client = MultiServerMCPClient(
        {"rss": {"transport": "streamable_http", "url": session.url("rss")}}
    )
    workflow = create_react_agent(llm, await client.get_tools())
    result = await workflow.ainvoke({"messages": [HumanMessage(content=task)]})

# 3. After the session: session.workflows is the chain so far, including every tool call.
#    Build the Assistant's answer on it, and pass that to the next actor.
adna_workflow_from_agent = AGENT.build(
    payload=str(result["messages"][-1].content),
    previous_workflows=session.workflows,
)
```

Good to know:

- `session.url("rss")` uses the name from step 1. One relay can front several MCP servers: call `session.url(name)` for each.
- The agent doesn't have to call a tool. If it calls none, `session.workflows` is simply the workflow it received.
- If the incoming workflow fails verification, opening the session raises `WorkflowRejected`. If an MCP server's answer can't be verified, the agent gets an error instead of the result, so it never acts on output that's missing from the chain.
- If an MCP server refuses a tool call (its whitelist, CoCA or CBAC check fails), it records the refusal, and that chain has ended. The relay then refuses the session's further tool calls, and closing the session raises `ChainTerminated`: stop the run there, and don't build on or pass on its workflow.
- In synchronous code, use `with` instead of `async with`.
- MCP servers must be reachable over Streamable HTTP.

For how the relay works, see [agentdna/mcp/relay/README.md](agentdna/mcp/relay/README.md). For complete examples, see [GitHub Repository Agent](examples/github_repository_agent) (LangGraph), [RSS Research Agent](examples/rss_research_agent) (LangGraph, two agents) and [SQLite Analytics Agent](examples/sqlite_analytics_agent) (CrewAI).

**2. MCP Server Middleware**

`AgentDNAMCPMiddleware` intercepts protected MCP requests on the server side and performs security checks before allowing the underlying MCP handler to execute.

The security checks include:

- Agent Whitelisting
- CoCA Verification
- CBAC Verification

CBAC Verification is configurable. AgentDNA provides a [CBAC Service](https://github.com/agent-dna/cbac-server), but organizations can use their own Policy methodology and spin up a custom CBAC Server. The custom CBAC integration must provide a function matching the CbacFn contract:

```py
# agentdna/mcp/server/types.py

CbacFn: TypeAlias = Callable[
    [
        # Agent ID: Agent which is making the request to resource
        str,

        # MCP Server ID: The identifier or address of the MCP server sending the request
        str,

        # Tool Name: Name of the tool being invoked
        str,

        # Tool Argument: Arguments passed to the tool
        dict[str, Any],

        # User Intent: The intent of the user making the request
        str | None,

        # Tool Description: Description of the tool being invoked.
        # Its normally taken from the tool's comments. Hence, empty
        # values are accepted.
        str | None,

        # Intent ID: The identifier of the user intent associated with the request.
        str | None,
    ], 
    Awaitable[
        tuple[
            # Decision: The decision made by the CBAC server.
            # The value "allow" should be sent for an Allow decision since
            # the AgentDNA middleware relies on this value to enforce access control decisions.
            str,

            # Status Code: The HTTP-like status code representing the result of the CBAC decision.
            int,

            # Message Hash: It represent the hash of the message associated with the CBAC decision.
            # It is not encouraged to share the actual information contained in the message, since
            # it may have PII information, which should not be stored directly on the Provenance layer
            str
        ]
    ]
]
```

Use the `AgentDNAMCPMiddleware` as follows:

```py
# mcp_server.py

from fastmcp import FastMCP

from agentdna import AgentDNA
from agentdna.mcp.server.fastmcp import AgentDNAMCPMiddleware
# AgentDNA provided CBAC authorization function
# This is can be replaced with custom CBAC server's authorize function
from cbac import authorize

# Define AgentDNA instance for the MCP Server
mcp_server_dna = AgentDNA(
    name="Github MCP",
    type="tool",
    api_key="<AgentDNA API Key>",
    provenance_layer_url="<Provenance Layer URL, if any>",
    admin_server_url="<Admin Server URL, if any. Used for agent whitelist verification>",
)

mcp = FastMCP("<App Name>")

# Add the AgentDNA MCP Middlware
#
# AgentDNAMCPMiddleware takes the following arguments:
#   - MCP Server AgentDNA Instance
#   - (Optional) CBAC Authorization Function
mcp.add_middleware(
    AgentDNAMCPMiddleware(
        mcp_server_dna,
        authorize
    )
)

###### ---- Rest of the Business Logic remains unchanged ---- ######
```

Currently supported MCP server integrations:

- [FastMCP](https://github.com/PrefectHQ/fastmcp) - `from agentdna.mcp.server.fastmcp import AgentDNAMCPMiddleware`
- [MCP v2 Python SDK](https://github.com/modelcontextprotocol/python-sdks) - Refer [here](./examples/rss_research_agent/mcp_server_mcp2.py) for a complete usage example

## Authentication evidence 

AgentDNA can record how each request authenticated, including the credential type used on each hop and the authentication an MCP server uses when it calls its own backends.

Authentication evidence is disabled by default. When it is off, AgentDNA does not read or record authentication headers.

### Enable authentication evidence

Set this before starting the application:

```env
AGENTDNA_AUTH_EVIDENCE=true
```

### Credential handling

AgentDNA never stores raw credentials. When authentication evidence is enabled, detected credentials are converted into fingerprints.

### What is covered

AgentDNA currently observes:

- HTTP calls made with `httpx` or `requests`
- The following authentication headers:
  - `Authorization`
  - `X-API-Key`
  - `Api-Key`
- Authentication used by MCP servers when making HTTP calls to their own backends

AgentDNA's own outbound calls are not recorded.

### What is not covered

Authentication evidence does not currently cover:

- `aiohttp`
- Database connections
- Non-HTTP protocols
- Other HTTP clients not listed above

## Open Beta

AgentDNA is currently running an Open BETA programme. We welcome developers to explore the framework and share valuable feedback and report issues.

Head over to [AgentDNA Dashboard](https://dashboard.agentdna.io) to get started
