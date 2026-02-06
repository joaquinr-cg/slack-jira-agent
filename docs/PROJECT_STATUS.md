# JIRA Slack Agent - Project Status & Roadmap

## Overview

The JIRA Slack Agent is a multi-tenant system that helps Product Managers (PMs) keep their JIRA tickets synchronized with decisions made in Slack conversations and team meetings (Google Drive transcripts).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TRIGGER FLOW (TO BE BUILT)                     │
│  Scheduled execution to detect new Google Drive transcripts                 │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌─────────────┐  │
│  │  DynamoDB    │──▶│   GDrive     │──▶│   Compare    │──▶│   Return    │  │
│  │  Component   │   │   Check      │   │   Timestamps │   │  slack_ids  │  │
│  │  (read PMs)  │   │   Latest     │   │   Iterator   │   │  with new   │  │
│  └──────────────┘   └──────────────┘   └──────────────┘   └─────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           SLACK MICROSERVICE (DEPLOYED)                     │
│  Python service running on EC2 that handles Slack interactions              │
│                                                                             │
│  Current Features:                      Future Features:                    │
│  ✅ /jira-sync command                  ⬚ Read PM config from DynamoDB     │
│  ✅ Emoji reactions for marking msgs    ⬚ Pass tweaks to LangBuilder       │
│  ✅ Proposal approval/rejection         ⬚ Handle trigger flow results      │
│  ✅ Local SQLite for sessions           ⬚ PM onboarding commands           │
│                                         ⬚ "Generate tickets" button        │
└─────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            MAIN FLOW (DEPLOYED IN LANGBUILDER)              │
│  Generates JIRA ticket proposals from multiple sources                      │
│                                                                             │
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────────┐ │
│  │   Smart     │──▶│   Agent     │──▶│   JIRA      │──▶│  Proposals to   │ │
│  │ Enrichment  │   │   (LLM)     │   │   Tools     │   │  Slack          │ │
│  └─────────────┘   └─────────────┘   └─────────────┘   └─────────────────┘ │
│        │                                                                    │
│        ▼                                                                    │
│  Data Sources:                                                              │
│  • Slack messages (marked with emoji)                                       │
│  • Google Drive transcript (latest meeting)                                 │
│  • Current JIRA state (for comparison)                                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              DYNAMODB (CONFIGURED)                          │
│  Table: pm_configurations                                                   │
│  Stores per-PM credentials and settings for multi-tenant access             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Completed Work

### 1. CI/CD Pipeline
- **File**: `.github/workflows/deploy.yml`
- **Functionality**:
  - Triggers on push to `main` branch
  - SSHs into EC2 instance
  - Pulls latest code with `git reset --hard`
  - Rebuilds Docker image (with `--no-cache` for full rebuild)
  - Restarts container with `docker-compose`
- **Status**: ✅ Working

### 2. Slack Microservice Deployment
- **Location**: EC2 instance `slack-langflow-bridge` (us-east-1)
- **Connection**: `ec2-user@ec2-34-224-165-96.compute-1.amazonaws.com`
- **Components**:
  - Docker container running Python app
  - SQLite database for sessions and proposals
  - Slack Bolt framework with Socket Mode
- **Status**: ✅ Deployed and running

### 3. Bug Fixes Applied
| Issue | Fix | File |
|-------|-----|------|
| JSON parse error from LLM comments (`//`) | Updated system prompt to forbid comments | System prompt v2 |
| SQLite dict serialization error | Added `_serialize_value()` to convert dicts to JSON strings | `src/db/manager.py` |
| NULL ticket_key for create_issue | Changed fallback from `"UNKNOWN"` to `or "NEW"` | `src/slack_handler.py` |
| Dict slicing error in proposal display | Added type check and JSON serialization | `src/slack_handler.py` |

### 4. LangBuilder Components Updated
| Component | File | Purpose |
|-----------|------|---------|
| JIRA Smart Enrichment v2 | `langbuilder_components/jira_smart_enrichment_v2.py` | Builds prompts for Analysis/Execution modes |
| JIRA Agent System Prompt v2 | `JIRA_AGENT_SYSTEM_PROMPT_V2.md` | Instructions for the LLM agent |

### 5. DynamoDB Infrastructure
- **Table**: `pm_configurations`
- **Region**: us-east-1
- **Encryption**: AWS managed KMS
- **Billing**: On-demand
- **Schema**: See `docs/DYNAMODB_SCHEMA.md`
- **IAM Role**: `slack-jira-agent-ec2-role` with DynamoDB access policy
- **Status**: ✅ Table created, test record inserted, EC2 access verified

---

## Completed Work (Phases 1 & 4)

