"""
JIRA Smart Enrichment Component v2

A unified component that handles BOTH operating modes:
1. Analysis Mode (/jira-sync): Enriches with JIRA state + GDrive transcript
2. Execution Mode (approval_decisions): Passes through directly to Agent

Updated to support:
- create_issue change_type with null ticket_key
- Proper tool action names (Update Issue, Add Comment, etc.)
- Strict JSON validation rules (no comments)
"""

import json
import logging
from typing import Any

from langbuilder.custom.custom_component.component import Component

logger = logging.getLogger(__name__)
from langbuilder.io import MessageInput, MessageTextInput, MultilineInput, Output
from langbuilder.schema.message import Message


class JiraSmartEnrichmentComponent(Component):
    display_name = "JIRA Smart Enrichment v2"
    description = "Unified enrichment component that handles both analysis and execution modes."
    icon = "brain"
    name = "JiraSmartEnrichmentV2"

    inputs = [
        MessageInput(
            name="input_data",
            display_name="Input Data",
            info="JSON input from Slack microservice (either /jira-sync or approval_decisions).",
            required=True,
        ),
        MessageInput(
            name="gdrive_transcript",
            display_name="Google Drive Transcript",
            info="Latest meeting transcript from Google Drive Docs Parser.",
            required=False,
        ),
        MessageInput(
            name="jira_state",
            display_name="JIRA Current State",
            info="Current state of JIRA tickets from JIRA State Fetcher.",
            required=False,
        ),
        MultilineInput(
            name="additional_context",
            display_name="Additional Context",
            info="Any additional context or instructions to include in the prompt.",
            required=False,
            advanced=True,
        ),
        MessageTextInput(
            name="jira_project_key",
            display_name="JIRA Project Key",
            info="The JIRA project key to focus on (e.g., PROJ, CLOUD, etc.).",
            value="LAN",
            required=False,
            advanced=True,
        ),
    ]

    outputs = [
        Output(
            display_name="Agent Prompt",
            name="agent_prompt",
            method="process_input",
        ),
    ]

    def _get_parse_attempts(self, raw: str) -> list[str]:
        """Generate multiple variations of a string to attempt JSON parsing."""
        attempts = [raw]

        if '\\"' in raw:
            unescaped = raw.replace('\\"', '"')
            attempts.append(unescaped)

        if '\\\"' in raw:
            unescaped = raw.replace('\\\"', '"')
            if unescaped not in attempts:
                attempts.append(unescaped)

        sanitized = raw.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        if sanitized != raw and sanitized not in attempts:
            attempts.append(sanitized)

        for base in [raw]:
            if '\\"' in base:
                combo = base.replace('\\"', '"').replace('\n', '\\n').replace('\r', '\\r')
                if combo not in attempts:
                    attempts.append(combo)

        return attempts

    def _parse_json_input(self, input_data: Any) -> dict:
        """Parse input that may be JSON string, Message, or dict."""
        if input_data is None:
            return {}

        if isinstance(input_data, Message):
            content = input_data.text or ""
        elif isinstance(input_data, str):
            content = input_data
        elif isinstance(input_data, dict):
            if "raw_content" in input_data:
                raw = input_data["raw_content"]
                if isinstance(raw, str):
                    for attempt_raw in self._get_parse_attempts(raw):
                        try:
                            inner_parsed = json.loads(attempt_raw)
                            if isinstance(inner_parsed, dict):
                                return self._parse_json_input(inner_parsed)
                        except json.JSONDecodeError:
                            continue
            return input_data
        else:
            content = str(input_data)

        for attempt_content in self._get_parse_attempts(content):
            try:
                parsed = json.loads(attempt_content)
                if isinstance(parsed, dict):
                    return self._parse_json_input(parsed)
                return parsed
            except json.JSONDecodeError:
                continue

        return {"raw_content": content}

    def _extract_text(self, input_data: Any) -> str:
        """Extract text content from various input types."""
        if input_data is None:
            return ""

        if isinstance(input_data, Message):
            return input_data.text or ""
        elif isinstance(input_data, str):
            return input_data
        elif isinstance(input_data, dict):
            return json.dumps(input_data, indent=2)
        else:
            return str(input_data)

    def _format_slack_messages(self, parsed_input: dict) -> str:
        """Format Slack messages for the prompt."""
        messages = parsed_input.get("messages", [])
        if not messages:
            return "No Slack messages provided."

        formatted = []
        for i, msg in enumerate(messages, 1):
            text = msg.get("text", "")
            formatted.append(f"**Message {i}:**\n```\n{text}\n```")

        return "\n\n".join(formatted)

    def _format_transcript(self, transcript: str) -> str:
        """Format the meeting transcript for the prompt."""
        if not transcript or transcript.strip() == "":
            return "No meeting transcript available."

        if len(transcript) > 5000:
            transcript = transcript[:5000] + "\n\n[... transcript truncated for brevity ...]"

        return f"```\n{transcript}\n```"

    def _format_jira_state(self, jira_data: Any) -> str:
        """Format JIRA state for the prompt."""
        if not jira_data:
            return "No JIRA state provided."

        content = self._extract_text(jira_data)
        if not content or content.strip() == "":
            return "No JIRA state provided."

        try:
            parsed = json.loads(content) if isinstance(content, str) else content
            return f"```json\n{json.dumps(parsed, indent=2)}\n```"
        except (json.JSONDecodeError, TypeError):
            return f"```\n{content}\n```"

    def _build_analysis_prompt(self, parsed_input: dict) -> str:
        """Build the enriched prompt for Analysis Mode (/jira-sync)."""
        transcript_text = self._extract_text(self.gdrive_transcript)
        jira_state_text = self._extract_text(self.jira_state)
        project_key = self.jira_project_key or "LAN"

        prompt = f"""# JIRA Review Analysis Request

## Mode: ANALYSIS

You are in **ANALYSIS MODE**. Your task is to analyze the information below and generate structured JSON proposals for JIRA updates.

**CRITICAL JSON RULES - READ CAREFULLY:**
1. Your ENTIRE response must be valid, parseable JSON
2. NO COMMENTS - JSON does not support // or /* */ comments
3. NO trailing commas after the last item in arrays or objects
4. All property names and strings must use double quotes
5. Use null (not None), true/false (not True/False)
6. NO text before or after the JSON object

---

## Data Sources

### 1. Slack Messages (Marked for JIRA Review)
{self._format_slack_messages(parsed_input)}

### 2. Latest Meeting Transcript
{self._format_transcript(transcript_text)}

### 3. Current JIRA State (Project: {project_key})
{self._format_jira_state(jira_state_text)}

---

## Your Task

Analyze the information above and identify:
1. Existing JIRA tickets that need updates based on discussions
2. New tickets that should be created based on action items or requests
3. Discrepancies between discussions and current JIRA state

---

## Output Format

Respond with ONLY this JSON structure (no comments, no extra text):

```json
{{
  "analysis_summary": "Brief 1-2 sentence summary of findings",
  "proposals": [
    {{
      "proposal_id": "prop-001",
      "ticket_key": "{project_key}-123",
      "ticket_summary": "Existing ticket title",
      "change_type": "update_field",
      "field": "description",
      "current_value": "Current value or null",
      "proposed_value": "New proposed value",
      "source": "slack_thread",
      "source_excerpt": "Brief quote from source (max 100 chars)",
      "confidence": "high"
    }},
    {{
      "proposal_id": "prop-002",
      "ticket_key": null,
      "ticket_summary": "New ticket to create",
      "change_type": "create_issue",
      "field": null,
      "current_value": null,
      "proposed_value": {{
        "project_key": "{project_key}",
        "summary": "Ticket title",
        "description": "Detailed description",
        "issue_type": "Task",
        "assignee": "Person Name",
        "priority": "Medium"
      }},
      "source": "slack_thread",
      "source_excerpt": "Create a ticket for...",
      "confidence": "high"
    }}
  ],
  "no_action_items": []
}}
```

### Valid change_type values:
- `"update_field"` - Update a field on existing ticket (description, summary, priority, labels)
- `"add_comment"` - Add a comment to existing ticket
- `"transition"` - Change ticket status (To Do, In Progress, Done)
- `"create_issue"` - Create a new ticket (ticket_key must be null)
- `"assign"` - Assign ticket to someone
- `"set_due_date"` - Set or change due date

### For create_issue:
- Set `ticket_key` to `null`
- Set `proposed_value` to an object with: project_key, summary, description, issue_type, assignee, priority

### Confidence levels:
- `"high"` - Explicit, unambiguous decision in source
- `"medium"` - Implied or partially specified
- `"low"` - Mentioned but not clearly decided

---

## IMPORTANT REMINDERS

1. **NO COMMENTS IN JSON** - This will cause parsing errors
2. **DO NOT use any tools** - Only output JSON in Analysis Mode
3. **Only propose changes based on explicit information** - Don't assume or infer
4. **For new tickets, ticket_key must be null** - Don't invent ticket keys
"""

        if self.additional_context:
            prompt += f"\n---\n\n## Additional Context\n{self.additional_context}\n"

        return prompt

    def _build_execution_prompt(self, parsed_input: dict) -> str:
        """Build the prompt for Execution Mode (approval_decisions)."""
        decisions = parsed_input.get("decisions", [])

        approved = [d for d in decisions if d.get("decision") == "approved"]
        rejected = [d for d in decisions if d.get("decision") == "rejected"]

        prompt = """# JIRA Update Execution Request

## Mode: EXECUTION

You are in **EXECUTION MODE**. Execute the approved proposals using the JIRA Reader/Writer tool.

---

## Approved Proposals (EXECUTE THESE)
"""
        if approved:
            for i, decision in enumerate(approved, 1):
                proposed_value = decision.get('proposed_value', 'N/A')
                if isinstance(proposed_value, dict):
                    proposed_display = json.dumps(proposed_value, indent=2)
                else:
                    proposed_display = str(proposed_value)

                prompt += f"""
### {i}. {decision.get('ticket_key') or 'NEW TICKET'} - {decision.get('change_type', 'unknown')}
- **Proposal ID:** {decision.get('proposal_id')}
- **Change Type:** {decision.get('change_type')}
- **Field:** {decision.get('field_name') or decision.get('field', 'N/A')}
- **Proposed Value:**
```
{proposed_display}
```
"""
        else:
            prompt += "\n*No proposals were approved.*\n"

        prompt += """
---

## Rejected Proposals (SKIP THESE)
"""
        if rejected:
            for decision in rejected:
                prompt += f"- {decision.get('ticket_key') or 'NEW'} ({decision.get('proposal_id')}): REJECTED by user\n"
        else:
            prompt += "\n*No proposals were rejected.*\n"

        prompt += """
---

## Tool Usage Guide

Use the **JIRA Reader/Writer** tool with the appropriate `action` parameter:

| change_type | Tool Action | Key Parameters |
|-------------|-------------|----------------|
| update_field | "Update Issue" | issue_key, + field to update (summary, description, priority, labels) |
| add_comment | "Add Comment" | issue_key, comment |
| transition | "Transition Issue" | issue_key, transition_to |
| create_issue | "Create Issue" | project_key, summary, description, issue_type, assignee, priority |
| assign | "Assign Issue" | issue_key, assignee |
| set_due_date | "Set Due Date" | issue_key, due_date |

### Examples:

**Update Issue:**
```
action: "Update Issue"
issue_key: "LAN-123"
description: "Updated description text"
```

**Create Issue:**
```
action: "Create Issue"
project_key: "LAN"
summary: "New ticket title"
description: "Ticket description"
issue_type: "Task"
assignee: "Joaquin"
priority: "Medium"
```

**Transition Issue:**
```
action: "Transition Issue"
issue_key: "LAN-123"
transition_to: "In Progress"
```

**Add Comment:**
```
action: "Add Comment"
issue_key: "LAN-123"
comment: "Comment text here"
```

---

## Instructions

1. Execute ONLY the **APPROVED** proposals listed above
2. **SKIP all REJECTED** proposals - do not execute them
3. For `create_issue`, use the values from the `proposed_value` object
4. After executing, provide a summary of what was done

**IMPORTANT:** Actually call the tools to execute the changes. Do not just describe what you would do.
"""

        return prompt

    def process_input(self) -> Message:
        """Process the input and route to appropriate mode."""
        logger.info(f"[SmartEnrichment] Input type: {type(self.input_data)}")
        if isinstance(self.input_data, Message):
            logger.info(f"[SmartEnrichment] Message.text (first 500 chars): {(self.input_data.text or '')[:500]}")
        elif isinstance(self.input_data, dict):
            logger.info(f"[SmartEnrichment] Dict keys: {list(self.input_data.keys())}")
        else:
            logger.info(f"[SmartEnrichment] Raw input (first 500 chars): {str(self.input_data)[:500]}")

        parsed_input = self._parse_json_input(self.input_data)
        logger.info(f"[SmartEnrichment] Parsed keys: {list(parsed_input.keys())}, command: {parsed_input.get('command', 'MISSING')}")

        command = parsed_input.get("command", "")

        if command == "/jira-sync":
            msg_count = len(parsed_input.get("messages", []))
            self.status = f"Analysis Mode - {msg_count} messages"
            prompt = self._build_analysis_prompt(parsed_input)

        elif command == "approval_decisions":
            decision_count = len(parsed_input.get("decisions", []))
            self.status = f"Execution Mode - {decision_count} decisions"
            prompt = self._build_execution_prompt(parsed_input)

        else:
            self.status = f"Unknown command: {command}"
            prompt = f"""# Unknown Command

Received command: `{command}`

Raw input:
```json
{json.dumps(parsed_input, indent=2)}
```

Expected commands:
- `/jira-sync` - For analysis mode
- `approval_decisions` - For execution mode
"""

        return Message(text=prompt)
