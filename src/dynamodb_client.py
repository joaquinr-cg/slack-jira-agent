"""DynamoDB client for reading PM configurations.

Uses boto3 to access the pm_configurations table.
All methods use asyncio.run_in_executor to avoid blocking the event loop.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

logger = logging.getLogger(__name__)


class DynamoDBClient:
    """Client for accessing PM configurations in DynamoDB."""

    def __init__(self, table_name: str, region: str = "us-east-1"):
        self.table_name = table_name
        self.region = region
        self._client = boto3.client("dynamodb", region_name=region)
        self._deserializer = TypeDeserializer()
        self._serializer = TypeSerializer()

    def _deserialize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Deserialize a DynamoDB item to a plain dict."""
        return {k: self._deserializer.deserialize(v) for k, v in item.items()}

    def _serialize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Serialize a plain dict to DynamoDB item format."""
        return {k: self._serializer.serialize(v) for k, v in item.items()}

    async def get_pm_config(self, slack_id: str) -> Optional[dict[str, Any]]:
        """Fetch PM configuration by Slack user ID.

        Args:
            slack_id: The Slack user ID (e.g. "U0123456789")

        Returns:
            PM configuration dict, or None if not found / disabled.
        """
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self._client.get_item(
                    TableName=self.table_name,
                    Key={"slack_id": {"S": slack_id}},
                ),
            )
        except Exception as e:
            logger.error("DynamoDB get_item failed for %s: %s", slack_id, e)
            return None

        raw_item = response.get("Item")
        if not raw_item:
            logger.warning("No PM config found for slack_id=%s", slack_id)
            return None

        item = self._deserialize_item(raw_item)

        if not item.get("enabled", False):
            logger.warning("PM config for %s is disabled", slack_id)
            return None

        return item

    async def update_last_processed(
        self, slack_id: str, transcript_info: dict[str, str]
    ) -> None:
        """Update the last_processed_transcript field for a PM.

        Args:
            slack_id: The Slack user ID
            transcript_info: Dict with file_id, file_name, modified_time, processed_at
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._client.update_item(
                    TableName=self.table_name,
                    Key={"slack_id": {"S": slack_id}},
                    UpdateExpression="SET last_processed_transcript = :transcript, updated_at = :now",
                    ExpressionAttributeValues={
                        ":transcript": {
                            "M": {
                                "file_id": {"S": transcript_info.get("file_id", "")},
                                "file_name": {"S": transcript_info.get("file_name", "")},
                                "modified_time": {"S": transcript_info.get("modified_time", "")},
                                "processed_at": {"S": transcript_info.get("processed_at", "")},
                            }
                        },
                        ":now": {"S": transcript_info.get("processed_at", "")},
                    },
                ),
            )
            logger.info("Updated last_processed_transcript for %s", slack_id)
        except Exception as e:
            logger.error("DynamoDB update failed for %s: %s", slack_id, e)

    async def list_enabled_pms(self) -> list[dict[str, Any]]:
        """List all enabled PM configurations.

        Returns:
            List of PM configuration dicts where enabled=True.
        """
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(
                None,
                lambda: self._client.scan(
                    TableName=self.table_name,
                    FilterExpression="enabled = :enabled",
                    ExpressionAttributeValues={":enabled": {"BOOL": True}},
                ),
            )
        except Exception as e:
            logger.error("DynamoDB scan failed: %s", e)
            return []

        items = response.get("Items", [])
        return [self._deserialize_item(item) for item in items]

    async def create_pm(self, pm_data: dict[str, Any]) -> None:
        """Create a new PM configuration.

        Args:
            pm_data: Dict with keys: slack_id, email, name, jira_config, gdrive_config, etc.
                     Timestamps (created_at, updated_at) are added automatically.
        """
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "slack_id": pm_data["slack_id"],
            "email": pm_data.get("email", ""),
            "name": pm_data.get("name", ""),
            "enabled": pm_data.get("enabled", True),
            "jira_config": pm_data.get("jira_config", {}),
            "gdrive_config": pm_data.get("gdrive_config", {}),
            "last_processed_transcript": pm_data.get("last_processed_transcript", {
                "file_id": "", "file_name": "", "modified_time": "", "processed_at": "",
            }),
            "flow_config": pm_data.get("flow_config", {
                "transcripts_only": False,
                "notification_channel": "",
                "auto_approve": False,
            }),
            "created_at": now,
            "updated_at": now,
        }

        serialized = self._serialize_item(item)
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._client.put_item(
                    TableName=self.table_name,
                    Item=serialized,
                ),
            )
            logger.info("Created PM config for %s", pm_data["slack_id"])
        except Exception as e:
            logger.error("DynamoDB put_item failed for %s: %s", pm_data["slack_id"], e)
            raise

    async def update_pm(self, slack_id: str, updates: dict[str, Any]) -> None:
        """Update specific fields on a PM configuration.

        Args:
            slack_id: The Slack user ID.
            updates: Dict of top-level fields to update (e.g. {"jira_config": {...}}).
                     updated_at is set automatically.
        """
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Build UpdateExpression dynamically
        set_parts = []
        attr_names = {}
        attr_values = {}
        for i, (key, value) in enumerate(updates.items()):
            placeholder_name = f"#k{i}"
            placeholder_value = f":v{i}"
            set_parts.append(f"{placeholder_name} = {placeholder_value}")
            attr_names[placeholder_name] = key
            attr_values[placeholder_value] = self._serializer.serialize(value)

        update_expr = "SET " + ", ".join(set_parts)

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._client.update_item(
                    TableName=self.table_name,
                    Key={"slack_id": {"S": slack_id}},
                    UpdateExpression=update_expr,
                    ExpressionAttributeNames=attr_names,
                    ExpressionAttributeValues=attr_values,
                ),
            )
            logger.info("Updated PM config for %s: %s", slack_id, list(updates.keys()))
        except Exception as e:
            logger.error("DynamoDB update failed for %s: %s", slack_id, e)
            raise

    async def disable_pm(self, slack_id: str) -> None:
        """Disable a PM configuration."""
        await self.update_pm(slack_id, {"enabled": False})
        logger.info("Disabled PM %s", slack_id)

    async def enable_pm(self, slack_id: str) -> None:
        """Enable a PM configuration."""
        await self.update_pm(slack_id, {"enabled": True})
        logger.info("Enabled PM %s", slack_id)


