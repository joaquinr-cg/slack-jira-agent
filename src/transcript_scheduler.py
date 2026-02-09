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
from .dynamodb_client import DynamoDBClient, build_tweaks_from_pm_config
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
        extra_tweaks = build_tweaks_from_pm_config(pm_config, default_gdrive)

        # The trigger flow uses TranscriptTrigger (not GoogleDriveDocsParserSA),
        # so copy the GDrive tweaks under that component name too.
        if "GoogleDriveDocsParserSA" in extra_tweaks:
            extra_tweaks["TranscriptTrigger"] = extra_tweaks["GoogleDriveDocsParserSA"].copy()

        last_processed = pm_config.get("last_processed_transcript", {})

        # Use a dedicated LangBuilderClient pointing at the trigger flow
        trigger_client = LangBuilderClient(
            flow_url=self.langbuilder.flow_url,
            flow_id=self.settings.trigger_flow_id,
            api_key=self.langbuilder.api_key,
            timeout=self.langbuilder.timeout,
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

        # Notify PM in Slack
        file_list = "\n".join(f"• {f.get('name', 'unknown')}" for f in new_files)
        await self.slack.chat_postMessage(
            channel=slack_id,
            text=f"New meeting transcript(s) detected:\n{file_list}",
        )

        # Auto-trigger the main jira-sync flow in transcripts_only mode
        if self.settings.trigger_auto_sync:
            await self._trigger_jira_sync(pm_config, extra_tweaks, slack_id)

    async def _trigger_jira_sync(
        self,
        pm_config: dict[str, Any],
        extra_tweaks: dict[str, Any],
        slack_id: str,
    ) -> None:
        """Trigger the main jira-sync flow in transcripts_only mode."""
        logger.info("Auto-triggering jira-sync (transcripts_only) for PM %s", slack_id)

        await self.slack.chat_postMessage(
            channel=slack_id,
            text="Automatically running JIRA sync on new transcript...",
        )

        session_id = str(uuid.uuid4())
        try:
            raw_response = await self.langbuilder.run_flow(
                session_id=session_id,
                input_data={
                    "command": "/jira-sync",
                    "messages": [],
                    "transcripts_only": True,
                },
                extra_tweaks=extra_tweaks,
            )
            logger.info("Auto jira-sync completed for PM %s (session=%s)", slack_id, session_id)

            # Notify PM with the result
            result_text = self._extract_summary(raw_response)
            await self.slack.chat_postMessage(
                channel=slack_id,
                text=f"JIRA sync complete:\n{result_text}",
            )
        except LangBuilderError as e:
            logger.error("Auto jira-sync failed for PM %s: %s", slack_id, e)
            await self.slack.chat_postMessage(
                channel=slack_id,
                text=f"Automatic JIRA sync failed: {e}",
            )

    @staticmethod
    def _extract_summary(raw_response: dict[str, Any]) -> str:
        """Pull the analysis_summary from a jira-sync response."""
        try:
            message = (
                raw_response.get("outputs", [{}])[0]
                .get("outputs", [{}])[0]
                .get("artifacts", {})
                .get("message", "")
            )
            if not message:
                return "No summary available."
            content = message.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            data = json.loads(content.strip())
            return data.get("analysis_summary", content[:500])
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return "Completed (could not parse summary)."

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
