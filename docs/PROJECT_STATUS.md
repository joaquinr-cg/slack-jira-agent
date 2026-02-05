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

## Pending Work

### Phase 1: Multi-Tenant Support (High Priority)

#### 1.1 Modify Slack Microservice for DynamoDB Integration

**Files to modify**: `src/slack_handler.py`, `requirements.txt`

**Tasks**:
- [ ] Add `boto3` to `requirements.txt`
- [ ] Create `src/dynamodb_client.py` module:
  ```python
  class DynamoDBClient:
      def get_pm_config(slack_id: str) -> dict
      def update_last_processed(slack_id: str, transcript_info: dict)
      def list_enabled_pms() -> list[dict]
      def create_pm(pm_data: dict)
      def update_pm(slack_id: str, updates: dict)
  ```
- [ ] Modify `_process_jira_sync()` to:
  1. Fetch PM config from DynamoDB using `slack_id`
  2. Build tweaks payload from config
  3. Pass tweaks to LangBuilder API call

**Tweaks payload structure**:
```python
payload = {
    "input_value": {"command": "/jira-sync", "messages": [...]},
    "tweaks": {
        "JiraReaderWriter": {
            "jira_url": pm_config["jira_config"]["jira_url"],
            "email": pm_config["jira_config"]["email"],
            "api_token": pm_config["jira_config"]["api_token"],
            "project_key": pm_config["jira_config"]["project_key"]
        },
        "JiraStateFetcher": {
            # Same as above
        },
        "GoogleDriveDocsParserSA": {
            "project_id": pm_config["gdrive_config"]["project_id"],
            "client_email": pm_config["gdrive_config"]["client_email"],
            "private_key": pm_config["gdrive_config"]["private_key"],
            "folder_id": pm_config["gdrive_config"]["folder_id"]
        }
    }
}
```

#### 1.2 Update LangBuilder Client

**File**: `src/langbuilder_client.py`

**Tasks**:
- [ ] Modify `run_flow()` to accept optional `tweaks` parameter
- [ ] Include tweaks in API request payload

---

### Phase 2: Transcripts Only Mode (Medium Priority)

#### 2.1 Implement `transcripts_only` Flag

**Purpose**: Allow processing only Google Drive transcripts without requiring Slack messages marked with emoji.

**Flow**:
1. PM triggers `/jira-sync --transcripts-only` OR
2. Auto-trigger flow detects new transcript and notifies PM
3. PM clicks "Generate tickets" button
4. Microservice calls main flow with `transcripts_only: true`

**Tasks**:
- [ ] Modify Smart Enrichment component to handle `transcripts_only` in input
- [ ] When `transcripts_only: true`:
  - Skip Slack messages section in prompt
  - Only include GDrive transcript and JIRA state
- [ ] Modify microservice to support `--transcripts-only` flag in command
- [ ] Store default preference in `flow_config.transcripts_only` in DynamoDB

---

### Phase 3: Auto-Trigger Flow (Lower Priority)

#### 3.1 Create DynamoDB Component for LangBuilder

**Purpose**: Allow LangBuilder flows to read PM configurations from DynamoDB.

**File to create**: `langbuilder_components/dynamodb_reader.py`

**Inputs**:
- `table_name`: DynamoDB table name
- `aws_region`: AWS region
- `filter_enabled`: Boolean to only return enabled PMs

**Outputs**:
- List of PM configurations

**Tasks**:
- [ ] Create component with AWS SDK integration
- [ ] Handle IAM authentication (instance role or access keys)
- [ ] Return list of PM configs for iteration

#### 3.2 Create Trigger Flow in LangBuilder

**Purpose**: Scheduled flow that checks for new transcripts across all PMs.

**Flow Steps**:
1. **DynamoDB Reader** → Get all enabled PMs
2. **Iterator** → For each PM:
   - Use PM's `gdrive_config` to check latest transcript
   - Compare `modified_time` with `last_processed_transcript.modified_time`
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
- [ ] After successful ticket generation, update DynamoDB:
  ```python
  dynamodb.update_pm(slack_id, {
      "last_processed_transcript": {
          "file_id": transcript["file_id"],
          "file_name": transcript["file_name"],
          "modified_time": transcript["modified_time"],
          "processed_at": datetime.utcnow().isoformat()
      }
  })
  ```

---

### Phase 4: PM Onboarding (Future)

#### 4.1 Slack Commands for PM Management

**Commands**:
- `/jira-agent setup` - Start onboarding wizard
- `/jira-agent config` - View current configuration
- `/jira-agent update jira` - Update JIRA credentials
- `/jira-agent update gdrive` - Update Google Drive settings

**Tasks**:
- [ ] Create interactive Slack modals for configuration
- [ ] Validate credentials before saving
- [ ] Store in DynamoDB

#### 4.2 Admin Commands

**Commands**:
- `/jira-agent admin list` - List all configured PMs
- `/jira-agent admin disable <slack_id>` - Disable a PM
- `/jira-agent admin stats` - Show usage statistics

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
│   ├── dynamodb_client.py          # TO BE CREATED
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

### After Phase 1
- [ ] PM config loaded from DynamoDB
- [ ] Tweaks passed to LangBuilder
- [ ] Different PMs use different JIRA/GDrive credentials

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

---

## Contact & Resources

- **LangBuilder**: https://dev-langbuilder.cloudgeometry.com
- **AWS Console**: https://console.aws.amazon.com (us-east-1)
- **Slack App**: Configure at https://api.slack.com/apps
