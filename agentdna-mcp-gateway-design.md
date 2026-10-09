# AgentDNA MCP Gateway: Design

**Status:** v1 implemented in `agentdna/mcp/gateway/`. The GitHub repository
example (`examples/github_repository_agent`) uses it in place of the
client adapter.

## 1. Summary

The **AgentDNA MCP Gateway** sits between an agent's MCP client and an
upstream MCP server. For every `tools/call` it signs a request
`IntentWorkflow` as the agent, attaches it to `_meta`, verifies the
successor workflow the server returns and advances the causal frontier.
Everything else passes through unchanged.

Workflow propagation therefore happens at the protocol boundary. The agent
can use any Streamable HTTP MCP client, with no framework-specific patch
(`install_mcp_client`).

For a developer, the whole integration looks like this:

```py
MCP = AGENT.mcp_gateway({"github": "http://127.0.0.1:8011/mcp"})

async with MCP.session(incoming_workflow) as session:   # verifies incoming_workflow
    tools = await load_tools(session.url("github"))      # any MCP client, unpatched
    result = await agent.ainvoke(...)

reply = session.reply(str(result))                       # signed on top of every tool call
```

By default the gateway runs **inside the agent's process**, on a background
thread, and starts on first use. It can also run as a standalone service
(`agentdna-gateway`), reached with `AGENT.mcp_gateway(url=..., token=...)`
and the same session code.

## 2. Problem

The client adapters (`agentdna.mcp.client.langchain`, `.crewai`) tie
workflow propagation to particular frameworks:

- Each new client needs its own adapter.
- Each adapter monkey-patches framework internals that can change under it.
- Clients without an adapter cannot take part at all.

The gateway removes that coupling.

## 3. Goals

| Goal | Status |
| ---- | ------ |
| Forward MCP requests to an upstream MCP server | Done (Streamable HTTP) |
| Create or continue an `IntentWorkflow` for each `tools/call` | Done |
| Carry it in `_meta` using the existing metadata format | Done |
| Preserve unrelated metadata | Done |
| Extract and verify successor workflows | Done |
| Causal continuity across sequential and concurrent calls, per run | Done |
| Reuse `AgentDNA` for identity, signing, serialisation and verification | Done |
| Keep transport separate from workflow logic | Done: `app.py` vs `propagation.py` |
| Minimal developer effort | Done: one `mcp_gateway(...)` call plus a `session` block; no extra process or configuration (§6.1) |
| Several MCP servers per agent | Done: named servers, one session and one frontier across them |

## 4. Architecture

```text
Agent application                       (MCP.session(...) does 1 and 3)
   │  1. POST /runs  {workflows: [incoming IntentWorkflow]}
   │     ◀── {run_id, mcp_path, servers}
   │
   │  2. any MCP client ── /runs/{run_id}/mcp/{server} ──▶ ┌──────────────────────┐
   │                                                       │ AgentDNA MCP Gateway │──▶ MCP server "github"
   │                                                       │  ├─ HTTP pass-through │──▶ MCP server "jira"
   │                                                       │  ├─ propagation       │    (AgentDNAMCPMiddleware)
   │                                                       │  ├─ runs (frontiers)  │◀──
   │                                                       │  └─ AgentDNA (agent)  │
   │                                                       └──────────────────────┘
   │  3. DELETE /runs/{run_id}
   │     ◀── {workflows: [final frontier]}
   ▼
session.reply(payload)  =  AGENT.build(payload, previous_workflows=final_frontier)
```

The gateway is the same HTTP application in both deployment modes. In
process, it is served on a loopback port chosen by the OS (§6.1).

The gateway understands MCP, not only HTTP. It parses JSON-RPC
messages in both `application/json` and `text/event-stream` responses so it
can find `tools/call` requests and their responses. Everything else is
forwarded byte for byte: `initialize`, `tools/list`, notifications, the GET
notification stream, `DELETE`, and `mcp-session-id` and other headers.

## 5. Handing the workflow to the gateway: runs

This was the central open question. With the client adapters, the
agent sets `agentdna_context(AGENT, incoming_workflow)` and the adapter
reads it. A separate gateway process cannot see that context, and it cannot
start a chain on its own: `AgentDNA.build()` without a parent is allowed only
for `user` actors.

