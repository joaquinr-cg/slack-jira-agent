# JIRA Slack Agent - PM User Guide

## What is the JIRA Slack Agent?

The JIRA Slack Agent automatically keeps your JIRA tickets in sync with decisions made in Slack conversations and meeting transcripts from Google Drive. It reads your messages, analyzes what changed, and proposes JIRA updates for your approval.

---

## Getting Started

### Step 1: Set Up Your Configuration

Run the following command in any Slack channel:

```
/jira-agent setup
```

A modal will open asking for:

| Section | Field | Description |
|---------|-------|-------------|
| **Basic Info** | Name | Your full name |
| | Email | Your work email |
| **JIRA** | JIRA URL | Your Atlassian URL (e.g. `https://company.atlassian.net`) |
| | JIRA Email | Email associated with your JIRA account |
| | JIRA API Token | API token from [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens) |
| | JIRA Project Key | The project key prefix (e.g. `LAN`, `PROJ`) |
| **Google Drive** | GCP Project ID | Google Cloud project (optional — uses shared default if empty) |
| | Service Account Email | Service account email (optional — uses shared default if empty) |
| | Private Key | Service account private key (optional — uses shared default if empty) |
| | Folder ID | The Google Drive folder ID containing your meeting transcripts |
| | Folder Name | Fallback folder name if Folder ID is not provided (optional) |

Click **Save**. You'll receive a confirmation DM from the bot.

> **Note**: Your admin has set up a shared Google Drive service account. You only need to provide your **JIRA credentials** and a **Google Drive Folder ID**. The GCP Project ID, Service Account Email, and Private Key fields can be left empty — they fall back to the shared defaults.

### Step 2: Mark Messages for Review

In any Slack channel where you discuss JIRA work, add the :ticket: emoji to messages that contain decisions, updates, or action items you want reflected in JIRA.

The bot will react with :eyes: to confirm the message has been marked.

### Step 3: Run a Sync

When you're ready to process all marked messages, run:

```
/jira-sync
```

The bot will:
1. Collect all marked messages in the channel
2. Fetch the latest meeting transcript from your Google Drive folder
3. Read your current JIRA ticket state
4. Analyze everything and propose JIRA updates

### Step 4: Review Proposals

For each proposed change, you'll see a card with:
- **Ticket**: The JIRA ticket key (or `NEW` for new tickets)
- **Change**: What type of change (update field, add comment, create issue, transition)
- **Current value**: What the field currently says
- **Proposed value**: What the agent wants to change it to
- **Confidence**: How confident the agent is (low/medium/high)
- **Source**: Where the information came from (Slack message or transcript)

Click **Approve** or **Reject** on each proposal.

Once all proposals are reviewed, approved changes are automatically executed in JIRA.

---

## Commands Reference

### `/jira-agent` - Configuration & Admin

| Command | Description |
|---------|-------------|
| `/jira-agent setup` | Open the setup wizard (creates or updates your config) |
| `/jira-agent config` | View your current configuration (sensitive values are masked) |
| `/jira-agent update jira` | Update only your JIRA credentials |
| `/jira-agent update gdrive` | Update only your Google Drive settings |
| `/jira-agent check-transcripts` | Manually check for new transcripts now (instead of waiting for scheduler) |
| `/jira-agent` | Show help text with all available commands |

### `/jira-sync` - Trigger Analysis

| Command | Description |
|---------|-------------|
| `/jira-sync` | Process all :ticket:-marked messages in the current channel |
| `/jira-sync --transcripts-only` | Analyze only the latest transcript (skip Slack messages) |

### `/jira-review` - Mark for Review

| Command | Description |
|---------|-------------|
| `/jira-review` | Mark the current channel context for JIRA review |

> **Tip**: Using the :ticket: emoji on specific messages is usually more precise than `/jira-review`.

---

## Automatic Transcript Detection

If your admin has enabled the transcript scheduler, the system automatically checks your Google Drive folder for new meeting transcripts at regular intervals (default: every 10 minutes).

