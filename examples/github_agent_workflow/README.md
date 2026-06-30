# GithubAgent

An AI-powered GitHub automation demo built using LangGraph, MCP (Model Context Protocol), and AgentDNA.

The project demonstrates how multiple AI agents can collaboratively execute GitHub operations while maintaining an auditable chain of trust. Every participant—the human user, AI agents, and GitHub application—contributes to a verifiable workflow that can be published as a provenance record at the end of execution.

---

# Architecture

The system consists of two cooperating AI agents.

```
                    +-------------------+
                    |      Human        |
                    +---------+---------+
                              |
                              | Initial IntentWorkflow
                              v
                    +-------------------+
                    |   Coordinator     |
                    |-------------------|
                    | • Verify workflow |
                    | • Plan task       |
                    +---------+---------+
                              |
                              | Updated Workflow
                              v
                    +-------------------+
                    |      Worker       |
                    |-------------------|
                    | • Verify workflow |
                    | • Execute task    |
                    +---------+---------+
                              |
                              | MCP Tool Call
                              | + IntentWorkflow
                              v
                    +-------------------+
                    | GitHub MCP Server |
                    +---------+---------+
                              |
                       CBAC Authorization
                              |
                              v
                         GitHub REST API
                              |
                              v
                    +-------------------+
                    |      Worker       |
                    | Append Result     |
                    +---------+---------+
                              |
                              v
                    +-------------------+
                    |      Human        |
                    |-------------------|
                    | Publish Workflow  |
                    | Provenance        |
                    +-------------------+
```

---

# Components

## Coordinator

Responsible for understanding the user's request.

Responsibilities:

* Verify incoming workflow
* Interpret natural language
* Produce a structured task specification
* Forward the workflow to the Worker

The Coordinator never calls GitHub directly.

---

## Worker

Responsible for execution.

Responsibilities:

* Verify Coordinator workflow
* Invoke GitHub MCP tools
* Receive updated workflow from the MCP server
* Append execution results
* Return the completed workflow

The Worker never talks directly to GitHub. All interactions happen through MCP.

---

## GitHub MCP Server

Provides GitHub operations as MCP tools.

Currently implemented:

* `create_issue`
* `create_pr`

Each tool:

1. Receives the current IntentWorkflow
2. Performs CBAC authorization
3. Executes the GitHub REST request
4. Appends an application event to the workflow
5. Returns the updated workflow to the Worker

---

## AgentDNA

AgentDNA provides:

* Identity management
* Workflow verification
* Envelope signing
* Provenance generation

Each participant possesses an identity:

* Human
* Coordinator
* Worker

GitHub is currently modeled as an application actor.

---

# Workflow Lifecycle

The execution lifecycle is:

```
Human
    │
    ▼
Create IntentWorkflow
    │
    ▼
Coordinator verifies workflow
    │
    ▼
Coordinator appends event
    │
    ▼
Worker verifies workflow
    │
    ▼
Worker calls MCP
    │
    ▼
GitHub authorization (CBAC)
    │
    ▼
GitHub API
    │
    ▼
Worker appends result
    │
    ▼
Human publishes workflow provenance
```

---

# Verification

Every incoming workflow is verified before an agent performs work.

If verification fails:

* the workflow is updated with a rejection event
* execution terminates
* the final workflow is still returned to the initiating human

This preserves an auditable history of failed requests.

---

# CBAC

GitHub operations are protected by Context-Based Access Control (CBAC).

Before any GitHub API request is executed, the MCP server performs authorization using:

* Worker identity
* Requested GitHub action
* Current IntentWorkflow

Possible outcomes include:

* Allow
* Deny
* Error

Every decision is recorded in the workflow.

---

# Provenance

Only the Human AgentDNA instance publishes workflow provenance.

After execution finishes, the application calls:

```
UserSession.complete(...)
```

which internally invokes

```
AgentDNA.create_workflow_provenance(...)
```

This produces a provenance record representing the complete execution.

---

# Repository Structure

```
app/
├── agents/
│   ├── coordinator/
│   ├── worker/
│   ├── graph.py
│   └── agentdna_helpers.py
│
├── integrations/
│   └── agentdna/
│
├── mcp_client/
│
├── mcp_server/
│
├── utils.py
│
scripts/
├── start_mcp.py
├── run_flow.py
│
streamlit_app.py
```

---

# Configuration

Copy the sample environment file.

```
cp .env.sample .env
```

Populate the following values:

| Variable             | Description                  |
| -------------------- | ---------------------------- |
| `GITHUB_TOKEN`       | GitHub Personal Access Token |
| `GEMINI_API_KEY`     | Gemini API key               |
| `AGENTDNA_API_KEY`   | AgentDNA API key             |
| `AGENTDNA_CHAIN_URL` | AgentDNA chain endpoint      |
| `ORGANISATION_ID`    | Organisation identifier      |
| `AGENTDNA_CBAC_URL`  | CBAC service URL             |

---

# Running the Demo

## 1. Start the MCP Server

```
python scripts/start_mcp.py
```

---

## 2. Run the CLI Demo

```
python scripts/run_flow.py \
"Create an issue in owner/repo titled 'Bug' with body 'Fix this bug.'"
```

---

## 3. Launch the Streamlit UI

```
streamlit run streamlit_app.py
```

---

# Environment Variables