# LangBuilder flow component instance IDs (from the deployed jira-tickets flow)
COMPONENT_ID_GDRIVE_PARSER = "CustomComponent-swCo4"      # GoogleDriveDocsParserSA
COMPONENT_ID_JIRA_STATE_FETCHER = "CustomComponent-h9t4Q"  # JiraStateFetcher (read)
COMPONENT_ID_JIRA_READER_WRITER = "CustomComponent-MvTpp"  # JiraReaderWriter (read/write)

# LangBuilder flow component instance IDs (from the deployed trigger/automatic-parser flow)
TRIGGER_COMPONENT_ID_TRANSCRIPT = "TranscriptTrigger-NxiAw"


def build_tweaks_from_pm_config(
    pm_config: dict[str, Any],
    default_gdrive: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build LangBuilder tweaks payload from a PM configuration.

    Maps DynamoDB PM config fields to the LangBuilder component inputs
    so each PM's own JIRA/GDrive credentials are injected at runtime.

    GDrive uses shared service account credentials from default_gdrive,
    with PMs able to override folder_id and client_email in DynamoDB.

    Args:
        pm_config: PM configuration dict from DynamoDB.
        default_gdrive: Shared GDrive service account config from Settings.

    Returns:
        Tweaks dict keyed by component instance ID.
    """
    jira = pm_config.get("jira_config", {})
    pm_gdrive = pm_config.get("gdrive_config", {})

    tweaks: dict[str, Any] = {}

    # JIRA credentials for both reader and writer components
    if jira:
        jira_tweaks = {
            "jira_url": jira.get("jira_url", ""),
            "email": jira.get("email", ""),
            "api_token": jira.get("api_token", ""),
            "auth_type": jira.get("auth_type", "basic"),
            "project_key": jira.get("project_key", ""),
        }
        tweaks[COMPONENT_ID_JIRA_READER_WRITER] = jira_tweaks
        tweaks[COMPONENT_ID_JIRA_STATE_FETCHER] = jira_tweaks.copy()

    # Google Drive: shared service account + per-PM overrides for folder_id & client_email
    base_gdrive = default_gdrive or {}
    gdrive_tweaks = {
        "project_id": base_gdrive.get("project_id", ""),
        "client_email": base_gdrive.get("client_email", ""),
        "private_key": base_gdrive.get("private_key", ""),
        "private_key_id": base_gdrive.get("private_key_id", ""),
        "client_id": base_gdrive.get("client_id", ""),
        "folder_id": base_gdrive.get("folder_id", ""),
        "folder_name": base_gdrive.get("folder_name", ""),
        "file_filter": base_gdrive.get("file_filter", ""),
    }

    # PM overrides: only folder_id and client_email
    if pm_gdrive.get("folder_id"):
        gdrive_tweaks["folder_id"] = pm_gdrive["folder_id"]
    if pm_gdrive.get("client_email"):
        gdrive_tweaks["client_email"] = pm_gdrive["client_email"]

    tweaks[COMPONENT_ID_GDRIVE_PARSER] = gdrive_tweaks

    return tweaks
