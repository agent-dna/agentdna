"""
Proof of concept: the framework-agnostic CBAC guard decorator.

Demonstrates the new `agentdna.guard` layer WITHOUT any agent framework,
MCP server, or LLM: a guarded GitHub tool is just an async function
returning an AppRequest, and the guard handles authorization (CBAC) plus
envelope attestation automatically, reading identity/workflow from the
ambient governance context.

No existing example code is modified; this script only reuses the
example's config, registry, and UserSession read-only.

Usage:
    python scripts/guard_poc.py --repo owner/name --title "Test" --body "..."

    CBAC_MODE=local  python scripts/guard_poc.py ... --demo-plain
    python scripts/guard_poc.py ... --no-governance   # pass-through path
    python scripts/guard_poc.py ... --finalize        # write provenance card
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent_names import worker_name
from app.config import settings
from app.constants import ANONYMOUS_USER_EMAIL
from app.integrations.agentdna import UserSession
from app.integrations.agentdna.registry import agentdna_registry, is_agentdna_enabled

from agentdna.guard import AppRequest, cbac_context, cbac_guard, configure
from agentdna.helpers import get_root_envelope, unwrap_workflow

WORKER_SKILLS_FILE = str(
    Path(__file__).resolve().parent.parent / "app" / "agents" / "worker" / "skills.md"
)


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }


# ── Guarded tools: pure business logic, zero governance code ─────────────────

@cbac_guard(
    app_name="github",
    describe=lambda kw: {
        "action": "create_issue",
        "repo": kw["repo"],
        "title": kw["title"],
        "body_preview": kw["body"][:200],
    },
    action_intent=lambda kw: f"github:create_issue:{kw['repo']}",
    parse_response=lambda data, status: {
        "issue_number": data.get("number"),
        "html_url": data.get("html_url"),
        "title": data.get("title"),
    },
)
async def create_issue(repo: str, title: str, body: str) -> AppRequest:
    """Create a GitHub issue."""
    return AppRequest(
        url=f"{settings.github_api_url}/repos/{repo}/issues",
        headers=_gh_headers(),
        body={"title": title, "body": body},
    )


@cbac_guard(app_name="poc")
async def summarize_task(text: str) -> dict:
    """A plain (non-HTTP) callable: guard authorizes before it runs."""
    words = text.split()
    return {"summary": " ".join(words[:12]), "word_count": len(words)}


# ── Reporting helpers ─────────────────────────────────────────────────────────

def print_chain(workflow) -> None:
    print("\n─── Envelope chain (root → latest) ─────────────────────────")
    for i, env in enumerate(reversed(unwrap_workflow(workflow))):
        try:
            payload = json.loads(env.payload)
        except (ValueError, TypeError):
            payload = env.payload
        line = (
            f"  [{i}] {env.from_.name or env.from_.id[:16]} ({env.from_.type})"
            f" → {env.to.name or env.to.id[:16]} ({env.to.type})"
        )
        print(line)
        print(f"      payload: {json.dumps(payload)[:160]}")
        for issue in env.issues or []:
            print(f"      issue:   depth={issue.depth} {issue.reason[:120]}")
    print()


# ── Main flow ─────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--title", default="Guard PoC issue")
    parser.add_argument("--body", default="Created by the cbac_guard proof of concept.")
    parser.add_argument("--email", default=os.environ.get("GITHUB_AGENT_USER_EMAIL", ANONYMOUS_USER_EMAIL))
    parser.add_argument("--mode", choices=["remote", "local"], default=os.environ.get("CBAC_MODE", "remote"))
    parser.add_argument("--demo-plain", action="store_true", help="also run the plain-callable demo (local mode only)")
    parser.add_argument("--finalize", action="store_true", help="write the workflow provenance card at the end")
    parser.add_argument("--no-governance", action="store_true", help="run without a governance context (pass-through)")
    args = parser.parse_args()

    configure(
        mode=args.mode,
        cbac_url=os.environ.get("CBAC_URL", "https://cbac-admin.agentdna.io"),
        cbac_timeout=float(os.environ.get("CBAC_TIMEOUT", "103600")),
    )
    print(f"[guard] mode={args.mode}")

    # ── Pass-through demo: no context set, guard steps aside. ──
    if args.no_governance:
        print("[guard] no governance context — pass-through execution\n")
        result = await create_issue(repo=args.repo, title=args.title, body=args.body)
        print(f"[tool] create_issue → {json.dumps(result, indent=2)[:400]}")
        return

    if not is_agentdna_enabled():
        raise SystemExit("AGENTDNA_API_KEY is not set (or use --no-governance)")

    # ── Identity: same setup the real example uses, read-only. ──
    worker_dna = agentdna_registry.get(worker_name(), policy_file=WORKER_SKILLS_FILE)
    if worker_dna is None:
        raise SystemExit("failed to load worker AgentDNA")
    print(f"[agentdna] worker_did={worker_dna.get_actor_id()}")

    session = await UserSession.open(
        intent={
            "intent": "github_task",
            "workflow_type": "github_action",
            "submitted_by": args.email,
            "request_preview": f"Create issue '{args.title}' in {args.repo}",
        },
        submitted_by=args.email,
        submitter_email=args.email,
        first_agent=worker_dna,
    )
    if session is None:
        raise SystemExit("failed to open user session")
    print(f"[agentdna] user_did={session.user_id}")

    # Worker verifies the inbound workflow (the protocol's handle() step).
    handle_result = worker_dna.handle(session.workflow)
    print(f"[agentdna] inbound verification valid={handle_result.verification.valid}")
    if not handle_result.verification.valid:
        for issue in handle_result.verification.issues:
            print(f"[agentdna]   issue: {issue.reason}")
        raise SystemExit("inbound workflow failed verification")

    root = get_root_envelope(handle_result.workflow)
    user_intent = root.payload if root else ""

    # ── The one line of governance the developer writes. ──
    with cbac_context(
        actor=worker_dna,
        workflow=handle_result.workflow,
        user_intent=user_intent,
    ) as gov:
        print(f"\n[tool] create_issue(repo={args.repo!r}, title={args.title!r})")
        result = await create_issue(repo=args.repo, title=args.title, body=args.body)
        print(f"[tool] → {json.dumps(result, indent=2)[:400]}")

        if args.demo_plain:
            if args.mode == "local":
                print("\n[tool] summarize_task(...)")
                plain = await summarize_task(text=args.body)
                print(f"[tool] → {json.dumps(plain)[:200]}")
            else:
                print("\n[note] --demo-plain skipped: plain callables need CBAC_MODE=local")

        final_workflow = gov.workflow

    print_chain(final_workflow)

    if args.finalize:
        completed = session.complete(final_workflow)
        print(f"[agentdna] provenance written = {completed}")


if __name__ == "__main__":
    asyncio.run(main())
