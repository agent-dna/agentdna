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
  "parent_envelope": "List of Envelopes upon which the current envelope is being built upon"
}
```

Following are the supported status code:

| Codes      | Description                                             |
| ---------- | ------------------------------------------------------- |
| 1000       | No issues found                                         |
| 2001       | Envelope verification failed under `light` mode         |
| 2002       | Envelope verification failed under `heavy` mode         |
| 2003       | Envelope verification failed under `boundary` mode      |

---

### IntentWorkflow

An `IntentWorkflow` represents the complete lifecycle of an intent.

Rather than storing a sequence of events, AgentDNA stores the latest `Envelope`. Every Envelope recursively references its parent, allowing the entire chain of custody to be reconstructed from a single object.

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
    "status_code": {},
    "signature": "...",
    "parent_envelope": [{
      "from": "coordinator_actor_id",
      "to": "worker_actor_id",
      "payload": "{\"action\":\"produce_task_spec\"}",
      "epoch": 1782668362,
      "status_code": {},
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

Let's walk through a simple example involving a Human and a single AI Agent.

Suppose Alice wants an AI Assistant to summarize a document.

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

Although this appears to be a simple request, several important questions arise:

* How does the Assistant know the request genuinely came from Alice?
* How can the Assistant verify that the request has not been modified?
* How can Alice later prove what was requested and what response was returned?

AgentDNA answers these questions through three operations: `build()`, `handle()` and `create_workflow_provenance()`.

### Initialize the participants

Every participant in a workflow is represented by an `AgentDNA` instance.

For this example, we'll create one Human and one AI Agent.

```python
from agentdna.core import AgentDNA

user = AgentDNA(
    name="Alice",
    type="user",
    api_key="<Optional, only required for Beta (Explained later)>"
)

assistant = AgentDNA(
    name="Assistant",
    type="agent",
    api_key="<Optional, only required for Beta (Explained later)>"
)
```

Supported Actor types are:

* `user`
* `agent`
* `app`

### Step 1. Build the initial workflow

Alice creates the first signed `Envelope` and sends it to the Assistant.

```python
workflow = human.build(
    payload='{"request":"Summarize this document."}'
)
```

The workflow now contains a single signed Envelope.

```text
Alice ─────────▶ Assistant
```

### Step 2. Verify before acting

Before processing the request, the Assistant verifies the workflow.

```python
result = assistant.verify(workflow)

if not result:
    return
```

This verifies the chain of custody and evaluates whether the request should be accepted according to the Assistant's policy.

Only after successful verification should the Assistant perform its work. Else, they can return the response back to the user.

### Step 3. Build the response

Once the summary has been generated, the Assistant appends its own signed Envelope to the existing workflow.

```python
workflow = assistant.build(
    payload='{"summary":"..."}',
    previous_workflows=workflow,
)
```

The workflow now contains two linked Envelopes.

```text
Alice ─────────▶ Assistant
                     │
                     ▼
Alice ◀──────── Assistant
```

Notice that the original request is preserved. The Assistant simply appends a new signed Envelope, extending the chain of custody.

### Step 4. Store the completed workflow

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

By the time the workflow completes, the nested Envelopes form a verifiable chain of custody that records every participant, every decision and every interaction from the original request to the final response. Remember, creation of workflow provenance encompases both success and failures between Actors.

For more detailed implementations, you can refer the examples [here](./examples)

## Open Beta

AgentDNA is currently running an Open BETA programme. We welcome developers to explore the framework and share valuable feedback and report issues.

Head over to [AgentDNA Dashboard](https://dashboard.agentdna.io) to get started
