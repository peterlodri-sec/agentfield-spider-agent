# claude-routine

Cloud Claude Code routine that **dispatches Linear issues to agents** (Cursor / Codex / Copilot / Claude) by opening draft PRs with `@`-tagged kickoff comments, then **monitors** those PRs and syncs CI status back to Linear.

## What it does

```
┌──────────────────────────────────────────────────────────────────────┐
│  Hetzner box, every 30 min via systemd timer                          │
│                                                                       │
│  ┌────────────────┐    ┌──────────────────┐    ┌──────────────────┐   │
│  │ dispatch.py    │    │ monitor.py       │    │ Self-implement   │   │
│  │ (one issue →   │    │ (open PRs →      │    │ agent-claude PRs │   │
│  │  draft PR +    │    │  Linear status   │    │ (when applicable)│   │
│  │  @-mention)    │    │  comments)       │    │                  │   │
│  └────────┬───────┘    └────────┬─────────┘    └────────┬─────────┘   │
│           │                     │                       │             │
└───────────┼─────────────────────┼───────────────────────┼─────────────┘
            │                     │                       │
            ▼                     ▼                       ▼
       Linear (issue +       GitHub (PR +              GitHub (push +
       comment)              kickoff comment)          PR ready)
```

The routine never auto-merges. Humans (Peter) gate the merge.

## Conventions it relies on

| Linear label        | What dispatch does                                     |
|---------------------|--------------------------------------------------------|
| `agent-cursor`      | `@cursor` tagged in PR kickoff                         |
| `agent-codex`       | `@codex` tagged in PR kickoff                          |
| `agent-copilot`     | `@github-copilot` tagged + a model from `model-*` label|
| `agent-claude`      | `@claude` tagged; the routine itself may implement     |
| `model-gpt5`        | Used with `agent-copilot`: GPT-5                       |
| `model-claude-opus` | Used with `agent-copilot`: Claude Opus 4.7             |
| `model-gemini-pro`  | Used with `agent-copilot`: Gemini 2.5 Pro              |

Each issue must have **exactly one** `agent-*` label and (for copilot) **exactly one** `model-*` label. Multiple labels of the same family are processed in the order listed in `dispatch.py:AGENT_LABELS`.

Branch names follow Linear's `branchName` field as-is (e.g. `peterlodri/vib-14-delete-sessions-persist_profile-on-close`).

## Files

| File                 | What                                                       |
|----------------------|------------------------------------------------------------|
| `dispatch.py`        | Pick one Linear issue, open a draft PR with kickoff.       |
| `monitor.py`         | Walk open PRs, post status changes to Linear.              |
| `runbook.md`         | Prompt for the Hetzner Claude Code session — copy/paste.   |
| `README.md`          | This file.                                                 |

## Local usage (development)

```bash
# from the spider-agent repo root
export LINEAR_API_KEY="lin_api_..."          # Linear settings → API → Personal API keys
export GH_TOKEN="gho_..."                    # already in env on most setups

# dry-run dispatch: see what it WOULD do, no PR opens
.venv/bin/python scripts/claude-routine/dispatch.py --dry-run --once

# real dispatch (one issue, draft PR + kickoff comment)
.venv/bin/python scripts/claude-routine/dispatch.py --once

# filter to one agent
.venv/bin/python scripts/claude-routine/dispatch.py --once --agent copilot

# monitor: walk open PRs, post status changes
.venv/bin/python scripts/claude-routine/monitor.py
.venv/bin/python scripts/claude-routine/monitor.py --dry-run
```

Both scripts are stdlib-only (no extra pip installs needed). Tested on Python 3.13+.

## Hetzner deployment

### One-time host setup

```bash
# on the Hetzner box
sudo useradd -m -s /bin/bash routine
sudo -iu routine
git clone https://github.com/peterlodri-sec/agentfield-spider-agent.git
cd agentfield-spider-agent

# install uv + Python 3.13
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.13
.venv/bin/uv sync

# install gh + claude
sudo apt install gh
# install Claude Code per https://docs.claude.com/en/docs/claude-code (Hetzner needs the linux .deb)

# auth (run as the routine user)
gh auth login                                # web flow once
claude /login                                # OAuth flow once

# env file — read by systemd unit below
mkdir -p ~/.config/claude-routine
cat > ~/.config/claude-routine/env <<'EOF'
LINEAR_API_KEY=lin_api_xxxxxxxxxxxxxxxxxx
GH_TOKEN=gho_xxxxxxxxxxxxxxxxxx
GIT_AUTHOR_NAME=claude-routine
GIT_AUTHOR_EMAIL=routine+claude@your-domain.example
PATH=/home/routine/.local/bin:/usr/local/bin:/usr/bin:/bin
EOF
chmod 600 ~/.config/claude-routine/env
```

### systemd unit + timer

`/etc/systemd/system/claude-routine.service`:

```ini
[Unit]
Description=claude-routine: dispatch + monitor Linear issues
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=routine
WorkingDirectory=/home/routine/agentfield-spider-agent
EnvironmentFile=/home/routine/.config/claude-routine/env
ExecStart=/bin/bash -lc 'claude --print < scripts/claude-routine/runbook.md'
TimeoutStartSec=2100
StandardOutput=append:/home/routine/.local/var/log/claude-routine.log
StandardError=append:/home/routine/.local/var/log/claude-routine.log
```

`/etc/systemd/system/claude-routine.timer`:

```ini
[Unit]
Description=Run claude-routine every 30 minutes during business hours

[Timer]
# Every 30 min between 07:00-19:00 CET; every 2 hours otherwise.
OnCalendar=*-*-* 07,07:30,08,08:30,09,09:30,10,10:30,11,11:30,12,12:30,13,13:30,14,14:30,15,15:30,16,16:30,17,17:30,18,18:30,19:00:00 Europe/Berlin
OnCalendar=*-*-* 21,23,01,03,05:00:00 Europe/Berlin
Persistent=true

[Install]
WantedBy=timers.target
```

Activate:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now claude-routine.timer
systemctl status claude-routine.timer
journalctl -u claude-routine -f
```

### Pause / resume

`touch /home/routine/agentfield-spider-agent/.routine-paused` to stop dispatching. `rm` to resume. The runbook checks this file.

### Logs + state

- Runtime log: `/home/routine/.local/var/log/claude-routine.log`
- State (which PRs we've commented on): `<repo>/.routine-state.json` (gitignored)
- Per-run audit: `scripts/claude-routine/runs/<ISO>.log` (gitignored)

## Safety properties

- **No auto-merge.** Only Peter merges PRs.
- **No force-push** on agent-owned branches.
- **No mutation** of Linear issue state (state transitions are out of v1 scope — comments only).
- **One issue per invocation** by default (`--once`). Override with care.
- **Idempotent monitor**: `monitor.py` posts a Linear comment only when state changes from the previous invocation.
- **Pre-flight check**: dispatch skips issues that already have an open PR for the matching branch name.

## Bugs / known gaps

See the open Linear issues tagged `claude-routine` for the backlog.

If the routine misbehaves:
1. `touch .routine-paused` on the host
2. Inspect `/home/routine/.local/var/log/claude-routine.log`
3. Inspect `.routine-state.json` for stuck state entries
4. File a bug as a `triage`-labeled Linear issue with the log snippet.
