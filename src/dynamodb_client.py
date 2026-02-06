"""DynamoDB client for reading PM configurations.

Uses boto3 to access the pm_configurations table.
All methods use asyncio.run_in_executor to avoid blocking the event loop.
"""

import asyncio
import logging
from typing import Any, Optional

import boto3
from boto3.dynamodb.types import TypeDeserializer

logger = logging.getLogger(__name__)


class DynamoDBClient:
    """Client for accessing PM configurations in DynamoDB."""

    def __init__(self, table_name: str, region: str = "us-east-1"):
        self.table_name = table_name
        self.region = region
        self._client = boto3.client("dynamodb", region_name=region)
        self._deserializer = TypeDeserializer()

    def _deserialize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Deserialize a DynamoDB item to a plain dict."""
        return {k: self._deserializer.deserialize(v) for k, v in item.items()}

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
        Tweaks dict keyed by component name.
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
        tweaks["JiraReaderWriter"] = jira_tweaks
        tweaks["JiraStateFetcher"] = jira_tweaks.copy()

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

    tweaks["GoogleDriveDocsParserSA"] = gdrive_tweaks

    return tweaks
