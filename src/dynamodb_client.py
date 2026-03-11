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

from .jira_tenant import build_jira_api_base_url

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
        loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()
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
        loop = asyncio.get_running_loop()
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

        loop = asyncio.get_running_loop()
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


def _get_component_ids() -> dict[str, str]:
    """Get LangBuilder component instance IDs from settings.

    Returns a dict with keys matching the old constant names for easy migration.
    """
    from .config import get_settings
    s = get_settings()
    return {
        "GDRIVE_PARSER": s.lb_main_gdrive_parser_id,
        "JIRA_STATE_FETCHER": s.lb_main_jira_state_fetcher_id,
        "JIRA_READER_WRITER": s.lb_main_jira_reader_writer_id,
        "CHAT_JIRA_STATE_FETCHER": s.lb_chat_jira_state_fetcher_id,
        "CHAT_JIRA_READER_WRITER": s.lb_chat_jira_reader_writer_id,
        "TRIGGER_TRANSCRIPT": s.lb_trigger_transcript_id,
        "TRIGGER_CHAT_INPUT": s.lb_trigger_chat_input_id,
    }


def build_tweaks_from_pm_config(
    pm_config: dict[str, Any],
    default_gdrive: Optional[dict[str, Any]] = None,
    shared_jira: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build LangBuilder tweaks payload from a PM configuration.

    Maps DynamoDB PM config fields to the LangBuilder component inputs
    so each PM's own JIRA/GDrive credentials are injected at runtime.

    GDrive uses shared service account credentials from default_gdrive,
    with PMs able to override folder_id and client_email in DynamoDB.

    When shared_jira is provided and PM has no individual api_token,
    the shared service account credentials are used instead.

    Args:
        pm_config: PM configuration dict from DynamoDB.
        default_gdrive: Shared GDrive service account config from Settings.
        shared_jira: Shared JIRA service account config from Settings (optional).

    Returns:
        Tweaks dict keyed by component instance ID.
    """
    ids = _get_component_ids()
    jira = pm_config.get("jira_config", {})
    pm_gdrive = pm_config.get("gdrive_config", {})

    tweaks: dict[str, Any] = {}

    # JIRA credentials for both reader and writer components
    # Multi-project support: read project_keys (list) with backward compat for project_key (string)
    project_keys = jira.get("project_keys", [])
    if not project_keys:
        legacy_key = jira.get("project_key", "")
        if legacy_key:
            project_keys = [legacy_key]
    project_key_value = ",".join(project_keys) if project_keys else ""

    # Use shared JIRA service account if PM has no individual token
    if shared_jira and not jira.get("api_token"):
        logger.info(
            "Using shared Jira credentials for PM %s (projects=%s, email=%s)",
            pm_config.get("slack_id", "unknown"),
            project_key_value or "none",
            shared_jira.get("email", ""),
        )
        jira_tweaks = {
            "jira_url": shared_jira.get("jira_url", ""),
            "email": shared_jira.get("email", ""),
            "api_token": shared_jira.get("api_token", ""),
            "auth_type": "basic",
            "project_key": project_key_value,
        }
        api_base_url = build_jira_api_base_url(
            jira_tweaks["jira_url"],
            jira_tweaks["email"],
            cloud_id=shared_jira.get("cloud_id"),
        )
        if api_base_url:
            jira_tweaks["api_base_url"] = api_base_url
    elif jira:
        logger.info(
            "Using PM-specific Jira credentials for PM %s (projects=%s, email=%s)",
            pm_config.get("slack_id", "unknown"),
            project_key_value or "none",
            jira.get("email", ""),
        )
        jira_tweaks = {
            "jira_url": jira.get("jira_url", ""),
            "email": jira.get("email", ""),
            "api_token": jira.get("api_token", ""),
            "auth_type": jira.get("auth_type", "basic"),
            "project_key": project_key_value,
        }
        api_base_url = build_jira_api_base_url(
            jira_tweaks["jira_url"],
            jira_tweaks["email"],
            cloud_id=jira.get("cloud_id"),
        )
        if api_base_url:
            jira_tweaks["api_base_url"] = api_base_url
    else:
        jira_tweaks = {}

    if jira_tweaks:
        tweaks[ids["JIRA_READER_WRITER"]] = jira_tweaks
        tweaks[ids["JIRA_STATE_FETCHER"]] = jira_tweaks.copy()

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

    tweaks[ids["GDRIVE_PARSER"]] = gdrive_tweaks

    return tweaks
