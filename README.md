# JIRA Slack Agent

Slack microservice for the JIRA Reviewer Agent workflow.

## What This Service Does

1. **Listens** for marked messages in Slack (via 🎫 emoji or `/jira-review`)
2. **Collects** marked messages when `/jira-sync` is triggered
3. **Sends** messages to LangBuilder flow for analysis
4. **Displays** proposals with Approve/Reject buttons
5. **Sends** approved proposals to LangBuilder for JIRA execution

**Note:** This service is a pure orchestrator. ALL JIRA operations (read AND write) are handled by LangBuilder.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    SLACK MICROSERVICE                            │
│                    (This Service)                                │
│                                                                 │
│  Responsibilities:                                              │
│  • Listen for 🎫 emoji reactions                                │
│  • Handle /jira-review and /jira-sync commands                  │
│  • Collect and format Slack messages                            │
│  • Send to LangBuilder flow for analysis                        │
│  • Parse structured JSON response                               │
│  • Render approval UI (Approve/Reject buttons)                  │
│  • Track approval state per proposal                            │
│  • Send approved proposals to LangBuilder for execution         │
│                                                                 │
│  NOTE: This service does NOT access JIRA directly.              │
│  All JIRA operations go through LangBuilder.                    │
│                                                                 │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              │ Slack messages + session_id
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    LANGBUILDER FLOW                              │
│                    (Separate Service)                            │
│                                                                 │
│  Responsibilities:                                              │
│  • Enrich with JIRA current state (via AtlassianMCP tools)      │
│  • Enrich with Google Drive transcripts (via GDrive tools)      │
│  • Compare input with JIRA tickets                              │
│  • Generate structured JSON proposals                           │
│  • Execute approved JIRA updates (via AtlassianMCP tools)       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Clone and configure

```bash
cd slack_jira_agent
cp .env.example .env
# Edit .env with your credentials
```

### 2. Create Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Create new app → From scratch
3. Enable **Socket Mode** (under Settings)
4. Add **Bot Token Scopes**:
   - `chat:write`
   - `channels:history`
   - `groups:history`
   - `reactions:read`
   - `reactions:write`
   - `commands`
   - `users:read`

5. Add **Event Subscriptions**:
   - `reaction_added`
   - `reaction_removed`

6. Add **Slash Commands**:
   - `/jira-review` - Mark thread for JIRA review
   - `/jira-sync` - Process all marked messages

7. Enable **Interactivity** (for button clicks)

8. Install to workspace and copy tokens to `.env`

### 3. Run locally

```bash
pip install -r requirements.txt
python -m src.main
```

### 4. Run with Docker

```bash
docker-compose up -d
```

## Usage

### Mark messages for review

**Option A: Emoji reaction**
- Add 🎫 (`:ticket:`) emoji to any message
- Bot will add 👀 to acknowledge

**Option B: Command**
- Type `/jira-review` to mark context for review

### Process marked messages

1. Run `/jira-sync` in any channel
2. Bot collects all marked messages
3. Sends to LangBuilder flow for analysis
4. Bot posts proposals with Approve/Reject buttons
5. PM reviews each proposal
6. When all responded, approved changes execute to JIRA

## Data Flow

```
User marks messages with 🎫
         │
         ▼
/jira-sync triggered
         │
         ▼
Fetch all marked messages from DB
         │
         ▼
Send to LangBuilder: {
  "session_id": "uuid",
  "command": "/jira-sync",
  "slack_messages": [...]
}
         │
         ▼
LangBuilder enriches & analyzes
(JIRA state + GDrive transcripts)
         │
         ▼
Returns structured JSON proposals
         │
         ▼
Parse & store proposals in DB
         │
         ▼
Render approval UI per proposal
         │
         ▼
PM clicks Approve/Reject on each proposal
         │
         ▼
All proposals responded?
         │
         ▼
Send decisions to LangBuilder (same session_id): {
  "session_id": "uuid",
  "command": "approval_decisions",
  "decisions": [
    {"proposal_id": "prop-001", "decision": "approved"},
    {"proposal_id": "prop-002", "decision": "rejected"}
  ]
}
         │
         ▼
LLM receives decisions and DECIDES to execute
approved changes using AtlassianMCP tools
         │
         ▼
Returns summary to Slack
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_BOT_TOKEN` | Yes | Slack bot token (xoxb-...) |
| `SLACK_APP_TOKEN` | Yes | Slack app token for Socket Mode (xapp-...) |
| `LANGBUILDER_FLOW_URL` | Yes | LangBuilder flow URL |
| `LANGBUILDER_FLOW_ID` | Yes | LangBuilder flow ID |
| `LANGBUILDER_API_KEY` | No | LangBuilder API key |
| `DATABASE_PATH` | No | SQLite database path (default: ./data/jira_agent.db) |
| `LOG_LEVEL` | No | Logging level (default: INFO) |

## Database Schema

SQLite with three tables:

- **sessions**: Tracks `/jira-sync` invocations
- **marked_messages**: Messages marked for review (🎫 or /jira-review)
- **proposals**: LLM-generated proposals and their approval status

## LLM Response Format

The LangBuilder flow must return structured JSON:

```json
{
  "session_id": "uuid",
  "analysis_summary": "Found 3 tickets discussed...",
  "proposals": [
    {
      "proposal_id": "prop-001",
      "ticket_key": "PROJ-123",
      "ticket_summary": "User Authentication",
      "change_type": "update_field",
      "field": "description",
      "current_value": "...",
      "proposed_value": "...",
      "source": "slack_thread",
      "source_excerpt": "...",
      "confidence": "high"
    }
  ],
  "no_action_items": []
}
```

## File Structure

```
slack_jira_agent/
├── src/
│   ├── __init__.py
│   ├── config.py              # Settings from environment
│   ├── main.py                # Entry point
│   ├── slack_handler.py       # Slack events & commands
│   ├── langbuilder_client.py  # LangBuilder communication
│   └── db/
│       ├── __init__.py
│       ├── models.py          # Dataclasses
│       └── manager.py         # Database operations
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## Troubleshooting

### Bot not responding to emoji

- Check that `reaction_added` event is subscribed
- Verify bot has `reactions:read` scope
- Check bot is in the channel

### /jira-sync shows no messages

- Ensure messages are marked with 🎫 emoji
- Check that messages haven't already been processed

### JIRA updates failing

- Verify LangBuilder flow has AtlassianMCP component configured
- Check AtlassianMCP has correct credentials
- Review LangBuilder flow logs for specific errors

### LangBuilder errors

- Check flow URL and ID are correct
- Verify API key if required
- Check flow is deployed and running
