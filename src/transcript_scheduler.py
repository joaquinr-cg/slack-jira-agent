"""Scheduled transcript detection via LangBuilder trigger flow.

Periodically calls a LangBuilder flow that:
1. Reads all enabled PMs from DynamoDB
2. Checks each PM's GDrive folder for new transcripts
3. Compares timestamps against last_processed_transcript
4. Returns slack_ids of PMs with new transcripts

When new transcripts are found, notifies the PM in Slack.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from .config import Settings
from .dynamodb_client import (
    TRIGGER_CHAT_INPUT_ID,
    TRIGGER_COMPONENT_ID_TRANSCRIPT,
    DynamoDBClient,
)
from .langbuilder_client import LangBuilderClient, LangBuilderError

logger = logging.getLogger(__name__)


class TranscriptScheduler:
    """Periodically checks for new Google Drive transcripts via LangBuilder."""

    def __init__(
        self,
        settings: Settings,
        langbuilder_client: LangBuilderClient,
        dynamodb_client: DynamoDBClient,
        slack_client: Any,
    ):
        self.settings = settings
        self.langbuilder = langbuilder_client
        self.dynamodb = dynamodb_client
        self.slack = slack_client
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start the background polling loop."""
        if not self.settings.trigger_flow_id:
            logger.warning("TRIGGER_FLOW_ID not set — transcript scheduler disabled")
            return

        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Transcript scheduler started (every %d min, flow=%s)",
            self.settings.trigger_interval_minutes,
            self.settings.trigger_flow_id,
        )

    async def stop(self) -> None:
        """Cancel the background loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Transcript scheduler stopped")

    async def _loop(self) -> None:
        """Run check_for_transcripts on a fixed interval."""
        interval = self.settings.trigger_interval_minutes * 60
        while True:
            try:
                await self._check_for_transcripts()
            except Exception:
                logger.exception("Transcript check failed")
            await asyncio.sleep(interval)

    async def _check_for_transcripts(self) -> None:
        """Call the trigger flow and notify PMs with new transcripts."""
        logger.info("Running scheduled transcript check...")

        enabled_pms = await self.dynamodb.list_enabled_pms()
        if not enabled_pms:
            logger.info("No enabled PMs — skipping")
            return

        default_gdrive = {
            "project_id": self.settings.gdrive_project_id,
            "client_email": self.settings.gdrive_client_email,
            "private_key": self.settings.gdrive_private_key,
            "private_key_id": self.settings.gdrive_private_key_id,
            "client_id": self.settings.gdrive_client_id,
            "folder_id": self.settings.gdrive_folder_id,
            "folder_name": self.settings.gdrive_folder_name,
            "file_filter": self.settings.gdrive_file_filter,
        }

        for pm in enabled_pms:
            slack_id = pm.get("slack_id", "")
            if not slack_id:
                continue

            try:
                await self._check_pm(pm, default_gdrive)
            except Exception:
                logger.exception("Transcript check failed for PM %s", slack_id)

    async def _check_pm(
        self, pm_config: dict[str, Any], default_gdrive: dict[str, str]
    ) -> None:
        """Check a single PM for new transcripts."""
        slack_id = pm_config["slack_id"]
        pm_gdrive = pm_config.get("gdrive_config", {})

        # Build GDrive tweaks: shared service account + per-PM overrides
        gdrive_tweaks = {
            "project_id": default_gdrive.get("project_id", ""),
            "client_email": default_gdrive.get("client_email", ""),
            "private_key": default_gdrive.get("private_key", ""),
            "private_key_id": default_gdrive.get("private_key_id", ""),
            "client_id": default_gdrive.get("client_id", ""),
            "folder_id": default_gdrive.get("folder_id", ""),
            "folder_name": default_gdrive.get("folder_name", ""),
            "file_filter": default_gdrive.get("file_filter", ""),
        }
        if pm_gdrive.get("folder_id"):
            gdrive_tweaks["folder_id"] = pm_gdrive["folder_id"]
        if pm_gdrive.get("client_email"):
            gdrive_tweaks["client_email"] = pm_gdrive["client_email"]

        extra_tweaks = {TRIGGER_COMPONENT_ID_TRANSCRIPT: gdrive_tweaks}

        last_processed = pm_config.get("last_processed_transcript", {})

        # Use a dedicated LangBuilderClient pointing at the trigger flow
        trigger_client = LangBuilderClient(
            flow_url=self.langbuilder.flow_url,
            flow_id=self.settings.trigger_flow_id,
            api_key=self.langbuilder.api_key,
            timeout=self.langbuilder.timeout,
            chat_input_id=TRIGGER_CHAT_INPUT_ID,
        )

        session_id = str(uuid.uuid4())
        raw_response = await trigger_client.run_flow(
            session_id=session_id,
            input_data={
                "command": "check_transcripts",
                "slack_id": slack_id,
                "last_processed": last_processed,
            },
            extra_tweaks=extra_tweaks,
        )

        result = self._parse_trigger_response(raw_response)
        if not result or not result.get("has_new_transcripts"):
            logger.info("No new transcripts for PM %s", slack_id)
            return

        new_files = result.get("new_files", [])
        logger.info(
            "New transcripts detected for PM %s: %d file(s)", slack_id, len(new_files)
        )

        # Update last_processed_transcript in DynamoDB *before* triggering
        # so a slow jira-sync run won't cause duplicate triggers on the next tick.
        latest = result.get("latest_file", {})
        if latest.get("file_id"):
            from datetime import datetime, timezone

            await self.dynamodb.update_last_processed(
                slack_id,
                {
                    "file_id": latest["file_id"],
                    "file_name": latest.get("name", ""),
                    "modified_time": latest.get("modified_time", ""),
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        # Notify PM in Slack with a button to generate tickets
        file_list = "\n".join(f"• {f.get('name', 'unknown')}" for f in new_files)
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*New meeting transcript(s) detected:*\n{file_list}",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Generate Tickets from Latest Transcript",
                            "emoji": True,
                        },
                        "style": "primary",
                        "action_id": "generate_from_transcript",
                        "value": json.dumps({"slack_id": slack_id}),
                    },
                ],
            },
        ]

        await self.slack.chat_postMessage(
            channel=slack_id,
            text=f"New meeting transcript(s) detected:\n{file_list}",
            blocks=blocks,
        )
        logger.info("Sent transcript notification with button to PM %s", slack_id)

    @staticmethod
    def _parse_trigger_response(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Extract the trigger flow result from LangBuilder response."""
        try:
            message = (
                raw.get("outputs", [{}])[0]
                .get("outputs", [{}])[0]
                .get("artifacts", {})
                .get("message", "")
            )
            if not message:
                return None
            content = message.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            return json.loads(content.strip())
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as e:
            logger.error("Failed to parse trigger response: %s", e)
            return None
