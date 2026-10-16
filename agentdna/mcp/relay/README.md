# Provenance Relay

Provenance Relay intercepts the `tools/call` requests between an MCP client and an MCP server, and injects the IntentWorkflow into the `_meta` field of each JSON-RPC request. It works with any MCP client library, unmodified.

```text
agent ──▶ MCP client ──▶ Provenance Relay ──▶ MCP server + AgentDNAMCPMiddleware
          (any library)  signs each call       verifies the call, runs the tool,
                         verifies each answer  signs the answer
```

## Why a relay

An MCP server running `AgentDNAMCPMiddleware` refuses tool calls that don't carry a signed IntentWorkflow. Something on the agent's side has to sign each call and attach it. Doing that inside each MCP client library means patching each library's internals. The relay does it at the protocol boundary instead: the agent's MCP client talks to the relay as if it were the MCP server, and the relay talks to the real server.

## Quick start

```py
from langchain_mcp_adapters.client import MultiServerMCPClient

from agentdna.core import AgentDNA
from agentdna.mcp.relay import ProvenanceRelay

AGENT = AgentDNA(name="github-agent", type="agent", ...)

# Once per process: a name for each MCP server, and its Streamable HTTP URL.
PROVENANCE_RELAY = ProvenanceRelay({"github": "http://127.0.0.1:8011/mcp"})

# Per task: a session on the IntentWorkflow the agent received.
async with PROVENANCE_RELAY.session(AGENT, incoming_workflow) as session:
    client = MultiServerMCPClient(
        {"github": {"transport": "streamable_http", "url": session.url("github")}}
    )
    output = await agent.ainvoke(...)  # every tool call is signed as AGENT

# After the session: the incoming workflow plus every tool call.
reply = AGENT.build(payload=str(output), previous_workflows=session.workflows)
```

Synchronous code uses `with` instead of `async with`. The examples in [`examples/`](../../../examples) use the relay with `langchain-mcp-adapters` (GitHub and RSS agents) and CrewAI's `MCPServerAdapter` (SQLite agent).

## What happens

**Opening a session** verifies the incoming workflow with the agent's own `AgentDNA` instance. If it fails, `session()` raises `WorkflowRejected` and no tool call can be made. Otherwise the relay opens a *run* with an unguessable ID, and `session.url(name)` is that run's MCP endpoint for the named server:

```text
http://127.0.0.1:{port}/runs/{run_id}/mcp/{name}
```

**On every `tools/call`** sent to that URL, the relay:

1. signs a request envelope as the session's agent, on top of the run's current chain (its *frontier*);
2. puts the workflow in `params._meta.agentdna`, keeping any other `_meta` fields, and forwards the request to the MCP server;
3. takes the server's signed successor workflow from the result's `_meta`, checks that it was signed directly on top of this request, and verifies it;
4. moves the frontier forward to the successor, and only then returns the result to the agent.

Every other MCP message (initialisation, `tools/list`, notifications, the GET stream, session headers) passes through untouched. Responses can be plain JSON or SSE; on an SSE stream only the call's response is rewritten, and progress notifications pass through as they arrive.

**Closing the session** returns the final frontier: `session.workflows` after the `with` block, or the return value of `close()` / `await aclose()` inside it.

## Concurrent tool calls

Tool calls that overlap in time form a *batch*. Every call in a batch is signed on the same frontier: the one that existed when the batch began. When the last call in the batch finishes, the frontier becomes the batch's successors, in the order the calls started. A call that never gets an answer (server unreachable, stream cut, client gone) is dropped from its batch without inventing a successor, and the other calls are unaffected.

## Failing closed

The agent never acts on a result that is missing from the chain. When the relay can't sign a request or can't verify an answer, the agent gets a JSON-RPC error with code `-32050` instead of the result.

A server passes a successor workflow down only for a success (`1000`) or a tool execution failure (`4001`). Any other failure, such as a whitelist, CoCA or CBAC check, is recorded on the provenance layer by the server, and a recorded IntentWorkflow is never passed forward: that chain has ended. The same goes for an answer the relay itself records because it fails verification. When the chain ends, so does the run:

- the agent sees the error as it was worded;
- every further tool call in the run is refused by the relay, without reaching the server;
- closing the session raises `ChainTerminated` instead of returning workflows, so there is nothing to build on or pass forward.

