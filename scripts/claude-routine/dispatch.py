#!/usr/bin/env python3
"""claude-routine/dispatch.py — pick a Linear issue, open a draft PR with kickoff.

Usage::

    LINEAR_API_KEY=lin_api_... GH_TOKEN=gho_... python dispatch.py --once
    python dispatch.py --dry-run                # don't push or comment
    python dispatch.py --agent cursor --once    # filter to one agent
    python dispatch.py --repo /path/to/repo     # explicit checkout

Flow per issue:

  1. Query Linear for Backlog issues with an ``agent-*`` label.
  2. Skip ones that already have an open PR for ``branchName``.
  3. Pick the highest-priority remaining issue.
  4. Identify agent (cursor / codex / copilot / claude) + model from labels.
  5. Branch from ``origin/main`` to Linear's ``branchName``.
  6. Empty commit (kickoff scaffold).
  7. Push branch + open a draft PR.
  8. Comment on the PR with the agent ``@mention`` + kickoff prompt.
  9. Comment on the Linear issue with the PR URL.

Designed to be invoked by a Hetzner-side Claude Code session via cron.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, cast

# ─── constants ──────────────────────────────────────────────────────
LINEAR_API_URL = "https://api.linear.app/graphql"

# Agent labels we recognize. Order = preference when multiple agent
# labels appear on the same issue (shouldn't happen in practice).
AGENT_LABELS: list[str] = [
    "agent-cursor",
    "agent-codex",
    "agent-copilot",
    "agent-claude",
]

# Model labels (only meaningful for copilot today).
MODEL_LABELS: dict[str, str] = {
    "model-gpt5": "GPT-5",
    "model-claude-opus": "Claude Opus 4.7",
    "model-gemini-pro": "Gemini 2.5 Pro",
}

# Per-agent invocation config.
AGENTS: dict[str, dict[str, Any]] = {
    "cursor": {
        "handle": "@cursor",
        "requires_model": False,
        "extra_hint": (
            "Cursor: please pull the branch in your IDE, follow the "
            "kickoff, run `uv run pytest -q` locally, then push."
        ),
    },
    "codex": {
        "handle": "@codex",
        "requires_model": False,
        "extra_hint": (
            "Codex: this is a greenfield/isolated task suited to TDD. "
            "Start with the test file, then the module."
        ),
    },
    "copilot": {
        "handle": "@github-copilot",
        "requires_model": True,
        "extra_hint": (
            "Copilot Workspace: please proceed with the model named "
            "above. Run `uv run pytest -q` before pushing."
        ),
    },
    "claude": {
        "handle": "@claude",
        "requires_model": False,
        "extra_hint": (
            "Claude Code Action: please pick up the branch, follow the "
            "acceptance criteria, and push commits to this PR."
        ),
    },
}


# ─── Linear GraphQL client (stdlib only) ────────────────────────────
def linear_query(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise RuntimeError("LINEAR_API_KEY env var is required")
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        LINEAR_API_URL,
        data=body,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Linear HTTP {e.code}: {e.read().decode()}") from e
    if "errors" in data:
        raise RuntimeError(f"Linear GraphQL errors: {data['errors']}")
    return cast(dict[str, Any], data["data"])


LIST_ISSUES_QUERY = """
query EligibleIssues($labels: [String!]!) {
  issues(
    filter: {
      labels: { name: { in: $labels } }
      state: { type: { in: ["backlog", "unstarted"] } }
    }
    orderBy: updatedAt
    first: 50
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      branchName
      url
      labels { nodes { name } }
      state { name type }
      project { id name }
      team { key }
    }
  }
}
"""

CREATE_COMMENT_MUTATION = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id }
  }
}
"""


def list_eligible_issues() -> list[dict[str, Any]]:
    data = linear_query(LIST_ISSUES_QUERY, {"labels": AGENT_LABELS})
    return cast(list[dict[str, Any]], data["issues"]["nodes"])


def post_linear_comment(issue_id: str, body: str, *, dry_run: bool = False) -> None:
    if dry_run:
        print(f"  [dry-run] Linear comment ({len(body)} chars):")
        for line in body.splitlines():
            print(f"  | {line}")
        return
    linear_query(CREATE_COMMENT_MUTATION, {"issueId": issue_id, "body": body})


# ─── git + gh helpers ───────────────────────────────────────────────
def run(cmd: list[str], *, cwd: str | None = None, check: bool = True) -> str:
    """Run a subprocess; on failure include stderr in the raised error.

    Default ``subprocess.run(check=True)`` raises ``CalledProcessError``
    but discards stderr from the message. Real-world failures (gh rate
    limit, git push conflict, auth failure) all have informative stderr
    — surface it so the routine's log is actually useful.
    """
    res = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, text=True)
    if check and res.returncode != 0:
        short = " ".join(cmd[:3]) + (" ..." if len(cmd) > 3 else "")
        msg = f"command failed (exit {res.returncode}): {short}"
        if res.stderr.strip():
            msg += f"\nstderr: {res.stderr.strip()}"
        if res.stdout.strip():
            msg += f"\nstdout: {res.stdout.strip()}"
        raise RuntimeError(msg)
    return res.stdout.strip()


