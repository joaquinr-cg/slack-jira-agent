# DynamoDB Schema - PM Configurations

## Overview

This document describes the DynamoDB table schema used to store Product Manager (PM) configurations for the JIRA Slack Agent multi-tenant architecture.

## Architecture Context

```
┌─────────────────────────────────────────────────────────────────────┐
│                        TRIGGER FLOW                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐     │
│  │ DynamoDB │───▶│ GDrive   │───▶│ Compare  │───▶│ Return   │     │
│  │ Read PMs │    │ Status   │    │ Iterator │    │ slack_ids│     │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘     │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      SLACK MICROSERVICE                             │
│  - Receives slack_ids with new transcripts                         │
│  - Notifies each PM with "Generate tickets" button                 │
│  - On approval → triggers main flow with tweaks from DynamoDB      │
│  - CRUD operations on DynamoDB (create/update PMs)                 │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      MAIN FLOW (existing)                           │
│  - Generates tickets from Slack messages + GDrive + JIRA state     │
│  - Receives PM credentials via tweaks in payload                   │
│  - No direct DB access - all config comes from microservice        │
└─────────────────────────────────────────────────────────────────────┘
```

## Table Configuration

| Property | Value |
|----------|-------|
| **Table Name** | `pm_configurations` |
| **Partition Key** | `slack_id` (String) |
| **Sort Key** | None |
| **Encryption** | AWS managed key (KMS) |
| **Billing Mode** | On-demand (recommended for variable workloads) |

## Schema Definition

### Full Item Structure

```json
{
  "slack_id": "U0123456789",
  "email": "pm@company.com",
  "name": "Joaquin",
  "enabled": true,

  "jira_config": {
    "jira_url": "https://company.atlassian.net",
    "email": "pm@company.com",
    "api_token": "ATATT3x...",
    "auth_type": "basic",
    "project_key": "LAN"
  },

  "gdrive_config": {
    "project_id": "my-gcp-project-123",
    "client_email": "service@project.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\n...",
    "private_key_id": "",
    "client_id": "",
    "folder_id": "1ABC123xyz",
    "folder_name": "Meet recordings",
    "file_filter": ""
  },

  "last_processed_transcript": {
    "file_id": "",
    "file_name": "",
    "modified_time": "",
    "processed_at": ""
  },

  "flow_config": {
    "transcripts_only": false,
    "notification_channel": "C0123456789",
    "auto_approve": false
  },

  "created_at": "2026-02-05T19:00:00Z",
  "updated_at": "2026-02-05T19:00:00Z"
}
```

### Field Descriptions

#### Root Level Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `slack_id` | String | Yes | Slack user ID (Primary Key). Format: `U` + 10 alphanumeric chars |
| `email` | String | Yes | PM's email address for reference |
| `name` | String | Yes | Display name of the PM |
| `enabled` | Boolean | Yes | Whether this PM is active for auto-trigger processing |
| `created_at` | String | Yes | ISO 8601 timestamp of record creation |
| `updated_at` | String | Yes | ISO 8601 timestamp of last update |

#### `jira_config` Object

Configuration for JIRA API access. Used by `JiraReaderWriter` and `JiraStateFetcher` components.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `jira_url` | String | Yes | JIRA instance URL (e.g., `https://company.atlassian.net`) |
| `email` | String | Yes | Atlassian account email for API authentication |
| `api_token` | String | Yes | JIRA API token (sensitive - encrypted at rest) |
| `auth_type` | String | No | Authentication type: `basic` (default) or `bearer` |
| `project_key` | String | Yes | JIRA project key (e.g., `LAN`, `PROJ`) |

#### `gdrive_config` Object

Configuration for Google Drive access. Used by `GoogleDriveDocsParserSA` component.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project_id` | String | Yes | Google Cloud project ID |
| `client_email` | String | Yes | Service account email (ends with `.iam.gserviceaccount.com`) |
| `private_key` | String | Yes | Service account private key (sensitive - encrypted at rest) |
| `private_key_id` | String | No | Private key ID from service account JSON |
| `client_id` | String | No | Numeric client ID from service account JSON |
| `folder_id` | String | Yes* | Google Drive folder ID containing transcripts |
| `folder_name` | String | No | Folder name (fallback if `folder_id` not provided) |
| `file_filter` | String | No | Filter files by name (contains match) |

*Either `folder_id` or `folder_name` must be provided.

#### `last_processed_transcript` Object

Tracking for the auto-trigger flow to detect new transcripts.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file_id` | String | No | Google Drive file ID of last processed transcript |
| `file_name` | String | No | Name of last processed transcript file |
| `modified_time` | String | No | ISO 8601 timestamp of file's last modification |
| `processed_at` | String | No | ISO 8601 timestamp when we processed it |

