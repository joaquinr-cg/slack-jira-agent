# JIRA Slack Agent - Project Status & Roadmap

## Overview

The JIRA Slack Agent is a multi-tenant system that helps Product Managers (PMs) keep their JIRA tickets synchronized with decisions made in Slack conversations and team meetings (Google Drive transcripts).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TRIGGER FLOW (DEPLOYED)                          │
│  Background scheduler checks GDrive for new transcripts per PM            │
│                                                                            │
│  TranscriptScheduler (Python)        TranscriptTrigger (LangBuilder)       │
│  ┌──────────────┐                    ┌──────────────────────────────┐       │
│  │ Every N min: │──── per PM ──────▶│  GDrive check + timestamp   │       │
│  │ list PMs     │                    │  comparison via LangBuilder │       │
│  │ from DynamoDB│◀── result ────────│  trigger flow               │       │
│  └──────┬───────┘                    └──────────────────────────────┘       │
│         │ new transcript found                                             │
│         ▼                                                                  │
│  ┌──────────────┐                                                          │
│  │ Notify PM    │  → sends "Generate Tickets" button via Slack DM          │
│  │ via Slack DM │  → updates last_processed_transcript in DynamoDB        │
│  └──────────────┘                                                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SLACK MICROSERVICE (DEPLOYED)                       │
│  Python service on EC2 — Slack Bolt + Socket Mode                          │
│                                                                            │
│  ✅ /jira-sync command               ✅ DynamoDB PM config lookup          │
│  ✅ Emoji reactions for marking       ✅ Per-PM tweaks to LangBuilder      │
│  ✅ Proposal approve/reject buttons   ✅ Transcript scheduler (background) │
│  ✅ /jira-agent PM onboarding         ✅ Admin commands                    │
│  ✅ Local SQLite for sessions         ✅ Shared GDrive SA + PM overrides   │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       MAIN FLOW (DEPLOYED IN LANGBUILDER)                   │
│  Generates JIRA ticket proposals from multiple sources                      │
│                                                                            │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌───────────────┐  │
│  │   Smart     │──▶│   Agent     │──▶│   JIRA      │──▶│  Proposals   │  │
│  │ Enrichment  │   │   (LLM)     │   │   Tools     │   │  to Slack    │  │
│  └─────────────┘   └─────────────┘   └─────────────┘   └───────────────┘  │
│        │                                                                    │
│        ▼                                                                    │
│  Data Sources:                                                              │
│  - Slack messages (marked with emoji)                                       │
│  - Google Drive transcript (latest meeting)                                 │
│  - Current JIRA state (for comparison)                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            DYNAMODB (DEPLOYED)                              │
│  Table: pm_configurations                                                   │
│  Stores per-PM credentials, GDrive folder overrides, and settings           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Two LangBuilder Flows

| Flow | Config Key | Purpose |
|------|-----------|---------|
| **Main Flow** | `LANGBUILDER_FLOW_ID` | Analyzes messages/transcripts, generates JIRA proposals, executes approved changes |
| **Trigger Flow** | `TRIGGER_FLOW_ID` | Checks GDrive for new transcripts per PM (uses `TranscriptTrigger` component) |

Both flows share the same `LANGBUILDER_FLOW_URL` and `LANGBUILDER_API_KEY`.

> **Note**: The `LangBuilderClient` uses a hardcoded `CHAT_INPUT_ID = "ChatInput-UMrKl"`. If the trigger flow's ChatInput has a different component ID, it must either match or this value needs to be made configurable.

---

## Completed Phases

### Phase 1: Multi-Tenant Support

| Component | Status | Details |
|-----------|--------|---------|
| `src/dynamodb_client.py` | Done | Full CRUD: `get_pm_config`, `create_pm`, `update_pm`, `disable_pm`, `enable_pm`, `list_enabled_pms`, `update_last_processed` |
| `build_tweaks_from_pm_config()` | Done | Maps DynamoDB config to LangBuilder tweaks using component instance IDs. Shared GDrive SA with per-PM `folder_id`/`client_email` overrides |
| `src/slack_handler.py` | Done | `_process_jira_sync()` and `_send_approval_decisions_to_llm()` both load PM config and pass tweaks |
| `src/langbuilder_client.py` | Done | `run_flow()` accepts `extra_tweaks` parameter |
| `src/config.py` | Done | `aws_region`, `dynamodb_table_name`, shared `GDRIVE_*` settings |
| `src/main.py` | Done | Initializes DynamoDB client on startup |

### Phase 3: Auto-Trigger Flow