The relay can't stop the agent's framework in the middle of the failing call, because MCP clients hand error results back to the model as data. Closing the session is where the run stops, and until then nothing more reaches the server.

| What happened | What the agent gets | The run |
|---|---|---|
| Successor verified | the server's result | the frontier moves to the successor |
| Tool error, recorded by the server as a tool execution failure (`4001`) | the server's error result | the frontier moves to the failure envelope |
| No successor on a successful result, or one not built on this request | `-32050` error | goes on; the frontier is unchanged |
| Request can't be signed, or the server is unreachable | `-32050` error | goes on; the frontier is unchanged |
| Server refused the call: an error result without a `4001` successor, or a JSON-RPC error | the server's error, unchanged | **ends** |
| Successor fails verification | `-32050` error; the relay records the failure | **ends** |
| Any tool call after the run ended | `-32050` error | stays ended |

## API

`ProvenanceRelay(servers, *, run_ttl_seconds=3600.0, upstream_timeout_seconds=300.0)`

- `servers` maps a name to each MCP server's Streamable HTTP URL. Names may contain letters, digits, `-` and `_`.
- `session(agent, workflows)` returns a `RelaySession` for `agent` on the workflow(s) it received. It opens when the `with` block is entered.
- `close()` stops the relay. It also runs at exit.

Create one relay per process and reuse it. It holds no identity of its own: each session signs as the agent that opened it, so one relay serves every agent in the process.

`RelaySession`

| | |
|---|---|
| `url(name=None)` | The MCP URL to give the agent's MCP client for server `name`. `name` may be left out when the relay fronts a single server. |
| `workflows` | The final workflow(s), once the session is closed. Raises while it is open, and raises `ChainTerminated` if the run's chain ended. |
| `frontier()` / `await afrontier()` | The workflows the tool calls have produced so far. |
| `advance(workflows)` / `await aadvance(...)` | Moves the frontier forward to workflows the agent signed itself, for hops outside MCP such as a hand-off to another agent. The new workflows must contain the current frontier, and it is refused while calls are in flight. |
| `close(force=False)` / `await aclose(force=False)` | Closes the session and returns its final workflows. Refused while calls are in flight, unless `force`. Raises `ChainTerminated` instead if the run's chain ended. Leaving the `with` block closes it, with `force` when the block raised. |

Errors, all `RelayError`s (with `.status` and `.body`):

- `RelayError` when the relay refuses a session operation;
- `WorkflowRejected`, with `.status_code` (the AgentDNA verification code), when the incoming workflow fails verification;
- `ChainTerminated`, with `.reason` (as worded by whoever ended the chain) and `.status_code` when there is one, from closing a session, or reading its `workflows`, after the run's chain ended.

## Inside the process

The relay is a small Starlette application served by uvicorn on a background thread, with its own event loop, on a loopback port picked by the OS (port 0, so any number of relays can coexist and no privileged port is ever used). It starts on first use; there is nothing to install, start or configure.

Runs are opened and closed by calling the relay directly, not over HTTP. Its only HTTP surface is each run's MCP endpoint, reachable only through the run's unguessable URL. A run that sits idle for `run_ttl_seconds` is dropped, unless a call is in flight. The separate thread matters: the agent's MCP client talks to the relay over HTTP, and the agent may wait on it synchronously from inside its own event loop, which would deadlock a relay sharing that loop.

## Limits

- MCP servers must be reachable over Streamable HTTP. stdio servers are not supported yet.
- A JSON-RPC batch that contains a `tools/call` is refused.
- `upstream_timeout_seconds` bounds each request to an MCP server, except the server's long-lived GET notification stream.

## Code map

| Module | What it does |
|---|---|
| `client.py` | `ProvenanceRelay` and `RelaySession`: the public API |
| `config.py` | `ProvenanceRelayConfig`, validated when the relay is created |
| `embedded.py` | `EmbeddedRelay`: runs the app on its own thread and loop |
| `service.py` | `RunService`: open, frontier, advance and close a run |
| `runs.py` | `Run` and `RunRegistry`: in-memory runs, expired when idle |
| `app.py` | The HTTP surface: forwards MCP traffic, finds `tools/call` requests and their responses in JSON or SSE bodies |
| `propagation.py` | Signs a call's request, verifies its response, moves the frontier |

The batching is in `agentdna/mcp/context.py` (`AgentDNAContext`), and the canonical form of a signed `tools/call` is in `agentdna/mcp/payload.py`.