**Decision: explicit, run-scoped URLs.**

1. The agent application opens a run by sending the workflow(s) it received
   to `POST /runs`. The gateway verifies them before accepting (`422` if
   verification fails).
2. The gateway returns an unguessable `run_id` (`secrets.token_urlsafe(24)`)
   and the run's MCP path `/runs/{run_id}/mcp`.
3. The agent points any MCP client at that URL. Every `tools/call` on it
   builds on, and advances, that run's frontier and no other.
4. The agent closes the run with `DELETE /runs/{run_id}` and receives the
   final frontier. That is the equivalent of `ctx.workflows`, and the agent
   builds its reply on it.

The developer never sees runs or frontiers directly. `GatewaySession`
(from `MCP.session(...)`) wraps the protocol:

| Session API | Does |
| ----------- | ---- |
| `with` / `async with` | Opens the run (the gateway verifies the incoming workflow; `WorkflowRejected` carries the status code on failure). On exit, closes it. If the block raised, it force-closes and lets the original exception propagate |
| `session.url(name)` | The MCP URL to give the agent's MCP client for that server (`url()` works when there is only one) |
| `session.reply(payload)` / `await session.areply(...)` | Closes the run if needed and signs the agent's answer on the final frontier |
| `session.frontier()` / `afrontier()` | The workflows produced so far, without closing |

Verification of the incoming workflow moved into the session. The agent no
longer calls `AGENT.verify()` itself for this; doing so would only verify the
workflow twice.

**Alternatives considered:**

| Option | Why not |
| ------ | ------- |
| MCP `mcp-session-id` as the run key | Sessions do not map to runs. `langchain-mcp-adapters` opens a new session per tool call by default, and long-lived clients reuse one session across unrelated tasks. |
| Workflow in a request header | Workflows grow with every hop and soon exceed header size limits. Many clients only let you set headers when the client is built. |
| Run ID in a header on a fixed `/mcp` URL | Needs per-run header support in the client. The path works with every client that accepts a URL. |
| Fully transparent (no hand-off) | Impossible without losing the user's root intent: the gateway has no parent to build on. |

**Run lifecycle:**
- Runs live in memory (`RunRegistry`).
- A run is dropped after `AGENTDNA_GATEWAY_RUN_TTL_SECONDS` idle (default 3600), but never while a call is in flight.
- Closing a run with calls in flight returns `409` unless `?force=true`.

## 6. Deployment modes, identity and signing

### 6.1 In process (default)

`AGENT.mcp_gateway(servers)` returns an `MCPGateway`. On the first session
it starts the gateway application (`embedded.py`) under uvicorn, on a thread
of its own with its own event loop, bound to `127.0.0.1` on a port the OS
picks. It signs with **the agent's own `AgentDNA` instance**, so the gateway
cannot sign as the wrong identity.

- **Own thread and loop.** The agent's MCP client calls the gateway over
  HTTP, and `session.reply()` makes a synchronous call even when it is
  invoked from async code. A gateway on the agent's own loop would deadlock.
  Runs survive across `asyncio.run()` calls.
- **Private token.** The in-process gateway generates a random bearer token
  that only this process knows, so other local processes cannot open runs
  and have tool calls signed as the agent.
- **Host application untouched.** uvicorn is started with `log_config=None`,
  so it does not reconfigure the host's logging. Signal handlers are not
  installed off the main thread. The server is stopped at exit, or with
  `MCP.close()`.
- **Trust.** The agent process signs, as it did with the client adapters.

### 6.2 Standalone

`agentdna-gateway` initialises a normal `AgentDNA(type="agent")` from the
environment, with `AGENTDNA_AGENT_NAME` as its name. The agent reaches it
with `AGENT.mcp_gateway(url=..., token=...)`. No separate identity resolver
or registry exists.

- **Same identity as the agent app.** The DID comes from the name and the
  config dir, so the gateway must run with the same `AGENTDNA_AGENT_NAME`
  and `AGENTDNA_CONFIG_DIR` (default `~/.agentdna`) as the agent application.
  The gateway loads `.env` from its working directory, and existing
  environment variables take precedence.