def branch_has_open_pr(branch: str, *, repo: str) -> bool:
    """True when ``gh pr list --head <branch> --state open`` is non-empty.

    Returns ``False`` (rather than raising) on gh failure — callers
    treat that as "unknown / don't skip". Errors get printed so a rate
    limit or auth issue isn't silently swallowed.
    """
    try:
        out = run(
            ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number"],
            cwd=repo,
        )
    except RuntimeError as e:
        print(f"  warn: branch_has_open_pr({branch!r}) failed: {e}")
        return False
    return bool(json.loads(out or "[]"))


# ─── kickoff prompt ─────────────────────────────────────────────────
def build_kickoff(
    *,
    handle: str,
    agent: str,
    model: str | None,
    issue: dict[str, Any],
    branch: str,
) -> str:
    """Build the @-tag kickoff comment for the assigned agent."""
    identifier = issue["identifier"]
    title = issue["title"]
    description = issue.get("description") or ""

    model_line = ""
    if model is not None:
        model_line = f"\n**Model:** {model}"

    hint = AGENTS[agent].get("extra_hint", "")

    return (
        f"{handle} please pick this up.\n\n"
        f"**Linear:** [{identifier}]({issue['url']}) — {title}\n"
        f"**Branch:** `{branch}`\n"
        f"**Base:** `main`{model_line}\n\n"
        f"---\n\n"
        f"### Acceptance criteria\n\n"
        f"{description}\n\n"
        f"---\n\n"
        f"### House rules\n\n"
        f"- Match existing style: `ruff check` + `mypy` + `pyright` (strict on new modules) clean.\n"
        f"- Run `.venv/bin/pytest -q` (or `uv run pytest -q`) before pushing — all green required.\n"
        f"- Push commits to this branch; the routine watches CI and updates Linear.\n"
        f"- Keep PR scope tight: only changes that trace back to the acceptance criteria.\n"
        f"- When ready, mark the PR **Ready for review** and request review from `@peterlodri`.\n\n"
        f"_{hint}_\n\n"
        f"---\n\n"
        f"_Kickoff dispatched by_ `claude-routine/dispatch.py`."
    )


# ─── PR lifecycle ───────────────────────────────────────────────────
def open_pr_with_kickoff(
    *,
    issue: dict[str, Any],
    agent: str,
    model: str | None,
    repo: str,
    dry_run: bool,
) -> str | None:
    """Branch + empty commit + push + draft PR + kickoff comment.

    Returns the PR URL on success, ``None`` in dry-run.
    """
    branch = issue["branchName"]
    identifier = issue["identifier"]
    title = issue["title"]
    description = issue.get("description") or ""

    print(f"  → branch: {branch}")

    if dry_run:
        print("  [dry-run] would: fetch, branch, empty commit, push, gh pr create, gh pr comment")
        return None

    # Fetch latest origin/main.
    run(["git", "fetch", "origin", "main"], cwd=repo)

    # Branch from origin/main. -B reuses local branch if it exists.
    run(["git", "checkout", "-B", branch, "origin/main"], cwd=repo)

    # Empty kickoff commit.
    commit_body = (
        f"chore({identifier.lower()}): kickoff — {title}\n\n"
        f"Linear: {issue['url']}\n\n"
        f"Empty kickoff commit. The assigned agent ({agent}"
        f"{f', model {model}' if model else ''}) "
        f"will add the implementation in subsequent commits."
    )
    run(["git", "commit", "--allow-empty", "-m", commit_body], cwd=repo)

    # Push branch.
    run(["git", "push", "-u", "origin", branch], cwd=repo)

    # Open draft PR.
    pr_body = (
        f"## {identifier}: {title}\n\n"
        f"Linear: {issue['url']}\n\n"
        f"### Description\n\n{description}\n\n"
        f"---\n\n"
        f"This PR was opened by `claude-routine/dispatch.py` and is awaiting "
        f"kickoff from `{agent}`"
        f"{f' (model: {model})' if model else ''}. "
        f"Watch the PR for the agent's first commit.\n\n"
        f"**Status flow:**\n"
        f"- 🟡 Draft (now) → assigned agent has not pushed code yet\n"
        f"- 🟢 Ready for review → agent has finished + CI green\n"
        f"- 🔴 Failing → CI red; routine will comment with details"
    )
    pr_url = run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            f"{identifier}: {title}",
            "--body",
            pr_body,
        ],
        cwd=repo,
    )
    print(f"  ✓ opened {pr_url}")

    # Kickoff @-mention comment.
    handle = AGENTS[agent]["handle"]
    kickoff = build_kickoff(
        handle=handle,
        agent=agent,
        model=model,
        issue=issue,
        branch=branch,
    )
    run(["gh", "pr", "comment", pr_url, "--body", kickoff], cwd=repo)
    print("  ✓ kickoff comment posted")

    return pr_url