### Phase 1: Multi-Tenant Support - COMPLETED

#### 1.1 DynamoDB Client (`src/dynamodb_client.py`)
- [x] `get_pm_config(slack_id)` - Fetch PM config by Slack ID
- [x] `update_last_processed(slack_id, transcript_info)` - Track processed transcripts
- [x] `list_enabled_pms()` - Scan all enabled PMs
- [x] `create_pm(pm_data)` - Create new PM configuration
- [x] `update_pm(slack_id, updates)` - Update specific fields
- [x] `disable_pm(slack_id)` / `enable_pm(slack_id)` - Toggle PM status
- [x] `build_tweaks_from_pm_config()` - Map DynamoDB config to LangBuilder tweaks

#### 1.2 Slack Microservice DynamoDB Integration
- [x] `boto3` added to `requirements.txt`
- [x] `_process_jira_sync()` fetches PM config, builds tweaks, passes to LangBuilder
- [x] `_send_approval_decisions_to_llm()` also loads PM tweaks for execution phase
- [x] `transcripts_only` flag read from PM config's `flow_config`

#### 1.3 LangBuilder Client Updates
- [x] `run_flow()` accepts optional `extra_tweaks` parameter
- [x] Tweaks merged into payload alongside ChatInput tweaks

#### 1.4 Configuration & Infrastructure
- [x] `config.py` - Added `aws_region`, `dynamodb_table_name` settings
- [x] `main.py` - Initializes DynamoDB client on startup
- [x] `.env.example` - Documents all environment variables including AWS/DynamoDB

---

### Phase 4: PM Onboarding - COMPLETED

#### 4.1 Slack Commands for PM Management (`/jira-agent`)

> **Note**: The `/jira-agent` slash command must be registered in the Slack App configuration
> (Settings > Slash Commands > Create New Command).

**Implemented commands**:
- [x] `/jira-agent setup` - Opens full onboarding modal (name, email, JIRA config, GDrive config)
- [x] `/jira-agent config` - Shows current configuration (ephemeral, secrets masked)
- [x] `/jira-agent update jira` - Opens modal to update JIRA credentials
- [x] `/jira-agent update gdrive` - Opens modal to update Google Drive settings
- [x] `/jira-agent` (no args) - Shows help text with all available commands

**Modal features**:
- Pre-fills non-sensitive fields when updating existing config
- Sensitive fields (API token, private key) are never pre-filled
- "Leave empty to keep current" placeholder for secret fields on updates
- Secrets stored in `private_metadata` for the setup modal to preserve on re-save

#### 4.2 Admin Commands
- [x] `/jira-agent admin list` - Lists all enabled PMs with project key and last sync time
- [x] `/jira-agent admin disable <slack_id>` - Disables a PM
- [x] `/jira-agent admin enable <slack_id>` - Enables a PM
- [x] `/jira-agent admin stats` - Shows PM count, sessions, proposals, pending messages
- [x] Admin check via `ADMIN_USER_IDS` environment variable

---

## Pending Work

### Phase 2: Transcripts Only Mode

#### 2.1 Implement `transcripts_only` Flag

**Purpose**: Allow processing only Google Drive transcripts without requiring Slack messages marked with emoji.

**Already done**:
- [x] `transcripts_only` read from DynamoDB `flow_config` in `_process_jira_sync()`
- [x] Skips Slack messages when `transcripts_only: true`
- [x] Passes `transcripts_only` flag to LangBuilder input

**Remaining tasks**:
- [ ] Support `--transcripts-only` flag in `/jira-sync` command text (override DynamoDB default)
- [ ] Modify Smart Enrichment component to handle `transcripts_only` in input
  - Skip Slack messages section in prompt
  - Only include GDrive transcript and JIRA state

---

### Phase 3: Auto-Trigger Flow

#### 3.1 Create DynamoDB Component for LangBuilder

**File to create**: `langbuilder_components/dynamodb_reader.py`

**Tasks**:
- [ ] Create component with AWS SDK integration
- [ ] Handle IAM authentication (instance role or access keys)
- [ ] Return list of PM configs for iteration

#### 3.2 Create Trigger Flow in LangBuilder

**Flow Steps**:
1. **DynamoDB Reader** → Get all enabled PMs
2. **Iterator** → For each PM: check latest GDrive transcript vs `last_processed_transcript`
3. **Filter** → Keep only PMs with new transcripts
4. **Output** → Return list of `slack_id`s with new content

**Tasks**:
- [ ] Design flow in LangBuilder UI
- [ ] Create comparison/iterator component
- [ ] Test with multiple PM configurations

#### 3.3 Microservice: Handle Trigger Flow Results

