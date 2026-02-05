"""
JIRA State Fetcher - Demo Component (Simple Version)

Copy this code directly into LangBuilder's Custom Component editor.
Returns hardcoded JIRA data for demonstration purposes.
"""

import json
from datetime import datetime

from langbuilder.custom.custom_component.component import Component
from langbuilder.io import MessageTextInput, Output
from langbuilder.schema.message import Message


class JiraStateFetcherDemo(Component):
    display_name = "JIRA State Fetcher (Demo)"
    description = "Returns mock JIRA ticket data for demonstration purposes."
    icon = "ticket"
    name = "JiraStateFetcherDemo"

    inputs = [
        MessageTextInput(
            name="project_key",
            display_name="Project Key",
            info="The JIRA project key (e.g., LAN)",
            value="LAN",
            required=False,
        ),
    ]

    outputs = [
        Output(
            display_name="JIRA State",
            name="jira_state",
            method="fetch_jira_state",
        ),
    ]

    def fetch_jira_state(self) -> Message:
        """Return mock JIRA state."""
        project_key = self.project_key or "LAN"

        # Demo tickets - key tickets for the demo
        tickets = [
            {
                "key": "LAN-91",
                "summary": "Create LB Flow that integrates Atlassian and Slack (Andrei)",
                "status": {"name": "In Progress", "category": "In Progress"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Andrei Lupica"},
                "due_date": "Not set"
            },
            {
                "key": "LAN-92",
                "summary": "Create LB Flow that integrates Atlassian and Slack (Joaquin)",
                "status": {"name": "Not Started", "category": "To Do"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Joaquin Andres Robador"},
                "due_date": "Not set"
            },
            {
                "key": "LAN-103",
                "summary": "Create LB Flow that integrates Atlassian and Slack (Antoine)",
                "status": {"name": "Not Started", "category": "To Do"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Antoine Dubuc"},
                "due_date": "Not set"
            },
            {
                "key": "LAN-99",
                "summary": "Migrate API endpoints from v1 to v2",
                "status": {"name": "Not Started", "category": "To Do"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Antoine Dubuc"},
                "due_date": "End of February"
            },
            {
                "key": "LAN-93",
                "summary": "Calculate actual production costs for Carter's flows",
                "status": {"name": "Not Started", "category": "To Do"},
                "priority": {"name": "Critical"},
                "assignee": {"display_name": "Antoine Dubuc"},
                "due_date": "Not set"
            },
            {
                "key": "LAN-85",
                "summary": "Work on the AI SDLC MVP",
                "status": {"name": "In Progress", "category": "In Progress"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Antoine Dubuc"},
                "due_date": "Not set"
            },
            {
                "key": "LAN-62",
                "summary": "Finalize BI SDR Agent",
                "status": {"name": "In Progress", "category": "In Progress"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Michael Philip"},
                "due_date": "Not set"
            },
            {
                "key": "LAN-68",
                "summary": "Need SES config in AWS Dev",
                "status": {"name": "Not Started", "category": "To Do"},
                "priority": {"name": "Blocker"},
                "assignee": {"display_name": "Vadim Tsarfin"},
                "due_date": "Not set"
            },
        ]

        jira_state = {
            "project": project_key,
            "total_tickets": len(tickets),
            "fetched_at": datetime.utcnow().isoformat(),
            "tickets": tickets,
        }

        self.status = f"Demo: {len(tickets)} tickets"
        return Message(text=json.dumps(jira_state, indent=2))
