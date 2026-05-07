# A2A Friend Scheduling Demo
This document describes a multi-agent application demonstrating how to orchestrate conversations between different agents to schedule a meeting.

This application contains four agents:
*   **Host Agent**: The primary agent that orchestrates the scheduling task.
*   **Kaitlynn Agent**: An agent representing Kaitlynn's calendar and preferences.
*   **Nate Agent**: An agent representing Nate's calendar and preferences.
*   **Karley Agent**: An agent representing Karley's calendar and preferences.

## Setup and Deployment

### Prerequisites

Before running the application locally, ensure you have the following installed:

1. **uv:** The Python package management tool used in this project. Follow the installation guide: [https://docs.astral.sh/uv/getting-started/installation/](https://docs.astral.sh/uv/getting-started/installation/)
2. **python 3.11+** Python 3.11 or later is required.
3. **set up .env**

Create a `.env` file in the `multi_agent_system` directory:
```
cp .env.sample .env
```

Set the following environment variables:
```
GOOGLE_API_KEY=your_google_api_key_here
AGENTDNA_API_KEY=your_agentdna_api_key_here   # Get from https://agentdna.io/beta
```

## Run the Agents

### Single command (recommended)

From the `multi_agent_system` directory:
```bash
./run.sh
```

This sets up all virtual environments (first run only), starts Karley (10002), Nate (10003), and Kaitlynn (10004) as background processes, then opens the Streamlit UI. Press Ctrl+C to stop everything.

### Manual startup (separate terminals)

<details>
<summary>Expand for per-terminal instructions</summary>

### Terminal 1: Run Kaitlynn Agent
```bash
cd kaitlynn_agent_langgraph
uv sync
uv run --no-sync -m app.__main__
```

### Terminal 2: Run Nate Agent
```bash
cd nate_agent_crewai
uv sync
uv run --no-sync .
```

### Terminal 3: Run Karley Agent
```bash
cd karley_agent_adk
uv sync
uv run --no-sync .
```

### Terminal 4: Run Host Agent
```bash
cd host_agent_adk
uv sync
uv run --no-sync streamlit run webui_app.py
```

</details>

## Interact with the Host Agent

Once all agents are running, the host agent will begin the scheduling process. You can view the interaction in the terminal output of the `host_agent`.

## References
- https://github.com/google/a2a-python
- https://codelabs.developers.google.com/intro-a2a-purchasing-concierge#1
