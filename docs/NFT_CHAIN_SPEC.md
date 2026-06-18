# AgentDNA NFT Chain Specification

Every verified agent interaction is recorded as a single nested chain written to a Rubix NFT. This document defines the structure of that chain, what each field means, how to parse it, and what the chain looks like for every supported agentic flow.

---

## Table of Contents

1. [Top-Level NFT Record](#1-top-level-nft-record)
2. [Block — The Core Unit](#2-block--the-core-unit)
3. [Block Types and Payload Schemas](#3-block-types-and-payload-schemas)
4. [The Chain Field](#4-the-chain-field)
5. [How to Parse](#5-how-to-parse)
6. [Flow Examples](#6-flow-examples)
   - [Flow 1 — Simple (User → Agent → User)](#flow-1--simple-user--agent--user)
   - [Flow 2 — Delegated (User → Agent1 → Agent2 → Agent1 → User)](#flow-2--delegated-user--agent1--agent2--agent1--user)
   - [Flow 3 — CBAC / MCP (User → Agent → CBAC → App → CBAC → Agent → User)](#flow-3--cbac--mcp-user--agent--cbac--app--cbac--agent--user)
   - [Flow 4 — Delegated + CBAC (User → Agent1 → Agent2 → CBAC → App → CBAC → Agent2 → Agent1 → User)](#flow-4--delegated--cbac)
   - [Flow 5 — Fan-Out (User → Coordinator → [Agents] → Synthesizer → User)](#flow-5--fan-out)
   - [Flow 6 — Retry / Fallback](#flow-6--retry--fallback)
   - [Flow 7 — Multi-User Co-Signing](#flow-7--multi-user-co-signing)
   - [Flow 8 — External Trigger (No User)](#flow-8--external-trigger-no-user)

---

## 1. Top-Level NFT Record

Every NFT record has the same top-level structure regardless of flow type.

```json
{
  "comment":      "<human readable description of the interaction>",
  "executor":     "user | system",
  "did":          "<DID of the entity that owns and writes this NFT>",
  "verification": {
    "status":       "ok | failed | unknown",
    "chain_depth":  "<number of signed blocks in the chain>",
    "trust_issues": ["<string>", "..."]
  },
  "chain": { <outermost block — everything else nested inside> }
}
```

### Top-Level Field Definitions

| Field | Type | Description |
|---|---|---|
| `comment` | string | Human-readable label for the interaction. Always `"Agent communication — <names>"`. |
| `executor` | `"user"` \| `"system"` | Who initiated and owns this chain. `"user"` for human-initiated flows. `"system"` for external triggers (webhooks, cron, events). |
| `did` | string | The DID of the NFT owner — the entity that writes the audit record. For `executor: "user"` this is the user's DID. For `executor: "system"` this is the system DID. |
| `verification.status` | `"ok"` \| `"failed"` \| `"unknown"` | Overall result of the full chain verification. `"ok"` only when every block's signature is valid and no trust issues exist. |
| `verification.chain_depth` | integer | Total number of signed blocks in the chain (outbound + inbound combined). A simple user→agent→user flow has depth 3 (user outbound + agent outbound + agent inbound + user inbound = 4, but the user inbound is the outermost wrapper so depth counts the inner signed chain). |
| `verification.trust_issues` | string[] | Aggregated trust issues from all hops. Empty array when clean. |
| `chain` | Block | The outermost signed block. All other blocks are nested inside via `parent_block`. |

---

## 2. Block — The Core Unit

Every signed participant in the chain — user, agent, or system — produces one or two blocks: one outbound (when sending) and one inbound (when returning). Each block has the same top-level structure.

```json
{
  "agent":       "<DID of the signer>",
  "name":        "<human readable agent name>",
  "direction":   "outbound | inbound",
  "type":        "<block type — see Section 3>",
  "envelope":    { <signed content — see Section 3> },
  "signature":   "<cryptographic signature over envelope>",
  "verification": {
    "signature_valid": true,
    "trust_issues":    []
  }
}
```

### Block Field Definitions

| Field | Type | Always Present | Description |
|---|---|---|---|
| `agent` | string | Yes | DID of the entity that signed this block. |
| `name` | string | Yes | Human-readable name of the agent, user, or system. Used for display and logging. |
| `direction` | `"outbound"` \| `"inbound"` | Yes | `"outbound"` = this block was signed when sending a request forward. `"inbound"` = this block was signed when returning a response back. |
| `type` | string | Yes | Specific block type. Determines which keys are present in `envelope.payload`. See Section 3. |
| `envelope` | object | Yes | The content that was signed. Contains `payload` and optionally `parent_block`. |
| `envelope.payload` | object | Yes | The actual message content. Shape determined by `type`. |
| `envelope.parent_block` | Block | Conditional | The previous block in the chain that this block wraps and commits to. Present on all blocks except the root (innermost). Sits inside `envelope` so the signature covers it. |
| `signature` | string | Yes | Cryptographic signature over `envelope`. Produced by the Rubix signing service using the agent's DID key. |
| `verification.signature_valid` | boolean | Yes | Whether this block's signature was successfully verified at the time of NFT write. |
| `verification.trust_issues` | string[] | Yes | Any trust issues detected at this hop. Empty array when clean. |

### Chain Direction

The chain is written as a single nested structure. The **outermost block** is the last thing signed (user's final verification). The **innermost block** is the first thing signed (user's initial intent or system trigger). Every block's `envelope.parent_block` points inward toward the origin.

```
outermost → ... → innermost
user_inbound → agent1_inbound → agent1_outbound → user_outbound(intent)
```

Walking `parent_block` inward reads the chain from most-recent to origin. Reversing the walk reads it from origin to completion.

---

## 3. Block Types and Payload Schemas

`type` is the single discriminator. A parser reads `type` first, then knows exactly which payload keys to expect.

---

### `intent`
**direction:** `outbound` | **who:** user (root, innermost block)

The user's initial signed intent. Always the innermost block in user-initiated flows.

```json
{
  "type": "intent",
  "direction": "outbound",
  "envelope": {
    "payload": {
      "message":     "<the user's natural language intent>",
      "delegate_to": "<name of the first agent>",
      "ts":          "<ISO 8601 timestamp>"
    }
  }
}
```

| Key | Required | Description |
|---|---|---|
| `message` | Yes | The user's intent in plain text or structured form. |
| `delegate_to` | Yes | Name of the agent the user is handing this to. |
| `ts` | Yes | Timestamp of when the user signed. |

---

### `approval`
**direction:** `outbound` | **who:** co-signing user (wraps another user's `intent` or `approval`)

A second (or further) user signing off on an existing intent. Used in multi-user co-signing flows. Structurally identical to `intent` but with `type: "approval"` to distinguish the role.

```json
{
  "type": "approval",
  "direction": "outbound",
  "envelope": {
    "payload": {
      "message":     "<approval statement or echo of original intent>",
      "delegate_to": "<name of the first agent>",
      "ts":          "<ISO 8601 timestamp>"
    },
    "parent_block": { <previous user's intent or approval block> }
  }
}
```

---

### `trigger`
**direction:** `outbound` | **who:** external system (root, innermost block for system-initiated flows)

Replaces `intent` when there is no human user. The external system (webhook, cron, event bus) has its own DID and signs the triggering event.

```json
{
  "type": "trigger",
  "direction": "outbound",
  "envelope": {
    "payload": {
      "message":    "<description of what triggered this>",
      "source":     "<github | cron | event_bus | ...>",
      "event":      "<event name, e.g. pull_request_merged>",
      "metadata":   { "<any event-specific key-value data>" },
      "ts":         "<ISO 8601 timestamp>"
    }
  }
}
```

| Key | Required | Description |
|---|---|---|
| `message` | Yes | Human-readable description of the trigger. |
| `source` | Yes | Which external system fired this. |
| `event` | Yes | The specific event type. |
| `metadata` | No | Arbitrary event payload (repo name, sha, schedule expression, etc.). |
| `ts` | Yes | Timestamp of the triggering event. |

---

### `delegate`
**direction:** `outbound` | **who:** any agent that passes work to another agent

An agent received a task and decided to delegate it to a downstream agent. The most common outbound block type.

```json
{
  "type": "delegate",
  "direction": "outbound",
  "envelope": {
    "payload": {
      "message":      "<the task being delegated>",
      "received_from": "<name of the upstream agent or user>",
      "decision":     "delegate",
      "delegate_to":  "<name of the downstream agent>",
      "attempts": [
        {
          "delegate_to":     "<name of agent that was tried first>",
          "status":          "failed",
          "error":           "<reason — timeout | rejected | error>",
          "signed_outbound": { <the signed block from the failed attempt> },
          "signed_response": null
        }
      ]
    },
    "parent_block": { <upstream's signed block> }
  }
}
```

| Key | Required | Description |
|---|---|---|
| `message` | Yes | The task being handed to the downstream agent. |
| `received_from` | Yes | Who sent this agent the task (user name or agent name). |
| `decision` | Yes | Always `"delegate"` for this type. |
| `delegate_to` | Yes | The downstream agent receiving this task. |
| `attempts` | No | Present only on retry flows. Lists each failed delegation attempt before this successful one. Each entry carries the signed outbound block from that attempt and the signed response if any came back (null on timeout). |

---

### `execute`
**direction:** `outbound` | **who:** the terminal agent — the one that calls the app, tool, or external service

The last agent before the app. It does not delegate further — it executes directly (via CBAC if required).

```json
{
  "type": "execute",
  "direction": "outbound",
  "envelope": {
    "payload": {
      "message":       "<what is being executed>",
      "received_from": "<name of upstream agent>",
      "decision":      "execute",
      "attempts": [ ]
    },
    "parent_block": { <upstream agent's signed block> }
  }
}
```

| Key | Required | Description |
|---|---|---|
| `message` | Yes | The action being executed against the app or tool. |
| `received_from` | Yes | The agent that delegated this execution. |
| `decision` | Yes | Always `"execute"` for this type. |
| `attempts` | No | Same as `delegate` — present only on retry flows. |

---

### `response`
**direction:** `inbound` | **who:** any agent returning a result back up the chain

An agent received a response from downstream, verified the downstream agent's signature, and is signing its own response to pass further up.

```json
{
  "type": "response",
  "direction": "inbound",
  "envelope": {
    "payload": {
      "response":          "<the result being returned upstream>",
      "verified_upstream": "<name of the downstream agent whose signature was verified>",
      "cbac": {
        "decision": "allow | deny",
        "app":      "<app name, e.g. gmail>",
        "request":  "<what was requested, e.g. send_email>",
        "response": "<what the app returned, e.g. message_id>"
      },
      "sub_responses": [
        {
          "agent":           "<researcher DID>",
          "name":            "<researcher name>",
          "subtopic":        "<what this sub-agent researched>",
          "response_sha256": "<sha256 of the full response>",
          "signature":       "<researcher's signature>"
        }
      ]
    },
    "parent_block": { <downstream's inbound or outbound block> }
  }
}
```

| Key | Required | Description |
|---|---|---|
| `response` | Yes | The result this agent is returning to its upstream caller. |
| `verified_upstream` | Yes | Name of the downstream agent whose signature this agent verified before signing this block. |
| `cbac` | No | Present only when this agent went through CBAC to reach an app. Contains the policy decision and app context. |
| `cbac.decision` | Yes (if cbac) | `"allow"` or `"deny"`. |
| `cbac.app` | Yes (if cbac) | Which app was accessed (e.g. `"gmail"`, `"github"`, `"sheets"`). |
| `cbac.request` | Yes (if cbac) | What type of request was made to the app (e.g. `"send_email"`, `"create_issue"`). |
| `cbac.response` | Yes (if cbac) | What the app returned (e.g. `"message_id"`, `"issue_number"`). |
| `sub_responses` | No | Present only on fan-out aggregator blocks. Each entry is a compact digest of one sub-agent's signed response. Full responses are not stored — only the sha256 and signature for cryptographic commitment. |

---

### `verify`
**direction:** `inbound` | **who:** user or system (outermost block — the one who writes the NFT)

The final block. The user or system verified the top-most agent's signature and is writing the NFT. Always the outermost block in the chain.

```json
{
  "type": "verify",
  "direction": "inbound",
  "envelope": {
    "payload": {
      "response":          "<the final result returned to the user>",
      "verified_upstream": "<name of the agent whose signature was verified>",
      "nft_executed":      true
    },
    "parent_block": { <outermost agent's inbound block> }
  }
}
```

| Key | Required | Description |
|---|---|---|
| `response` | Yes | The final result the user received. |
| `verified_upstream` | Yes | The top-most agent in the chain whose signature the user verified. |
| `nft_executed` | Yes | Always `true`. Marks this as the block that triggered the NFT write. |

---

## 4. The Chain Field

`chain` in the top-level record is always the outermost block (type `verify`). Everything else is nested inside via `envelope.parent_block`.

**Reading order:**

| Direction | How to traverse | What you get |
|---|---|---|
| Origin → completion | Collect all blocks recursively, reverse the list | Reads as a timeline from first intent to final result |
| Completion → origin | Walk `envelope.parent_block` inward | Reads from most recent signature back to the root |

**Chain bridge (outbound meets inbound):**

Every agent that both sends and receives appears twice in the chain — once outbound, once inbound. The inbound block's `parent_block` points to the outbound block of the next agent down (not itself). The terminal agent is where the outbound chain ends and the inbound chain begins: the terminal's `response` block wraps the terminal's `execute` block.

```
verify(user) → response(agent1) → response(agent2) → execute(agent2) → delegate(agent1) → intent(user)
                                   ↑ bridge point: agent2 inbound wraps agent2 outbound
```

---

## 5. How to Parse

### Reading the full chain

```python
def walk_chain(block):
    chain = []
    current = block
    while current is not None:
        chain.append(current)
        envelope = current.get("envelope", {})
        current = envelope.get("parent_block")
    return chain  # outermost → innermost

# Reverse for chronological order
chronological = list(reversed(walk_chain(nft_record["chain"])))
```

### Reading a single block

```python
def parse_block(block):
    agent     = block["agent"]
    name      = block["name"]
    direction = block["direction"]
    btype     = block["type"]
    payload   = block["envelope"]["payload"]
    sig_valid = block["verification"]["signature_valid"]
    issues    = block["verification"]["trust_issues"]

    match btype:
        case "intent":
            # payload.message, payload.delegate_to, payload.ts
        case "approval":
            # same as intent
        case "trigger":
            # payload.message, payload.source, payload.event, payload.metadata
        case "delegate":
            # payload.message, payload.received_from, payload.delegate_to
            # optionally: payload.attempts
        case "execute":
            # payload.message, payload.received_from
            # optionally: payload.attempts
        case "response":
            # payload.response, payload.verified_upstream
            # optionally: payload.cbac, payload.sub_responses
        case "verify":
            # payload.response, payload.verified_upstream, payload.nft_executed
```

### Checking for optional features

```python
# Did this flow involve CBAC?
has_cbac = any(
    b["type"] == "response" and "cbac" in b["envelope"]["payload"]
    for b in walk_chain(nft["chain"])
)

# Did any hop have retries?
had_retry = any(
    b["type"] in ("delegate", "execute")
    and b["envelope"]["payload"].get("attempts")
    for b in walk_chain(nft["chain"])
)

# Was this a fan-out flow?
is_fanout = any(
    b["type"] == "response" and "sub_responses" in b["envelope"]["payload"]
    for b in walk_chain(nft["chain"])
)

# Was this system-triggered?
is_system = nft["executor"] == "system"

# Was this co-signed?
is_cosigned = any(b["type"] == "approval" for b in walk_chain(nft["chain"]))
```

---

## 6. Flow Examples

---

### Flow 1 — Simple (User → Agent → User)

**Pattern:** User signs intent → Agent processes → Agent responds → User verifies and writes NFT.

**Chain depth:** 3 (intent + delegate/execute + response + verify = 4 blocks)

```
verify(user) → response(agent1) → execute(agent1) → intent(user)
```

```json
{
  "comment": "Agent communication — SearchAgent",
  "executor": "user",
  "did": "<user_did>",
  "verification": { "status": "ok", "chain_depth": 4, "trust_issues": [] },
  "chain": {
    "agent": "<user_did>",
    "name": "user",
    "direction": "inbound",
    "type": "verify",
    "envelope": {
      "payload": {
        "response":          "{\"results\":[{\"title\":\"...\",\"url\":\"...\"}]}",
        "verified_upstream": "SearchAgent",
        "nft_executed":      true
      },
      "parent_block": {
        "agent": "<agent1_did>",
        "name": "SearchAgent",
        "direction": "inbound",
        "type": "response",
        "envelope": {
          "payload": {
            "response":          "{\"results\":[{\"title\":\"...\",\"url\":\"...\"}]}",
            "verified_upstream": "user"
          },
          "parent_block": {
            "agent": "<agent1_did>",
            "name": "SearchAgent",
            "direction": "outbound",
            "type": "execute",
            "envelope": {
              "payload": {
                "message":       "{\"task\":\"search quantum computing papers\"}",
                "received_from": "user",
                "decision":      "execute"
              },
              "parent_block": {
                "agent": "<user_did>",
                "name": "user",
                "direction": "outbound",
                "type": "intent",
                "envelope": {
                  "payload": {
                    "message":     "search quantum computing papers",
                    "delegate_to": "SearchAgent",
                    "ts":          "2026-05-27T10:00:00Z"
                  }
                },
                "signature": "<user_sig>",
                "verification": { "signature_valid": true, "trust_issues": [] }
              }
            },
            "signature": "<agent1_outbound_sig>",
            "verification": { "signature_valid": true, "trust_issues": [] }
          }
        },
        "signature": "<agent1_inbound_sig>",
        "verification": { "signature_valid": true, "trust_issues": [] }
      }
    },
    "signature": "<user_final_sig>",
    "verification": { "signature_valid": true, "trust_issues": [] }
  }
}
```

---

### Flow 2 — Delegated (User → Agent1 → Agent2 → Agent1 → User)

**Pattern:** Agent1 receives from user and delegates a subtask to Agent2. Agent2 responds. Agent1 wraps Agent2's response and returns to user.

**Chain depth:** 6

```
verify(user) → response(agent1) → response(agent2) → execute(agent2) → delegate(agent1) → intent(user)
```

```json
{
  "comment": "Agent communication — CoordinatorAgent → SpecialistAgent",
  "executor": "user",
  "did": "<user_did>",
  "verification": { "status": "ok", "chain_depth": 6, "trust_issues": [] },
  "chain": {
    "agent": "<user_did>",
    "name": "user",
    "direction": "inbound",
    "type": "verify",
    "envelope": {
      "payload": {
        "response":          "{\"summary\":\"...\",\"sources\":[...]}",
        "verified_upstream": "CoordinatorAgent",
        "nft_executed":      true
      },
      "parent_block": {
        "agent": "<agent1_did>",
        "name": "CoordinatorAgent",
        "direction": "inbound",
        "type": "response",
        "envelope": {
          "payload": {
            "response":          "{\"summary\":\"...\",\"sources\":[...]}",
            "verified_upstream": "SpecialistAgent"
          },
          "parent_block": {
            "agent": "<agent2_did>",
            "name": "SpecialistAgent",
            "direction": "inbound",
            "type": "response",
            "envelope": {
              "payload": {
                "response":          "{\"papers\":[...]}",
                "verified_upstream": "CoordinatorAgent"
              },
              "parent_block": {
                "agent": "<agent2_did>",
                "name": "SpecialistAgent",
                "direction": "outbound",
                "type": "execute",
                "envelope": {
                  "payload": {
                    "message":       "{\"subtask\":\"deep search on quantum error correction\"}",
                    "received_from": "CoordinatorAgent",
                    "decision":      "execute"
                  },
                  "parent_block": {
                    "agent": "<agent1_did>",
                    "name": "CoordinatorAgent",
                    "direction": "outbound",
                    "type": "delegate",
                    "envelope": {
                      "payload": {
                        "message":       "{\"task\":\"search quantum computing papers\"}",
                        "received_from": "user",
                        "decision":      "delegate",
                        "delegate_to":   "SpecialistAgent"
                      },
                      "parent_block": {
                        "agent": "<user_did>",
                        "name": "user",
                        "direction": "outbound",
                        "type": "intent",
                        "envelope": {
                          "payload": {
                            "message":     "search quantum computing papers",
                            "delegate_to": "CoordinatorAgent",
                            "ts":          "2026-05-27T10:00:00Z"
                          }
                        },
                        "signature": "<user_sig>",
                        "verification": { "signature_valid": true, "trust_issues": [] }
                      }
                    },
                    "signature": "<agent1_outbound_sig>",
                    "verification": { "signature_valid": true, "trust_issues": [] }
                  }
                },
                "signature": "<agent2_outbound_sig>",
                "verification": { "signature_valid": true, "trust_issues": [] }
              }
            },
            "signature": "<agent2_inbound_sig>",
            "verification": { "signature_valid": true, "trust_issues": [] }
          }
        },
        "signature": "<agent1_inbound_sig>",
        "verification": { "signature_valid": true, "trust_issues": [] }
      }
    },
    "signature": "<user_final_sig>",
    "verification": { "signature_valid": true, "trust_issues": [] }
  }
}
```

---

### Flow 3 — CBAC / MCP (User → Agent → CBAC → App → CBAC → Agent → User)

**Pattern:** Agent calls an external app through CBAC. CBAC is not a signer — its allow/deny decision is attested inside the agent's `response` block. From the chain's perspective this looks identical to Flow 1, with `cbac` added to the response payload.

**Chain depth:** 4

```
verify(user) → response(agent1)[cbac] → execute(agent1) → intent(user)
```

```json
{
  "comment": "Agent communication — EmailAgent via Gmail CBAC",
  "executor": "user",
  "did": "<user_did>",
  "verification": { "status": "ok", "chain_depth": 4, "trust_issues": [] },
  "chain": {
    "agent": "<user_did>",
    "name": "user",
    "direction": "inbound",
    "type": "verify",
    "envelope": {
      "payload": {
        "response":          "{\"status\":\"sent\",\"message_id\":\"gmail_xyz\"}",
        "verified_upstream": "EmailAgent",
        "nft_executed":      true
      },
      "parent_block": {
        "agent": "<agent1_did>",
        "name": "EmailAgent",
        "direction": "inbound",
        "type": "response",
        "envelope": {
          "payload": {
            "response":          "{\"status\":\"sent\",\"message_id\":\"gmail_xyz\"}",
            "verified_upstream": "user",
            "cbac": {
              "decision": "allow",
              "app":      "gmail",
              "request":  "send_email",
              "response": "message_id"
            }
          },
          "parent_block": {
            "agent": "<agent1_did>",
            "name": "EmailAgent",
            "direction": "outbound",
            "type": "execute",
            "envelope": {
              "payload": {
                "message":       "{\"action\":\"send_email\",\"to\":\"john@example.com\"}",
                "received_from": "user",
                "decision":      "execute"
              },
              "parent_block": {
                "agent": "<user_did>",
                "name": "user",
                "direction": "outbound",
                "type": "intent",
                "envelope": {
                  "payload": {
                    "message":     "send email to john about the meeting",
                    "delegate_to": "EmailAgent",
                    "ts":          "2026-05-27T10:00:00Z"
                  }
                },
                "signature": "<user_sig>",
                "verification": { "signature_valid": true, "trust_issues": [] }
              }
            },
            "signature": "<agent1_outbound_sig>",
            "verification": { "signature_valid": true, "trust_issues": [] }
          }
        },
        "signature": "<agent1_inbound_sig>",
        "verification": { "signature_valid": true, "trust_issues": [] }
      }
    },
    "signature": "<user_final_sig>",
    "verification": { "signature_valid": true, "trust_issues": [] }
  }
}
```

---

### Flow 4 — Delegated + CBAC

**Pattern:** User → Agent1 → Agent2 → CBAC → App → CBAC → Agent2 → Agent1 → User. Agent1 delegates to Agent2. Agent2 calls the app through CBAC. CBAC decision is in Agent2's response block.

**Chain depth:** 8

```
verify(user) → response(agent1) → response(agent2)[cbac] → execute(agent2) → delegate(agent1) → intent(user)
```

```json
{
  "comment": "Agent communication — OrchestratorAgent → GmailAgent via CBAC",
  "executor": "user",
  "did": "<user_did>",
  "verification": { "status": "ok", "chain_depth": 6, "trust_issues": [] },
  "chain": {
    "agent": "<user_did>",
    "name": "user",
    "direction": "inbound",
    "type": "verify",
    "envelope": {
      "payload": {
        "response":          "{\"status\":\"done\"}",
        "verified_upstream": "OrchestratorAgent",
        "nft_executed":      true
      },
      "parent_block": {
        "agent": "<agent1_did>",
        "name": "OrchestratorAgent",
        "direction": "inbound",
        "type": "response",
        "envelope": {
          "payload": {
            "response":          "{\"status\":\"done\"}",
            "verified_upstream": "GmailAgent"
          },
          "parent_block": {
            "agent": "<agent2_did>",
            "name": "GmailAgent",
            "direction": "inbound",
            "type": "response",
            "envelope": {
              "payload": {
                "response":          "{\"status\":\"sent\",\"message_id\":\"gmail_xyz\"}",
                "verified_upstream": "OrchestratorAgent",
                "cbac": {
                  "decision": "allow",
                  "app":      "gmail",
                  "request":  "send_email",
                  "response": "message_id"
                }
              },
              "parent_block": {
                "agent": "<agent2_did>",
                "name": "GmailAgent",
                "direction": "outbound",
                "type": "execute",
                "envelope": {
                  "payload": {
                    "message":       "{\"action\":\"send_email\",\"to\":\"john@example.com\"}",
                    "received_from": "OrchestratorAgent",
                    "decision":      "execute"
                  },
                  "parent_block": {
                    "agent": "<agent1_did>",
                    "name": "OrchestratorAgent",
                    "direction": "outbound",
                    "type": "delegate",
                    "envelope": {
                      "payload": {
                        "message":       "{\"task\":\"email john about the meeting\"}",
                        "received_from": "user",
                        "decision":      "delegate",
                        "delegate_to":   "GmailAgent"
                      },
                      "parent_block": {
                        "agent": "<user_did>",
                        "name": "user",
                        "direction": "outbound",
                        "type": "intent",
                        "envelope": {
                          "payload": {
                            "message":     "email john about the meeting",
                            "delegate_to": "OrchestratorAgent",
                            "ts":          "2026-05-27T10:00:00Z"
                          }
                        },
                        "signature": "<user_sig>",
                        "verification": { "signature_valid": true, "trust_issues": [] }
                      }
                    },
                    "signature": "<agent1_outbound_sig>",
                    "verification": { "signature_valid": true, "trust_issues": [] }
                  }
                },
                "signature": "<agent2_outbound_sig>",
                "verification": { "signature_valid": true, "trust_issues": [] }
              }
            },
            "signature": "<agent2_inbound_sig>",
            "verification": { "signature_valid": true, "trust_issues": [] }
          }
        },
        "signature": "<agent1_inbound_sig>",
        "verification": { "signature_valid": true, "trust_issues": [] }
      }
    },
    "signature": "<user_final_sig>",
    "verification": { "signature_valid": true, "trust_issues": [] }
  }
}
```

---

### Flow 5 — Fan-Out

**Pattern:** Coordinator delegates to N parallel sub-agents. A synthesizer aggregates their results. Full sub-agent response bodies are not stored on chain — only sha256 digests and signatures, giving cryptographic commitment without bloating the NFT. Stored on the synthesizer's `response` block under `sub_responses`.

**Chain depth:** 6 (user intent + coordinator delegate + synthesizer execute + synthesizer response + coordinator response + user verify)

```
verify(user) → response(coordinator) → response(synthesizer)[sub_responses] → execute(synthesizer) → delegate(coordinator) → intent(user)
```

```json
{
  "comment": "Agent communication — Coordinator → [Researcher x3] → Synthesizer",
  "executor": "user",
  "did": "<user_did>",
  "verification": { "status": "ok", "chain_depth": 6, "trust_issues": [] },
  "chain": {
    "agent": "<user_did>",
    "name": "user",
    "direction": "inbound",
    "type": "verify",
    "envelope": {
      "payload": {
        "response":          "{\"report\":\"...\"}",
        "verified_upstream": "Coordinator",
        "nft_executed":      true
      },
      "parent_block": {
        "agent": "<coordinator_did>",
        "name": "Coordinator",
        "direction": "inbound",
        "type": "response",
        "envelope": {
          "payload": {
            "response":          "{\"report\":\"...\"}",
            "verified_upstream": "Synthesizer"
          },
          "parent_block": {
            "agent": "<synthesizer_did>",
            "name": "Synthesizer",
            "direction": "inbound",
            "type": "response",
            "envelope": {
              "payload": {
                "response":          "{\"report\":\"...\"}",
                "verified_upstream": "Coordinator",
                "sub_responses": [
                  {
                    "agent":           "<researcher1_did>",
                    "name":            "Researcher_1",
                    "subtopic":        "quantum error correction",
                    "response_sha256": "<sha256>",
                    "signature":       "<r1_sig>"
                  },
                  {
                    "agent":           "<researcher2_did>",
                    "name":            "Researcher_2",
                    "subtopic":        "quantum algorithms",
                    "response_sha256": "<sha256>",
                    "signature":       "<r2_sig>"
                  },
                  {
                    "agent":           "<researcher3_did>",
                    "name":            "Researcher_3",
                    "subtopic":        "quantum hardware",
                    "response_sha256": "<sha256>",
                    "signature":       "<r3_sig>"
                  }
                ]
              },
              "parent_block": {
                "agent": "<synthesizer_did>",
                "name": "Synthesizer",
                "direction": "outbound",
                "type": "execute",
                "envelope": {
                  "payload": {
                    "message":       "{\"task_type\":\"synthesize\",\"question\":\"...\",\"subtopics\":[...]}",
                    "received_from": "Coordinator",
                    "decision":      "execute"
                  },
                  "parent_block": {
                    "agent": "<coordinator_did>",
                    "name": "Coordinator",
                    "direction": "outbound",
                    "type": "delegate",
                    "envelope": {
                      "payload": {
                        "message":       "{\"task_type\":\"research\",\"question\":\"...\"}",
                        "received_from": "user",
                        "decision":      "delegate",
                        "delegate_to":   "Synthesizer"
                      },
                      "parent_block": {
                        "agent": "<user_did>",
                        "name": "user",
                        "direction": "outbound",
                        "type": "intent",
                        "envelope": {
                          "payload": {
                            "message":     "research quantum computing",
                            "delegate_to": "Coordinator",
                            "ts":          "2026-05-27T10:00:00Z"
                          }
                        },
                        "signature": "<user_sig>",
                        "verification": { "signature_valid": true, "trust_issues": [] }
                      }
                    },
                    "signature": "<coordinator_outbound_sig>",
                    "verification": { "signature_valid": true, "trust_issues": [] }
                  }
                },
                "signature": "<synthesizer_outbound_sig>",
                "verification": { "signature_valid": true, "trust_issues": [] }
              }
            },
            "signature": "<synthesizer_inbound_sig>",
            "verification": { "signature_valid": true, "trust_issues": [] }
          }
        },
        "signature": "<coordinator_inbound_sig>",
        "verification": { "signature_valid": true, "trust_issues": [] }
      }
    },
    "signature": "<user_final_sig>",
    "verification": { "signature_valid": true, "trust_issues": [] }
  }
}
```

---

### Flow 6 — Retry / Fallback

**Pattern:** Agent1 tries AgentA, it fails (timeout or error). Agent1 retries with AgentB, which succeeds. The failed attempt is recorded in `attempts` on Agent1's successful `delegate` block. The signed outbound block from the failed attempt is preserved. If AgentA returned a signed error response, it is also preserved; otherwise `signed_response` is null.

**Chain depth:** 6

```
verify(user) → response(agent1) → response(agentB) → execute(agentB) → delegate(agent1)[attempts] → intent(user)
```

```json
{
  "comment": "Agent communication — OrchestratorAgent with retry (AgentA failed → AgentB succeeded)",
  "executor": "user",
  "did": "<user_did>",
  "verification": { "status": "ok", "chain_depth": 6, "trust_issues": [] },
  "chain": {
    "agent": "<user_did>",
    "name": "user",
    "direction": "inbound",
    "type": "verify",
    "envelope": {
      "payload": {
        "response":          "{\"result\":\"...\"}",
        "verified_upstream": "OrchestratorAgent",
        "nft_executed":      true
      },
      "parent_block": {
        "agent": "<agent1_did>",
        "name": "OrchestratorAgent",
        "direction": "inbound",
        "type": "response",
        "envelope": {
          "payload": {
            "response":          "{\"result\":\"...\"}",
            "verified_upstream": "AgentB"
          },
          "parent_block": {
            "agent": "<agentB_did>",
            "name": "AgentB",
            "direction": "inbound",
            "type": "response",
            "envelope": {
              "payload": {
                "response":          "{\"result\":\"...\"}",
                "verified_upstream": "OrchestratorAgent"
              },
              "parent_block": {
                "agent": "<agentB_did>",
                "name": "AgentB",
                "direction": "outbound",
                "type": "execute",
                "envelope": {
                  "payload": {
                    "message":       "{\"task\":\"...\"}",
                    "received_from": "OrchestratorAgent",
                    "decision":      "execute"
                  },
                  "parent_block": {
                    "agent": "<agent1_did>",
                    "name": "OrchestratorAgent",
                    "direction": "outbound",
                    "type": "delegate",
                    "envelope": {
                      "payload": {
                        "message":       "{\"task\":\"...\"}",
                        "received_from": "user",
                        "decision":      "delegate",
                        "delegate_to":   "AgentB",
                        "attempts": [
                          {
                            "delegate_to": "AgentA",
                            "status":      "failed",
                            "error":       "timeout",
                            "signed_outbound": {
                              "agent":     "<agent1_did>",
                              "name":      "OrchestratorAgent",
                              "direction": "outbound",
                              "type":      "delegate",
                              "envelope": {
                                "payload": {
                                  "message":       "{\"task\":\"...\"}",
                                  "received_from": "user",
                                  "decision":      "delegate",
                                  "delegate_to":   "AgentA"
                                }
                              },
                              "signature": "<agent1_to_agentA_sig>"
                            },
                            "signed_response": null
                          }
                        ]
                      },
                      "parent_block": {
                        "agent": "<user_did>",
                        "name": "user",
                        "direction": "outbound",
                        "type": "intent",
                        "envelope": {
                          "payload": {
                            "message":     "...",
                            "delegate_to": "OrchestratorAgent",
                            "ts":          "2026-05-27T10:00:00Z"
                          }
                        },
                        "signature": "<user_sig>",
                        "verification": { "signature_valid": true, "trust_issues": [] }
                      }
                    },
                    "signature": "<agent1_to_agentB_sig>",
                    "verification": { "signature_valid": true, "trust_issues": [] }
                  }
                },
                "signature": "<agentB_outbound_sig>",
                "verification": { "signature_valid": true, "trust_issues": [] }
              }
            },
            "signature": "<agentB_inbound_sig>",
            "verification": { "signature_valid": true, "trust_issues": [] }
          }
        },
        "signature": "<agent1_inbound_sig>",
        "verification": { "signature_valid": true, "trust_issues": [] }
      }
    },
    "signature": "<user_final_sig>",
    "verification": { "signature_valid": true, "trust_issues": [] }
  }
}
```

---

### Flow 7 — Multi-User Co-Signing

**Pattern:** User1 signs the original intent. User2 wraps User1's block and adds their approval before any agent acts. Sequential co-signing — no new fields needed. User2 is the NFT owner and writes the record. User1's intent is preserved as the innermost block.

**Chain depth:** 5

```
verify(user2) → response(agent1) → execute(agent1) → approval(user2) → intent(user1)
```

```json
{
  "comment": "Agent communication — co-signed by user1 and user2",
  "executor": "user",
  "did": "<user2_did>",
  "verification": { "status": "ok", "chain_depth": 5, "trust_issues": [] },
  "chain": {
    "agent": "<user2_did>",
    "name": "user2",
    "direction": "inbound",
    "type": "verify",
    "envelope": {
      "payload": {
        "response":          "{\"results\":[...]}",
        "verified_upstream": "SearchAgent",
        "nft_executed":      true
      },
      "parent_block": {
        "agent": "<agent1_did>",
        "name": "SearchAgent",
        "direction": "inbound",
        "type": "response",
        "envelope": {
          "payload": {
            "response":          "{\"results\":[...]}",
            "verified_upstream": "user2"
          },
          "parent_block": {
            "agent": "<agent1_did>",
            "name": "SearchAgent",
            "direction": "outbound",
            "type": "execute",
            "envelope": {
              "payload": {
                "message":       "{\"task\":\"search quantum computing papers\"}",
                "received_from": "user2",
                "decision":      "execute"
              },
              "parent_block": {
                "agent": "<user2_did>",
                "name": "user2",
                "direction": "outbound",
                "type": "approval",
                "envelope": {
                  "payload": {
                    "message":     "approved: search quantum computing papers",
                    "delegate_to": "SearchAgent",
                    "ts":          "2026-05-27T10:01:00Z"
                  },
                  "parent_block": {
                    "agent": "<user1_did>",
                    "name": "user1",
                    "direction": "outbound",
                    "type": "intent",
                    "envelope": {
                      "payload": {
                        "message":     "search quantum computing papers",
                        "delegate_to": "user2_for_approval",
                        "ts":          "2026-05-27T10:00:00Z"
                      }
                    },
                    "signature": "<user1_sig>",
                    "verification": { "signature_valid": true, "trust_issues": [] }
                  }
                },
                "signature": "<user2_approval_sig>",
                "verification": { "signature_valid": true, "trust_issues": [] }
              }
            },
            "signature": "<agent1_outbound_sig>",
            "verification": { "signature_valid": true, "trust_issues": [] }
          }
        },
        "signature": "<agent1_inbound_sig>",
        "verification": { "signature_valid": true, "trust_issues": [] }
      }
    },
    "signature": "<user2_final_sig>",
    "verification": { "signature_valid": true, "trust_issues": [] }
  }
}
```

---

### Flow 8 — External Trigger (No User)

**Pattern:** An external system (webhook, cron job, event bus) initiates the chain. There is no human user. The system has its own DID and signs a `trigger` block as the innermost. The system also writes the NFT at the end (outermost `verify` block). `executor` is `"system"`.

**Chain depth:** 4

```
verify(system) → response(agent1) → execute(agent1) → trigger(system)
```

```json
{
  "comment": "Agent communication — triggered by github webhook → DeployAgent",
  "executor": "system",
  "did": "<system_did>",
  "verification": { "status": "ok", "chain_depth": 4, "trust_issues": [] },
  "chain": {
    "agent": "<system_did>",
    "name": "github_webhook",
    "direction": "inbound",
    "type": "verify",
    "envelope": {
      "payload": {
        "response":          "{\"deploy\":\"success\",\"sha\":\"abc123\"}",
        "verified_upstream": "DeployAgent",
        "nft_executed":      true
      },
      "parent_block": {
        "agent": "<agent1_did>",
        "name": "DeployAgent",
        "direction": "inbound",
        "type": "response",
        "envelope": {
          "payload": {
            "response":          "{\"deploy\":\"success\",\"sha\":\"abc123\"}",
            "verified_upstream": "github_webhook"
          },
          "parent_block": {
            "agent": "<agent1_did>",
            "name": "DeployAgent",
            "direction": "outbound",
            "type": "execute",
            "envelope": {
              "payload": {
                "message":       "{\"action\":\"deploy\",\"sha\":\"abc123\",\"target\":\"production\"}",
                "received_from": "github_webhook",
                "decision":      "execute"
              },
              "parent_block": {
                "agent": "<system_did>",
                "name": "github_webhook",
                "direction": "outbound",
                "type": "trigger",
                "envelope": {
                  "payload": {
                    "message":  "pull_request_merged on org/repo",
                    "source":   "github",
                    "event":    "pull_request_merged",
                    "metadata": {
                      "repo":   "org/repo",
                      "sha":    "abc123",
                      "branch": "main"
                    },
                    "ts": "2026-05-27T10:00:00Z"
                  }
                },
                "signature": "<system_sig>",
                "verification": { "signature_valid": true, "trust_issues": [] }
              }
            },
            "signature": "<agent1_outbound_sig>",
            "verification": { "signature_valid": true, "trust_issues": [] }
          }
        },
        "signature": "<agent1_inbound_sig>",
        "verification": { "signature_valid": true, "trust_issues": [] }
      }
    },
    "signature": "<system_final_sig>",
    "verification": { "signature_valid": true, "trust_issues": [] }
  }
}
```

---

*Async and parallel fan-out (Option C tree structure) are not covered in this version and will be specified separately.*