# ─── selection logic ────────────────────────────────────────────────
def select_agent(labels: list[str]) -> tuple[str | None, str | None]:
    """Pick (agent_name, model_name) from an issue's labels.

    Order of ``AGENT_LABELS`` defines preference when multiple
    ``agent-*`` labels appear on one issue. ``model`` is only set when
    the chosen agent's ``requires_model`` is True.
    """
    agent: str | None = None
    for lbl in AGENT_LABELS:
        if lbl in labels:
            agent = lbl.removeprefix("agent-")
            break
    if agent is None:
        return None, None
    model: str | None = None
    if AGENTS[agent]["requires_model"]:
        for lbl, name in MODEL_LABELS.items():
            if lbl in labels:
                model = name
                break
    return agent, model


def filter_issues(
    issues: list[dict[str, Any]],
    *,
    agent_filter: str | None,
    repo: str,
) -> list[dict[str, Any]]:
    """Drop issues we can't or shouldn't dispatch.

    Sorted by Linear priority ascending (1=Urgent first). Issues with
    priority 0 (None) sort last.
    """
    out: list[dict[str, Any]] = []
    for issue in issues:
        labels = [n["name"] for n in issue["labels"]["nodes"]]
        agent, _ = select_agent(labels)
        if agent is None:
            continue
        if agent_filter is not None and agent != agent_filter:
            continue
        if branch_has_open_pr(issue["branchName"], repo=repo):
            print(
                f"  skip {issue['identifier']}: branch {issue['branchName']!r} already has open PR"
            )
            continue
        out.append(issue)

    def _key(x: dict[str, Any]) -> tuple[int, str]:
        # priority 0 (None) → 5, so it sorts last.
        p = x.get("priority") or 5
        return (p, x["identifier"])

    out.sort(key=_key)
    return out


# ─── main ───────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="dispatch one issue then exit")
    ap.add_argument("--dry-run", action="store_true", help="don't push or comment")
    ap.add_argument(
        "--agent",
        choices=list(AGENTS.keys()),
        help="filter to one agent (cursor/codex/copilot/claude)",
    )
    ap.add_argument("--repo", default=os.getcwd(), help="path to spider-agent checkout")
    args = ap.parse_args()

    print(f"=== claude-routine/dispatch ({'dry-run' if args.dry_run else 'live'}) ===")
    print(f"repo: {args.repo}")
    if args.agent:
        print(f"agent filter: {args.agent}")
    print()

    try:
        issues = list_eligible_issues()
    except RuntimeError as e:
        print(f"FATAL: Linear list_eligible_issues failed: {e}")
        print("Exiting. Fix LINEAR_API_KEY / connectivity / Linear status, then retry.")
        return 2
    print(f"Linear: {len(issues)} backlog issue(s) with agent-* labels")

    eligible = filter_issues(issues, agent_filter=args.agent, repo=args.repo)
    print(f"Eligible (after PR + agent filter): {len(eligible)}")

    if not eligible:
        print()
        print("Nothing to do.")
        return 0

    to_process = eligible[:1] if args.once else eligible

    for issue in to_process:
        labels = [n["name"] for n in issue["labels"]["nodes"]]
        agent, model = select_agent(labels)
        if agent is None:
            print(f"  SKIP {issue['identifier']}: no agent label")
            continue
        print()
        print(f"=== {issue['identifier']}: {issue['title']} ===")
        print(f"  priority: {issue['priority']}, agent: {agent}, model: {model}")

        pr_url = open_pr_with_kickoff(
            issue=issue,
            agent=agent,
            model=model,
            repo=args.repo,
            dry_run=args.dry_run,
        )

        if pr_url is None and not args.dry_run:
            # gh failed; skip Linear comment.
            print("  ✗ PR creation failed; skipping Linear comment")
            continue

        if pr_url is not None:
            comment_body = (
                f"Dispatched to **{agent}**"
                f"{f' (model: {model})' if model else ''}.\n\n"
                f"PR: {pr_url}\n"
                f"Branch: `{issue['branchName']}`\n\n"
                f"_Opened by_ `claude-routine/dispatch.py`."
            )
            post_linear_comment(issue_id=issue["id"], body=comment_body, dry_run=False)
            print("  ✓ Linear comment posted")
        elif args.dry_run:
            # Show what we would have posted.
            handle = AGENTS[agent]["handle"]
            kickoff = build_kickoff(
                handle=handle,
                agent=agent,
                model=model,
                issue=issue,
                branch=issue["branchName"],
            )
            print()
            print("  --- [dry-run] PR kickoff comment ---")
            for line in kickoff.splitlines():
                print(f"  | {line}")
            print("  --- [dry-run] end kickoff ---")

    print()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