| Component | Status | Details |
|-----------|--------|---------|
| `src/transcript_scheduler.py` | Done | Background async loop polling every N minutes, iterates all enabled PMs |
| `langbuilder_components/transcript_trigger.py` | Done | LangBuilder component: checks GDrive folder, compares timestamps, returns new file list |
| DynamoDB Reader component | Skipped | Not needed — scheduler reads PMs directly via `dynamodb_client.py` and passes config via tweaks |
| Slack notifications | Done | DMs PM with "Generate Tickets from Latest Transcript" button when new transcripts found |
| Button trigger | Done | Button click triggers main flow with `transcripts_only` command via full proposal/approve/reject workflow |
| Manual check | Done | `/jira-agent check-transcripts` triggers transcript check immediately |
| `update_last_processed` | Done | Called before triggering sync to prevent duplicate triggers |
| Config | Done | `TRIGGER_FLOW_ID`, `TRIGGER_INTERVAL_MINUTES`, `TRIGGER_AUTO_SYNC` |

### Phase 4: PM Onboarding & Admin

| Command | Status | Details |
|---------|--------|---------|
| `/jira-agent setup` | Done | Full onboarding modal (name, email, JIRA config, GDrive config) |
| `/jira-agent config` | Done | Ephemeral message with secrets masked |
| `/jira-agent update jira` | Done | Modal pre-filled with current values (token left empty) |
| `/jira-agent update gdrive` | Done | Modal pre-filled with current values (private key left empty) |
| `/jira-agent admin list` | Done | Lists all enabled PMs |
| `/jira-agent admin disable <id>` | Done | Disables a PM |
| `/jira-agent admin enable <id>` | Done | Enables a PM |
| `/jira-agent check-transcripts` | Done | Manually trigger transcript check for the requesting PM |
| `/jira-agent admin stats` | Done | PM count, sessions, proposals, pending messages |
| Admin gating | Done | `ADMIN_USER_IDS` env var (if empty, all users are admin) |

> **Slack App requirement**: The `/jira-agent` slash command must be registered in the Slack App configuration (api.slack.com > Your App > Slash Commands).

### Infrastructure

| Component | Status |
|-----------|--------|
| CI/CD (GitHub Actions > EC2) | Done |
| Docker deployment | Done |
| DynamoDB table `pm_configurations` | Done |
| IAM role `slack-jira-agent-ec2-role` | Done |
| IAM inline policy for `cgbot` user | Done |

### Bug Fixes Applied

| Issue | Fix | File |
|-------|-----|------|
| JSON parse error from LLM comments (`//`) | Updated system prompt to forbid comments | System prompt v2 |
| SQLite dict serialization error | Added `_serialize_value()` for dicts | `src/db/manager.py` |
| NULL ticket_key for create_issue | Changed fallback to `or "NEW"` | `src/slack_handler.py` |
| Dict slicing error in proposal display | Added isinstance check | `src/slack_handler.py` |

---

## Pending Work

### Phase 2: Transcripts Only Mode — DONE

- [x] Microservice reads `transcripts_only` from DynamoDB `flow_config`
- [x] `/jira-sync --transcripts-only` CLI override
- [x] `transcripts_only` is a separate command (same level as `/jira-sync` and `approval_decisions`)
- [x] Smart Enrichment v2 handles `transcripts_only` command — skips Slack messages section, only uses GDrive transcript + JIRA state
- [x] Transcript notification button triggers main flow with `transcripts_only` command via full proposal/approve/reject workflow

### Component Instance IDs — DONE

LangBuilder tweaks use component **instance IDs** (not class names):

| Flow | Component | Instance ID |
|------|-----------|-------------|
| Jira Tickets | GoogleDriveDocsParserSA | `CustomComponent-swCo4` |
| Jira Tickets | JiraStateFetcher | `CustomComponent-h9t4Q` |
| Jira Tickets | JiraReaderWriter | `CustomComponent-MvTpp` |
| Trigger | TranscriptTrigger | `TranscriptTrigger-NxiAw` |

### Known Technical Debt

- `send_continuation()` method in `langbuilder_client.py` is unused — can be removed

---

## File Structure

```
slack_jira_agent/
├── .github/
│   └── workflows/
│       └── deploy.yml                  # CI/CD pipeline
├── docs/
│   ├── DYNAMODB_SCHEMA.md              # DynamoDB table documentation
│   ├── PM_USER_GUIDE.md                # PM-facing user guide
│   ├── PROJECT_STATUS.md               # This file
│   └── pm_config_template.json         # Template for PM configuration
├── langbuilder_components/
│   ├── jira_tickets/                   # Main flow components
│   │   ├── agent.py                    # LangBuilder agent component
│   │   ├── g_drive_doc_parser.py       # Google Drive parser (CustomComponent-swCo4)
│   │   ├── jira_smart_enrichment_v2.py # Smart enrichment (analysis/execution routing)
│   │   ├── jira_state_fetcher.py       # JIRA state reader (CustomComponent-h9t4Q)
│   │   ├── jira_tool.py               # JIRA reader/writer (CustomComponent-MvTpp)
│   │   └── system_prompt.md            # Agent system prompt
│   └── automatic_parser/               # Trigger flow components
│       ├── transcript_trigger.py       # GDrive transcript checker (TranscriptTrigger-NxiAw)
│       ├── dynamodb_config_reader.py   # DynamoDB PM config reader
│       ├── folder_extractor.py         # Folder ID extractor
│       └── json_extractor.py           # JSON field extractor
├── src/
│   ├── __init__.py
│   ├── config.py                       # Pydantic settings (env vars)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── manager.py                  # SQLite operations
│   │   └── models.py                   # Data models
│   ├── dynamodb_client.py              # DynamoDB CRUD for PM configs
│   ├── langbuilder_client.py           # LangBuilder API client
│   ├── main.py                         # App entry point + scheduler start
│   ├── slack_handler.py                # Slack event handlers + modals
│   └── transcript_scheduler.py         # Background GDrive polling loop
├── data/                               # SQLite database (gitignored)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── JIRA_AGENT_SYSTEM_PROMPT_V2.md      # Agent instructions
└── README.md
```