- **Registration.** When `AGENTDNA_AGENT_POLICY_FILE` is set, the gateway
  registers the agent at startup. Otherwise it skips registration and leaves
  it to the agent application, which owns the policy.
- **Key custody.** v1 is a sidecar: the gateway uses the same local key
  material as the agent. Remote signing is out of scope (§13).
- **Trust semantics.** A request envelope signed by a standalone gateway
  means "a client holding this run's URL made this call", where the adapter
  meant "the agent process made this call". In both modes the run URL is a
  bearer capability and must be treated as a secret.

## 7. Request and response lifecycle

### Outgoing `tools/call`

1. Validate the request: `params` is an object, `name` is non-empty,
   `arguments` and `_meta` are objects. Otherwise return a JSON-RPC error
   and do not forward the request.
2. `begin_mcp_call()` on the run's `AgentDNAContext`.
3. `AgentDNA.build()` on the batch's parent frontier, with the canonical
   payload from `agentdna.mcp.payload.build_mcp_tool_call_payload`. The
   client adapters now import this same function, so the payload is
   identical whichever path a call takes.
4. Merge `workflow_to_metadata()` into `params._meta`. Unrelated keys and
   namespaces are preserved. The gateway owns `_meta.agentdna` and logs a
   warning if a client supplied one.
5. Forward the request upstream.

### Incoming response: the policy

The policy fails closed. A successful tool result reaches the agent only
after its successor has been verified.