When a new transcript is detected:
1. You'll receive a Slack DM listing the new file(s)
2. The message includes a **"Generate Tickets from Latest Transcript"** button
3. Click the button to trigger a JIRA sync in transcript-only mode
4. You'll receive proposals with Approve/Reject buttons (same as `/jira-sync`)

The system tracks which transcripts have already been processed to avoid duplicates.

You can also manually trigger a check at any time with `/jira-agent check-transcripts`.

---

## How It Works

```
You mark messages with :ticket:
        │
        ▼
/jira-sync  ──────────────────────────────────┐
        │                                      │
        ▼                                      ▼
 Collect marked messages              Fetch GDrive transcript
        │                                      │
        └──────────┬───────────────────────────┘
                   │
                   ▼
        Read current JIRA state
                   │
                   ▼
         LLM analyzes everything
                   │
                   ▼
        Proposals sent to Slack
         (Approve / Reject)
                   │
                   ▼
        Approved → executed in JIRA
```

### What gets analyzed

- **Slack messages**: Any message marked with the :ticket: emoji in the current channel
- **Meeting transcripts**: The latest Google Doc in your configured Drive folder
- **JIRA state**: Current field values, status, and comments on tickets in your project

### What changes can be proposed

| Change Type | Example |
|------------|---------|
| **Update field** | Change a ticket's description, acceptance criteria, or story points |
| **Add comment** | Add a meeting note or decision to a ticket |
| **Transition** | Move a ticket from "To Do" to "In Progress" |
| **Create issue** | Create a new ticket from a discussion topic |

---

## Updating Your Configuration

### Update JIRA Credentials

If your API token expires or you need to change your JIRA project:

```
/jira-agent update jira
```

The modal pre-fills your current values (except the API token for security). Leave the token field empty to keep your current token.

### Update Google Drive Settings

To change your transcript folder:

```
/jira-agent update gdrive
```

The modal pre-fills your current values (except the private key). Leave the key field empty to keep the current one.

### View Current Config

To check what's configured:

```
/jira-agent config
```

This shows all your settings with sensitive values masked (e.g., API token shows first 8 characters only).

---

## Admin Commands

These commands are only available to users listed in the `ADMIN_USER_IDS` environment variable.

| Command | Description |
|---------|-------------|
| `/jira-agent admin list` | List all configured PMs with their project key and last sync time |
| `/jira-agent admin disable <slack_id>` | Disable a PM's configuration (they can't use the agent) |
| `/jira-agent admin enable <slack_id>` | Re-enable a disabled PM |
| `/jira-agent admin stats` | Show system-wide statistics: PM count, sessions, proposals, etc. |

The `<slack_id>` is the Slack user ID (e.g., `U0123456789`). You can find it by clicking on a user's profile in Slack.

---

## FAQ

**Q: Can I use the bot in any channel?**
A: Yes, the bot works in any channel it's been invited to. Mark messages with :ticket: and run `/jira-sync` in that channel.

**Q: What happens if I mark a message and then remove the emoji?**
A: If the message hasn't been processed yet, removing the emoji unmarks it. Once processed, removing the emoji has no effect.

**Q: Can I reject all proposals at once?**
A: No, each proposal must be reviewed individually. This ensures nothing is accidentally approved.

**Q: What if I don't have a Google Drive transcript?**
A: The sync still works. It will analyze your marked Slack messages and current JIRA state without the transcript.

**Q: How do I get a JIRA API token?**
A: Go to [Atlassian API Tokens](https://id.atlassian.com/manage-profile/security/api-tokens), click "Create API token", give it a label, and copy the token.

**Q: The bot isn't finding my Google Drive folder. What do I do?**
A: Make sure the folder is shared with the service account email. The service account needs at least "Viewer" access. You can find the service account email in your config (`/jira-agent config`).

**Q: Can multiple PMs use this on the same JIRA project?**
A: Yes. Each PM has their own credentials and folder, but they can point to the same JIRA project.
