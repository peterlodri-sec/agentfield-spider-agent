#!/usr/bin/env python3
"""claude-routine/monitor.py — sync open PR status back to Linear.

Usage::

    LINEAR_API_KEY=... GH_TOKEN=... python monitor.py
    python monitor.py --dry-run
    python monitor.py --repo /path/to/repo

For each open PR opened by the routine (identified by branch-name
pattern matching Linear's ``identifier`` like ``vib-XX``), this script:

  1. Reads PR mergeability + CI status via ``gh pr view``.
  2. Detects whether the assigned agent has pushed beyond the kickoff
     commit.
  3. Posts a compact status comment to the linked Linear issue when
     state changes since the last visit (tracked in ``.routine-state.json``).
  4. If CI is red, surfaces the failed-checks summary in the comment.

Does NOT auto-merge or auto-rebase. That's intentional — humans + the
assigned agent own the merge gate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

LINEAR_API_URL = "https://api.linear.app/graphql"

# Linear identifier pattern inside a branch name. We match VIB-NN style
# (team_key + dash + number). Adjust if you add more teams.
_BRANCH_ID_RE = re.compile(r"\b([A-Z]{2,5})-?(\d+)\b", re.IGNORECASE)


# ─── Linear client ──────────────────────────────────────────────────
def linear_query(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        raise RuntimeError("LINEAR_API_KEY env var is required")
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        LINEAR_API_URL,
        data=body,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Linear HTTP {e.code}: {e.read().decode()}") from e
    if "errors" in data:
        raise RuntimeError(f"Linear GraphQL errors: {data['errors']}")
    return cast(dict[str, Any], data["data"])


GET_ISSUE_QUERY = """
query IssueById($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    url
    state { name type }
  }
}
"""

CREATE_COMMENT_MUTATION = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
  }
}
"""


def get_issue_by_identifier(identifier: str) -> dict[str, Any] | None:
    """Fetch an issue by ``VIB-NN`` identifier. Linear API accepts that as id."""
    try:
        data = linear_query(GET_ISSUE_QUERY, {"id": identifier})
    except RuntimeError as e:
        print(f"  Linear lookup failed for {identifier}: {e}")
        return None
    issue = data.get("issue")
    return cast(dict[str, Any] | None, issue)


def post_linear_comment(issue_id: str, body: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] Linear comment ({len(body)} chars):")
        for line in body.splitlines():
            print(f"  | {line}")
        return
    linear_query(CREATE_COMMENT_MUTATION, {"issueId": issue_id, "body": body})


# ─── gh helpers ─────────────────────────────────────────────────────
def run(cmd: list[str], *, cwd: str | None = None, check: bool = True) -> str:
    """Run a subprocess; on failure include stderr in the raised error.

    Same pattern as ``dispatch.run``: bare ``subprocess.run(check=True)``
    discards stderr, which makes rate-limit / auth failures invisible
    in the routine log. We surface it.
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


def list_my_open_prs(repo: str) -> list[dict[str, Any]]:
    out = run(
        [
            "gh", "pr", "list",
            "--author", "@me",
            "--state", "open",
            "--json", "number,title,headRefName,baseRefName,isDraft,mergeable,url,commits,statusCheckRollup",
            "--limit", "50",
        ],
        cwd=repo,
    )
    return cast(list[dict[str, Any]], json.loads(out or "[]"))


def branch_identifier(branch: str) -> str | None:
    """Pull a Linear-style identifier (e.g. ``VIB-14``) out of a branch name.

    Linear's gitBranchName looks like
    ``peterlodri/vib-14-delete-sessions-persist_profile-on-close``.
    We match the first ``<TEAM>-<NUM>`` substring case-insensitive.
    """
    m = _BRANCH_ID_RE.search(branch)
    if not m:
        return None
    return f"{m.group(1).upper()}-{m.group(2)}"


def ci_summary(pr: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (emoji, failed_check_names) for the PR's check rollup."""
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return ("⏳", [])  # no checks yet
    failed: list[str] = []
    pending = False
    for c in checks:
        # `gh` rolls up GH Actions + status checks; both have a
        # `conclusion` and `state` field (one or the other).
        state = (c.get("state") or "").upper()
        conclusion = (c.get("conclusion") or "").upper()
        name = c.get("name") or c.get("context") or "(unknown)"
        if conclusion == "FAILURE" or state == "FAILURE":
            failed.append(name)
        elif state == "PENDING" or conclusion in ("", "NEUTRAL"):
            pending = True
    if failed:
        return ("🔴", failed)
    if pending:
        return ("🟡", [])
    return ("🟢", [])