#### `flow_config` Object

Configuration for flow behavior.

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `transcripts_only` | Boolean | No | `false` | If true, only process GDrive transcripts (ignore Slack messages) |
| `notification_channel` | String | No | - | Slack channel ID for notifications |
| `auto_approve` | Boolean | No | `false` | Future: auto-approve generated proposals |

## Access Patterns

### 1. Get PM Configuration (Microservice)

```python
response = dynamodb.get_item(
    TableName='pm_configurations',
    Key={'slack_id': {'S': 'U0123456789'}}
)
```

### 2. List All Enabled PMs (Trigger Flow)

```python
response = dynamodb.scan(
    TableName='pm_configurations',
    FilterExpression='enabled = :enabled',
    ExpressionAttributeValues={':enabled': {'BOOL': True}}
)
```

### 3. Update Last Processed Transcript

```python
dynamodb.update_item(
    TableName='pm_configurations',
    Key={'slack_id': {'S': 'U0123456789'}},
    UpdateExpression='SET last_processed_transcript = :transcript, updated_at = :now',
    ExpressionAttributeValues={
        ':transcript': {'M': {
            'file_id': {'S': '1XYZ789...'},
            'file_name': {'S': 'Meeting 2026-02-05.docx'},
            'modified_time': {'S': '2026-02-05T15:30:00Z'},
            'processed_at': {'S': '2026-02-05T16:00:00Z'}
        }},
        ':now': {'S': '2026-02-05T16:00:00Z'}
    }
)
```

### 4. Create New PM (Microservice)

```python
dynamodb.put_item(
    TableName='pm_configurations',
    Item={
        'slack_id': {'S': 'U0123456789'},
        'email': {'S': 'pm@company.com'},
        'name': {'S': 'Joaquin'},
        'enabled': {'BOOL': True},
        'jira_config': {'M': {...}},
        'gdrive_config': {'M': {...}},
        'last_processed_transcript': {'M': {}},
        'flow_config': {'M': {...}},
        'created_at': {'S': '2026-02-05T19:00:00Z'},
        'updated_at': {'S': '2026-02-05T19:00:00Z'}
    }
)
```

## Tweaks Mapping

When the microservice calls the main flow, it maps DynamoDB fields to component tweaks:

**Jira Tickets flow** (uses component instance IDs):

```python
payload = {
    "input_value": {...},
    "tweaks": {
        "CustomComponent-MvTpp": {       # JiraReaderWriter
            "jira_url": ..., "email": ..., "api_token": ...,
            "auth_type": ..., "project_key": ...
        },
        "CustomComponent-h9t4Q": {       # JiraStateFetcher
            "jira_url": ..., "email": ..., "api_token": ...,
            "auth_type": ..., "project_key": ...
        },
        "CustomComponent-swCo4": {       # GoogleDriveDocsParserSA
            "project_id": ..., "client_email": ..., "private_key": ...,
            "private_key_id": ..., "client_id": ...,
            "folder_id": ..., "folder_name": ..., "file_filter": ...
        }
    }
}
```

**Trigger flow** (only GDrive credentials):

```python
payload = {
    "input_value": {...},
    "tweaks": {
        "TranscriptTrigger-NxiAw": {     # TranscriptTrigger
            "project_id": ..., "client_email": ..., "private_key": ...,
            "private_key_id": ..., "client_id": ...,
            "folder_id": ..., "folder_name": ..., "file_filter": ...
        }
    }
}
```

> **Note**: GDrive tweaks use shared service account from env vars as base, with per-PM overrides for `folder_id` and `client_email`.

## Security Considerations

1. **Encryption at Rest**: Table uses AWS managed KMS encryption
2. **Sensitive Fields**: `api_token` and `private_key` contain secrets
3. **IAM Policies**: Restrict access to specific roles/services
4. **Future Enhancement**: Consider AWS Secrets Manager for credential rotation

## Related Components

- `langbuilder_components/jira_tool.py` - JIRA Reader/Writer
- `langbuilder_components/jira_state_fetcher.py` - JIRA State Fetcher
- `langbuilder_components/g_drive_doc_parser.py` - Google Drive Parser
- `src/slack_handler.py` - Slack microservice (will need DynamoDB integration)