---

## Environment Variables

```bash
# ── Slack ──
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...                  # Optional
ADMIN_USER_IDS=U12345678,U87654321        # Comma-separated, empty = all admin

# ── LangBuilder (main flow) ──
LANGBUILDER_FLOW_URL=https://dev-langbuilder.cloudgeometry.com
LANGBUILDER_FLOW_ID=206d31ae-...
LANGBUILDER_API_KEY=...

# ── LangBuilder (trigger flow) ──
TRIGGER_FLOW_ID=                          # Leave empty to disable scheduler
TRIGGER_INTERVAL_MINUTES=10               # Polling interval
TRIGGER_AUTO_SYNC=true                    # Auto-run jira-sync on new transcripts

# ── Database ──
DATABASE_PATH=./data/jira_agent.db

# ── AWS / DynamoDB ──
AWS_REGION=us-east-1
DYNAMODB_TABLE_NAME=pm_configurations
# AWS_ACCESS_KEY_ID=...                   # Only if not using IAM Role
# AWS_SECRET_ACCESS_KEY=...               # Only if not using IAM Role

# ── Google Drive (shared service account) ──
GDRIVE_PROJECT_ID=your-gcp-project-id
GDRIVE_CLIENT_EMAIL=sa@project.iam.gserviceaccount.com
GDRIVE_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
GDRIVE_PRIVATE_KEY_ID=
GDRIVE_CLIENT_ID=
GDRIVE_FOLDER_ID=default-folder-id
GDRIVE_FOLDER_NAME=Meet recordings
GDRIVE_FILE_FILTER=

# ── Application ──
REQUEST_TIMEOUT=300
LOG_LEVEL=INFO
MARK_EMOJI=ticket
PENDING_EMOJI=eyes
APPROVED_EMOJI=white_check_mark
REJECTED_EMOJI=x
```

---

## Testing Checklist

### Core Functionality
- [x] `/jira-sync` command triggers analysis
- [x] Emoji reactions mark messages
- [x] Proposals displayed with approve/reject buttons
- [x] Approved proposals execute JIRA actions
- [x] `create_issue` proposals work correctly

### Multi-Tenant (Phase 1)
- [x] PM config loaded from DynamoDB
- [x] Tweaks passed to LangBuilder
- [ ] Different PMs use different JIRA/GDrive credentials (needs testing with 2+ PMs)

### PM Onboarding (Phase 4)
- [ ] `/jira-agent setup` opens modal and saves to DynamoDB
- [ ] `/jira-agent config` shows masked config
- [ ] `/jira-agent update jira` / `update gdrive` updates credentials
- [ ] `/jira-agent admin list/disable/enable/stats` work (admin only)

### Auto-Trigger (Phase 3)
- [ ] Scheduler polls GDrive at configured interval
- [ ] Notifications sent with "Generate Tickets" button
- [ ] Button click triggers `transcripts_only` via proposal/approve/reject workflow
- [ ] `/jira-agent check-transcripts` manual trigger works
- [ ] `last_processed_transcript` updated in DynamoDB

### Transcripts Only (Phase 2)
- [ ] `/jira-sync --transcripts-only` override works
- [ ] `transcripts_only` command handled by Smart Enrichment (skips Slack messages)
- [ ] Transcript notification button triggers `transcripts_only` correctly

---

## Deployment

### EC2 Instance
- **Name**: slack-langflow-bridge
- **Region**: us-east-1
- **SSH**: `ssh -i "slack-bot-key.pem" ec2-user@ec2-34-224-165-96.compute-1.amazonaws.com`

### Docker Commands
```bash
docker-compose logs -f                                          # View logs
docker-compose down && docker-compose up -d                     # Restart
docker build --no-cache -t slack-jira-agent . && docker-compose up -d  # Rebuild
```

### GitHub
- **URL**: https://github.com/joaquinr-cg/slack-jira-agent
- **CI/CD**: Automatic deploy on push to `main`
