"""Recurring /jira-sync scheduler.

Runs on a background loop, checking each PM's schedule_config in DynamoDB.
When a PM's cron expression is due, triggers _process_jira_sync for them.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Set
from zoneinfo import ZoneInfo

from croniter import croniter

from .config import Settings
from .db import DatabaseManager
from .dynamodb_client import DynamoDBClient, build_tweaks_from_pm_config
from .langbuilder_client import LangBuilderClient

logger = logging.getLogger(__name__)


class SyncScheduler:
    """Periodically triggers /jira-sync for PMs with configured schedules."""

    def __init__(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        langbuilder_client: LangBuilderClient,
        dynamodb_client: DynamoDBClient,
        slack_handler: Any,
    ):
        self.settings = settings
        self.db = db_manager
        self.langbuilder = langbuilder_client
        self.dynamodb = dynamodb_client
        self.slack_handler = slack_handler
        self._task: Optional[asyncio.Task] = None
        self._running_syncs: Set[str] = set()

    def start(self) -> None:
        """Start the background schedule check loop."""
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Sync scheduler started (check every %d seconds)",
            self.settings.sync_schedule_check_interval,
        )

    async def stop(self) -> None:
        """Cancel the background loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Sync scheduler stopped")

    async def _loop(self) -> None:
        """Run schedule check on a fixed interval."""
        interval = self.settings.sync_schedule_check_interval
        while True:
            try:
                await self._check_schedules()
            except Exception:
                logger.exception("Schedule check failed")
            await asyncio.sleep(interval)

    async def _check_schedules(self) -> None:
        """Check all enabled PMs and trigger syncs for those whose schedules are due."""
        enabled_pms = await self.dynamodb.list_enabled_pms()
        if not enabled_pms:
            return

        for pm in enabled_pms:
            slack_id = pm.get("slack_id", "")
            if not slack_id:
                continue

            schedule = pm.get("schedule_config", {})
            if not schedule.get("enabled"):
                continue

            if slack_id in self._running_syncs:
                logger.debug("Sync already running for %s, skipping", slack_id)
                continue

            try:
                if self._is_due(schedule):
                    await self._run_scheduled_sync(pm)
            except Exception:
                logger.exception("Scheduled sync failed for PM %s", slack_id)

    def _is_due(self, schedule: dict) -> bool:
        """Check if a cron expression is due based on last_scheduled_run."""
        cron_expr = schedule.get("cron_expression", "")
        if not cron_expr:
            return False

        tz_name = schedule.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except (KeyError, ValueError):
            logger.warning("Invalid timezone %s, falling back to UTC", tz_name)
            tz = ZoneInfo("UTC")

        now = datetime.now(tz)

        last_run_str = schedule.get("last_scheduled_run", "")
        if last_run_str:
            try:
                last_run = datetime.fromisoformat(last_run_str).astimezone(tz)
            except (ValueError, TypeError):
                last_run = datetime.min.replace(tzinfo=tz)
        else:
            last_run = datetime.min.replace(tzinfo=tz)

        try:
            cron = croniter(cron_expr, last_run)
            next_run = cron.get_next(datetime)
            return now >= next_run
        except (ValueError, KeyError) as e:
            logger.error("Invalid cron expression '%s': %s", cron_expr, e)
            return False

    async def _run_scheduled_sync(self, pm_config: dict[str, Any]) -> None:
        """Run a scheduled jira-sync for a PM."""
        slack_id = pm_config["slack_id"]
        schedule = pm_config.get("schedule_config", {})
        target_channel = schedule.get("target_channel", slack_id)

        self._running_syncs.add(slack_id)
        try:
            logger.info("Running scheduled sync for PM %s", slack_id)

            # Get the Slack client from the handler
            client = self.slack_handler.app.client

            # Notify PM that scheduled sync is starting
            await client.chat_postMessage(
                channel=target_channel,
                text=f"Running scheduled JIRA sync for <@{slack_id}>...",
            )

            # Trigger the main sync flow via the slack_handler
            await self.slack_handler._process_jira_sync(
                channel_id=target_channel,
                user_id=slack_id,
                client=client,
                transcripts_only_override=pm_config.get("flow_config", {}).get("transcripts_only", False),
            )

            # Update last_scheduled_run in DynamoDB
            schedule["last_scheduled_run"] = datetime.now(timezone.utc).isoformat()
            await self.dynamodb.update_pm(slack_id, {"schedule_config": schedule})

            logger.info("Scheduled sync completed for PM %s", slack_id)
        finally:
            self._running_syncs.discard(slack_id)