| Upstream response | Agent receives | Frontier |
| ----------------- | -------------- | -------- |
| Success with a valid successor | Result unchanged (successor stays in `_meta`) | Advances |
| Success with no successor | JSON-RPC error `-32050` | Unchanged |
| Success, successor does not build directly on our request (wrong `id`, or no parent matching the request's hash and signature) | JSON-RPC error `-32050` | Unchanged |
| Success, successor fails `AgentDNA.verify()` | JSON-RPC error `-32050` with `data.agentdna_status_code`; the failure is built and recorded on the provenance layer, as the adapters do | Unchanged |
| `isError` with a `TOOL_EXECUTION_FAILED` successor | Result unchanged | Advances (the failure is part of the chain) |
| `isError` with no successor (a server-side whitelist, CoCA or CBAC refusal, which the server records itself) | Result unchanged | Unchanged |
| JSON-RPC `error` | Unchanged | Unchanged |
| `resultType` other than `complete` (`input_required`, tasks) | Unchanged; the follow-up call gets its own workflow | Unchanged |
| HTTP ≥ 400, or a non-MCP content type | Passed through | Unchanged |
| Upstream unreachable | HTTP 502 with JSON-RPC error `-32050` | Unchanged |
| Stream ends early, or the client disconnects | n/a | Unchanged (shielded cancel) |

The "builds directly on our request" check is new: the client adapters
did not have it. Without it, a server could answer with any validly signed
workflow, such as a replay or one from another run.

Every pending call is resolved exactly once, either completed or cancelled,
on every path. `finish_tool_call` and `abandon_tool_call` guarantee this.

## 8. Causal context and concurrency

Each run owns an `AgentDNAContext`, which the gateway reuses unchanged.
Overlapping calls form a batch that shares one parent frontier. When every
call in the batch is resolved, the successful successors become the new
frontier, in the order the calls started.

Reviewing that code for the gateway exposed a bug in `MCPExecutionBatch`,
which also affected the client adapters. It is now fixed:

- **Cancel, then succeed:** the batch never completed. Every later call was
  built on the stale frontier, and the agent's reply lost every tool call.
- **Succeed, then cancel:** the whole batch was dropped, including the
  successful branch.

Now a cancelled call counts as resolved but adds nothing to the frontier. If
every call in a batch is cancelled, the frontier stays where it started.
Repeated and late cancels are ignored. Tests:
`tests/test_mcp_context.py`.

## 9. Metadata compatibility

The format is unchanged:

```json
{ "agentdna": { "intent_workflow": "<serialized IntentWorkflow>" } }
```

The gateway uses the existing helpers (`workflow_to_metadata`,
`workflow_from_metadata`, `AGENTDNA_META_KEY`). The upstream server needs no
change: the existing `AgentDNAMCPMiddleware` cannot tell gateway traffic
from adapter traffic.

## 10. Error handling and security

| Case | Behaviour |
| ---- | --------- |
| Unknown or expired run, or unknown server name | `404`; nothing is forwarded upstream |
| Invalid run request | `400` |
| Incoming workflow fails verification | `422` with `status_code` |
| Verification cannot be performed | `503` |
| Batched JSON-RPC containing `tools/call` | `400` (batching was removed from MCP 2025-06-18) |
| Signing fails | JSON-RPC error; the call is cancelled |
| Missing or invalid configuration | The gateway refuses to start, with a message naming the variable |

**Security properties:**
- The gateway binds to `127.0.0.1` by default.
- The run endpoints (`/runs`, `/runs/{id}`) require a bearer token when one
  is configured. The token is compared in constant time. The in-process
  gateway always has one, generated at random (§6.1); the standalone gateway
  uses `AGENTDNA_GATEWAY_TOKEN`.
- The MCP endpoint is protected by the unguessable run ID.
- Request headers, including the agent's `Authorization` for the upstream,
  are forwarded unchanged except hop-by-hop headers and `accept-encoding`.
  Transport authentication stays separate from workflow verification.
- The gateway never logs headers, tokens or keys.

## 11. Configuration

The in-process gateway needs no configuration beyond the server map passed to
`mcp_gateway()`. The standalone gateway reads:

| Variable | Description |
| -------- | ----------- |
| `AGENTDNA_AGENT_NAME` | Required. The agent identity the gateway signs as. |
| `AGENTDNA_GATEWAY_UPSTREAMS` | Required. The MCP servers by name: `github=http://127.0.0.1:8011/mcp,jira=https://...`. Names may contain letters, digits, `-` and `_`. |
| `AGENTDNA_API_KEY`, `AGENTDNA_PROVENANCE_LAYER_URL`, `AGENTDNA_ADMIN_SERVER_URL`, `AGENTDNA_CONFIG_DIR` | Passed to `AgentDNA`. SDK defaults when unset. |
| `AGENTDNA_AGENT_POLICY_FILE` | When set, the agent is registered at startup. |
| `AGENTDNA_GATEWAY_HOST` / `AGENTDNA_GATEWAY_PORT` | Default `127.0.0.1:8090`. |
| `AGENTDNA_GATEWAY_TOKEN` | Optional bearer token for the run endpoints. |
| `AGENTDNA_GATEWAY_RUN_TTL_SECONDS` | Idle run expiry. Default `3600`. |
| `AGENTDNA_GATEWAY_UPSTREAM_TIMEOUT_SECONDS` | Read timeout per upstream request. Default `300`; GET streams have no read timeout. |

Both modes need `pip install 'agent-dna[gateway]'`, which adds `starlette` and
`uvicorn`. Run the standalone gateway with `agentdna-gateway` or
`python -m agentdna.mcp.gateway`.

## 12. Module layout

```text
agentdna/mcp/payload.py          canonical tools/call payload (shared with the client adapters)
agentdna/mcp/gateway/
  __init__.py                    exports MCPGateway, GatewaySession, GatewayError, WorkflowRejected
  client.py                      MCPGateway / GatewaySession: the developer-facing API
  embedded.py                    in-process mode: the app under uvicorn on its own thread
  config.py                      environment configuration and validation
  runs.py                        Run / RunRegistry: one AgentDNAContext per run
  propagation.py                 transport-neutral build → inject → verify → advance
  app.py                         Starlette app: run endpoints + Streamable HTTP pass-through
  __main__.py                    standalone CLI entry point
agentdna/core.py                 AgentDNA.mcp_gateway(): the one-line entry point
```

## 13. Decisions

| Question (from the original draft) | Decision |
| ---------------------------------- | -------- |
| First transport | Streamable HTTP. stdio is not supported yet. |
| Deployment | In process by default; standalone (`agentdna-gateway`) optional, same session API (§6). |
| Several MCP servers | Named servers behind one gateway; one frontier per session across all of them. |
| Identifying and isolating runs | Explicit runs with unguessable, run-scoped URLs (§5). |
| Missing or invalid successor | Fail closed for successful results; pass error results through (§7). |
| Merge policy for concurrent branches | Successful successors in call-start order; cancelled calls contribute nothing (§8). |
| Reuse the existing context? | Yes, after fixing the batch bugs (§8). |
| Remote signing interface | Not in v1. The gateway signs with the agent's own identity: the same instance in process, or the same name and config dir as a sidecar. |

## 14. Testing

- **`tests/test_gateway.py`** (48 tests, no network): a fake signer and a fake
  upstream drive every row of the §7 policy in both JSON and SSE. Also
  covered:
  - preservation of unrelated `_meta`;
  - pass-through of non-tool traffic;
  - the run lifecycle (TTL, `409`, token);
  - run isolation;
  - concurrent calls, including one failing alongside one succeeding;
  - one run spanning two MCP servers, with routing by name and a shared frontier;
  - the session API: open, `url()`, `reply()`, `WorkflowRejected`, force-close on error;
  - configuration validation.

  The suite was checked against mutations: disabling the "builds on our
  request" check, or reverting the context fix, makes tests fail.
- **`tests/test_gateway_e2e.py`** (real signatures on the provenance dev
  node, two real FastMCP servers with `AgentDNAMCPMiddleware`, and a plain
  `fastmcp.Client`). It covers:
  - the in-process gateway, with sequential, concurrent and failing calls
    across both servers; the agent's reply verifies in `heavy` mode;
  - synchronous use;
  - rejection of a tampered incoming workflow;
  - that other local processes cannot open runs on the in-process gateway;
  - the standalone gateway with a token.

  Only the admin whitelist is stubbed.
- **`tests/test_mcp_context.py`, `tests/test_unwrap.py`**: the SDK fixes in §8
  and §15.
- **GitHub example, run manually** (`langchain-mcp-adapters` 0.3.2,
  langgraph 1.2, mcp 1.x / fastmcp 3.x, in-process gateway): LangGraph's
  parallel tool calls went through an unpatched client and were signed with
  the agent's DID. The reply verified in heavy mode, and a second execution
  reused the same gateway thread. A scripted model and stand-in GitHub data were
  used; a run with a real LLM and a real GitHub token is still to be done.

## 15. SDK fixes made along the way

- **`MCPExecutionBatch` cancellation** (§8).
- **`IntentWorkflow.unwrap()` dropped branches.** It deduplicated envelopes
  by `hash`, which covers only an envelope's own content. A server's
  identical "success" answers to two concurrent calls in the same second
  shared a hash, so one answer and its whole branch disappeared. Because
  `verify_heavy` iterates `unwrap()`, that branch was never verified.
  Deduplication now uses `(hash, signature)`.
- **Duplicated payload builder.** `build_mcp_tool_call_payload` was copied in
  both adapters; it now lives in `agentdna/mcp/payload.py`.

## 16. Limitations and follow-ups

- **Signature model (needs a decision).** A signature commits to its
  parents' content hashes, not to their ancestry. An envelope with identical
  content but a different history could be substituted without breaking any
  signature. Fixing this means a Merkle-style hash, which changes the format
  of stored records.
- **Single instance.** Runs are held in memory. For the in-process gateway
  this is natural; a standalone gateway cannot be scaled horizontally
  without a shared run store.
- **No stdio transport.** Desktop MCP clients that launch servers over stdio
  cannot use the gateway yet. A wrapper process
  (`agentdna-gateway -- <server command>`) is the likely shape.
- **Client adapters.** The RSS and SQLite examples still use
  `install_mcp_client`. Once they are migrated, the adapters in
  `agentdna/mcp/client/` can be removed. Before then, check CrewAI's
  failure handling: the adapter stops the crew via `ToolExecutionFailedError`,
  whereas through the gateway CrewAI sees a JSON-RPC error or an `isError`
  result.
- **Auth evidence.** The gateway sees the upstream's response status, so it
  could record `auth_status` for the server hop, which
  `mcp/server/observer.py` currently records as `unknown`. Not wired yet.
- **Remote signing.** Needed before the gateway can be deployed anywhere
  other than next to the agent.
