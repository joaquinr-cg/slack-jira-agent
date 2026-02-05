"""
JIRA State Fetcher - Demo Component

Returns mock JIRA data for demonstration purposes.
This component simulates fetching current JIRA state without making actual API calls.
"""

import json
from datetime import datetime
from typing import Any

from langbuilder.custom.custom_component.component import Component
from langbuilder.io import DropdownInput, MessageTextInput, Output
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
            info="The JIRA project key (e.g., LAN, PROJ)",
            value="LAN",
            required=True,
        ),
        DropdownInput(
            name="filter_status",
            display_name="Filter by Status",
            options=["All", "To Do", "In Progress", "Done"],
            value="All",
            info="Filter tickets by status category",
            required=False,
            advanced=True,
        ),
    ]

    outputs = [
        Output(
            display_name="JIRA State",
            name="jira_state",
            method="fetch_jira_state",
        ),
    ]

    def _get_demo_tickets(self) -> list[dict[str, Any]]:
        """Return demo JIRA tickets."""
        return [
            {
                "key": "LAN-103",
                "summary": "Create LB Flow that integrates Atlassian and Slack (Antoine)",
                "status": {"name": "Not Started", "category": "To Do", "color": "blue-gray"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "reporter": {"display_name": "Michael Philip", "email": "michael@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Connect directly to Jira Cloud API - Search, create, update, and transition issues"
            },
            {
                "key": "LAN-91",
                "summary": "Create LB Flow that integrates Atlassian and Slack (Andrei)",
                "status": {"name": "In Progress", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Andrei Lupica", "email": "andreil@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-92",
                "summary": "Create LB Flow that integrates Atlassian and Slack (Joaquin)",
                "status": {"name": "Not Started", "category": "To Do", "color": "blue-gray"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Joaquin Andres Robador", "email": "joaquinr@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-99",
                "summary": "Migrate API endpoints from v1 to v2",
                "status": {"name": "Not Started", "category": "To Do", "color": "blue-gray"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Update all API endpoints from v1 to v2. Target completion: end of February."
            },
            {
                "key": "LAN-100",
                "summary": "Add rate limiting to new endpoints",
                "status": {"name": "Not Started", "category": "To Do", "color": "blue-gray"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Implement rate limiting for all new v2 API endpoints. Owner: adubuc."
            },
            {
                "key": "LAN-95",
                "summary": "Investigate how to use Langbuilder's native MCP server feature for our Atlassian mcp server",
                "status": {"name": "In Progress", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-93",
                "summary": "Calculate actual production costs for Carter's flows",
                "status": {"name": "Not Started", "category": "To Do", "color": "blue-gray"},
                "priority": {"name": "Critical"},
                "assignee": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-85",
                "summary": "Work on the AI SDLC MVP",
                "status": {"name": "In Progress", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-79",
                "summary": "investigate how to have an ai code debugger in our repo",
                "status": {"name": "Review", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Andrei Lupica", "email": "andreil@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Have a clean repo that a code reviewer can help with"
            },
            {
                "key": "LAN-80",
                "summary": "Document CICD Pipeline (LAN-2)",
                "status": {"name": "In Progress", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Vadim Tsarfin", "email": "vtsarfin@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Speak with Vadim, Joaquin, and Eugene to fully understand the pipeline and document it."
            },
            {
                "key": "LAN-62",
                "summary": "Finalize BI SDR Agent",
                "status": {"name": "In Progress", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Michael Philip", "email": "michael@cloudgeometry.com"},
                "reporter": {"display_name": "Michael Philip", "email": "michael@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Finalize for Questionnaire BI SDR logic, questions, instructions, behavior"
            },
            {
                "key": "LAN-55",
                "summary": "Finalize the CG Bot",
                "status": {"name": "Review", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Joaquin Andres Robador", "email": "joaquinr@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Ensure the HubSpot component captures contact info and blueprint generated"
            },
            {
                "key": "LAN-51",
                "summary": "[Spike]Define QA Requirements for LB",
                "status": {"name": "Review", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Critical"},
                "assignee": {"display_name": "Joaquin Andres Robador", "email": "joaquinr@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Define Alerts when login page doesn't show up, Happy Path definitions"
            },
            {
                "key": "LAN-43",
                "summary": "Investigate (no fix) the flow visibility issue",
                "status": {"name": "In Progress", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Minor"},
                "assignee": {"display_name": "Joaquin Andres Robador", "email": "joaquinr@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "When setting a new LB api key to import the flows, the flows do not import"
            },
            {
                "key": "LAN-45",
                "summary": "Document LB upgrade process",
                "status": {"name": "In Progress", "category": "In Progress", "color": "yellow"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Joaquin Andres Robador", "email": "joaquinr@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Talk with eugene and Vadim on how to do updates for both langflow and openwebui"
            },
            {
                "key": "LAN-82",
                "summary": "Host HR Bot for Nicu",
                "status": {"name": "DONE", "category": "Done", "color": "green"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Andrei Lupica", "email": "andreil@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-83",
                "summary": "SES dev configuration",
                "status": {"name": "DONE", "category": "Done", "color": "green"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Vadim Tsarfin", "email": "vtsarfin@cloudgeometry.com"},
                "reporter": {"display_name": "Vadim Tsarfin", "email": "vtsarfin@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-78",
                "summary": "Review Community MCP Component for Atlassian",
                "status": {"name": "DONE", "category": "Done", "color": "green"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Joaquin Andres Robador", "email": "joaquinr@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-74",
                "summary": "Carter flow upgrades",
                "status": {"name": "DONE", "category": "Done", "color": "green"},
                "priority": {"name": "Major"},
                "assignee": {"display_name": "Joaquin Andres Robador", "email": "joaquinr@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": []
            },
            {
                "key": "LAN-68",
                "summary": "Need SES config in AWS Dev",
                "status": {"name": "Not Started", "category": "To Do", "color": "blue-gray"},
                "priority": {"name": "Blocker"},
                "assignee": {"display_name": "Vadim Tsarfin", "email": "vtsarfin@cloudgeometry.com"},
                "reporter": {"display_name": "Antoine Dubuc", "email": "adubuc@cloudgeometry.com"},
                "due_date": "Not set",
                "labels": [],
                "description": "Verify sender domain in AWS SES, Add DNS records, Request production access"
            },
        ]

    def fetch_jira_state(self) -> Message:
        """Fetch mock JIRA state for demo purposes."""
        project_key = self.project_key or "LAN"
        filter_status = self.filter_status or "All"

        # Get demo tickets
        all_tickets = self._get_demo_tickets()

        # Apply status filter if specified
        if filter_status != "All":
            status_map = {
                "To Do": "To Do",
                "In Progress": "In Progress",
                "Done": "Done",
            }
            target_category = status_map.get(filter_status)
            if target_category:
                all_tickets = [
                    t for t in all_tickets
                    if t.get("status", {}).get("category") == target_category
                ]

        # Build response
        jira_state = {
            "project": project_key,
            "total_tickets": len(all_tickets),
            "fetched_at": datetime.utcnow().isoformat(),
            "tickets": all_tickets,
        }

        self.status = f"Fetched {len(all_tickets)} tickets from {project_key}"

        return Message(text=json.dumps(jira_state, indent=2))