**Tasks**:
- [ ] Create endpoint or scheduled job to call trigger flow
- [ ] For each returned `slack_id`:
  - Send Slack notification: "New meeting transcript detected"
  - Include "Generate tickets" button
- [ ] Handle button click → call main flow with `transcripts_only: true`

#### 3.4 Update Last Processed Transcript

**Tasks**:
- [ ] After successful ticket generation, call `dynamodb.update_last_processed()`
  (method already exists in `dynamodb_client.py`)

---

## File Structure

```
slack_jira_agent/
├── .github/
│   └── workflows/
│       └── deploy.yml              # CI/CD pipeline
├── docs/
│   ├── DYNAMODB_SCHEMA.md          # DynamoDB table documentation
│   ├── PROJECT_STATUS.md           # This file
│   └── pm_config_template.json     # Template for PM configuration
├── langbuilder_components/
│   ├── agent.py                    # LangBuilder agent component
│   ├── g_drive_doc_parser.py       # Google Drive parser
│   ├── jira_smart_enrichment_v2.py # Smart enrichment (updated)
│   ├── jira_state_fetcher.py       # JIRA state reader
│   └── jira_tool.py                # JIRA reader/writer tool
├── src/
│   ├── __init__.py
│   ├── config.py                   # Pydantic settings
│   ├── db/
│   │   ├── __init__.py
│   │   ├── manager.py              # SQLite operations
│   │   └── models.py               # Data models
│   ├── dynamodb_client.py          # DynamoDB CRUD for PM configs
│   ├── langbuilder_client.py       # LangBuilder API client
│   ├── main.py                     # App entry point
│   └── slack_handler.py            # Slack event handlers
├── data/                           # SQLite database (local)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── JIRA_AGENT_SYSTEM_PROMPT_V2.md  # Agent instructions
└── README.md
```

---

## Environment Variables

### Current (.env)
```bash
# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# LangBuilder
LANGBUILDER_FLOW_URL=https://dev-langbuilder.cloudgeometry.com
LANGBUILDER_FLOW_ID=206d31ae-...
LANGBUILDER_API_KEY=...

# Database
DATABASE_PATH=./data/jira_agent.db

# Settings
REQUEST_TIMEOUT=300
LOG_LEVEL=INFO
```

### Future Additions
```bash
# AWS (only if not using IAM Role)
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...        # Optional if using IAM Role
AWS_SECRET_ACCESS_KEY=...    # Optional if using IAM Role

# DynamoDB
DYNAMODB_TABLE_NAME=pm_configurations
```

---

## Testing Checklist

### Current Functionality
- [x] `/jira-sync` command triggers analysis
- [x] Emoji reactions mark messages
- [x] Proposals displayed with approve/reject buttons
- [x] Approved proposals execute JIRA actions
- [x] `create_issue` proposals work correctly

### After Phase 1 (Implemented)
- [x] PM config loaded from DynamoDB
- [x] Tweaks passed to LangBuilder
- [ ] Different PMs use different JIRA/GDrive credentials (needs testing with 2+ PMs)

### After Phase 4 (Implemented)
- [ ] `/jira-agent setup` opens modal and saves to DynamoDB
- [ ] `/jira-agent config` shows masked config
- [ ] `/jira-agent update jira` updates JIRA credentials
- [ ] `/jira-agent update gdrive` updates GDrive credentials
- [ ] `/jira-agent admin list` lists PMs (admin only)
- [ ] `/jira-agent admin disable/enable` toggles PM (admin only)
- [ ] `/jira-agent admin stats` shows statistics (admin only)

### After Phase 2
- [ ] `--transcripts-only` flag works
- [ ] Flow processes only transcript (no Slack messages)

### After Phase 3
- [ ] Trigger flow detects new transcripts
- [ ] Notifications sent to correct PMs
- [ ] "Generate tickets" button works
- [ ] `last_processed_transcript` updated after processing

---

## Deployment Notes

### EC2 Instance
- **Name**: slack-langflow-bridge
- **Instance ID**: i-0a79e1a8504b4fcd3
- **Region**: us-east-1
- **Type**: t3.micro
- **SSH**: `ssh -i "slack-bot-key.pem" ec2-user@ec2-34-224-165-96.compute-1.amazonaws.com`

### Docker Commands
```bash
# View logs
docker-compose logs -f

# Restart
docker-compose down && docker-compose up -d

# Rebuild
docker build --no-cache -t slack-jira-agent . && docker-compose up -d
```

### GitHub Repository
- **URL**: https://github.com/joaquinr-cg/slack-jira-agent
- **CI/CD**: Automatic deploy on push to `main`