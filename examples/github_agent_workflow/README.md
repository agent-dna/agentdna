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

---

# Current Features

* Multi-agent GitHub automation
* LangGraph orchestration
* MCP tool integration
* AgentDNA identity verification
* Workflow signing
* Context-Based Access Control (CBAC)
* GitHub Issue creation
* GitHub Pull Request creation
* Workflow provenance generation
* Streamlit UI
* CLI execution

---

# Future Work

Potential extensions include:

* Additional GitHub operations
* Multi-worker execution
* Parallel workflows
* Human approval steps
* Rich workflow visualization
* Policy-based agent routing
* Support for additional MCP servers
* Multi-application workflows

---

# License

This project is provided for demonstration and evaluation purposes.