# ─── state tracking ─────────────────────────────────────────────────
def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return cast(dict[str, Any], json.loads(path.read_text()))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


# ─── main ───────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="don't post Linear comments")
    ap.add_argument("--repo", default=os.getcwd(), help="path to spider-agent checkout")
    ap.add_argument(
        "--state-file",
        default=None,
        help="path to JSON state file (default: <repo>/.routine-state.json)",
    )
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    state_path = Path(args.state_file) if args.state_file else repo / ".routine-state.json"
    state = load_state(state_path)

    print(f"=== claude-routine/monitor ({'dry-run' if args.dry_run else 'live'}) ===")
    print(f"repo: {repo}")
    print(f"state: {state_path}")
    print()

    try:
        prs = list_my_open_prs(str(repo))
    except RuntimeError as e:
        print(f"FATAL: gh pr list failed: {e}")
        print("Exiting without state update. Retry once gh's rate limit / auth resolves.")
        return 2
    print(f"Open PRs authored by me: {len(prs)}")

    updated = 0
    for pr in prs:
        branch = pr["headRefName"]
        identifier = branch_identifier(branch)
        if identifier is None:
            print(f"  skip PR #{pr['number']}: no Linear ID in branch {branch!r}")
            continue

        print()
        print(f"--- {identifier}: PR #{pr['number']} ({branch}) ---")

        emoji, failed = ci_summary(pr)
        n_commits = len(pr.get("commits") or [])
        kickoff_only = n_commits == 1  # routine pushed an empty kickoff
        is_draft = pr.get("isDraft", False)
        mergeable = pr.get("mergeable", "UNKNOWN")

        print(f"  ci: {emoji}, commits: {n_commits}, draft: {is_draft}, mergeable: {mergeable}")
        if failed:
            print(f"  failed checks: {', '.join(failed)}")

        # State key for change detection.
        prev = state.get(pr["url"], {})
        new_state = {
            "ci": emoji,
            "kickoff_only": kickoff_only,
            "draft": is_draft,
            "mergeable": mergeable,
            "n_commits": n_commits,
            "failed_checks": sorted(failed),
        }

        if new_state == prev:
            print("  no change since last visit; skipping Linear comment")
            continue

        # State changed → look up Linear issue + comment.
        issue = get_issue_by_identifier(identifier)
        if issue is None:
            print(f"  could not find Linear issue {identifier}; recording state anyway")
            state[pr["url"]] = new_state
            continue

        # Build the Linear comment body.
        parts = [f"**PR status:** {emoji}"]
        if kickoff_only and is_draft:
            parts.append("Still kickoff-only (assigned agent has not pushed yet).")
        elif is_draft:
            parts.append(f"Draft with {n_commits} commit(s) — work in progress.")
        elif emoji == "🟢":
            parts.append(f"Ready for review (CI green, {n_commits} commits).")
        elif emoji == "🔴":
            parts.append("CI failing. Failed checks:\n- " + "\n- ".join(failed))
        elif emoji == "🟡":
            parts.append("CI pending.")

        parts.append(f"\nPR: {pr['url']}")
        parts.append("\n_Updated by_ `claude-routine/monitor.py`.")
        body = "\n\n".join(parts)

        post_linear_comment(issue_id=issue["id"], body=body, dry_run=args.dry_run)
        state[pr["url"]] = new_state
        updated += 1

    if not args.dry_run:
        save_state(state_path, state)
    print()
    print(f"Done. {updated} status update(s) posted to Linear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
