# Support Refund Agent

A support lead asks an agent to refund a customer's double charge and log a ticket. The agents talk to each other, and to Stripe and Jira, over plain REST, with every call signed. The agents hold no Stripe or Jira key: Kong adds them, after the AgentDNA gate service has checked each call.

## Architecture

```text
support lead ──REST──▶ Support Agent (LLM) ──REST──▶ Payments Agent ──REST──▶ Kong + gate ──▶ Stripe   refund
                            │
                            └──────────REST──────────────────────────────▶ Kong + gate ──▶ Jira     ticket
```

- `support_lead.py`: the support lead signs the request, and records the finished workflow on the provenance layer.
- `support_agent.py`: its LLM reads the request and the customer's email (`inbox/`), and decides the refund and the ticket. It asks the Payments Agent to refund, and opens the Jira ticket through Kong. Its answer joins both.
- `payments_agent.py`: refunds the payment in Stripe, through Kong.
- `gate_service.py`: the checks Kong asks for on every call: whitelist, GRADE and CBAC. It signs an "allowed" step, and then the app's answer.
- `kong/`: Kong, with the `agentdna-gate` plugin, and `env-key`, which adds Stripe's or Jira's key only to calls the gate allowed.
- `policies/`: what each agent may do. Each identity registers it on its first run, and CBAC checks calls against it.

Each agent changes three lines to take part: `SignedRequest(...)` to open a call, `signed_request.client(...)` for its own calls, and `signed_request.respond(...)` to answer.

```py
signed_request = SignedRequest(agent, await request.body(), str(request.url))  # refused unless real
client = signed_request.client(KONG_URL)  # calls signed onto the workflow
response = client.post("/stripe/v1/refunds", data={...})
return JSONResponse(signed_request.respond({"refund": ...}))  # answer, signed
```

## The prompt injection

Order `4822`'s email carries a hidden note telling the agent to refund £4,000 instead of £40. If the LLM follows it, the Payments Agent's refund call reaches Kong asking for £4,000, while the support lead asked for £40. CBAC refuses it at the gateway, the refusal is signed and recorded, and no money moves. The Jira ticket is still logged, saying the refund was refused.

## Prerequisites

- Python 3.11 or 3.12, and Docker (for Kong)
- An LLM: Ollama, OpenAI (or an OpenAI-compatible service), or Gemini
- A Stripe account in test mode, and a Jira Cloud site with an API token
- An AgentDNA API key, from the [AgentDNA Dashboard](https://dashboard.agentdna.io)

## Setup

1. Create and activate a virtual environment, from this directory, and install the dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Copy `.env.example` to `.env`, and fill it in (see below).
3. In `kong/kong.yml`, set your Jira site (`url: https://your-site.atlassian.net`).
4. Create a £40 test payment to refund, and put its id in `.env`:

```sh
python make_payment.py        # prints STRIPE_PAYMENT_INTENT=pi_...
```

5. Start everything once (see [Run](#run)). Each identity registers on its first start, and prints its ID (in `logs/`, and when you run `support_lead.py`). Whitelist the Support Agent, the Payments Agent, the gateway and the support lead on the AgentDNA admin server: until then, the gate refuses their calls.

### Environment variables

| Variable | Description |
|---|---|
| `AGENTDNA_API_KEY` | Your AgentDNA API key. Required. |
| `PROVENANCE_LAYER_URL`, `ADMIN_SERVER_URL` | The AgentDNA services. The defaults in `.env.example` are production. |
| `CBAC_URL` | The CBAC service the gate asks whether each call fits what the support lead asked. |
| `SUPPORT_LEAD_NAME`, `SUPPORT_AGENT_NAME`, `PAYMENTS_AGENT_NAME`, `GATEWAY_NAME` | The identities' names. |
| `LLM_BACKEND` | `ollama`, `openai` or `gemini`, with `OLLAMA_MODEL`, `OPENAI_API_KEY` or `GOOGLE_API_KEY`, as in the other examples. |
| `STRIPE_AUTH` | `Bearer sk_test_...`, your Stripe test key. Only Kong uses it (and `make_payment.py`). |
| `STRIPE_PAYMENT_INTENT` | The payment to refund, from `make_payment.py`. |
| `JIRA_AUTH` | `Basic ` followed by base64 of `email:api_token` (`echo -n "you@example.com:TOKEN" \| base64`). Only Kong uses it. |
| `JIRA_PROJECT` | The Jira project's key, such as `KAN`. |
| `AGENTDNA_AUTH_EVIDENCE`, `AGENTDNA_FINGERPRINT_KEY` | Switch on AgentDNA's authentication evidence, which is off by default. With it on, the dashboard shows Stripe and Jira behind the gateway, and which key reached each one. See [Authentication evidence](../../README.md#authentication-evidence) in the main README. |

## Run

```sh
./start.sh                       # Kong :8880, gate service :8810, Support Agent :8811, Payments Agent :8812
python support_lead.py           # order 4821: refunds £40, opens a ticket
python support_lead.py 4822      # order 4822: the prompt injection, refused at the gateway
./stop.sh
```

Each run refunds in Stripe's test mode, opens a real Jira ticket, and records the workflow on the provenance layer.

Logs are in `logs/`. On the dashboard, each run shows the support lead's request, both agents' calls, the gate's "allowed" (or its refusal), and the apps' signed answers.