| Variable               | Description                   |
| ---------------------- | ----------------------------- |
| `LLM_PROVIDER`         | `gemini` or `ollama`          |
| `GEMINI_API_KEY`       | Gemini API key                |
| `GEMINI_MODEL`         | Gemini model                  |
| `OLLAMA_BASE_URL`      | Ollama server                 |
| `OLLAMA_MODEL`         | Ollama model                  |
| `GITHUB_TOKEN`         | GitHub Personal Access Token  |
| `GITHUB_API_URL`       | GitHub REST API URL           |
| `MCP_SERVER_HOST`      | MCP host                      |
| `MCP_SERVER_PORT`      | MCP port                      |
| `AGENTDNA_API_KEY`     | AgentDNA API key              |
| `AGENTDNA_CHAIN_URL`   | AgentDNA chain URL            |
| `AGENTDNA_CBAC_URL`    | CBAC endpoint                 |
| `CBAC_TIMEOUT_SECONDS` | Maximum authorization timeout |
| `ORGANISATION_ID`      | Organisation identifier       |
| `AGENT_COORDINATOR`    | Coordinator agent name        |
| `AGENT_WORKER`         | Worker agent name             |

# AgentDNA Integration

This demo keeps the AgentDNA integration intentionally lightweight. Rather than introducing a separate orchestration layer, AgentDNA is integrated into the existing LangGraph workflow. Every participant verifies the incoming workflow, performs its work, appends a signed `Envelope` and forwards the updated workflow to the next participant.


## 1. Entry Point

Start with:

```text
streamlit_app.py
```

When a user submits a request, the application creates a `UserSession` by calling:

```python
session = await UserSession.open(...)
```

This is the only place where the Human starts a new workflow.

Internally, `UserSession.open()`:

* Creates (or retrieves) the Human identity.
* Creates the initial `IntentWorkflow`.
* Signs the first `Envelope`.
* Returns the workflow that will be passed into LangGraph.

The resulting workflow is stored inside the graph state:

```python
initial_state = {
    "user_input": user_input,
    "_agentdna_workflow": serialize_workflow(...),
}
```

---

## 2. Human Integration

The Human integration lives under:

```text
app/
└── integrations/
    └── agentdna/
        └── user_session.py
```

This file is responsible for the complete Human lifecycle.

When a workflow starts:

```python
UserSession.open(...)
```

When the workflow finishes:

```python
UserSession.complete(...)
```

Internally, `complete()` verifies the returned workflow before publishing it to the Provenance Layer using:

```python
AgentDNA.create_workflow_provenance(...)
```

The Human is therefore responsible for both creating the first `Envelope` and publishing the final provenance record.

---

## 3. Agent Helpers

Before looking at the Agents themselves, open:

```text
app/
└── agents/
    └── agentdna_helpers.py
```

This file contains the common AgentDNA helper functions used throughout the project.

These helpers are responsible for:

* Loading Agent identities
* Deserializing workflows
* Verifying inbound workflows
* Returning the verification result to the Agent

Keeping these operations here allows the Coordinator and Worker implementations to remain focused on business logic.

---

## 4. Coordinator Integration

Next, open:

```text
app/
└── agents/
    └── coordinator/
        └── agent.py
```

The Coordinator is the first Agent in the workflow.

The first operation it performs is:

```python
verify_inbound(...)
```

which internally invokes:

```python
AgentDNA.handle(...)
```

Only after the workflow has been successfully verified does the Coordinator invoke the language model to produce the task specification.

Finally, it appends its own signed `Envelope` using:

```python
AgentDNA.build(...)
```

before forwarding the updated workflow to the Worker.

---

## 5. Worker Integration

Continue with:

```text
app/
└── agents/
    └── worker/
        └── agent.py
```

The Worker follows exactly the same pattern.

It begins by verifying the incoming workflow:

```python
AgentDNA.handle(...)
```

If verification succeeds, it performs the requested GitHub operation through the MCP client.

After execution completes, the Worker appends its own signed `Envelope` using:

```python
AgentDNA.build(...)
```

before returning the updated workflow.

This repeated `handle()` → work → `build()` pattern is the foundation of Chain of Custody Authentication (CoCA).

---

## 6. MCP Integration

The Worker never communicates directly with GitHub.

Instead, it uses the MCP client located under:

```text
app/
└── mcp_client/
```

which communicates with the GitHub MCP server located under:

```text
app/
└── mcp_server/
```

Every MCP tool receives the current `IntentWorkflow`.

Before calling the GitHub REST API, the MCP server performs Context-Based Access Control (CBAC).

After execution, it appends an application `Envelope` representing GitHub's participation in the workflow before returning the updated workflow to the Worker.

This allows external applications to participate in the same verifiable chain of custody as Human users and AI Agents.

---

## 7. LangGraph Orchestration

Finally, open:

```text
app/
└── agents/
    └── graph.py
```

This file contains the LangGraph workflow definition.

Notice that AgentDNA does not change how LangGraph is wired together.

Instead, it augments each node with verification and signing, allowing the orchestration logic to remain unchanged while every transition becomes cryptographically verifiable.

---

## Putting it all together

The complete execution flow becomes:

```text
streamlit_app.py
        │
        ▼
UserSession.open()
        │
        ▼
coordinator/agent.py
        │
        ▼
worker/agent.py
        │
        ▼
mcp_server/
        │
        ▼
worker/agent.py
        │
        ▼
UserSession.complete()
```

Throughout this flow, every participant follows the same lifecycle:

```text
Receive Workflow
        │
        ▼
 AgentDNA.handle()
        │
Verify Workflow
        │
Perform Work
        │
        ▼
 AgentDNA.build()
        │
Forward Workflow
```

The Human starts the workflow by creating the initial `IntentWorkflow` and ends it by publishing the completed workflow to the Provenance Layer. Every participant in between simply verifies, performs its work and appends another signed `Envelope`, allowing the complete execution history to be reconstructed from the final `IntentWorkflow`.
